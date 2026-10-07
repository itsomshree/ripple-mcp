import textwrap

import pytest

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.resolver import Binding, ModuleIndex, ModuleResolver
from ripple.graph.schema import EdgeType, FileNode
from ripple.graph.schema import UnresolvedReason as R
from ripple.parsers.python_parser import parse_python_source


def parsed(files: dict[str, str]):
    return {
        path: parse_python_source(textwrap.dedent(src).lstrip("\n"), path)
        for path, src in files.items()
    }


def build(files: dict[str, str]) -> CodeGraph:
    builder = GraphBuilder()
    for pf in parsed(files).values():
        builder.add(pf)
    return builder.build()


def resolver(files: dict[str, str]) -> ModuleResolver:
    return ModuleResolver(parsed(files))


def imports(graph: CodeGraph) -> set[tuple[str, str]]:
    return {(e.src, e.dst) for e in graph.edges_of(EdgeType.IMPORTS)}


INDEX_PATHS = [
    "src/ripple/__init__.py",
    "src/ripple/graph/__init__.py",
    "src/ripple/graph/builder.py",
    "scripts/run.py",
]


def test_module_names_and_packages():
    idx = ModuleIndex(INDEX_PATHS)
    assert idx.module_of("src/ripple/graph/builder.py") == "ripple.graph.builder"
    assert idx.module_of("src/ripple/graph/__init__.py") == "ripple.graph"
    assert idx.package_of("src/ripple/graph/__init__.py") == "ripple.graph"
    assert idx.package_of("src/ripple/graph/builder.py") == "ripple.graph"
    assert idx.resolve("ripple.graph") == "src/ripple/graph/__init__.py"
    assert idx.resolve("ripple.nope") is None
    assert idx.has_package("ripple") and not idx.has_package("os")


@pytest.mark.parametrize(
    "module, level, expected",
    [
        ("os.path", 0, "os.path"),
        ("schema", 1, "ripple.graph.schema"),
        ("", 1, "ripple.graph"),
        ("utils.logging", 2, "ripple.utils.logging"),
        ("x", 3, None),
    ],
)
def test_absolute_resolution(module, level, expected):
    idx = ModuleIndex(INDEX_PATHS)
    assert idx.absolute("src/ripple/graph/builder.py", module, level) == expected


def test_ambiguous_module_name_resolves_to_none():
    idx = ModuleIndex(["a/x.py", "src/a/x.py"])
    assert idx.resolve("a.x") is None


# pass 1: ingest
def test_ingest_nodes_and_defined_in_with_no_dangling_edges():
    g = build({"app/a.py": "def f(): pass\n", "app/b.py": "class C: pass\n"})
    assert {"app/a.py", "app/a.py::f", "app/b.py", "app/b.py::C"} <= set(g.nodes)
    for e in g.edges:
        assert e.src in g.nodes and e.dst in g.nodes
    assert len(g.edges_of(EdgeType.DEFINED_IN)) == 2


def test_syntax_error_file_contributes_only_its_file_node():
    g = build({"app/bad.py": "def broken(:\n", "app/ok.py": "import app.bad\n"})
    assert isinstance(g.nodes["app/bad.py"], FileNode)
    assert not [n for n in g.nodes if n.startswith("app/bad.py::")]
    assert imports(g) == {("app/ok.py", "app/bad.py")}


# pass 2: IMPORTS edges
def test_absolute_imports_forms():
    g = build(
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": "def f(): pass\n",
            "a.py": "import pkg.mod\n",
            "b.py": "from pkg import mod\n",
            "c.py": "from pkg.mod import f\n",
        }
    )
    assert imports(g) == {
        ("a.py", "pkg/mod.py"),
        ("b.py", "pkg/__init__.py"),
        ("b.py", "pkg/mod.py"),
        ("c.py", "pkg/mod.py"),
    }


def test_src_layout():
    g = build(
        {
            "src/ripple/__init__.py": "",
            "src/ripple/x.py": "def f(): pass\n",
            "tests/test_x.py": "from ripple.x import f\n",
        }
    )
    assert imports(g) == {("tests/test_x.py", "src/ripple/x.py")}


def test_relative_imports():
    g = build(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from . import sibling\nfrom .sibling import f\n",
            "pkg/sibling.py": "def f(): pass\n",
            "pkg/core.py": "def util(): pass\n",
            "pkg/sub/m.py": "from ..core import util\n",
        }
    )
    assert imports(g) == {
        ("pkg/a.py", "pkg/__init__.py"),
        ("pkg/a.py", "pkg/sibling.py"),
        ("pkg/sub/m.py", "pkg/core.py"),
    }


