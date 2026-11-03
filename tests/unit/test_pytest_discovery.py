import textwrap

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.pytest_discovery import find_tests
from ripple.graph.schema import ClassNode, EdgeType, FunctionNode, NodeType, TestNode
from ripple.parsers.python_parser import parse_python_source


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def found(files: dict[str, str]) -> list[str]:
    parsed = {p: parse_python_source(src_of(s), p) for p, s in files.items()}
    return [fn.id for fn in find_tests(parsed)]


def build(files: dict[str, str]) -> GraphBuilder:
    builder = GraphBuilder()
    for path, text in files.items():
        builder.add(parse_python_source(src_of(text), path))
    return builder


def edge_pairs(g: CodeGraph, type_: EdgeType) -> set[tuple[str, str]]:
    return {(e.src, e.dst) for e in g.edges_of(type_)}


T = "tests/test_a.py"


# discovery
def test_module_level_functions_by_prefix():
    assert found(
        {
            T: """
            def test_x(): ...
            def testy(): ...
            def helper(): ...
            def check_test(): ...
            """
        }
    ) == [f"{T}::test_x", f"{T}::testy"]


def test_class_rules():
    assert found(
        {
            T: """
            import unittest
            from unittest import TestCase

            class TestUser:
                def test_get(self): ...
                def helper(self): ...

            class TestWithInit:
                def __init__(self): ...
                def test_x(self): ...

            class Helper:
                def test_y(self): ...

            class Anything(unittest.TestCase):
                def test_z(self): ...

            class Other(TestCase):
                def test_w(self): ...
            """
        }
    ) == [f"{T}::TestUser.test_get", f"{T}::Anything.test_z", f"{T}::Other.test_w"]


def test_fixtures_are_not_tests():
    assert found(
        {
            T: """
            import pytest
            from pytest import fixture

            @pytest.fixture
            def test_client(): ...

            @pytest.fixture()
            def test_db(): ...

            @fixture(scope="session")
            def test_cfg(): ...

            def test_real(): ...
            """
        }
    ) == [f"{T}::test_real"]


def test_nested_functions_and_nested_classes_are_not_detected():
    assert found(
        {
            T: """
            def test_outer():
                def test_inner(): ...

            class TestA:
                def test_m(self):
                    def test_deep(): ...

            class Outer:
                class TestInner:
                    def test_n(self): ...
            """
        }
    ) == [f"{T}::test_outer", f"{T}::TestA.test_m"]


def test_only_files_pytest_would_collect():
    body = "def test_x(): ...\n"
    files = {
        "tests/test_a.py": body,
        "app/test_users.py": body,
        "app/users_test.py": body,
        "tests/helpers.py": body,
        "conftest.py": body,
        "app/users.py": body,
        "src/ripple/parsers/pytest_parser.py": body,
    }
    assert found(files) == [
        "app/test_users.py::test_x",
        "app/users_test.py::test_x",
        "tests/test_a.py::test_x",
    ]


def test_syntax_error_file_yields_no_tests():
    assert found({T: "def test_x(:\n"}) == []


# promotion in the graph
FILES = {
    "app/svc.py": "def get(): ...\n",
    T: """
        from app.svc import get

        class TestSvc:
            async def test_get(self):
                get()

        def helper():
            get()
        """,
}


def test_tests_are_promoted_and_keep_their_edges():
    g = build(FILES).build()
    tid = f"{T}::TestSvc.test_get"

    node = g.nodes[tid]
    assert isinstance(node, TestNode) and node.type == NodeType.TEST
    assert (node.is_async, node.class_name, node.name) == (True, "TestSvc", "test_get")

    assert isinstance(g.nodes[f"{T}::helper"], FunctionNode)
    assert isinstance(g.nodes[f"{T}::TestSvc"], ClassNode)
    assert isinstance(g.nodes["app/svc.py::get"], FunctionNode)

    assert (tid, "app/svc.py::get") in edge_pairs(g, EdgeType.CALLS)
    assert (tid, T) in edge_pairs(g, EdgeType.DEFINED_IN)
    for e in g.edges:
        assert e.src in g.nodes and e.dst in g.nodes, e


def test_promotion_is_repeatable_and_removal_clears_tests():
    builder = build(FILES)
    first = builder.build()
    again = builder.build()
    assert again.edges == first.edges
    assert {n.id for n in again.nodes.values() if isinstance(n, TestNode)} == {
        f"{T}::TestSvc.test_get"
    }

    builder.remove(T)
    g = builder.build()
    assert not [n for n in g.nodes.values() if isinstance(n, TestNode)]


def test_testnode_is_not_collected_by_pytest():
    assert TestNode.__test__ is False
