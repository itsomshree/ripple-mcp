from pathlib import Path

import pytest

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.diff import GraphDelta, diff_graphs
from ripple.graph.schema import (
    AccessMode,
    DBTableNode,
    Edge,
    EdgeType,
    FunctionNode,
    TestNode,
)
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"


def fn(path: str, q: str) -> FunctionNode:
    return FunctionNode(
        id=f"{path}::{q}",
        name=q.rpartition(".")[2],
        qualname=q,
        file_path=path,
        line_start=1,
        line_end=2,
    )


def graph(nodes=(), edges=()) -> CodeGraph:
    g = CodeGraph()
    for n in nodes:
        g.add_node(n)
    for e in edges:
        g.add_edge(e)
    return g


def call(src: str, dst: str) -> Edge:
    return Edge(type=EdgeType.CALLS, src=src, dst=dst)


def apply(old: CodeGraph, delta: GraphDelta) -> CodeGraph:
    nodes = dict(old.nodes)
    for n in delta.nodes_removed:
        del nodes[n.id]
    for n in (*delta.nodes_added, *delta.nodes_changed):
        nodes[n.id] = n
    gone = {c.edge for c in delta.edges_removed}
    edges = (set(old.edges) - gone) | {c.edge for c in delta.edges_added}
    return CodeGraph(nodes=nodes, edges=edges)


def fixture_graph(skip: frozenset[str] = frozenset()) -> CodeGraph:
    b = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        if rel in skip:
            continue
        b.add(
            parse_python_file(p, rel),
            parse_fastapi_file(p, rel),
            parse_db_file(p, rel),
            parse_coverage_file(p, rel),
        )
    return b.build()


def test_identical_graphs_have_an_empty_delta():
    g = graph([fn("m.py", "a"), fn("m.py", "b")], [call("m.py::a", "m.py::b")])
    d = diff_graphs(g, g)
    assert d.is_empty
    assert set(d.counts().values()) == {0}


def test_added_removed_and_changed_nodes_and_edges():
    old = graph(
        [fn("m.py", "a"), fn("m.py", "b"), fn("m.py", "c")],
        [call("m.py::a", "m.py::b")],
    )
    moved = fn("m.py", "b").model_copy(update={"line_end": 9})
    new = graph(
        [fn("m.py", "a"), moved, fn("m.py", "d")],
        [call("m.py::a", "m.py::d")],
    )
    d = diff_graphs(old, new)
    assert [n.id for n in d.nodes_added] == ["m.py::d"]
    assert [n.id for n in d.nodes_removed] == ["m.py::c"]
    assert [n.id for n in d.nodes_changed] == ["m.py::b"]
    assert d.nodes_changed[0].line_end == 9
    assert [c.edge.dst for c in d.edges_added] == ["m.py::d"]
    assert [c.edge.dst for c in d.edges_removed] == ["m.py::b"]


def test_a_node_that_changes_type_is_replaced_and_its_edges_rewritten():
    old = graph([fn("t.py", "t"), fn("m.py", "f")], [call("t.py::t", "m.py::f")])
    promoted = TestNode(
        id="t.py::t",
        name="t",
        qualname="t",
        file_path="t.py",
        line_start=1,
        line_end=2,
    )
    new = graph([promoted, fn("m.py", "f")], [call("t.py::t", "m.py::f")])

    d = diff_graphs(old, new)
    assert [n.type.value for n in d.nodes_removed] == ["Function"]
    assert [n.type.value for n in d.nodes_added] == ["Test"]
    assert d.nodes_changed == ()
    (gone,), (fresh,) = d.edges_removed, d.edges_added
    assert gone.edge == fresh.edge
    assert (gone.src_label, fresh.src_label) == ("Function", "Test")
    assert apply(old, d).nodes == new.nodes


def test_access_is_part_of_edge_identity_and_labels_follow_the_endpoints():
    table = DBTableNode(id="table: t", name="t")
    read = Edge(
        type=EdgeType.QUERIES, src="m.py::f", dst="table: t", access=AccessMode.READ
    )
    write = read.model_copy(update={"access": AccessMode.WRITE})
    d = diff_graphs(
        graph([fn("m.py", "f"), table], [read]),
        graph([fn("m.py", "f"), table], [write]),
    )
    assert [c.edge.access for c in d.edges_removed] == [AccessMode.READ]
    assert [c.edge.access for c in d.edges_added] == [AccessMode.WRITE]
    assert (d.edges_added[0].src_label, d.edges_added[0].dst_label) == (
        "Function",
        "DBTable",
    )


def test_diff_is_deterministic():
    a, b = fixture_graph(), fixture_graph(frozenset({"app/db.py"}))
    assert diff_graphs(a, b) == diff_graphs(a, b)


@pytest.mark.parametrize(
    "skip",
    [
        {"app/db.py"},
        {"tests/test_orders.py"},
        {"app/routers/orders.py", "app/services/order_service.py"},
    ],
)
def test_delta_between_real_graphs_reproduces_the_target_both_ways(skip):
    full, partial = fixture_graph(), fixture_graph(frozenset(skip))
    for old, new in ((full, partial), (partial, full)):
        d = diff_graphs(old, new)
        assert not d.is_empty
        rebuilt = apply(old, d)
        assert rebuilt.nodes == new.nodes and rebuilt.edges == new.edges
        assert diff_graphs(rebuilt, new).is_empty


def test_edges_in_untouched_files_go_when_their_target_disappears():
    d = diff_graphs(fixture_graph(), fixture_graph(frozenset({"app/db.py"})))
    sources = {c.edge.src for c in d.edges_removed if c.edge.type == EdgeType.CALLS}
    assert "app/services/user_service.py::UserService.get" in sources
