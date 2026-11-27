from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.diff import GraphDelta
from ripple.graph.resolver import DEFAULT_SOURCE_ROOTS
from ripple.scanner.repo_scanner import (
    MAX_FILE_BYTES,
    MAX_LISTED_ERRORS,
    FileBundle,
    FileError,
    ScanError,
    ScanResult,
    ScanSummary,
    discover_files,
    parse_bytes,
    resolve_root,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileChanges:
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]
    unchanged: int


@dataclass(frozen=True)
class Refresh:
    result: ScanResult
    changes: FileChanges


class IncrementalScanner:
    """
    Keeps parsed facts between scans and reparses only files whose bytes changed
    """

    def __init__(
        self,
        repo_path: str | Path,
        *,
        exclude: Iterable[str] = (),
        source_roots: tuple[str, ...] = DEFAULT_SOURCE_ROOTS,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self._repo = repo_path
        self._exclude = (exclude,) if isinstance(exclude, str) else tuple(exclude)
        self._max_bytes = max_file_bytes
        self._source_roots = source_roots
        self._builder = GraphBuilder(source_roots)
        self._hashes: dict[str, str] = {}
        self._errors: dict[str, str] = {}

    def reset(self) -> None:
        self._builder = GraphBuilder(self._source_roots)
        self._hashes.clear()
        self._errors.clear()

    def refresh(self) -> Refresh:
        started = time.perf_counter()
        root = resolve_root(self._repo)
        found = discover_files(root, self._exclude, self._max_bytes)

        unreadable = 0
        seen: dict[str, str] = {}
        staged: dict[str, FileBundle] = {}
        for rel in found.files:
            try:
                raw = (root / rel).read_bytes()
            except OSError as exc:
                logger.warning("cannot read %s: %s", rel, exc)
                unreadable += 1
                continue
            digest = hashlib.sha256(raw).hexdigest()
            seen[rel] = digest
            if self._hashes.get(rel) != digest:
                staged[rel] = parse_bytes(raw, rel, digest)

        if not seen:
            logger.warning("no readable Python files found under %s", root)
            raise ScanError("no readable Python files found")

        removed = sorted(self._hashes.keys() - seen.keys())
        added = sorted(staged.keys() - self._hashes.keys())
        changed = sorted(staged.keys() & self._hashes.keys())

        for rel in removed:
            self._builder.remove(rel)
            del self._hashes[rel]
            self._errors.pop(rel, None)
        for rel in (*added, *changed):
            bundle = staged[rel]
            self._builder.add(bundle.parsed, bundle.facts, bundle.db, bundle.cov)
            self._hashes[rel] = seen[rel]
            if bundle.parsed.error is None:
                self._errors.pop(rel, None)
            else:
                self._errors[rel] = bundle.parsed.error

        graph = self._builder.build()
        changes = FileChanges(
            added=tuple(added),
            changed=tuple(changed),
            removed=tuple(removed),
            unchanged=len(seen) - len(added) - len(changed),
        )
        summary = self._summary(root, found, unreadable, len(seen), graph)
        summary.duration_seconds = time.perf_counter() - started
        logger.info(
            "refreshed %d files: %d added, %d changed, %d removed, %d unchanged",
            len(seen),
            len(added),
            len(changed),
            len(removed),
            changes.unchanged,
        )
        return Refresh(ScanResult(graph=graph, summary=summary), changes)

    def _summary(
        self, root: Path, found, unreadable: int, scanned: int, graph: CodeGraph
    ) -> ScanSummary:
        errors = sorted(self._errors.items())
        return ScanSummary(
            repo_path=str(root),
            files_scanned=scanned,
            files_excluded=found.excluded,
            files_too_large=found.too_large,
            files_unreadable=unreadable,
            parse_error_count=len(errors),
            parse_errors=[
                FileError(path=p, error=e) for p, e in errors[:MAX_LISTED_ERRORS]
            ],
            nodes=dict(Counter(n.type.value for n in graph.nodes.values())),
            edges=dict(Counter(e.type.value for e in graph.edges)),
            route_issues=dict(Counter(i.reason.value for i in graph.route_issues)),
            db_issues=dict(Counter(i.reason.value for i in graph.db_issues)),
            unresolved={
                reason.value: n for reason, n in graph.unresolved_by_reason().items()
            },
        )


class RescanSummary(ScanSummary):
    """A ScanSummary plus what changed since the previous scan"""

    files_added: int = 0
    files_changed: int = 0
    files_removed: int = 0
    files_unchanged: int = 0
    graph_delta: dict[str, int] = Field(default_factory=dict)

    def format(self) -> str:
        head, _, rest = super().format().partition("\n")
        delta = ", ".join(f"{k} {v}" for k, v in self.graph_delta.items())
        lines = [
            head,
            f"  changes: {self.files_added} added, {self.files_changed} changed, ",
            f"{self.files_removed} removed, {self.files_unchanged} unchanged",
            f"  graph delta: {delta or 'none'}",
        ]
        if rest:
            lines.append(rest)
        return "\n".join(lines)


def rescan_summary(
    refresh: Refresh, delta: GraphDelta, duration: float
) -> RescanSummary:
    c = refresh.changes
    return RescanSummary(
        **refresh.result.summary.model_dump(exclude={"duration_seconds"}),
        files_added=len(c.added),
        files_changed=len(c.changed),
        files_removed=len(c.removed),
        files_unchanged=c.unchanged,
        graph_delta=delta.counts(),
        duration_seconds=duration,
    )
