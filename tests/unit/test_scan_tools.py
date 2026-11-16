from pathlib import Path

import pytest

from ripple.scanner.repo_scanner import ScanError
from ripple.services import InMemoryServices, ScanInProgress, ServiceStatus
from ripple.tools._common import ToolStatus
from ripple.tools.scan_tools import get_status, run_scan

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"


def test_scan_returns_the_summary_without_the_server_path():
    r = run_scan(InMemoryServices(repo_path=FIXTURE))
    assert r.status is ToolStatus.OK
    assert r.summary is not None
    assert r.summary.files_scanned == 13
    assert r.summary.repo_path == "sample_fastapi_repo"


def test_a_scan_already_running_is_a_result_not_an_error():
    class Busy(InMemoryServices):
        def scan(self):
            raise ScanInProgress()

    r = run_scan(Busy())
    assert r.status is ToolStatus.SCAN_IN_PROGRESS and r.summary is None


def test_scan_errors_propagate_for_the_tool_layer_to_raise():
    with pytest.raises(ScanError):
        run_scan(InMemoryServices())


def test_status_of_an_unscanned_graph_points_at_scan_repo():
    s = get_status(InMemoryServices(repo_path=FIXTURE))
    assert (s.store, s.store_ok, s.graph_empty, s.repo) == (
        "memory",
        True,
        True,
        "sample_fastapi_repo",
    )
    assert s.message is not None and "scan_repo" in s.message


def test_status_after_a_scan():
    services = InMemoryServices(repo_path=FIXTURE)
    run_scan(services)
    s = get_status(services)
    assert not s.graph_empty and s.message is None
    assert (sum(s.nodes.values()), sum(s.edges.values())) == (48, 79)
    assert s.last_scan is not None and s.last_scan_at is not None
    assert s.last_scan.files_scanned == 13
    assert s.last_scan.repo_path == "sample_fastapi_repo"
    assert s.last_scan_at.endswith("+00:00")


def test_status_passes_a_store_failure_through():
    class Down(InMemoryServices):
        def status(self):
            return ServiceStatus(
                store="neo4j",
                store_ok=False,
                store_error="connection refused",
                repo="r",
                scanning=False,
                nodes={},
                edges={},
                last_scan=None,
                last_scan_at=None,
            )

    s = get_status(Down())
    assert s.store_ok is False and s.store_error == "connection refused"
    assert s.message is None
