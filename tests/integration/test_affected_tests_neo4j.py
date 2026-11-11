import pytest

from ripple.analysis.affected_tests import find_affected_tests
from ripple.analysis.impact import CodeGraphView
from ripple.analysis.neo4j_view import GraphQueryView
from ripple.graph.query import GraphQuery

STARTS = [
    "app/services/user_service.py::UserService.get",
    "app/models.py::User",
    "endpoint::GET /users/{user_id}",
    "endpoint::GET /health",
]


@pytest.fixture(scope="module")
def views(store, sample_graph):
    store.replace_graph(sample_graph)
    return CodeGraphView(sample_graph), GraphQueryView(GraphQuery(store))


@pytest.mark.parametrize("start_id", STARTS)
def test_neo4j_view_matches_in_memory_view(views, start_id):
    mem, db = views
    expected = find_affected_tests(mem, [mem.node(start_id)])
    actual = find_affected_tests(db, [db.node(start_id)])
    assert actual == expected
