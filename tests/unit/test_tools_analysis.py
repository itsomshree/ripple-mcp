from pathlib import Path

import pytest

from ripple.analysis.affected_tests import AffectedTest, AffectedTests, HelperRef
from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import Edge, EdgeType, FileNode, FunctionNode
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file
from ripple.tools._common import MAX_HOPS_LIMIT, GraphAccess, ToolStatus
from ripple.tools.affected_tests_tools import affected_tests, pytest_command
from ripple.tools.impact_tools import what_breaks_if_i_change
from ripple.tools.trace_tools import TableAccessOut, trace_request_flow

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
U = "app/services/user_service.py::UserService"
O = "app/services/order_service.py::OrderService"
R = "app/routers/"
DB = "app/db.py::get_session"
TU, TO = "tests/test_users.py", "tests/test_orders.py"


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


def _callers_graph(n: int) -> GraphAccess:
    g = CodeGraph()
    g.add_node(FileNode(id="m.py", name="m.py", path="m.py", content_hash="x"))
    names = ["A", *(f"c{i:02}" for i in range(n))]
    for name in names:
        g.add_node(
            FunctionNode(
                id=f"m.py::{name}",
                name=name,
                qualname=name,
                file_path="m.py",
                line_start=1,
                line_end=2,
            )
        )
    for name in names[1:]:
        g.add_edge(Edge(type=EdgeType.CALLS, src=f"m.py::{name}", dst="m.py::A"))
    return GraphAccess.from_graph(g)


# what_breaks_if_i_change
def test_service_method(access):
    r = what_breaks_if_i_change(access, "UserService.get")
    assert r.status is ToolStatus.OK
    assert r.entity.id == f"{U}.get"
    assert r.risk.level == "medium" and r.risk.caveats == []
    assert (
        r.functions.total,
        r.classes.total,
        r.endpoints.total,
        r.tables.total,
        r.tests.total,
        r.files.total,
    ) == (3, 0, 2, 1, 4, 0)
    first = r.endpoints.items[0]
    assert (first.id, first.hops, first.via, first.parent) == (
        "endpoint::GET /users/{user_id}",
        2,
        "HANDLES",
        f"{R}users.py::get_user",
    )
    assert not r.truncated and not r.depth_limited
    assert r.max_hops == 5


def test_default_depth_reports_that_more_exists(access):
    r = what_breaks_if_i_change(access, "app/models.py::User")
    assert r.depth_limited and r.tests.total == 5
    assert any("beyond 5 hops" in c for c in r.risk.caveats)


def test_deeper_search_finds_the_last_test(access):
    r = what_breaks_if_i_change(access, "app/models.py::User", max_hops=8)
    assert not r.depth_limited and r.tests.total == 6
    assert r.risk.level == "high"


def test_endpoint_start_returns_its_handler_as_context(access):
    r = what_breaks_if_i_change(access, "endpoint::GET /health")
    assert [c.id for c in r.context] == ["app/main.py::health"]
    assert any("No dependents found" in c for c in r.risk.caveats)
    assert r.functions.total == 0 and r.tests.total == 0


def test_problems_come_back_as_results_without_payload(access):
    r = what_breaks_if_i_change(access, "get")
    assert r.status is ToolStatus.AMBIGUOUS and len(r.candidates) == 2
    assert r.entity is None and r.risk is None and r.functions is None

    r = what_breaks_if_i_change(access, "nope")
    assert r.status is ToolStatus.NOT_FOUND

    empty = GraphAccess.from_graph(CodeGraph())
    assert what_breaks_if_i_change(empty, "x").status is ToolStatus.NOT_SCANNED


def test_groups_are_capped_but_report_the_total():
    a = _callers_graph(40)
    r = what_breaks_if_i_change(a, "m.py::A")
    assert (r.functions.total, len(r.functions.items), r.functions.more) == (
        40,
        25,
        True,
    )
    assert len(what_breaks_if_i_change(a, "m.py::A", limit=5).functions.items) == 5


def test_depth_and_limit_are_clamped(access):
    r = what_breaks_if_i_change(access, "UserService.get", max_hops=999, limit=0)
    assert r.max_hops == MAX_HOPS_LIMIT
    assert len(r.functions.items) == 1 and r.functions.more is True


