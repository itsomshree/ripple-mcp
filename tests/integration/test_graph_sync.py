from pathlib import Path

import pytest
from neo4j.exceptions import CypherSyntaxError

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.diff import EdgeChange, GraphDelta
from ripple.graph.query import GraphQuery
from ripple.graph.schema import Edge, EdgeType, NodeType
from ripple.graph.sync import sync_graph
from ripple.parsers.db_parser import parse_db_source
from ripple.parsers.fastapi_parser import parse_fastapi_source
from ripple.parsers.pytest_parser import parse_coverage_source
from ripple.parsers.python_parser import parse_python_source

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
TEST_USERS = "tests/test_users.py"


def source(rel: str) -> str:
    return (FIXTURE / rel).read_bytes().decode("utf-8")


def build(skip: frozenset[str] = frozenset(), edits: dict[str, str] | None = None):
    edits = edits or {}
    b = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        if rel in skip:
            continue
        src = edits.get(rel, source(rel))
        b.add(
            parse_python_source(src, rel),
            parse_fastapi_source(src, rel),
            parse_db_source(src, rel),
            parse_coverage_source(src, rel),
        )
    return b.build()


def stored(store) -> CodeGraph:
    return GraphQuery(store).load_graph()


def assert_same(a: CodeGraph, b: CodeGraph) -> None:
    assert a.nodes == b.nodes
    assert a.edges == b.edges


@pytest.fixture
def seeded(store, sample_graph):
    store.replace_graph(sample_graph)
    return store


def test_sync_into_an_empty_store_writes_the_whole_graph(store, sample_graph):
    store.clear()
    d = sync_graph(store, sample_graph)
    assert (len(d.nodes_added), len(d.edges_added)) == (48, 79)
    assert_same(stored(store), sample_graph)


def test_syncing_an_unchanged_graph_is_a_no_op(seeded, sample_graph):
    assert sync_graph(seeded, sample_graph).is_empty
    assert_same(stored(seeded), sample_graph)


def test_a_change_in_one_file_updates_edges_that_start_in_other_files(
    seeded, sample_graph
):
    db = source("app/db.py").replace("get_session", "open_session")
    changed = build(edits={"app/db.py": db})

    d = sync_graph(seeded, changed)
    dropped = {c.edge.src for c in d.edges_removed if c.edge.type == EdgeType.CALLS}
    assert "app/services/user_service.py::UserService.get" in dropped
    assert_same(stored(seeded), changed)

    sync_graph(seeded, sample_graph)
    assert_same(stored(seeded), sample_graph)


def test_removing_a_file_removes_its_nodes_and_edges(seeded):
    smaller = build(skip=frozenset({TEST_USERS}))
    assert not sync_graph(seeded, smaller).is_empty
    assert_same(stored(seeded), smaller)
    assert sync_graph(seeded, smaller).is_empty


def test_a_node_that_changes_label_is_replaced_with_its_edges(seeded):
    extra = "\n\nclass TestExtra:\n    def test_extra(self):\n        pass\n"
    as_test = source(TEST_USERS) + extra
    not_test = as_test.replace(
        "    def test_extra(self):",
        "    def __init__(self):\n        pass\n\n    def test_extra(self):",
    )
    method = f"{TEST_USERS}::TestExtra.test_extra"

    first = build(edits={TEST_USERS: as_test})
    sync_graph(seeded, first)
    assert stored(seeded).nodes[method].type == NodeType.TEST

    second = build(edits={TEST_USERS: not_test})
    sync_graph(seeded, second)
    assert stored(seeded).nodes[method].type == NodeType.FUNCTION
    assert_same(stored(seeded), second)


def test_a_failing_delta_rolls_back_completely(seeded, sample_graph):
    victim = sample_graph.nodes["app/db.py::get_session"]

    # control: the delete on its own really removes the node
    seeded.apply_delta(GraphDelta(nodes_removed=(victim,)))
    assert victim.id not in stored(seeded).nodes
    seeded.replace_graph(sample_graph)

    # the same delete followed by a statement that fails must leave no trace
    bad = EdgeChange(
        Edge(type=EdgeType.CALLS, src="a", dst="b"), "Bad`Label", "Function"
    )
    with pytest.raises(CypherSyntaxError):
        seeded.apply_delta(GraphDelta(nodes_removed=(victim,), edges_added=(bad,)))
    assert_same(stored(seeded), sample_graph)
