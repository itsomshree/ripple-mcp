import threading
from pathlib import Path

import pytest
from neo4j.exceptions import ServiceUnavailable
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from ripple.config import Settings
from ripple.graph.builder import CodeGraph
from ripple.graph.diff import diff_graphs
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


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("REPO_PATH", "SCAN_EXCLUDE", "NEO4J_URI", "NEO4J_USER"):
        monkeypatch.delenv(key, raising=False)


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
    services.graph()


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


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# rescan, in memory
def test_in_memory_rescan_reports_file_and_graph_changes(tmp_path):
    _write(tmp_path, "a.py", "def f(): ...\n")
    s = InMemoryServices(repo_path=tmp_path)
    s.scan()

    first = s.rescan()
    assert (first.files_added, first.files_unchanged) == (1, 0)
    assert set(first.graph_delta.values()) == {0}

    second = s.rescan()
    assert (second.files_added, second.files_unchanged) == (0, 1)

    _write(tmp_path, "a.py", "def f(): ...\ndef g(): ...\n")
    third = s.rescan()
    assert third.files_changed == 1
    assert third.graph_delta == {
        "nodes_added": 1,
        "nodes_changed": 1,
        "nodes_removed": 0,
        "edges_added": 1,
        "edges_removed": 0,
    }
    assert s.status().last_scan == third


def test_in_memory_rescan_needs_a_repo():
    with pytest.raises(ScanError, match="REPO_PATH"):
        InMemoryServices().rescan()


def test_a_rescan_is_refused_while_another_runs(tmp_path):
    _write(tmp_path, "a.py", "def f(): ...\n")
    started, release = threading.Event(), threading.Event()

    class SlowRescan(InMemoryServices):
        def _rescan(self):
            started.set()
            release.wait(5)
            return super()._rescan()

    s = SlowRescan(repo_path=tmp_path)
    t = threading.Thread(target=s.rescan)
    t.start()
    assert started.wait(5)
    with pytest.raises(ScanInProgress):
        s.rescan()
    with pytest.raises(ScanInProgress):
        s.graph()
    release.set()
    t.join(5)
    assert s.status().scanning is False


# rescan, Neo4j services with a fake store and a fake sync
def test_neo4j_rescan_syncs_the_store_and_reuses_its_cache(monkeypatch, tmp_path):
    _write(tmp_path, "a.py", "def f(): ...\n")
    synced = []

    def fake_sync(store, graph):
        synced.append((store, graph))
        return diff_graphs(CodeGraph(), graph)

    monkeypatch.setattr("ripple.services.sync_graph", fake_sync)
    services, store, calls = make(repo_path=tmp_path)

    first = services.rescan()
    assert synced[0][0] is store
    assert (first.files_added, first.files_unchanged) == (1, 0)
    assert first.graph_delta["nodes_added"] == 2  # File + Function
    assert services.status().last_scan == first

    second = services.rescan()
    assert (second.files_added, second.files_unchanged) == (0, 1)
    assert calls == [1]


def test_neo4j_rescan_without_a_repo_path_fails_before_touching_the_store():
    services, _, calls = make()
    with pytest.raises(ScanError, match="REPO_PATH"):
        services.rescan()
    assert calls == []


def test_a_store_failure_during_rescan_propagates_and_the_next_one_heals(
    monkeypatch, tmp_path
):
    _write(tmp_path, "a.py", "def f(): ...\n")
    outcomes = []

    def flaky_sync(store, graph):
        outcomes.append(graph)
        if len(outcomes) == 1:
            raise ServiceUnavailable("down")
        return diff_graphs(CodeGraph(), graph)

    monkeypatch.setattr("ripple.services.sync_graph", flaky_sync)
    services, _, _ = make(repo_path=tmp_path)

    with pytest.raises(ServiceUnavailable):
        services.rescan()
    assert services.status().scanning is False
    assert services.status().last_scan is None

    healed = services.rescan()
    assert healed.files_unchanged == 1
    assert healed.graph_delta["nodes_added"] == 2
    assert len(outcomes[1].nodes) == 2
