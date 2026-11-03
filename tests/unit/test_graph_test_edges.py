import textwrap

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.coverage import CoverageLinks
from ripple.graph.schema import Edge, EdgeType, Evidence
from ripple.parsers.python_parser import parse_python_source

SVC = "app/svc.py"


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def builder_for(files: dict[str, str]) -> GraphBuilder:
    builder = GraphBuilder()
    for path, text in files.items():
        builder.add(parse_python_source(src_of(text), path))
    return builder


def build(files: dict[str, str]) -> CodeGraph:
    return builder_for(files).build()


def covers(g: CodeGraph) -> list[tuple[str, str, Evidence | None]]:
    return [(e.src, e.dst, e.via) for e in g.edges_of(EdgeType.TESTS)]


def assert_no_dangling(g: CodeGraph) -> None:
    for e in g.edges:
        assert e.src in g.nodes and e.dst in g.nodes, e


# direct calls
def test_cross_file_call_forms():
    g = build(
        {
            "app/__init__.py": "",
            SVC: "def get(): ...\n",
            "tests/test_a.py": "from app.svc import get\ndef test_a():\n    get()\n",
            "tests/test_b.py": "import app.svc\ndef test_b():\n    app.svc.get()\n",
            "tests/test_c.py": "from app import svc\ndef test_c():\n    svc.get()\n",
            "tests/test_d.py": "import app.svc as m\ndef test_d():\n    m.get()\n",
        }
    )
    assert covers(g) == [
        (f"tests/test_{x}.py::test_{x}", f"{SVC}::get", Evidence.CALL) for x in "abcd"
    ]
    assert_no_dangling(g)


def test_class_member_and_instantiation_targets():
    g = build(
        {
            "app/__init__.py": "",
            SVC: """
                class Svc:
                    def __init__(self): ...

                    @staticmethod
                    def make(): ...

                class Plain:
                    x = 1
                """,
            "tests/test_cls.py": """
                from app.svc import Svc, Plain

                def test_method():
                    Svc.make()

                def test_instantiate():
                    Svc()

                def test_plain():
                    Plain()
                """,
        }
    )
    t = "tests/test_cls.py"
    assert set(covers(g)) == {
        (f"{t}::test_method", f"{SVC}::Svc.make", Evidence.CALL),
        (f"{t}::test_instantiate", f"{SVC}::Svc", Evidence.CALL),
        (f"{t}::test_instantiate", f"{SVC}::Svc.__init__", Evidence.CALL),
        (f"{t}::test_plain", f"{SVC}::Plain", Evidence.CALL),
    }
    assert_no_dangling(g)


def test_test_method_in_class_and_repeated_calls_collapse():
    g = build(
        {
            SVC: "def get(): ...\n",
            "tests/test_svc.py": """
                from app.svc import get

                class TestSvc:
                    def test_get(self):
                        get()
                        get()
                """,
        }
    )
    assert covers(g) == [
        ("tests/test_svc.py::TestSvc.test_get", f"{SVC}::get", Evidence.CALL)
    ]


# exclusions
def test_only_direct_calls_from_tests_to_non_test_code_count():
    g = build(
        {
            "app/__init__.py": "",
            SVC: "def get(): ...\n",
            "tests/helpers.py": "from app.svc import get\ndef make():\n    get()\n",
            "tests/test_x.py": """
                import os
                import pytest
                from app.svc import get
                from tests.helpers import make

                @pytest.fixture
                def client():
                    get()

                def helper():
                    get()

                def test_direct():
                    get()

                def test_via_helper():
                    make()
                    os.getcwd()

                def test_calls_test():
                    test_direct()

                def test_nested():
                    def run():
                        get()
                    run()
                """,
        }
    )
    assert covers(g) == [("tests/test_x.py::test_direct", f"{SVC}::get", Evidence.CALL)]
    assert_no_dangling(g)


def test_module_level_call_in_a_test_file_is_not_coverage():
    g = build(
        {
            SVC: "def get(): ...\n",
            "tests/test_m.py": "from app.svc import get\nget()\n",
        }
    )
    assert covers(g) == []


# evidence collapse
def test_strongest_evidence_wins_regardless_of_order():
    orders = [
        [Evidence.NAME, Evidence.HTTP, Evidence.CALL],
        [Evidence.CALL, Evidence.HTTP, Evidence.NAME],
        [Evidence.HTTP, Evidence.CALL, Evidence.NAME],
    ]
    for order in orders:
        links = CoverageLinks()
        for via in order:
            links.add("t", "f", via)
        assert links.items() == [("t", "f", Evidence.CALL)]


def test_http_beats_name_and_items_are_sorted():
    links = CoverageLinks()
    links.add("b", "x", Evidence.CALL)
    links.add("a", "y", Evidence.NAME)
    links.add("a", "x", Evidence.NAME)
    links.add("a", "x", Evidence.HTTP)
    assert links.items() == [
        ("a", "x", Evidence.HTTP),
        ("a", "y", Evidence.NAME),
        ("b", "x", Evidence.CALL),
    ]


# schema
def test_via_is_part_of_edge_identity():
    call = Edge(type=EdgeType.TESTS, src="t", dst="f", via=Evidence.CALL)
    assert call != call.model_copy(update={"via": Evidence.NAME})
    assert Edge(type=EdgeType.CALLS, src="a", dst="b").via is None


def test_edges_of_orders_ties_by_via():
    g = CodeGraph()
    for via in (Evidence.NAME, Evidence.CALL):
        g.add_edge(Edge(type=EdgeType.TESTS, src="t", dst="f", via=via))
    assert [e.via for e in g.edges_of(EdgeType.TESTS)] == [Evidence.CALL, Evidence.NAME]


# builder semantics
def test_rebuilds_are_deterministic():
    builder = builder_for(
        {
            "app/__init__.py": "",
            SVC: "def get(): ...\nclass C: ...\n",
            "tests/test_a.py": "from app.svc import get, C\ndef test_a():\n    get()\n    C()\n",
        }
    )
    first = builder.build()
    assert covers(first)
    again = builder.build()
    assert again.edges == first.edges
    assert set(again.nodes) == set(first.nodes)


def test_removing_files_clears_edges():
    files = {
        "app/__init__.py": "",
        SVC: "def get(): ...\n",
        "tests/test_a.py": "from app.svc import get\ndef test_a():\n    get()\n",
    }
    builder = builder_for(files)
    assert len(covers(builder.build())) == 1

    builder.remove(SVC)
    g = builder.build()
    assert covers(g) == []
    assert_no_dangling(g)

    builder = builder_for(files)
    builder.remove("tests/test_a.py")
    assert covers(builder.build()) == []
