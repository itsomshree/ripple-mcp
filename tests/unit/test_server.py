import asyncio
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ripple.server import build_server
from ripple.services import InMemoryServices

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
TOOLS = {
    "what_breaks_if_i_change",
    "trace_request_flow",
    "affected_tests",
    "scan_repo",
    "status",
    "rescan",
}


def structured(result: Any) -> dict[str, Any]:
    assert result.structured_content is not None, result
    return result.structured_content


async def call(client: Client, name: str, **args: Any) -> dict[str, Any]:
    return structured(await client.call_tool(name, args))


@pytest_asyncio.fixture
async def client():
    async with Client(build_server(InMemoryServices(repo_path=FIXTURE))) as c:
        yield c


@pytest_asyncio.fixture
async def scanned(client):
    await call(client, "scan_repo")
    return client


# registration
async def test_exactly_the_six_tools_with_output_schemas(client):
    tools = await client.list_tools()
    assert {t.name for t in tools} == TOOLS
    assert all(t.output_schema is not None for t in tools)


# before a scan
async def test_status_of_an_unscanned_graph(client):
    s = await call(client, "status")
    assert (s["store"], s["store_ok"], s["graph_empty"]) == ("memory", True, True)
    assert "scan_repo" in s["message"]


async def test_queries_before_a_scan_say_to_scan_first(client):
    r = await call(client, "what_breaks_if_i_change", entity="UserService.get")
    assert r["status"] == "not_scanned" and "scan_repo" in r["message"]


# after a scan
async def test_scan_repo_returns_the_summary_without_the_server_path(client):
    r = await call(client, "scan_repo")
    assert r["status"] == "ok"
    assert r["summary"]["files_scanned"] == 13
    assert r["summary"]["repo_path"] == "sample_fastapi_repo"
    assert (await call(client, "status"))["graph_empty"] is False


async def test_what_breaks_if_i_change(scanned):
    r = await call(scanned, "what_breaks_if_i_change", entity="UserService.get")
    assert r["status"] == "ok"
    assert r["entity"]["id"] == "app/services/user_service.py::UserService.get"
    assert r["risk"]["level"] == "medium"
    assert (r["endpoints"]["total"], r["tests"]["total"]) == (2, 4)


async def test_trace_request_flow(scanned):
    r = await call(scanned, "trace_request_flow", endpoint="POST /api/orders")
    assert r["status"] == "ok"
    assert [t["name"] for t in r["tables"]] == ["orders", "users"]


async def test_affected_tests(scanned):
    r = await call(scanned, "affected_tests", entity="UserService.get")
    assert r["status"] == "ok" and r["tests_total"] == 4
    assert r["pytest_command"].startswith(
        "pytest tests/test_users.py::test_get_user_service"
    )


async def test_ambiguous_name_is_data_not_an_error(scanned):
    r = await call(scanned, "what_breaks_if_i_change", entity="get")
    assert r["status"] == "ambiguous"
    assert len(r["candidates"]) == 2


async def test_arguments_are_validated_by_the_schema(scanned):
    with pytest.raises(ToolError):
        await scanned.call_tool("what_breaks_if_i_change", {})


# errors
async def test_scan_without_a_repo_raises_tool_error():
    async with Client(build_server(InMemoryServices())) as c:
        with pytest.raises(ToolError, match="REPO_PATH"):
            await c.call_tool("scan_repo", {})


# concurrency
class SlowScan(InMemoryServices):
    """Holds a scan open until the test releases it"""

    def __init__(self, started: threading.Event, release: threading.Event, **kw):
        super().__init__(**kw)
        self._started, self._release = started, release

    def _rebuild(self):
        self._started.set()
        self._release.wait(10)
        return super()._rebuild()


async def test_status_and_queries_answer_while_a_scan_runs():
    started, release = threading.Event(), threading.Event()
    services = SlowScan(started, release, repo_path=FIXTURE)
    async with Client(build_server(services)) as c:
        scan = asyncio.create_task(c.call_tool("scan_repo", {}))
        try:
            assert await asyncio.to_thread(started.wait, 5)

            s = await asyncio.wait_for(call(c, "status"), 2)
            assert s["scanning"] is True

            q = await asyncio.wait_for(
                call(c, "what_breaks_if_i_change", entity="x"), 2
            )
            assert q["status"] == "scan_in_progress"

            again = await asyncio.wait_for(call(c, "scan_repo"), 2)
            assert again["status"] == "scan_in_progress"
        finally:
            release.set()
        done = await asyncio.wait_for(scan, 10)
        assert structured(done)["summary"]["files_scanned"] == 13


async def test_rescan_after_a_scan_changes_nothing(scanned):
    r = await call(scanned, "rescan")
    assert r["status"] == "ok"
    assert r["summary"]["files_added"] == 13
    assert set(r["summary"]["graph_delta"].values()) == {0}
    assert "already matches" in r["message"]

    again = await call(scanned, "rescan")
    assert again["summary"]["files_added"] == 0
    assert again["summary"]["files_unchanged"] == 13


async def test_rescan_picks_up_an_edit(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    async with Client(build_server(InMemoryServices(repo_path=repo))) as c:
        await call(c, "scan_repo")
        await call(c, "rescan")

        db = repo / "app" / "db.py"
        db.write_text(db.read_text() + "\n\ndef extra():\n    return get_session()\n")

        r = await call(c, "rescan")
        assert r["summary"]["files_changed"] == 1
        assert r["summary"]["graph_delta"] == {
            "nodes_added": 1,
            "nodes_changed": 1,
            "nodes_removed": 0,
            "edges_added": 2,
            "edges_removed": 0,
        }
        found = await call(c, "what_breaks_if_i_change", entity="extra")
        assert found["status"] == "ok"


async def test_rescan_without_a_repo_raises_tool_error():
    async with Client(build_server(InMemoryServices())) as c:
        with pytest.raises(ToolError, match="REPO_PATH"):
            await c.call_tool("rescan", {})
