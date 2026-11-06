from pathlib import Path

import pytest

from ripple.analysis.impact import CodeGraphView, Group, impact
from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import Edge, FileNode, FunctionNode, TestNode
from ripple.graph.schema import EdgeType as T
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
U = "app/services/user_service.py::UserService"
O = "app/services/order_service.py::OrderService"
R = "app/routers/"
TU, TO = "tests/test_users.py::", "tests/test_orders.py::"


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


def run(view, node_id, **kw):
    kw.setdefault("max_hops", 8)
    return impact(view, [view.node(node_id)], **kw)


def hops(entries):
    return {e.node.id: e.hops for e in entries}


def test_service_method(view):
    r = run(view, f"{U}.get")
    assert hops(r.functions) == {
        f"{R}users.py::get_user": 1,
        f"{O}.place": 1,
        f"{R}orders.py::place_order": 2,
    }
    assert hops(r.classes) == {}
    assert hops(r.endpoints) == {
        "endpoint::GET /users/{user_id}": 2,
        "endpoint::POST /api/orders": 3,
    }
    assert hops(r.tables) == {"table: users": 1}
    assert hops(r.tests) == {
        f"{TU}test_get_user_service": 1,
        f"{TO}test_place_order_service": 2,
        f"{TU}test_get_user_endpoint": 3,
        f"{TO}test_place_order_endpoint": 4,
    }
    assert not r.truncated


def test_path_explains_a_distant_test(view):
    r = run(view, f"{U}.get")
    steps = r.path_to(f"{TO}test_place_order_endpoint")
    assert [s.dst for s in steps] == [
        f"{O}.place",
        f"{R}orders.py::place_order",
        "endpoint::POST /api/orders",
        f"{TO}test_place_order_endpoint",
    ]
    assert steps[0].src == f"{U}.get"
    assert r.path_to(f"{U}.get") == []


def test_model_class_reaches_orm_only_readers_through_its_table(view):
    r = run(view, "app/models.py::User")
    assert hops(r.functions) == {
        f"{U}.create": 1,
        f"{U}.list_all": 2,
        f"{U}.get": 2,
        f"{U}.delete": 2,
        f"{R}users.py::create_user": 2,
        f"{R}users.py::list_users": 3,
        f"{R}users.py::get_user": 3,
        f"{O}.place": 3,
        f"{R}users.py::delete_user": 3,
        f"{R}orders.py::place_order": 4,
    }
    assert hops(r.tables) == {"table: users": 1}
    assert hops(r.endpoints) == {
        "endpoint::POST /users": 3,
        "endpoint::GET /users": 4,
        "endpoint::GET /users/{user_id}": 4,
        "endpoint::DELETE /users/{user_id}": 4,
        "endpoint::POST /api/orders": 5,
    }
    assert hops(r.tests) == {
        "tests/conftest.py::make_user": 2,
        f"{TU}test_create_user_via_helper": 2,
        f"{TU}test_get_user_service": 3,
        f"{TO}test_place_order_service": 4,
        f"{TU}test_get_user_endpoint": 5,
        f"{TO}test_place_order_endpoint": 6,
    }
    assert "endpoint::GET /health" not in r.entries


def test_table(view):
    r = run(view, "table: users")
    assert hops(r.classes) == {"app/models.py::User": 1}
    assert hops(r.tables) == {}
    assert hops(r.functions) == {
        f"{U}.create": 1,
        f"{U}.delete": 1,
        f"{U}.get": 1,
        f"{U}.list_all": 1,
        f"{R}users.py::create_user": 2,
        f"{R}users.py::list_users": 2,
        f"{R}users.py::get_user": 2,
        f"{O}.place": 2,
        f"{R}users.py::delete_user": 2,
        f"{R}orders.py::place_order": 3,
    }
    assert hops(r.endpoints) == {
        "endpoint::POST /users": 3,
        "endpoint::GET /users": 3,
        "endpoint::GET /users/{user_id}": 3,
        "endpoint::DELETE /users/{user_id}": 3,
        "endpoint::POST /api/orders": 4,
    }
    assert hops(r.tests) == {
        "tests/conftest.py::make_user": 2,
        f"{TU}test_create_user_via_helper": 2,
        f"{TU}test_get_user_service": 2,
        f"{TO}test_place_order_service": 3,
        f"{TU}test_get_user_endpoint": 4,
        f"{TO}test_place_order_endpoint": 5,
    }


