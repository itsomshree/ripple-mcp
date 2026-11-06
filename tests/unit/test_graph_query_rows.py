import pytest

from ripple.graph.query import Direction, Step, edge_from_row, node_from_row
from ripple.graph.schema import (
    AccessMode,
    APIEndpointNode,
    ClassNode,
    DBTableNode,
    Edge,
    EdgeType,
    Evidence,
    FileNode,
    FunctionNode,
    TestNode,
)

SPAN = {"file_path": "a.py", "line_start": 1, "line_end": 5}

NODES = [
    FileNode(id="a.py", name="a.py", path="a.py", content_hash="0" * 64, is_test=True),
    FunctionNode(
        id="a.py::C.f",
        name="f",
        qualname="C.f",
        is_async=True,
        class_name="C",
        decorators=["staticmethod"],
        **SPAN,
    ),
    FunctionNode(id="a.py::g", name="g", qualname="g", **SPAN),
    ClassNode(id="a.py::C", name="C", qualname="C", bases=["Base", "m.Mixin"], **SPAN),
    APIEndpointNode(
        id="endpoint::GET /x",
        name="GET /x",
        method="GET",
        route="/x",
        exact=False,
        mounted=False,
    ),
    DBTableNode(id="table: users", name="users"),
    TestNode(id="a.py::test_x", name="test_x", qualname="test_x", **SPAN),
]


@pytest.mark.parametrize("node", NODES, ids=lambda n: f"{n.type.value}:{n.id}")
def test_node_round_trips_through_properties(node):
    assert node_from_row(node.type.value, node.to_properties()) == node


def test_unknown_label_is_rejected():
    with pytest.raises(ValueError):
        node_from_row("Widget", {"id": "x", "name": "x"})


def test_edge_sentinels_become_none():
    assert edge_from_row("CALLS", "a", "b", "", "") == Edge(
        type=EdgeType.CALLS, src="a", dst="b"
    )
    assert edge_from_row("CALLS", "a", "b", None, None).access is None


def test_edge_access_and_via_are_restored():
    q = edge_from_row("QUERIES", "f", "table: t", "write", "")
    assert (q.access, q.via) == (AccessMode.WRITE, None)
    t = edge_from_row("TESTS", "t", "e", "", "http")
    assert (t.access, t.via) == (None, Evidence.HTTP)


def test_unknown_edge_type_is_rejected():
    with pytest.raises(ValueError):
        edge_from_row("LIKES", "a", "b", "", "")


def test_steps_are_value_objects():
    s = Step(EdgeType.CALLS, Direction.IN)
    assert s == Step(EdgeType.CALLS, Direction.IN)
    assert s != Step(EdgeType.CALLS, Direction.OUT)
    assert len({s, Step(EdgeType.CALLS, Direction.IN)}) == 1
