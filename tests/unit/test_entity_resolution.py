from pathlib import Path

import pytest

from ripple.analysis.entity import GraphFinder, ResolveStatus, resolve_entity
from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import FunctionNode, NodeType
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
USERS = "app/services/user_service.py"
ORDERS = "app/services/order_service.py"


@pytest.fixture(scope="module")
def finder():
    b = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        b.add(
            parse_python_file(p, rel),
            parse_fastapi_file(p, rel),
            parse_db_file(p, rel),
            parse_coverage_file(p, rel),
        )
    return GraphFinder(b.build())


@pytest.mark.parametrize(
    "query, expected",
    [
        (f"{USERS}::UserService.get", f"{USERS}::UserService.get"),
        ("UserService.get", f"{USERS}::UserService.get"),
        ("UserService", f"{USERS}::UserService"),
        ("users", "table: users"),
        ("table: users", "table: users"),
        ("POST /api/orders", "endpoint::POST /api/orders"),
        ("  post   /api/orders ", "endpoint::POST /api/orders"),
        ("endpoint::GET /health", "endpoint::GET /health"),
        ("GET /users/", "endpoint::GET /users"),
        ("services/user_service.py", USERS),
        ("user_service.py", USERS),
        ("app\\services\\user_service.py", USERS),
    ],
)
def test_resolves_to_one_node(finder, query, expected):
    r = resolve_entity(finder, query)
    assert r.status is ResolveStatus.OK
    assert r.node.id == expected
    assert r.candidates == []


def test_bare_name_shared_by_two_methods_is_ambiguous(finder):
    r = resolve_entity(finder, "get")
    assert r.status is ResolveStatus.AMBIGUOUS
    assert r.node is None
    assert {n.id for n in r.candidates} == {
        f"{USERS}::UserService.get",
        f"{ORDERS}::OrderService.get",
    }


def test_type_filter_can_disambiguate_or_exclude(finder):
    r = resolve_entity(finder, "get", types=[NodeType.CLASS])
    assert r.status is ResolveStatus.NOT_FOUND
    assert r.candidates == []
    r = resolve_entity(finder, "users", types=[NodeType.DB_TABLE])
    assert r.node.id == "table: users"


def test_not_found_suggests_by_the_nearest_segment(finder):
    r = resolve_entity(finder, "UserService.nope")
    assert r.status is ResolveStatus.NOT_FOUND
    assert [n.id for n in r.candidates] == [f"{USERS}::UserService"]


def test_not_found_without_suggestions(finder):
    r = resolve_entity(finder, "GET /nope")
    assert r.status is ResolveStatus.NOT_FOUND
    assert r.candidates == []


@pytest.mark.parametrize("query", ["", "   "])
def test_blank_query_is_not_found(finder, query):
    r = resolve_entity(finder, query)
    assert r.status is ResolveStatus.NOT_FOUND and r.candidates == []


# handcrafted graphs
def _fn(path: str, qualname: str) -> FunctionNode:
    return FunctionNode(
        id=f"{path}::{qualname}",
        name=qualname.rpartition(".")[2],
        qualname=qualname,
        file_path=path,
        line_start=1,
        line_end=2,
    )


def _graph(*nodes) -> GraphFinder:
    g = CodeGraph()
    for n in nodes:
        g.add_node(n)
    return GraphFinder(g)


def test_better_match_tier_wins_over_a_looser_one():
    f = _graph(_fn("m.py", "get"), _fn("m.py", "C.get"))
    r = resolve_entity(f, "get")
    assert r.status is ResolveStatus.OK and r.node.id == "m.py::get"


def test_same_qualname_in_two_files_is_ambiguous():
    f = _graph(_fn("a.py", "f"), _fn("b.py", "f"))
    r = resolve_entity(f, "f")
    assert r.status is ResolveStatus.AMBIGUOUS
    assert [n.id for n in r.candidates] == ["a.py::f", "b.py::f"]


def test_ambiguous_candidates_are_capped():
    f = _graph(*(_fn(f"m{i:02}.py", "f") for i in range(12)))
    assert len(resolve_entity(f, "f").candidates) == 10
    assert len(resolve_entity(f, "f", limit=3).candidates) == 3
