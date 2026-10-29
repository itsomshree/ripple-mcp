from ripple.graph.builder import CodeGraph
from ripple.graph.schema import AccessMode, Edge, EdgeType


def test_edges_default_to_no_access_and_still_compare_equal():
    a = Edge(type=EdgeType.CALLS, src="a", dst="b")
    assert a.access is None
    assert a == Edge(type=EdgeType.CALLS, src="a", dst="b")


def test_access_is_part_of_edge_identity():
    r = Edge(type=EdgeType.QUERIES, src="f", dst="table: t", access=AccessMode.READ)
    w = r.model_copy(update={"access": AccessMode.WRITE})

    assert r != w


def test_edges_of_orders_ties_by_access():
    g = CodeGraph()
    for access in (AccessMode.WRITE, AccessMode.READ):
        g.add_edge(Edge(type=EdgeType.QUERIES, src="f", dst="table: t", access=access))
    g.add_edge(
        Edge(type=EdgeType.QUERIES, src="e", dst="table: t", access=AccessMode.READ)
    )
    assert [(e.src, e.access) for e in g.edges_of(EdgeType.QUERIES)] == [
        ("e", "read"),
        ("f", "read"),
        ("f", "write"),
    ]


def test_maps_to_edge_type_exists():
    assert EdgeType.MAPS_TO == "MAPS_TO"