def test_file(view):
    r = run(view, "app/services/user_service.py")
    assert hops(r.classes) == {U: 1}
    assert hops(r.tables) == {"table: users": 2}
    assert hops(r.files) == {
        f"{R}users.py": 1,
        "app/services/order_service.py": 1,
        "tests/conftest.py": 1,
        "tests/test_users.py": 1,
        "app/main.py": 2,
        f"{R}orders.py": 2,
        "tests/test_orders.py": 2,
    }
    assert hops(r.functions) == {
        f"{U}.create": 1,
        f"{U}.delete": 1,
        f"{U}.get": 1,
        f"{U}.list_all": 1,
        f"{R}users.py::create_user": 2,
        f"{R}users.py::delete_user": 2,
        f"{R}users.py::get_user": 2,
        f"{R}users.py::list_users": 2,
        f"{O}.place": 2,
        f"{R}orders.py::place_order": 3,
    }


def test_endpoint_start_lists_handler_as_context_not_impact(view):
    r = run(view, "endpoint::GET /users/{user_id}")
    assert [n.id for n in r.context] == [f"{R}users.py::get_user"]
    assert hops(r.tests) == {f"{TU}test_get_user_endpoint": 1}
    assert r.functions == []


def test_default_depth_stops_before_the_farthest_test(view):
    r = run(view, "app/models.py::User", max_hops=5)
    assert f"{TO}test_place_order_endpoint" not in r.entries
    assert not r.truncated


# handcrafted graphs
def _fn(path, q):
    return FunctionNode(
        id=f"{path}::{q}", name=q, qualname=q, file_path=path, line_start=1, line_end=2
    )


def _calls(pairs):
    g = CodeGraph()
    g.add_node(FileNode(id="m.py", name="m.py", path="m.py", content_hash="x"))
    for name in sorted({x for p in pairs for x in p}):
        g.add_node(_fn("m.py", name))
    for caller, callee in pairs:
        g.add_edge(Edge(type=T.CALLS, src=f"m.py::{caller}", dst=f"m.py::{callee}"))
    return CodeGraphView(g)


def test_diamond_counts_the_join_once_at_the_shortest_distance():
    v = _calls([("B", "A"), ("C", "A"), ("D", "B"), ("D", "C")])
    r = impact(v, [v.node("m.py::A")])
    assert hops(r.functions) == {"m.py::B": 1, "m.py::C": 1, "m.py::D": 2}


def test_cycle_terminates_and_never_reports_the_start():
    v = _calls([("A", "B"), ("B", "A")])
    r = impact(v, [v.node("m.py::A")])
    assert hops(r.functions) == {"m.py::B": 1}


def test_cap_truncates_farthest_first_and_sets_the_flag():
    v = _calls([(f"c{i}", "A") for i in range(10)])
    r = impact(v, [v.node("m.py::A")], max_results=4)
    assert len(r.entries) == 4 and r.truncated
    assert not impact(v, [v.node("m.py::A")], max_results=10).truncated


def test_test_helpers_are_grouped_with_tests_not_functions():
    g = CodeGraph()
    for path, is_test in (
        ("app/svc.py", False),
        ("tests/conftest.py", True),
        ("tests/test_a.py", True),
    ):
        g.add_node(
            FileNode(id=path, name=path, path=path, content_hash="x", is_test=is_test)
        )
    g.add_node(_fn("app/svc.py", "get"))
    g.add_node(_fn("tests/conftest.py", "make"))
    g.add_node(
        TestNode(
            id="tests/test_a.py::test_x",
            name="test_x",
            qualname="test_x",
            file_path="tests/test_a.py",
            line_start=1,
            line_end=2,
        )
    )
    g.add_edge(Edge(type=T.CALLS, src="tests/conftest.py::make", dst="app/svc.py::get"))
    g.add_edge(
        Edge(type=T.CALLS, src="tests/test_a.py::test_x", dst="tests/conftest.py::make")
    )
    v = CodeGraphView(g)
    r = impact(v, [v.node("app/svc.py::get")])
    assert r.functions == []
    assert hops(r.tests) == {"tests/conftest.py::make": 1, "tests/test_a.py::test_x": 2}
    assert all(e.group == Group.TESTS for e in r.entries.values())


def test_depth_limited_is_set_only_when_more_exists_beyond_max_hops(view):
    start = "app/models.py::User"
    assert run(view, start, max_hops=5).depth_limited is True
    assert run(view, start, max_hops=6).depth_limited is False
    assert run(view, start, max_hops=8).depth_limited is False


def test_depth_limited_on_a_chain():
    v = _calls([("B", "A"), ("C", "B"), ("D", "C")])
    start = [v.node("m.py::A")]
    assert impact(v, start, max_hops=2).depth_limited is True
    assert impact(v, start, max_hops=3).depth_limited is False
