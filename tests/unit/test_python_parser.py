import hashlib
import textwrap

import pytest

from ripple.graph.schema import ClassNode, EdgeType, FileNode, FunctionNode, SymbolNode
from ripple.parsers.python_parser import parse_python_file, parse_python_source

PATH = "app/mod.py"


def parse(src: str, path: str = PATH):
    return parse_python_source(textwrap.dedent(src).lstrip("\n"), path)


def symbols(result) -> dict[str, SymbolNode]:
    return {n.id: n for n in result.nodes if isinstance(n, SymbolNode)}


def func(result, qualname: str, path: str = PATH) -> FunctionNode:
    node = symbols(result)[f"{path}::{qualname}"]
    assert isinstance(node, FunctionNode)
    return node


def cls(result, qualname: str, path: str = PATH) -> ClassNode:
    node = symbols(result)[f"{path}::{qualname}"]
    assert isinstance(node, ClassNode)
    return node


def calls(result):
    return {(c.caller_id, c.target) for c in result.calls}


def file_node(result):
    return next(n for n in result.nodes if isinstance(n, FileNode))


# file node
def test_file_node_hash_and_path_normalization():
    r = parse_python_source("x = 1\n", "app\\mod.py")
    f = file_node(r)
    assert f.id == f.path == "app/mod.py"
    assert f.name == "mod.py"
    assert len(f.content_hash) == 64
    assert r.error is None


def test_empty_source():
    r = parse("")
    assert [n.id for n in r.nodes] == [PATH]
    assert r.calls == [] and r.imports == [] and r.error is None


@pytest.mark.parametrize(
    "path, expected",
    [
        ("tests/unit/test_x.py", True),
        ("app/test_users.py", True),
        ("app/users_test.py", True),
        ("conftest.py", True),
        ("app/users.py", False),
        ("app/tests_helper.py", False),
        ("app/contest.py", False),
    ],
)
def test_is_test_detection(path, expected):
    assert file_node(parse_python_source("", path)).is_test is expected


# definitions
def test_class_methods_and_defined_in_edges():
    r = parse("""
        class Service:
            def create(self):
                pass

            async def fetch(self):
                pass

        def top():
            pass
        """)
    s = symbols(r)
    assert set(s) == {
        f"{PATH}::Service",
        f"{PATH}::Service.create",
        f"{PATH}::Service.fetch",
        f"{PATH}::top",
    }
    assert func(r, "Service.create").class_name == "Service"
    assert func(r, "top").class_name is None
    assert func(r, "Service.fetch").is_async is True
    assert func(r, "Service.create").is_async is False

    defined_in = {(e.src, e.dst) for e in r.edges if e.type == EdgeType.DEFINED_IN}
    assert defined_in == {(sid, PATH) for sid in s}


def test_nested_function_qualname_and_call_attribution():
    r = parse("""
        def outer():
            def inner():
                return load()
            return inner()
        """)
    assert f"{PATH}::outer.inner" in symbols(r)
    assert calls(r) == {
        (f"{PATH}::outer.inner", "load"),
        (f"{PATH}::outer", "inner"),
    }


def test_span_starts_at_first_decorator():
    r = parse("""
        @decorator
        class Service:
            @staticmethod
            def run():
                pass
        """)
    s = symbols(r)
    assert (s[f"{PATH}::Service"].line_start, s[f"{PATH}::Service"].line_end) == (1, 5)
    assert (
        s[f"{PATH}::Service.run"].line_start,
        s[f"{PATH}::Service.run"].line_end,
    ) == (3, 5)


def test_decorators_and_bases_stored_raw():
    r = parse("""
        @router.get("/users/{id}")
        def get_user(id): ...

        class User(Base, mixins.Audit):
            pass
        """)
    assert func(r, "get_user").decorators == ["router.get('/users/{id}')"]
    assert cls(r, "User").bases == ["Base", "mixins.Audit"]


def test_duplicate_definitions_first_wins():
    r = parse("""
        class C:
            @property
            def x(self): return 1

            @x.setter
            def x(self, v): pass
        """)
    xs = [n for n in r.nodes if n.id == f"{PATH}::C.x"]
    assert len(xs) == 1
    assert func(r, "C.x").line_start == 2
    assert sum(1 for e in r.edges if e.src == f"{PATH}::C.x") == 1


# calls
def test_decorator_and_default_calls_belong_to_enclosing_scope():
    r = parse("""
        @register("x")
        def f(a=make_default()):
            inner_call()
        """)
    assert calls(r) == {
        (PATH, "register"),
        (PATH, "make_default"),
        (f"{PATH}::f", "inner_call"),
    }


def test_module_and_class_body_calls_belong_to_file():
    r = parse("""
        CONFIG = load_config()

        class M:
            col = Column(Integer)
        """)
    assert calls(r) == {(PATH, "load_config"), (PATH, "Column")}


def test_dotted_targets_and_unresolvable_callees():
    r = parse("""
        def f():
            self.repo.save(x)
            handlers[k]()
            make().run()
        """)
    # handlers[k]() is skipped; make().run() yields only the inner `make`
    assert calls(r) == {(f"{PATH}::f", "self.repo.save"), (f"{PATH}::f", "make")}


# imports
def test_imports_normalized():
    r = parse("""
        from __future__ import annotations
        import os, a.b.c as abc
        from pkg.mod import x, y as z
        from . import sibling
        from ..core import util
        from lib import *

        def f():
            import json
        """)
    got = [(i.level, i.module, i.names, i.alias) for i in r.imports]
    assert got == [
        (0, "os", [], None),
        (0, "a.b.c", [], "abc"),
        (0, "pkg.mod", [("x", None), ("y", "z")], None),
        (1, "", [("sibling", None)], None),
        (2, "core", [("util", None)], None),
        (0, "lib", [("*", None)], None),
        (0, "json", [], None),
    ]


# robustness
def test_syntax_error_is_reported_not_raised():
    r = parse("def broken(:\n    pass\n")
    assert r.error is not None and r.error.startswith("SyntaxError")
    assert [type(n) for n in r.nodes] == [FileNode]


def test_null_bytes_do_not_crash():
    assert parse_python_source("x = 1\0", PATH).error is not None


def test_parse_file_handles_non_utf8_and_hashes_raw_bytes(tmp_path):
    raw = b"# caf\xe9\ndef f():\n    pass\n"
    p = tmp_path / "bad_encoding.py"
    p.write_bytes(raw)
    r = parse_python_file(p, "bad_encoding.py")
    assert r.error is None
    assert "bad_encoding.py::f" in symbols(r)
    assert file_node(r).content_hash == hashlib.sha256(raw).hexdigest()
