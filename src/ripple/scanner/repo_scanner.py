from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from ripple.graph.builder import CodeGraph
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
def parse_bytes(raw: bytes, rel: str, digest: str | None = None) -> FileBundle:
    """Run every parser over one file's bytes"""
    source = raw.decode("utf-8", errors="replace")
    digest = digest or hashlib.sha256(raw).hexdigest()
    return FileBundle(
        parsed=parse_python_source(source, rel, content_hash=digest),
        facts=parse_fastapi_source(source, rel),
        db=parse_db_source(source, rel),
        cov=parse_coverage_source(source, rel),
    )


def parse_file(root: Path, rel: str) -> FileBundle:
    """Read a file once and run every parser over it"""
    return parse_bytes((root / rel).read_bytes(), rel)


# discovery
@dataclass
class Found:
    files: list[str] = field(default_factory=list)
    excluded: int = 0
    too_large: int = 0


def _skip_dir(path: Path) -> bool:
    return (
        path.name in SKIP_DIRS
        or path.name.endswith(".egg-info")
        or (path / "pyvenv.cfg").is_file()
    )


def discover_files(root: Path, exclude: tuple[str, ...], max_bytes: int) -> Found:
    patterns = tuple(p.replace("\\", "/") for p in exclude)
    found = Found()
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


def resolve_root(repo_path: str | Path) -> Path:
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        logger.warning("repo path is not a directory: %s", root)
        raise ScanError("repo path is not a directory")
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
    from ripple.scanner.incremental import IncrementalScanner

    scanner = IncrementalScanner(
        repo_path,
        exclude=exclude,
        source_roots=source_roots,
        max_file_bytes=max_file_bytes,
    )
    return scanner.refresh().result


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
