from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastmcp import Client
from pydantic import SecretStr

from ripple.config import Settings
from ripple.server import build_server
from ripple.services import InMemoryServices, Neo4jServices

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"


def structured(result: Any) -> dict[str, Any]:
    assert result.structured_content is not None, result
    return result.structured_content


async def call(client: Client, name: str, **args: Any) -> dict[str, Any]:
    return structured(await client.call_tool(name, args))


def _without_timing(scan: dict[str, Any]) -> dict[str, Any]:
    summary = dict(scan["summary"])
    summary.pop("duration_seconds")
    return {**scan, "summary": summary}


@pytest_asyncio.fixture
async def clients(store):
    """(in-memory client, Neo4j client), both already scanned"""
    settings = Settings(
        _env_file=None, neo4j_password=SecretStr("unused"), repo_path=FIXTURE
    )
    db = Neo4jServices(settings, store_factory=lambda _s: store)
    async with (
        Client(build_server(InMemoryServices(repo_path=FIXTURE))) as mem,
        Client(build_server(db)) as neo,
    ):
        scans = (await call(mem, "scan_repo"), await call(neo, "scan_repo"))
        assert _without_timing(scans[0]) == _without_timing(scans[1])
        yield mem, neo


async def test_scan_through_the_server_writes_the_graph_to_neo4j(clients, store):
    _, neo = clients
    nodes, edges = store.counts()
    assert (sum(nodes.values()), sum(edges.values())) == (48, 79)

    s = await call(neo, "status")
    assert (s["store"], s["store_ok"], s["graph_empty"]) == ("neo4j", True, False)
    assert s["nodes"] == nodes and s["edges"] == edges
    assert s["repo"] == "sample_fastapi_repo"


CASES = [
    ("what_breaks_if_i_change", {"entity": "UserService.get"}),
    ("what_breaks_if_i_change", {"entity": "app/models.py::User", "max_hops": 8}),
    ("what_breaks_if_i_change", {"entity": "table: users"}),
    ("what_breaks_if_i_change", {"entity": "services/user_service.py"}),
    ("what_breaks_if_i_change", {"entity": "GET /health"}),
    ("what_breaks_if_i_change", {"entity": "UserService.get", "limit": 2}),
    ("what_breaks_if_i_change", {"entity": "get"}),  # ambiguous
    ("what_breaks_if_i_change", {"entity": "UserService.nope"}),  # not found
    ("trace_request_flow", {"endpoint": "POST /api/orders"}),
    ("trace_request_flow", {"endpoint": "DELETE /users/{user_id}"}),
    ("trace_request_flow", {"endpoint": "UserService.get"}),
    ("trace_request_flow", {"endpoint": "table: users"}),  # unsupported start
    ("affected_tests", {"entity": "UserService.get"}),
    ("affected_tests", {"entity": "app/models.py::User"}),
    ("affected_tests", {"entity": "GET /health"}),
]


@pytest.mark.parametrize(
    "tool, args", CASES, ids=[f"{t}:{next(iter(a.values()))}" for t, a in CASES]
)
async def test_neo4j_answers_match_the_in_memory_answers(clients, tool, args):
    mem, neo = clients
    assert await call(neo, tool, **args) == await call(mem, tool, **args)
