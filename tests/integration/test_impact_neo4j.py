from pathlib import Path

import pytest

from ripple.analysis.impact import CodeGraphView, impact
from ripple.analysis.neo4j_view import GraphQueryView
from ripple.graph.builder import GraphBuilder
from ripple.graph.query import GraphQuery
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
U = "app/services/user_service.py::UserService"

STARTS = [
    f"{U}.get",
    "app/models.py::User",
    "table: users",
    "app/services/user_service.py",
    "endpoint::GET /users/{user_id}",
    "endpoint::GET /health",
]


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


@pytest.fixture(scope="module")
def views(store, graph):
    store.replace_graph(graph)
    return CodeGraphView(graph), GraphQueryView(GraphQuery(store))


@pytest.mark.parametrize("start_id", STARTS)
@pytest.mark.parametrize("max_hops", [2, 5, 8])
def test_neo4j_view_matches_in_memory_view(views, start_id, max_hops):
    mem, db = views
    expected = impact(mem, [mem.node(start_id)], max_hops=max_hops)
    actual = impact(db, [db.node(start_id)], max_hops=max_hops)
    assert actual == expected


def test_importers_flag_matches_too(views):
    mem, db = views
    start = f"{U}.get"
    expected = impact(mem, [mem.node(start)], include_importers=True)
    actual = impact(db, [db.node(start)], include_importers=True)
    assert actual == expected
    assert expected.files


def test_cap_behaves_the_same_through_neo4j(views):
    mem, db = views
    start = "table: users"
    expected = impact(mem, [mem.node(start)], max_results=5)
    actual = impact(db, [db.node(start)], max_results=5)
    assert actual == expected
    assert actual.truncated and len(actual.entries) == 5