def test_relative_import_inside_init_refers_to_own_package():
    g = build(
        {
            "pkg/__init__.py": "from .mod import X\n",
            "pkg/mod.py": "class X: pass\n",
        }
    )
    assert imports(g) == {("pkg/__init__.py", "pkg/mod.py")}


def test_external_and_out_of_range_imports_are_ignored():
    g = build({"a.py": "import os\nfrom fastapi import APIRouter\nfrom ... import x\n"})
    assert imports(g) == set()


def test_import_cycle_terminates():
    g = build({"a.py": "import b\n", "b.py": "import a\n"})
    assert imports(g) == {("a.py", "b.py"), ("b.py", "a.py")}


def test_reexport_edge_goes_through_init_but_binding_points_at_definition():
    files = {
        "pkg/__init__.py": "from .service import Service\n",
        "pkg/service.py": "class Service: pass\n",
        "app/main.py": "from pkg import Service\n",
    }
    assert imports(build(files)) == {
        ("app/main.py", "pkg/__init__.py"),
        ("pkg/__init__.py", "pkg/service.py"),
    }
    b = resolver(files).imports("app/main.py").bindings["Service"]
    assert b == Binding("symbol", "pkg.service", "pkg/service.py", "Service")


def test_reexport_cycle_does_not_loop():
    r = resolver(
        {
            "pkg/__init__.py": "from .a import X\n",
            "pkg/a.py": "from pkg import X\n",
        }
    )
    assert r.imports("pkg/a.py").bindings["X"].symbol is None


def test_star_import_binds_public_symbols_only():
    r = resolver(
        {
            "lib.py": "def public(): pass\ndef _private(): pass\n",
            "app.py": "from lib import *\n",
        }
    )
    fi = r.imports("app.py")
    assert set(fi.bindings) == {"public"}
    assert fi.depends_on == {"lib.py"}


def test_binding_kinds_for_aliases_and_externals():
    r = resolver(
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": "def f(): pass\n",
            "app.py": """
            import pkg.mod as pm
            import os.path
            from pkg.mod import f as g
            from fastapi import APIRouter
        """,
        }
    )
    b = r.imports("app.py").bindings
    assert b["pm"] == Binding("module", "pkg.mod", "pkg/mod.py")
    assert b["os"] == Binding("module", "os", None)
    assert b["g"] == Binding("symbol", "pkg.mod", "pkg/mod.py", "f")
    assert b["APIRouter"] == Binding("symbol", "fastapi", None, "APIRouter")


def test_ambiguous_module_creates_no_edge():
    g = build({"a/x.py": "", "src/a/x.py": "", "m.py": "import a.x\n"})
    assert imports(g) == set()


# determinism / incremental semantics
def test_build_is_deterministic_and_replace_updates_graph():
    builder = GraphBuilder()
    for pf in parsed({"a.py": "import b\n", "b.py": "", "c.py": ""}).values():
        builder.add(pf)
    first = builder.build()
    assert builder.build().edges == first.edges

    builder.add(parse_python_source("import c\n", "a.py"))
    assert imports(builder.build()) == {("a.py", "c.py")}

    builder.remove("c.py")
    assert imports(builder.build()) == set()


# pass 3: CALLS
def calls_of(g: CodeGraph) -> set[tuple[str, str]]:
    return {(e.src, e.dst) for e in g.edges_of(EdgeType.CALLS)}


def unresolved(g: CodeGraph) -> dict[tuple[str, str], R]:
    return {(u.caller_id, u.target): u.reason for u in g.unresolved}


def assert_no_dangling(g: CodeGraph) -> None:
    for e in g.edges:
        assert e.src in g.nodes and e.dst in g.nodes, e


def test_same_file_and_nested_calls():
    g = build(
        {
            "app.py": """
        def helper(): pass
        def run():
            helper()
        def outer():
            def a():
                b()
            def b(): pass
            a()
        """
        }
    )
    assert calls_of(g) == {
        ("app.py::run", "app.py::helper"),
        ("app.py::outer.a", "app.py::outer.b"),
        ("app.py::outer", "app.py::outer.a"),
    }
    assert_no_dangling(g)


def test_methods_do_not_see_sibling_methods_by_bare_name():
    g = build(
        {
            "app.py": """
        class C:
            def a(self):
                b()
            def b(self): pass
        """
        }
    )
    assert calls_of(g) == set()
    assert unresolved(g) == {("app.py::C.a", "b"): R.NOT_FOUND}


def test_module_level_call_has_file_as_caller():
    g = build({"app.py": "def make(): pass\napp = make()\n"})
    assert calls_of(g) == {("app.py", "app.py::make")}


