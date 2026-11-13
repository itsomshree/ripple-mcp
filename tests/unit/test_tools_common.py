from pathlib import Path

import pytest

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import Edge, EdgeType, NodeType
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file
from ripple.tools._common import (
    DEFAULT_LIST_LIMIT,
    MAX_HOPS_LIMIT,
    MAX_LIST_LIMIT,
    GraphAccess,
    Item,
    Problem,
    ToolResult,
    ToolStatus,
    clamp_hops,
    clamp_limit,
    make_group,
    make_item,
    resolve,
    unsupported,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
U = "app/services/user_service.py::UserService"


@pytest.fixture(scope="module")
def access():
    b = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        b.add(
            parse_python_file(p, rel),
            parse_fastapi_file(p, rel),
            parse_db_file(p, rel),
            parse_coverage_file(p, rel),
        )
    return GraphAccess.from_graph(b.build())


# resolve
def test_resolves_a_unique_entity(access):
    node = resolve(access, "UserService.get")
    assert not isinstance(node, Problem)
    assert node.id == f"{U}.get"


def test_ambiguous_name_returns_candidates_not_a_guess(access):
    p = resolve(access, "get")
    assert isinstance(p, Problem)
    assert p.status is ToolStatus.AMBIGUOUS
    assert {c.id for c in p.candidates} == {
        f"{U}.get",
        "app/services/order_service.py::OrderService.get",
    }
    assert "candidate" in p.message


def test_not_found_suggests_the_nearest_match(access):
    p = resolve(access, "UserService.nope")
    assert isinstance(p, Problem) and p.status is ToolStatus.NOT_FOUND
    assert [c.id for c in p.candidates] == [U]
    assert [c.kind for c in p.candidates] == ["Class"]


def test_not_found_without_suggestions(access):
    p = resolve(access, "GET /nope")
    assert isinstance(p, Problem) and p.status is ToolStatus.NOT_FOUND
    assert p.candidates == []
    assert "Did you mean" not in p.message


def test_type_filter_applies_to_resolution(access):
    p = resolve(access, "get", types=[NodeType.CLASS])
    assert isinstance(p, Problem) and p.status is ToolStatus.NOT_FOUND


def test_an_empty_graph_says_to_scan_first():
    empty = GraphAccess.from_graph(CodeGraph())
    p = resolve(empty, "anything")
    assert isinstance(p, Problem) and p.status is ToolStatus.NOT_SCANNED
    assert "scan_repo" in p.message


def test_emptiness_is_only_checked_when_nothing_matched(access):
    # a match is a match, whatever the emptiness check says
    probe = GraphAccess(access.view, access.finder, lambda: True)
    assert not isinstance(resolve(probe, "UserService"), Problem)


def test_unsupported_helper():
    p = unsupported("start from an endpoint or a function")
    assert p.status is ToolStatus.UNSUPPORTED


# shaping
def test_item_carries_kind_hops_edge_and_line(access):
    node = access.view.node(f"{U}.get")
    edge = Edge(type=EdgeType.CALLS, src="a", dst=node.id)
    item = make_item(node, hops=2, parent="a", via=edge)
    assert (item.id, item.kind, item.hops, item.via, item.parent) == (
        f"{U}.get",
        "Function",
        2,
        "CALLS",
        "a",
    )
    assert isinstance(item.line, int)


def test_item_for_a_node_without_lines_or_edge(access):
    ep = access.view.node("endpoint::GET /health")
    item = make_item(ep, hops=1, parent=None, via=None)
    assert (item.kind, item.line, item.via, item.parent) == (
        "APIEndpoint",
        None,
        None,
        None,
    )


def _items(n):
    return [Item(id=f"n{i}", kind="Function", hops=1) for i in range(n)]


def test_group_is_capped_but_reports_the_total():
    g = make_group(_items(30))
    assert (g.total, len(g.items), g.more) == (30, DEFAULT_LIST_LIMIT, True)
    assert [i.id for i in g.items] == [f"n{i}" for i in range(DEFAULT_LIST_LIMIT)]


def test_group_at_exactly_the_limit_is_not_marked_more():
    g = make_group(_items(DEFAULT_LIST_LIMIT))
    assert g.more is False and len(g.items) == g.total


def test_group_limit_can_be_changed_or_zero():
    assert len(make_group(_items(10), limit=3).items) == 3
    g = make_group(_items(10), limit=0)
    assert (g.total, g.items, g.more) == (10, [], True)


# responses
def test_results_default_to_ok_and_serialise_the_status_as_text():
    assert ToolResult().status is ToolStatus.OK
    dumped = Problem(status=ToolStatus.NOT_FOUND, message="x").model_dump(mode="json")
    assert dumped["status"] == "not_found" and dumped["candidates"] == []


def test_clamps():
    assert (clamp_hops(0), clamp_hops(5), clamp_hops(999)) == (1, 5, MAX_HOPS_LIMIT)
    assert (clamp_limit(0), clamp_limit(25), clamp_limit(10_000)) == (
        1,
        25,
        MAX_LIST_LIMIT,
    )


def test_a_problem_converts_into_a_tools_result_type():
    class Mine(ToolResult):
        payload: int | None = None

    p = Problem(status=ToolStatus.NOT_FOUND, message="x")
    r = p.into(Mine)
    assert isinstance(r, Mine)
    assert (r.status, r.message, r.payload) == (ToolStatus.NOT_FOUND, "x", None)
