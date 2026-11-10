import pytest

from ripple.analysis.entity import GraphFinder
from ripple.graph.query import GraphQuery
from ripple.graph.schema import NodeType

NAMES = [
    "get",
    "UserService.get",
    "users",
    "services/user_service.py",
    "user_service.py",
    "POST /api/orders",
    "app/models.py::User",
    "nope",
]


@pytest.fixture(scope="module")
def finders(store, sample_graph):
    store.replace_graph(sample_graph)
    return GraphFinder(sample_graph), GraphQuery(store)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("types", [None, [NodeType.FUNCTION, NodeType.CLASS]])
def test_in_memory_finder_matches_neo4j(finders, name, types):
    mem, db = finders
    assert mem.find_nodes(name, types) == db.find_nodes(name, types)
