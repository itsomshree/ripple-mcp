from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar

from neo4j.exceptions import DriverError, Neo4jError

from ripple.config import Settings, get_settings
from ripple.graph.builder import CodeGraph
from ripple.graph.diff import diff_graphs
from ripple.graph.store import GraphStore
from ripple.graph.sync import sync_graph
from ripple.scanner.incremental import (
    IncrementalScanner,
    RescanSummary,
    rescan_summary,
)
from ripple.scanner.repo_scanner import (
    ScanError,
    ScanResult,
    ScanSummary,
    build_graph,
    scan_repo,
)
from ripple.tools._common import GraphAccess

Scanner = Callable[..., ScanResult]
_S = TypeVar("_S", bound=ScanSummary)


class ScanInProgress(Exception):
    """A scan is running, so the graph is being replaced"""


@dataclass(frozen=True)
class ServiceStatus:
    store: str
    store_ok: bool
    store_error: str | None
    repo: str | None
    scanning: bool
    nodes: dict[str, int]
    edges: dict[str, int]
    last_scan: ScanSummary | None
    last_scan_at: str | None


class Services(Protocol):
    def graph(self) -> GraphAccess:
        """Raises ScanInProgress while a scan is replacing the graph"""
        ...

    def scan(self) -> ScanSummary:
        """Raises ScanInProgress, or ScanError if the repo cannot be scanned"""
        ...

    def rescan(self) -> RescanSummary:
        """Like scan, but reparses only changed files and writes only the diff"""
        ...

    def status(self) -> ServiceStatus: ...


class ScanGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last: tuple[ScanSummary, str] | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def run(self, scan: Callable[[], _S]) -> _S:
        if not self._lock.acquire(blocking=False):
            raise ScanInProgress()
        try:
            summary = scan()
        finally:
            self._lock.release()
        self.last = (summary, datetime.now(UTC).isoformat(timespec="seconds"))
        return summary


def _repo_name(path: Path | None) -> str | None:
    return path.expanduser().resolve().name if path is not None else None


class Neo4jServices:
    """Production services"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store_factory: Callable[[Settings], GraphStore] = GraphStore.from_settings,
        scanner: Scanner = scan_repo,
    ) -> None:
        self._settings = settings or get_settings()
        self._store_factory = store_factory
        self._scanner = scanner
        self._store: GraphStore | None = None
        self._store_lock = threading.Lock()
        self._gate = ScanGate()
        self._incremental: IncrementalScanner | None = None

    def _get_store(self) -> GraphStore:
        with self._store_lock:
            if self._store is None:
                self._store = self._store_factory(self._settings)
            return self._store

    def close(self) -> None:
        with self._store_lock:
            if self._store is not None:
                self._store.close()
                self._store = None

    def graph(self) -> GraphAccess:
        if self._gate.busy:
            raise ScanInProgress()
        return GraphAccess.from_store(self._get_store())

    def scan(self) -> ScanSummary:
        repo = self._settings.repo_path
        if repo is None:
            raise ScanError("REPO_PATH is not configured on the server")
        return self._gate.run(
            lambda: (
                self._scanner(
                    repo, self._get_store(), exclude=self._settings.exclude_globs
                ).summary
            )
        )

    def rescan(self) -> RescanSummary:
        repo = self._settings.repo_path
        if repo is None:
            raise ScanError("REPO_PATH is not configured on the server")
        return self._gate.run(lambda: self._rescan(repo))

    def _rescan(self, repo: Path) -> RescanSummary:
        started = time.perf_counter()
        if self._incremental is None:
            self._incremental = IncrementalScanner(
                repo, exclude=self._settings.exclude_globs
            )
        refresh = self._incremental.refresh()
        delta = sync_graph(self._get_store(), refresh.result.graph)
        return rescan_summary(refresh, delta, time.perf_counter() - started)

    def status(self) -> ServiceStatus:
        last = self._gate.last
        common = {
            "store": "neo4j",
            "repo": _repo_name(self._settings.repo_path),
            "scanning": self._gate.busy,
            "last_scan": last[0] if last else None,
            "last_scan_at": last[1] if last else None,
        }
        try:
            store = self._get_store()
            store.verify()
            nodes, edges = store.counts()
        except (DriverError, Neo4jError, OSError) as exc:
            return ServiceStatus(
                store_ok=False, store_error=str(exc), nodes={}, edges={}, **common
            )
        return ServiceStatus(
            store_ok=True, store_error=None, nodes=nodes, edges=edges, **common
        )


class InMemoryServices:
    def __init__(
        self,
        graph: CodeGraph | None = None,
        *,
        repo_path: Path | None = None,
        exclude: tuple[str, ...] = (),
    ) -> None:
        self._graph = graph if graph is not None else CodeGraph()
        self._repo_path = repo_path
        self._exclude = exclude
        self._gate = ScanGate()
        self._incremental: IncrementalScanner | None = None

    def graph(self) -> GraphAccess:
        if self._gate.busy:
            raise ScanInProgress()
        return GraphAccess.from_graph(self._graph)

    def scan(self) -> ScanSummary:
        if self._repo_path is None:
            raise ScanError("REPO_PATH is not configured on the server")
        return self._gate.run(self._rebuild)

    def _rebuild(self) -> ScanSummary:
        assert self._repo_path is not None
        result = build_graph(self._repo_path, exclude=self._exclude)
        self._graph = result.graph
        return result.summary

    def rescan(self) -> RescanSummary:
        if self._repo_path is None:
            raise ScanError("REPO_PATH is not configured on the server")
        return self._gate.run(self._rescan)

    def _rescan(self) -> RescanSummary:
        assert self._repo_path is not None
        started = time.perf_counter()
        if self._incremental is None:
            self._incremental = IncrementalScanner(
                self._repo_path, exclude=self._exclude
            )
        refresh = self._incremental.refresh()
        delta = diff_graphs(self._graph, refresh.result.graph)
        self._graph = refresh.result.graph
        return rescan_summary(refresh, delta, time.perf_counter() - started)

    def status(self) -> ServiceStatus:
        last = self._gate.last
        return ServiceStatus(
            store="memory",
            store_ok=True,
            store_error=None,
            repo=_repo_name(self._repo_path),
            scanning=self._gate.busy,
            nodes=dict(Counter(n.type.value for n in self._graph.nodes.values())),
            edges=dict(Counter(e.type.value for e in self._graph.edges)),
            last_scan=last[0] if last else None,
            last_scan_at=last[1] if last else None,
        )