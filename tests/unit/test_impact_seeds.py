from ripple.analysis.impact import CodeGraphView, context_for, seeds_for
from ripple.graph.builder import CodeGraph
from ripple.graph.schema import (
    APIEndpointNode,
    ClassNode,
    DBTableNode,
    Edge,
    FileNode,
    FunctionNode,
)
from ripple.graph.schema import EdgeType as T


def fn(path, q, cls=None):
    return FunctionNode(
        id=f"{path}::{q}",
        name=q.split(".")[-1],
        qualname=q,
        file_path=path,
        line_start=1,
        line_end=2,
        class_name=cls,
    )


def graph(nodes, edges):
    g = CodeGraph()
    for n in nodes:
        g.add_node(n)
    for t, s, d in edges:
        g.add_edge(Edge(type=t, src=s, dst=d))
    return g


def f(path):
    return FileNode(id=path, name=path.split("/")[-1], path=path, content_hash="x")


def test_class_seeds_methods_and_table():
    cls = ClassNode(
        id="m.py::User",
        name="User",
        qualname="User",
        file_path="m.py",
        line_start=1,
        line_end=9,
    )
    get, other = fn("m.py", "User.get", "User"), fn("m.py", "helper")
    tbl = DBTableNode(id="table: users", name="users")
    g = graph(
        [f("m.py"), cls, get, other, tbl],
        [
            (T.DEFINED_IN, get.id, "m.py"),
            (T.DEFINED_IN, other.id, "m.py"),
            (T.MAPS_TO, cls.id, tbl.id),
        ],
    )
    seeds = seeds_for(CodeGraphView(g), cls)
    assert [s.node_id for s in seeds] == ["m.py::User.get", "table: users"]
    assert all(s.parent == cls.id and s.hops == 1 for s in seeds)


def test_table_seeds_are_reverse_queries_and_maps_to():
    cls = ClassNode(
        id="m.py::User",
        name="User",
        qualname="User",
        file_path="m.py",
        line_start=1,
        line_end=2,
    )
    q = fn("m.py", "q")
    tbl = DBTableNode(id="table: users", name="users")
    g = graph([cls, q, tbl], [(T.QUERIES, q.id, tbl.id), (T.MAPS_TO, cls.id, tbl.id)])
    assert [s.node_id for s in seeds_for(CodeGraphView(g), tbl)] == [
        "m.py::User",
        "m.py::q",
    ]


def test_file_seeds_symbols_and_transitive_importers_with_hops():
    a, b, c = f("pkg/a.py"), f("pkg/__init__.py"), f("app/main.py")
    sym = fn("pkg/a.py", "go")
    g = graph(
        [a, b, c, sym],
        [
            (T.DEFINED_IN, sym.id, a.id),
            (T.IMPORTS, b.id, a.id),
            (T.IMPORTS, c.id, b.id),
        ],
    )
    seeds = {s.node_id: s.hops for s in seeds_for(CodeGraphView(g), a)}
    assert seeds == {"pkg/a.py::go": 1, "pkg/__init__.py": 1, "app/main.py": 2}
    assert seeds_for(CodeGraphView(g), a, max_hops=1).__len__() == 2


def test_importers_only_for_symbol_starts_when_asked():
    a, c = f("a.py"), f("c.py")
    sym = fn("a.py", "go")
    g = graph([a, c, sym], [(T.IMPORTS, c.id, a.id)])
    v = CodeGraphView(g)
    assert seeds_for(v, sym) == []
    assert [s.node_id for s in seeds_for(v, sym, include_importers=True)] == ["c.py"]


def test_endpoint_context_is_its_handler_not_a_seed():
    h = fn("app.py", "h")
    ep = APIEndpointNode(id="endpoint::GET /x", name="GET /x", method="GET", route="/x")
    g = graph([h, ep], [(T.HANDLES, ep.id, h.id)])
    v = CodeGraphView(g)
    assert seeds_for(v, ep) == []
    assert [n.id for n in context_for(v, ep)] == ["app.py::h"]
