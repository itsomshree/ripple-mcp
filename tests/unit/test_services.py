import threading
from pathlib import Path

import pytest
from neo4j.exceptions import ServiceUnavailable
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from ripple.config import Settings
from ripple.graph.builder import CodeGraph
from ripple.graph.store import GraphStore
from ripple.scanner.repo_scanner import ScanError, ScanResult, ScanSummary
from ripple.services import (
    InMemoryServices,
    Neo4jServices,
    ScanGate,
    ScanInProgress,
    Scanner,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"


class IsolatedSettings(Settings):
    """Settings that ignore a local .env file, so tests do not depend on it"""

    model_config = SettingsConfigDict(env_file=None)


def _summary() -> ScanSummary:
    return ScanSummary(repo_path="x")


# ScanGate
def test_gate_records_the_last_successful_scan():
    gate = ScanGate()
    assert gate.last is None and not gate.busy
    s = gate.run(_summary)
    assert gate.last is not None
    assert gate.last[0] == s and gate.last[1].endswith("+00:00")


def test_gate_releases_after_a_failure_and_keeps_the_previous_record():
    gate = ScanGate()

    def boom():
        raise ScanError("nope")

    with pytest.raises(ScanError):
        gate.run(boom)
    assert not gate.busy and gate.last is None


def test_second_scan_while_one_runs_is_refused():
    gate = ScanGate()
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return _summary()

    t = threading.Thread(target=gate.run, args=(slow,))
    t.start()
    assert started.wait(5)
    assert gate.busy
    with pytest.raises(ScanInProgress):
        gate.run(_summary)
    release.set()
    t.join(5)
    assert not gate.busy and gate.last is not None


# InMemoryServices
def test_in_memory_services_scan_then_query():
    s = InMemoryServices(repo_path=FIXTURE)
    assert s.graph().is_empty() is True
    assert s.scan().files_scanned == 13
    assert s.graph().is_empty() is False
    assert sum(s.status().nodes.values()) == 48


def test_in_memory_scan_needs_a_repo():
    with pytest.raises(ScanError, match="REPO_PATH"):
        InMemoryServices().scan()


# Neo4jServices, with a fake store
class FakeStore(GraphStore):
    """A GraphStore that never connects: it skips the driver setup"""

    def __init__(self, down: bool = False) -> None:
        self.down = down
        self.graphs: list[CodeGraph] = []

    def verify(self) -> None:
        if self.down:
            raise ServiceUnavailable("connection refused")

    def counts(self) -> tuple[dict[str, int], dict[str, int]]:
        return {"File": 2}, {"IMPORTS": 1}

    def replace_graph(self, graph: CodeGraph) -> None:
        self.graphs.append(graph)

    def close(self) -> None:
        pass


def make(
    *,
    store: FakeStore | None = None,
    scanner: Scanner | None = None,
    **settings,
) -> tuple[Neo4jServices, FakeStore, list[int]]:
    store = store if store is not None else FakeStore()
    calls: list[int] = []

    def factory(_settings: Settings) -> GraphStore:
        calls.append(1)
        return store

    extra = {"scanner": scanner} if scanner is not None else {}
    services = Neo4jServices(
        IsolatedSettings(neo4j_password=SecretStr("pw"), **settings),
        store_factory=factory,
        **extra,
    )
    return services, store, calls


def test_store_is_created_lazily_and_once():
    services, _, calls = make(repo_path=FIXTURE)
    assert calls == []
    services.graph()
    services.status()
    services.scan()
    assert calls == [1]


def test_scan_replaces_the_graph_using_the_configured_excludes():
    services, store, _ = make(repo_path=FIXTURE, scan_exclude="tests/*")
    summary = services.scan()
    assert (summary.files_scanned, summary.files_excluded) == (10, 3)
    assert len(store.graphs) == 1
    st = services.status()
    assert st.last_scan == summary
    assert st.last_scan_at is not None and st.last_scan_at.endswith("+00:00")


def test_scan_without_a_repo_path_fails_before_touching_the_store():
    services, _, calls = make()
    with pytest.raises(ScanError, match="REPO_PATH"):
        services.scan()
    assert calls == []


def test_queries_and_scans_are_refused_while_a_scan_runs():
    started, release = threading.Event(), threading.Event()

    def slow(repo, store, *, exclude):
        started.set()
        release.wait(5)
        return ScanResult(graph=CodeGraph(), summary=ScanSummary(repo_path=str(repo)))

    services, _, _ = make(scanner=slow, repo_path=FIXTURE)
    t = threading.Thread(target=services.scan)
    t.start()
    assert started.wait(5)
    with pytest.raises(ScanInProgress):
        services.graph()
    with pytest.raises(ScanInProgress):
        services.scan()
    assert services.status().scanning is True
    release.set()
    t.join(5)
    assert services.status().scanning is False
    services.graph()  # readable again


def test_status_reports_counts_and_the_repo_name_only():
    services, _, _ = make(repo_path=FIXTURE)
    st = services.status()
    assert (st.store, st.store_ok, st.store_error) == ("neo4j", True, None)
    assert st.repo == "sample_fastapi_repo"
    assert (st.nodes, st.edges, st.scanning, st.last_scan) == (
        {"File": 2},
        {"IMPORTS": 1},
        False,
        None,
    )


def test_status_survives_an_unreachable_store():
    services, _, _ = make(store=FakeStore(down=True))
    st = services.status()
    assert st.store_ok is False
    assert st.store_error is not None and "connection refused" in st.store_error
    assert st.nodes == {} and st.edges == {}
