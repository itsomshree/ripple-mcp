from collections import Counter, defaultdict
from pathlib import Path

import pytest

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import EdgeType, NodeType, UnresolvedReason
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.python_parser import parse_python_file

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"

ORDERS = "app/services/order_service.py::OrderService"
USERS = "app/services/user_service.py::UserService"


@pytest.fixture(scope="module")
def builder() -> GraphBuilder:
    b = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        b.add(
            parse_python_file(p, rel), parse_fastapi_file(p, rel), parse_db_file(p, rel)
        )
    return b


@pytest.fixture(scope="module")
def graph(builder: GraphBuilder) -> CodeGraph:
    return builder.build()


def edges(g: CodeGraph, type_: EdgeType):
    return [(e.src, e.dst, e.access) for e in g.edges_of(type_)]


def tables_reached(g: CodeGraph, start: str) -> set[tuple[str, str]]:
    """(table, access) pairs reachable from `start` via HANDLES/CALLS/DEPENDS_ON."""
    follow = {EdgeType.HANDLES, EdgeType.CALLS, EdgeType.DEPENDS_ON}
    adj: dict[str, list[str]] = defaultdict(list)
    queries: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for e in g.edges:
        if e.type in follow:
            adj[e.src].append(e.dst)
        elif e.type == EdgeType.QUERIES:
            queries[e.src].add((e.dst, e.access.value))

    seen: set[str] = set()
    stack = [start]
    found: set[tuple[str, str]] = set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        found |= queries[node]
        stack.extend(adj[node])
    return found


def test_node_counts_by_type(graph):
    assert dict(Counter(n.type for n in graph.nodes.values())) == {
        NodeType.FILE: 10,
        NodeType.CLASS: 5,
        NodeType.FUNCTION: 14,
        NodeType.API_ENDPOINT: 7,
        NodeType.DB_TABLE: 2,
    }


def test_edge_counts_by_type(graph):
    assert dict(Counter(e.type for e in graph.edges)) == {
        EdgeType.DEFINED_IN: 19,
        EdgeType.IMPORTS: 9,
        EdgeType.CALLS: 14,
        EdgeType.HANDLES: 7,
        EdgeType.MAPS_TO: 2,
        EdgeType.QUERIES: 7,
    }


def test_models_map_to_their_tables(graph):
    assert edges(graph, EdgeType.MAPS_TO) == [
        ("app/models.py::Order", "table: orders", None),
        ("app/models.py::User", "table: users", None),
    ]


def test_queries_edges_carry_access(graph):
    assert edges(graph, EdgeType.QUERIES) == [
        (f"{ORDERS}.get", "table: orders", "read"),
        (f"{ORDERS}.place", "table: orders", "write"),
        (f"{USERS}.create", "table: users", "write"),
        (f"{USERS}.delete", "table: users", "read"),
        (f"{USERS}.delete", "table: users", "write"),
        (f"{USERS}.get", "table: users", "read"),
        (f"{USERS}.list_all", "table: users", "read"),
    ]


@pytest.mark.parametrize(
    "endpoint, expected",
    [
        ("endpoint::GET /health", set()),
        ("endpoint::GET /api/orders/{order_id}", {("table: orders", "read")}),
        (
            "endpoint::POST /api/orders",
            {("table: orders", "write"), ("table: users", "read")},
        ),
        ("endpoint::GET /users", {("table: users", "read")}),
        ("endpoint::GET /users/{user_id}", {("table: users", "read")}),
        ("endpoint::POST /users", {("table: users", "write")}),
        (
            "endpoint::DELETE /users/{user_id}",
            {("table: users", "read"), ("table: users", "write")},
        ),
    ],
)
def test_endpoint_reaches_expected_tables(graph, endpoint, expected):
    assert endpoint in graph.nodes
    assert tables_reached(graph, endpoint) == expected


def test_no_dangling_edges(graph):
    for e in graph.edges:
        assert e.src in graph.nodes and e.dst in graph.nodes, e


def test_diagnostics_are_clean(graph):
    assert graph.route_issues == []
    assert graph.db_issues == []
    assert [
        u
        for u in graph.unresolved
        if u.reason in (UnresolvedReason.NOT_FOUND, UnresolvedReason.AMBIGUOUS)
    ] == []


def test_rebuild_is_deterministic(builder, graph):
    again = builder.build()
    assert again.edges == graph.edges
    assert set(again.nodes) == set(graph.nodes)
    assert again.db_issues == graph.db_issues
    assert again.route_issues == graph.route_issues
