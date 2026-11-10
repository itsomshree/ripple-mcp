import pytest

from ripple.analysis.impact import CodeGraphView
from ripple.analysis.neo4j_view import GraphQueryView
from ripple.analysis.request_flow import UnsupportedStart, trace_flow
from ripple.graph.query import GraphQuery

U = "app/services/user_service.py::UserService"
O = "app/services/order_service.py::OrderService"

STARTS = [
    "endpoint::POST /api/orders",
    "endpoint::GET /health",
    "endpoint::DELETE /users/{user_id}",
    f"{U}.get",
    f"{O}.place",
]


@pytest.fixture(scope="module")
def views(store, sample_graph):
    store.replace_graph(sample_graph)
    return CodeGraphView(sample_graph), GraphQueryView(GraphQuery(store))


@pytest.mark.parametrize("start_id", STARTS)
@pytest.mark.parametrize("max_hops", [1, 2, 6])
def test_neo4j_view_matches_in_memory_view(views, start_id, max_hops):
    mem, db = views
    expected = trace_flow(mem, mem.node(start_id), max_hops=max_hops)
    actual = trace_flow(db, db.node(start_id), max_hops=max_hops)
    assert actual == expected


def test_cap_behaves_the_same_through_neo4j(views):
    mem, db = views
    start = "endpoint::POST /api/orders"
    expected = trace_flow(mem, mem.node(start), max_results=2)
    actual = trace_flow(db, db.node(start), max_results=2)
    assert actual == expected
    assert actual.truncated and len(actual.steps) == 2


def test_unsupported_start_is_rejected_through_neo4j(views):
    _, db = views
    with pytest.raises(UnsupportedStart):
        trace_flow(db, db.node("table: users"))
