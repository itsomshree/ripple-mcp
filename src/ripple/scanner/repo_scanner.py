from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.resolver import DEFAULT_SOURCE_ROOTS
from ripple.graph.schema import CoverageFacts, DBFacts, FastAPIFacts, ParsedFile
from ripple.parsers.db_parser import parse_db_source
from ripple.parsers.fastapi_parser import parse_fastapi_source
from ripple.parsers.pytest_parser import parse_coverage_source
from ripple.parsers.python_parser import parse_python_source

logger = logging.getLogger(__name__)

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "site-packages",
        ".tox",
        ".nox",
        ".eggs",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
MAX_FILE_BYTES = 1_000_000
MAX_LISTED_ERRORS = 20


class ScanError(Exception):
    """The scan cannot run, or would produce a graph that should not replace the stored one"""


class GraphSink(Protocol):
    def replace_graph(self, graph: CodeGraph) -> None: ...


# results
class FileError(BaseModel):
    path: str
    error: str


class ScanSummary(BaseModel):
    repo_path: str
    files_scanned: int = 0
    files_excluded: int = 0
    files_too_large: int = 0
    files_unreadable: int = 0
    parse_error_count: int = 0
    parse_errors: list[FileError] = Field(default_factory=list)
    nodes: dict[str, int] = Field(default_factory=dict)
    edges: dict[str, int] = Field(default_factory=dict)
    route_issues: dict[str, int] = Field(default_factory=dict)
    db_issues: dict[str, int] = Field(default_factory=dict)
    unresolved: dict[str, int] = Field(default_factory=dict)
    duration_seconds: float = 0.0

    def format(self) -> str:
        lines = [
            f"Scanned {self.repo_path} in {self.duration_seconds:.2f}s",
            (
                f"  files: {self.files_scanned} scanned, "
                f"{self.files_excluded} excluded, "
                f"{self.files_too_large} too large, "
                f"{self.files_unreadable} unreadable"
            ),
            f"  nodes: {_counts(self.nodes)}",
            f"  edges: {_counts(self.edges)}",
        ]
        for label, data in (
            ("route issues", self.route_issues),
            ("db issues", self.db_issues),
            ("unresolved calls", self.unresolved),
        ):
            if data:
                lines.append(f"  {label}: {_counts(data)}")
        if self.parse_error_count:
            lines.append(f"  parse errors: {self.parse_error_count}")
            lines += [f"    {e.path}: {e.error}" for e in self.parse_errors]
            hidden = self.parse_error_count - len(self.parse_errors)
            if hidden > 0:
                lines.append(f"    ... and {hidden} more")
        return "\n".join(lines)


def _counts(data: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(data.items())) or "none"


@dataclass(frozen=True)
class ScanResult:
    graph: CodeGraph
    summary: ScanSummary


@dataclass(frozen=True)
class FileBundle:
    parsed: ParsedFile
    facts: FastAPIFacts
    db: DBFacts
    cov: CoverageFacts


# per-file parsing
def parse_file(root: Path, rel: str) -> FileBundle:
    """Read a file once and run every parser over it"""
    raw = (root / rel).read_bytes()
    source = raw.decode("utf-8", errors="replace")
    return FileBundle(
        parsed=parse_python_source(
            source, rel, content_hash=hashlib.sha256(raw).hexdigest()
        ),
        facts=parse_fastapi_source(source, rel),
        db=parse_db_source(source, rel),
        cov=parse_coverage_source(source, rel),
    )


# discovery
@dataclass
class _Found:
    files: list[str] = field(default_factory=list)
    excluded: int = 0
    too_large: int = 0


def _skip_dir(path: Path) -> bool:
    return (
        path.name in SKIP_DIRS
        or path.name.endswith(".egg-info")
        or (path / "pyvenv.cfg").is_file()
    )


def _discover(root: Path, exclude: tuple[str, ...], max_bytes: int) -> _Found:
    patterns = tuple(p.replace("\\", "/") for p in exclude)
    found = _Found()
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(base / d))
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = base / name
            if path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatchcase(rel, p) for p in patterns):
                found.excluded += 1
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > max_bytes:
                found.too_large += 1
                continue
            found.files.append(rel)
    found.files.sort()
    return found


def _resolve_root(repo_path: str | Path) -> Path:
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        raise ScanError(f"repo path is not a directory: {root}")
    return root


# scanning
def build_graph(
    repo_path: str | Path,
    *,
    exclude: Iterable[str] = (),
    source_roots: tuple[str, ...] = DEFAULT_SOURCE_ROOTS,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> ScanResult:
    """Walk, parse and build the graph in memory. Never touches Neo4j"""
    started = time.perf_counter()
    root = _resolve_root(repo_path)
    patterns = (exclude,) if isinstance(exclude, str) else tuple(exclude)
    found = _discover(root, patterns, max_file_bytes)

    summary = ScanSummary(
        repo_path=str(root),
        files_excluded=found.excluded,
        files_too_large=found.too_large,
    )
    builder = GraphBuilder(source_roots)
    for rel in found.files:
        try:
            bundle = parse_file(root, rel)
        except OSError as exc:
            logger.warning("cannot read %s: %s", rel, exc)
            summary.files_unreadable += 1
            continue
        summary.files_scanned += 1
        if bundle.parsed.error is not None:
            summary.parse_error_count += 1
            if len(summary.parse_errors) < MAX_LISTED_ERRORS:
                summary.parse_errors.append(
                    FileError(path=rel, error=bundle.parsed.error)
                )
        builder.add(bundle.parsed, bundle.facts, bundle.db, bundle.cov)

    if summary.files_scanned == 0:
        raise ScanError(f"no readable Python files found under {root}")

    graph = builder.build()
    summary.nodes = dict(Counter(n.type.value for n in graph.nodes.values()))
    summary.edges = dict(Counter(e.type.value for e in graph.edges))
    summary.route_issues = dict(Counter(i.reason.value for i in graph.route_issues))
    summary.db_issues = dict(Counter(i.reason.value for i in graph.db_issues))
    summary.unresolved = {
        reason.value: n for reason, n in graph.unresolved_by_reason().items()
    }
    summary.duration_seconds = time.perf_counter() - started
    logger.info(
        "scanned %d files into %d nodes and %d edges",
        summary.files_scanned,
        len(graph.nodes),
        len(graph.edges),
    )
    return ScanResult(graph=graph, summary=summary)


def scan_repo(
    repo_path: str | Path,
    store: GraphSink,
    *,
    exclude: Iterable[str] = (),
    source_roots: tuple[str, ...] = DEFAULT_SOURCE_ROOTS,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> ScanResult:
    started = time.perf_counter()
    result = build_graph(
        repo_path,
        exclude=exclude,
        source_roots=source_roots,
        max_file_bytes=max_file_bytes,
    )
    store.replace_graph(result.graph)
    result.summary.duration_seconds = time.perf_counter() - started
    return result
