from pathlib import Path

import pytest

from ripple.analysis.impact import CodeGraphView
from ripple.analysis.request_flow import UnsupportedStart, trace_flow
from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import (
    AccessMode,
    APIEndpointNode,
    DBTableNode,
    Edge,
    FileNode,
    FunctionNode,
)
from ripple.graph.schema import EdgeType as T
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
U = "app/services/user_service.py::UserService"
O = "app/services/order_service.py::OrderService"
R = "app/routers/"
DB = "app/db.py::get_session"


@pytest.fixture(scope="module")
def view():
    b = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        b.add(
            parse_python_file(p, rel),
            parse_fastapi_file(p, rel),
            parse_db_file(p, rel),
            parse_coverage_file(p, rel),
        )
    return CodeGraphView(b.build())


def flow(view, node_id, **kw):
    return trace_flow(view, view.node(node_id), **kw)


def hops(result):
    return {s.node.id: s.hops for s in result.steps}


# sample fixture
def test_post_orders_reaches_both_services_and_their_tables(view):
    r = flow(view, "endpoint::POST /api/orders")
    assert hops(r) == {
        f"{R}orders.py::place_order": 1,
        f"{O}.place": 2,
        f"{U}.get": 3,
        DB: 3,
    }
    assert [(t.name, t.access, t.touched_by, t.hops) for t in r.tables] == [
        ("orders", [AccessMode.WRITE], [f"{O}.place"], 2),
        ("users", [AccessMode.READ], [f"{U}.get"], 3),
    ]
    place = next(s for s in r.steps if s.node.id == f"{O}.place")
    assert [(t.table, t.access) for t in place.tables] == [
        ("table: orders", AccessMode.WRITE)
    ]
    assert not r.truncated and not r.depth_limited
    assert r.caveats == []


def test_delete_endpoint_merges_read_and_write_on_one_table(view):
    r = flow(view, "endpoint::DELETE /users/{user_id}")
    assert hops(r) == {f"{R}users.py::delete_user": 1, f"{U}.delete": 2, DB: 3}
    assert [(t.name, t.access, t.touched_by) for t in r.tables] == [
        ("users", [AccessMode.READ, AccessMode.WRITE], [f"{U}.delete"])
    ]


def test_endpoint_without_tables_gets_a_caveat(view):
    r = flow(view, "endpoint::GET /health")
    assert hops(r) == {"app/main.py::health": 1}
    assert r.tables == []
    assert any("No table access" in c for c in r.caveats)


def test_function_start_begins_at_hop_zero(view):
    r = flow(view, f"{U}.get")
    assert hops(r) == {DB: 1}
    assert [(t.table, t.access) for t in r.start_tables] == [
        ("table: users", AccessMode.READ)
    ]
    assert [(t.name, t.hops, t.touched_by) for t in r.tables] == [
        ("users", 0, [f"{U}.get"])
    ]
    assert r.path_to(f"{U}.get") == []


def test_path_explains_a_distant_node(view):
    r = flow(view, "endpoint::POST /api/orders")
    steps = r.path_to(f"{U}.get")
    assert [s.dst for s in steps] == [
        f"{R}orders.py::place_order",
        f"{O}.place",
        f"{U}.get",
    ]
    assert steps[0].src == "endpoint::POST /api/orders"
    assert steps[0].edge.type == T.HANDLES
    with pytest.raises(KeyError):
        r.path_to("nope")


def test_depth_limit_on_the_fixture(view):
    start = "endpoint::POST /api/orders"
    assert flow(view, start, max_hops=2).depth_limited is True
    assert flow(view, start, max_hops=3).depth_limited is False


def test_cap_on_the_fixture(view):
    r = flow(view, "endpoint::POST /api/orders", max_results=2)
    assert len(r.steps) == 2 and r.truncated
    assert any("result cap" in c for c in r.caveats)


@pytest.mark.parametrize(
    "node_id",
    [
        "app/models.py::User",
        "table: users",
        "app/services/user_service.py",
        "tests/test_users.py::test_get_user_service",
        "tests/conftest.py::make_user",
    ],
)
def test_unsupported_starts_are_rejected(view, node_id):
    with pytest.raises(UnsupportedStart):
        flow(view, node_id)


