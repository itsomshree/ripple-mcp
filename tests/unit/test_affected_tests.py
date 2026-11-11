from pathlib import Path

import pytest

from ripple.analysis.affected_tests import (
    DEFAULT_MAX_HOPS,
    HelperRef,
    affected_from_impact,
    find_affected_tests,
)
from ripple.analysis.impact import CodeGraphView, impact
from ripple.analysis.request_flow import UnsupportedStart
from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import Edge, Evidence, FileNode, FunctionNode, TestNode
from ripple.graph.schema import EdgeType as T
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
U = "app/services/user_service.py::UserService"
O = "app/services/order_service.py::OrderService"
R = "app/routers/"
TU, TO = "tests/test_users.py", "tests/test_orders.py"


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
    return find_affected_tests(view, [view.node(node_id)], **kw)


def rows(a):
    return [(t.pytest_id, t.hops, t.evidence) for t in a.tests]


# sample fixture
def test_service_method(view):
    a = run(view, f"{U}.get")
    assert rows(a) == [
        (f"{TU}::test_get_user_service", 1, Evidence.CALL),
        (f"{TO}::test_place_order_service", 2, Evidence.CALL),
        (f"{TU}::test_get_user_endpoint", 3, Evidence.HTTP),
        (f"{TO}::test_place_order_endpoint", 4, Evidence.HTTP),
    ]
    assert a.helpers == []
    assert a.files == [TU, TO]
    assert a.caveats == []
    assert not a.truncated and not a.depth_limited


def test_path_runs_from_the_start_to_the_test(view):
    a = run(view, f"{U}.get")
    far = next(t for t in a.tests if t.name == "test_place_order_endpoint")
    assert far.path == [
        f"{U}.get",
        f"{O}.place",
        f"{R}orders.py::place_order",
        "endpoint::POST /api/orders",
        far.id,
    ]


def test_model_class_lists_helpers_apart_from_runnable_tests(view):
    a = run(view, "app/models.py::User")
    assert rows(a) == [
        (f"{TU}::test_create_user_via_helper", 2, Evidence.CALL),
        (f"{TU}::test_get_user_service", 3, Evidence.CALL),
        (f"{TO}::test_place_order_service", 4, Evidence.CALL),
        (f"{TU}::test_get_user_endpoint", 5, Evidence.HTTP),
        (f"{TO}::test_place_order_endpoint", 6, Evidence.HTTP),
    ]
    assert a.helpers == [
        HelperRef(id="tests/conftest.py::make_user", file="tests/conftest.py", hops=2)
    ]
    assert "tests/conftest.py" not in a.files


def test_endpoint_start_finds_the_test_that_hits_it(view):
    a = run(view, "endpoint::GET /users/{user_id}")
    assert rows(a) == [(f"{TU}::test_get_user_endpoint", 1, Evidence.HTTP)]


def test_untested_endpoint_gets_a_caveat(view):
    a = run(view, "endpoint::GET /health")
    assert a.tests == [] and a.files == []
    assert any("No tests reach" in c for c in a.caveats)


def test_depth_limit_is_a_caveat_and_drops_the_farthest_test(view):
    a = run(view, "app/models.py::User", max_hops=5)
    assert a.depth_limited
    assert f"{TO}::test_place_order_endpoint" not in [t.pytest_id for t in a.tests]
    assert any("beyond 5 hops" in c for c in a.caveats)


def test_default_depth_is_eight(view):
    assert run(view, f"{U}.get").max_hops == DEFAULT_MAX_HOPS == 8


def test_from_impact_matches_the_wrapper(view):
    start = view.node(f"{U}.get")
    assert affected_from_impact(impact(view, [start], max_hops=8)) == run(
        view, f"{U}.get"
    )


def test_a_test_start_is_rejected(view):
    with pytest.raises(UnsupportedStart):
        run(view, f"{TU}::test_get_user_service")


# handcrafted graph
def _test_node(qualname, class_name=None):
    return TestNode(
        id=f"tests/test_a.py::{qualname}",
        name=qualname.rpartition(".")[2],
        qualname=qualname,
        file_path="tests/test_a.py",
        line_start=1,
        line_end=2,
        class_name=class_name,
    )


def test_pytest_ids_and_evidence_read_from_tests_edges():
    g = CodeGraph()
    for path, is_test in (("app/svc.py", False), ("tests/test_a.py", True)):
        g.add_node(
            FileNode(id=path, name=path, path=path, content_hash="x", is_test=is_test)
        )
    g.add_node(
        FunctionNode(
            id="app/svc.py::get",
            name="get",
            qualname="get",
            file_path="app/svc.py",
            line_start=1,
            line_end=2,
        )
    )
    method = _test_node("TestSvc.test_get", "TestSvc")
    plain = _test_node("test_plain")
    for node, via in ((method, Evidence.CALL), (plain, Evidence.HTTP)):
        g.add_node(node)
        g.add_edge(Edge(type=T.TESTS, src=node.id, dst="app/svc.py::get", via=via))

    v = CodeGraphView(g)
    a = find_affected_tests(v, [v.node("app/svc.py::get")])
    assert rows(a) == [
        ("tests/test_a.py::TestSvc::test_get", 1, Evidence.CALL),
        ("tests/test_a.py::test_plain", 1, Evidence.HTTP),
    ]
