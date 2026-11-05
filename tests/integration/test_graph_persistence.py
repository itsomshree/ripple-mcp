from collections import Counter
from pathlib import Path

import pytest

from ripple.graph.builder import GraphBuilder
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
USERS = "app/services/user_service.py::UserService"


@pytest.fixture(scope="module")
def graph():
    b = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        b.add(
            parse_python_file(p, rel),
            parse_fastapi_file(p, rel),
            parse_db_file(p, rel),
            parse_coverage_file(p, rel),
        )
    return b.build()


def expected(graph):
    nodes = Counter(n.type.value for n in graph.nodes.values())
    edges = Counter(e.type.value for e in graph.edges)
    return dict(nodes), dict(edges)


def test_replace_graph_round_trip(store, graph):
    store.replace_graph(graph)
    nodes, edges = store.counts()
    assert (nodes, edges) == expected(graph)
    assert sum(nodes.values()) == 48
    assert sum(edges.values()) == 79


def test_write_graph_twice_changes_nothing(store, graph):
    store.replace_graph(graph)
    store.write_graph(graph)
    assert store.counts() == expected(graph)


def test_replace_graph_drops_old_data(store, graph):
    store.execute("CREATE (:File {id: 'stale.py'})")
    store.replace_graph(graph)
    assert store.execute("MATCH (n {id: 'stale.py'}) RETURN n") == []


def test_access_and_via_survive_the_round_trip(store, graph):
    store.replace_graph(graph)
    delete_edges = store.execute(
        "MATCH (f {id: $id})-[e:QUERIES]->(:DBTable {id: 'table: users'}) "
        "RETURN e.access AS a ORDER BY a",
        {"id": f"{USERS}.delete"},
    )
    assert delete_edges == [{"a": "read"}, {"a": "write"}]

    via = store.execute(
        "MATCH (:Test)-[e:TESTS]->(:APIEndpoint) RETURN DISTINCT e.via AS v"
    )
    assert via == [{"v": "http"}]