# handcrafted graphs
def _fn(path, q):
    return FunctionNode(
        id=f"{path}::{q}", name=q, qualname=q, file_path=path, line_start=1, line_end=2
    )


def _g(nodes, edges):
    g = CodeGraph()
    g.add_node(FileNode(id="m.py", name="m.py", path="m.py", content_hash="x"))
    for n in nodes:
        g.add_node(n)
    for type_, src, dst, *rest in edges:
        g.add_edge(Edge(type=type_, src=src, dst=dst, access=rest[0] if rest else None))
    return CodeGraphView(g)


def _chain(*pairs, type_=T.CALLS):
    names = sorted({x for p in pairs for x in p})
    return _g(
        [_fn("m.py", n) for n in names],
        [(type_, f"m.py::{a}", f"m.py::{b}") for a, b in pairs],
    )


def trace(v, name, **kw):
    return trace_flow(v, v.node(f"m.py::{name}"), **kw)


def test_cycle_terminates_and_never_reports_the_start():
    v = _chain(("A", "B"), ("B", "A"))
    assert hops(trace(v, "A")) == {"m.py::B": 1}


def test_diamond_counts_the_join_once_at_the_shortest_distance():
    v = _chain(("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"))
    r = trace(v, "A")
    assert hops(r) == {"m.py::B": 1, "m.py::C": 1, "m.py::D": 2}
    assert [s.parent for s in r.steps if s.node.id == "m.py::D"] == ["m.py::B"]


def test_cap_truncates_and_sets_the_flag():
    v = _chain(*(("A", f"c{i}") for i in range(10)))
    r = trace(v, "A", max_results=4)
    assert len(r.steps) == 4 and r.truncated
    assert not trace(v, "A", max_results=10).truncated


def test_depth_limited_only_when_more_exists_beyond_max_hops():
    v = _chain(("A", "B"), ("B", "C"), ("C", "D"))
    assert trace(v, "A", max_hops=2).depth_limited is True
    assert trace(v, "A", max_hops=3).depth_limited is False


def test_dependency_chain_is_followed():
    v = _chain(("h", "current_user"), ("current_user", "get_db"), type_=T.DEPENDS_ON)
    r = trace(v, "h")
    assert hops(r) == {"m.py::current_user": 1, "m.py::get_db": 2}
    assert {s.via.type for s in r.steps} == {T.DEPENDS_ON}


def test_endpoint_with_two_handlers_reaches_both_at_hop_one():
    ep = APIEndpointNode(id="endpoint::GET /x", name="GET /x", method="GET", route="/x")
    v = _g(
        [ep, _fn("m.py", "a"), _fn("m.py", "b")],
        [(T.HANDLES, ep.id, "m.py::a"), (T.HANDLES, ep.id, "m.py::b")],
    )
    assert hops(trace_flow(v, ep)) == {"m.py::a": 1, "m.py::b": 1}


def test_tables_merge_access_across_the_flow_closest_first():
    t = DBTableNode(id="table: t", name="t")
    v = _g(
        [_fn("m.py", "f"), _fn("m.py", "g"), t],
        [
            (T.CALLS, "m.py::f", "m.py::g"),
            (T.QUERIES, "m.py::f", t.id, AccessMode.WRITE),
            (T.QUERIES, "m.py::g", t.id, AccessMode.READ),
        ],
    )
    (touch,) = trace(v, "f").tables
    assert (touch.name, touch.hops, touch.access, touch.touched_by) == (
        "t",
        0,
        [AccessMode.READ, AccessMode.WRITE],
        ["m.py::f", "m.py::g"],
    )


def test_endpoint_flags_become_caveats():
    ep = APIEndpointNode(
        id="endpoint::GET /x",
        name="GET /x",
        method="GET",
        route="/x",
        exact=False,
        mounted=False,
    )
    r = trace_flow(_g([ep], []), ep)
    assert any("incomplete" in c for c in r.caveats)
    assert any("never included" in c for c in r.caveats)