def test_duplicate_call_sites_collapse_to_one_edge():
    g = build({"app.py": "def a(): pass\ndef b():\n    a()\n    a()\n"})
    assert calls_of(g) == {("app.py::b", "app.py::a")}


def test_cross_file_call_forms():
    g = build(
        {
            "app/a.py": "def helper(): pass\n",
            "b.py": "from app.a import helper\ndef go():\n    helper()\n",
            "c.py": "import app.a\ndef go():\n    app.a.helper()\n",
            "d.py": "import app.a as m\ndef go():\n    m.helper()\n",
            "e.py": "from app import a\ndef go():\n    a.helper()\n",
            "f.py": "from app.a import *\ndef go():\n    helper()\n",
        }
    )
    assert calls_of(g) == {(f"{x}.py::go", "app/a.py::helper") for x in "bcdef"}
    assert_no_dangling(g)


def test_src_layout_test_calls_source():
    g = build(
        {
            "src/ripple/__init__.py": "",
            "src/ripple/x.py": "def f(): pass\n",
            "tests/test_x.py": "from ripple.x import f\ndef test_f():\n    f()\n",
        }
    )
    assert calls_of(g) == {("tests/test_x.py::test_f", "src/ripple/x.py::f")}


def test_class_attribute_call_through_reexport():
    g = build(
        {
            "pkg/__init__.py": "from .service import Service\n",
            "pkg/service.py": "class Service:\n    def create(self): pass\n",
            "app/main.py": "from pkg import Service\ndef f():\n    Service.create(None)\n",
        }
    )
    assert calls_of(g) == {("app/main.py::f", "pkg/service.py::Service.create")}


def test_self_calls_and_inheritance_across_files():
    g = build(
        {
            "base.py": "class Base:\n    def save(self): pass\n",
            "svc.py": """
            from base import Base
            class S(Base):
                def run(self):
                    self.save()
                    self.helper()
                def helper(self): pass
            """,
        }
    )
    assert calls_of(g) == {
        ("svc.py::S.run", "base.py::Base.save"),
        ("svc.py::S.run", "svc.py::S.helper"),
    }


def test_missing_self_method_external_vs_not_found():
    g = build(
        {
            "app.py": """
        from pydantic import BaseModel
        class M(BaseModel):
            def f(self):
                self.model_dump()
        class P:
            def f(self):
                self.missing()
        """
        }
    )
    assert unresolved(g) == {
        ("app.py::M.f", "self.model_dump"): R.EXTERNAL,
        ("app.py::P.f", "self.missing"): R.NOT_FOUND,
    }


def test_instantiation_targets_class_and_inherited_init():
    g = build(
        {
            "app.py": """
        class A:
            def __init__(self): pass
        class B:
            x = 1
        class C(A): pass
        def f():
            A()
            B()
            C()
        """
        }
    )
    assert calls_of(g) == {
        ("app.py::f", "app.py::A"),
        ("app.py::f", "app.py::A.__init__"),
        ("app.py::f", "app.py::B"),
        ("app.py::f", "app.py::C"),
    }


def test_base_cycle_terminates():
    g = build(
        {
            "app.py": """
        class A(B):
            def f(self):
                self.x()
        class B(A): pass
        """
        }
    )
    assert unresolved(g) == {("app.py::A.f", "self.x"): R.NOT_FOUND}


def test_unresolved_reasons():
    g = build(
        {
            "app.py": """
        import os
        class S:
            def run(self, svc):
                os.getcwd()
                print("x")
                svc.create()
                self.repo.save()
                missing()
        """
        }
    )
    caller = "app.py::S.run"
    assert calls_of(g) == set()
    assert unresolved(g) == {
        (caller, "os.getcwd"): R.EXTERNAL,
        (caller, "print"): R.BUILTIN,
        (caller, "svc.create"): R.UNKNOWN_RECEIVER,
        (caller, "self.repo.save"): R.UNKNOWN_RECEIVER,
        (caller, "missing"): R.NOT_FOUND,
    }
    assert dict(g.unresolved_by_reason()) == {
        R.EXTERNAL: 1,
        R.BUILTIN: 1,
        R.UNKNOWN_RECEIVER: 2,
        R.NOT_FOUND: 1,
    }


def test_ambiguous_module_call():
    g = build(
        {
            "a/x.py": "def go(): pass\n",
            "src/a/x.py": "def go(): pass\n",
            "m.py": "import a.x\ndef f():\n    a.x.go()\n",
        }
    )
    assert calls_of(g) == set()
    assert unresolved(g) == {("m.py::f", "a.x.go"): R.AMBIGUOUS}
