import pytest

from ripple.graph.builder import CodeGraph
from ripple.graph.query import Direction, GraphQuery, Step
from ripple.graph.schema import AccessMode, Edge, EdgeType, FunctionNode, NodeType

USERS = "app/services/user_service.py::UserService"
ORDERS = "app/services/order_service.py::OrderService"
GET_USER = "app/routers/users.py::get_user"
PLACE_ORDER = "app/routers/orders.py::place_order"
T_USERS = "tests/test_users.py"
T_ORDERS = "tests/test_orders.py"

IN = Direction.IN
OUT = Direction.OUT
IMPACT = [
    Step(EdgeType.CALLS, IN),
    Step(EdgeType.HANDLES, IN),
    Step(EdgeType.TESTS, IN),
]


@pytest.fixture
def q(store, sample_graph):
    store.replace_graph(sample_graph)
    return GraphQuery(store)


# whole graph
def test_load_graph_round_trips_exactly(q, sample_graph):
    loaded = q.load_graph()
    assert loaded.nodes == sample_graph.nodes
    assert loaded.edges == sample_graph.edges


# lookups
def test_node_and_nodes(q, sample_graph):
    nid = f"{USERS}.get"
    assert q.node(nid) == sample_graph.nodes[nid]
    assert q.node("nope") is None
    got = q.nodes([nid, "endpoint::GET /health", "nope", nid])
    assert set(got) == {nid, "endpoint::GET /health"}
    assert q.nodes([]) == {}


def test_find_nodes_by_qualname_name_and_path(q):
    assert [n.id for n in q.find_nodes("UserService.get")] == [f"{USERS}.get"]
    assert {n.id for n in q.find_nodes("get")} == {f"{USERS}.get", f"{ORDERS}.get"}
    files = q.find_nodes("services/user_service.py", types=[NodeType.FILE])
    assert [n.id for n in files] == ["app/services/user_service.py"]
    assert q.find_nodes("ser_service.py") == []  # not on a '/' boundary


def test_find_nodes_filters_and_limits(q):
    assert q.find_nodes("get", types=[NodeType.CLASS]) == []
    assert len(q.find_nodes("get", limit=1)) == 1
    assert q.find_nodes("") == []
    assert [n.id for n in q.find_nodes("users", types=[NodeType.DB_TABLE])] == [
        "table: users"
    ]


# neighbors
def test_neighbors_out_keeps_edge_direction_and_access(q, sample_graph):
    delete = sample_graph.nodes[f"{USERS}.delete"]
    got = q.neighbors([delete], [Step(EdgeType.QUERIES, OUT)])
    assert [(n.from_id, n.edge.src, n.edge.dst, n.edge.access) for n in got] == [
        (delete.id, delete.id, "table: users", AccessMode.READ),
        (delete.id, delete.id, "table: users", AccessMode.WRITE),
    ]


def test_neighbors_in(q, sample_graph):
    table = sample_graph.nodes["table: users"]
    got = q.neighbors([table], [Step(EdgeType.QUERIES, IN)])
    assert all(n.from_id == "table: users" == n.edge.dst for n in got)
    assert {(n.edge.src, n.edge.access) for n in got} == {
        (f"{USERS}.create", AccessMode.WRITE),
        (f"{USERS}.delete", AccessMode.READ),
        (f"{USERS}.delete", AccessMode.WRITE),
        (f"{USERS}.get", AccessMode.READ),
        (f"{USERS}.list_all", AccessMode.READ),
    }


def test_neighbors_mixes_labels_in_one_call(q, sample_graph):
    endpoint = sample_graph.nodes["endpoint::GET /users/{user_id}"]
    function = sample_graph.nodes[f"{USERS}.get"]
    got = q.neighbors([endpoint, function], [Step(EdgeType.HANDLES, OUT)])
    assert [(n.from_id, n.node.id) for n in got] == [(endpoint.id, GET_USER)]


def test_neighbors_with_nothing_to_do(q, sample_graph):
    start = sample_graph.nodes[f"{USERS}.get"]
    assert q.neighbors([start], []) == []
    assert q.neighbors([], [Step(EdgeType.CALLS, IN)]) == []


# reachable
def by_hops(reached):
    out: dict[int, set[str]] = {}
    for r in reached:
        out.setdefault(r.hops, set()).add(r.node.id)
    return out


def test_reverse_impact_of_a_service_method(q, sample_graph):
    start = sample_graph.nodes[f"{USERS}.get"]
    reached = q.reachable([start], IMPACT, max_hops=10)
    assert by_hops(reached) == {
        1: {GET_USER, f"{ORDERS}.place", f"{T_USERS}::test_get_user_service"},
        2: {
            "endpoint::GET /users/{user_id}",
            PLACE_ORDER,
            f"{T_ORDERS}::test_place_order_service",
        },
        3: {"endpoint::POST /api/orders", f"{T_USERS}::test_get_user_endpoint"},
        4: {f"{T_ORDERS}::test_place_order_endpoint"},
    }
    assert start.id not in {r.node.id for r in reached}


def test_max_hops_limits_the_walk(q, sample_graph):
    start = sample_graph.nodes[f"{USERS}.get"]
    assert set(by_hops(q.reachable([start], IMPACT, max_hops=1))) == {1}
    assert q.reachable([start], IMPACT, max_hops=0) == []
    assert q.reachable([start], [], max_hops=5) == []


def test_each_result_carries_the_edge_that_reached_it(q, sample_graph):
    start = sample_graph.nodes[f"{USERS}.get"]
    reached = q.reachable([start], IMPACT, max_hops=10)
    for r in reached:
        assert {r.via.src, r.via.dst} == {r.node.id, r.parent}
    endpoint = next(r for r in reached if r.node.id == "endpoint::GET /users/{user_id}")
    assert (endpoint.parent, endpoint.via.type) == (GET_USER, EdgeType.HANDLES)


def test_reachable_is_deterministic(q, sample_graph):
    start = sample_graph.nodes[f"{USERS}.get"]
    first = q.reachable([start], IMPACT, max_hops=10)
    assert q.reachable([start], IMPACT, max_hops=10) == first


def test_reachable_terminates_on_cycles(store):
    def fn(name):
        return FunctionNode(
            id=f"m.py::{name}",
            name=name,
            qualname=name,
            file_path="m.py",
            line_start=1,
            line_end=2,
        )

    a, b, c = fn("a"), fn("b"), fn("c")
    g = CodeGraph()
    for n in (a, b, c):
        g.add_node(n)
    for s, d in ((a, b), (b, c), (c, a)):
        g.add_edge(Edge(type=EdgeType.CALLS, src=s.id, dst=d.id))
    store.replace_graph(g)

    got = GraphQuery(store).reachable([a], [Step(EdgeType.CALLS, OUT)], max_hops=10)
    assert [(r.node.id, r.hops) for r in got] == [("m.py::b", 1), ("m.py::c", 2)]