# trace_request_flow
def test_post_orders_flow(access):
    r = trace_request_flow(access, "POST /api/orders")
    assert r.status is ToolStatus.OK
    assert r.start.id == "endpoint::POST /api/orders"
    assert {i.id: i.hops for i in r.steps.items} == {
        f"{R}orders.py::place_order": 1,
        f"{O}.place": 2,
        f"{U}.get": 3,
        DB: 3,
    }
    place = next(i for i in r.steps.items if i.id == f"{O}.place")
    assert place.tables == [TableAccessOut(table="table: orders", access="write")]
    assert [(t.name, t.access, t.touched_by, t.hops) for t in r.tables] == [
        ("orders", ["write"], [f"{O}.place"], 2),
        ("users", ["read"], [f"{U}.get"], 3),
    ]
    assert r.caveats == [] and r.max_hops == 6


def test_function_start_reports_its_own_tables(access):
    r = trace_request_flow(access, "UserService.get")
    assert r.start_tables == [TableAccessOut(table="table: users", access="read")]
    assert [i.id for i in r.steps.items] == [DB]


def test_endpoint_without_tables_gets_a_caveat(access):
    r = trace_request_flow(access, "GET /health")
    assert [i.id for i in r.steps.items] == ["app/main.py::health"]
    assert any("No table access" in c for c in r.caveats)


@pytest.mark.parametrize(
    "query, kind",
    [
        ("UserService", "Class"),
        ("table: users", "DBTable"),
        ("tests/conftest.py::make_user", "test helper"),
    ],
)
def test_starts_that_are_not_flows_say_what_they_are(access, query, kind):
    r = trace_request_flow(access, query)
    assert r.status is ToolStatus.UNSUPPORTED
    assert kind in r.message and r.steps is None


def test_trace_depth_and_step_caps(access):
    r = trace_request_flow(access, "POST /api/orders", max_hops=2)
    assert r.depth_limited and any("beyond 2 hops" in c for c in r.caveats)
    r = trace_request_flow(access, "POST /api/orders", limit=2)
    assert (r.steps.total, len(r.steps.items), r.steps.more) == (4, 2, True)


def test_trace_problems_come_back_as_results(access):
    assert trace_request_flow(access, "get").status is ToolStatus.AMBIGUOUS
    assert trace_request_flow(access, "GET /nope").status is ToolStatus.NOT_FOUND


# affected_tests
def test_service_method_tests_and_command(access):
    r = affected_tests(access, "UserService.get")
    assert r.status is ToolStatus.OK
    assert [(t.pytest_id, t.hops, t.evidence) for t in r.tests] == [
        (f"{TU}::test_get_user_service", 1, "call"),
        (f"{TO}::test_place_order_service", 2, "call"),
        (f"{TU}::test_get_user_endpoint", 3, "http"),
        (f"{TO}::test_place_order_endpoint", 4, "http"),
    ]
    assert r.pytest_command == (
        f"pytest {TU}::test_get_user_service {TO}::test_place_order_service "
        f"{TU}::test_get_user_endpoint {TO}::test_place_order_endpoint"
    )
    assert r.files == [TU, TO] and r.caveats == [] and r.max_hops == 8


def test_model_class_lists_its_helper(access):
    r = affected_tests(access, "app/models.py::User")
    assert r.tests_total == 5
    assert r.helpers == [
        HelperRef(id="tests/conftest.py::make_user", file="tests/conftest.py", hops=2)
    ]


def test_untested_endpoint_has_no_command_and_a_caveat(access):
    r = affected_tests(access, "GET /health")
    assert r.tests == [] and r.pytest_command is None
    assert any("No tests reach" in c for c in r.caveats)


def test_test_list_is_capped_but_the_command_is_not(access):
    r = affected_tests(access, "UserService.get", limit=2)
    assert (r.tests_total, len(r.tests), r.more_tests) == (4, 2, True)
    assert "test_place_order_endpoint" in r.pytest_command


def test_a_test_start_is_unsupported(access):
    r = affected_tests(access, f"{TU}::test_get_user_service")
    assert r.status is ToolStatus.UNSUPPORTED and "already a test" in r.message


def test_affected_tests_problems_come_back_as_results(access):
    r = affected_tests(access, "get")
    assert r.status is ToolStatus.AMBIGUOUS and r.tests == []


def test_command_falls_back_to_files_for_long_lists():
    tests = [
        AffectedTest(
            id=f"t{i}",
            name=f"test_{i}",
            file=f"tests/t{i % 2}.py",
            pytest_id=f"tests/t{i % 2}.py::test_{i}",
            hops=1,
            evidence=None,
            path=[],
        )
        for i in range(21)
    ]
    found = AffectedTests(starts=[], tests=tests, files=["tests/t0.py", "tests/t1.py"])
    assert pytest_command(found) == "pytest tests/t0.py tests/t1.py"
    assert pytest_command(AffectedTests(starts=[])) is None
