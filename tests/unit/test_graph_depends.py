import textwrap
from pathlib import Path

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import EdgeType
from ripple.graph.schema import UnresolvedReason as R
from ripple.parsers.fastapi_parser import parse_fastapi_file, parse_fastapi_source
from ripple.parsers.python_parser import parse_python_file, parse_python_source

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def build(files: dict[str, str]) -> CodeGraph:
    builder = GraphBuilder()
    for path, text in files.items():
        src = src_of(text)
        builder.add(parse_python_source(src, path), parse_fastapi_source(src, path))
    return builder.build()


def one(text: str) -> CodeGraph:
    return build({"app.py": text})


def depends(g: CodeGraph) -> set[tuple[str, str]]:
    return {(e.src, e.dst) for e in g.edges_of(EdgeType.DEPENDS_ON)}


def dep_unresolved(g: CodeGraph) -> dict[tuple[str, str], R]:
    return {
        (u.caller_id, u.target): u.reason for u in g.unresolved if u.kind == "depends"
    }


def assert_no_dangling(g: CodeGraph) -> None:
    for e in g.edges:
        assert e.src in g.nodes and e.dst in g.nodes, e


def test_same_file_default_dependency():
    g = one("""
        from fastapi import Depends

        def get_db(): ...
        def h(db=Depends(get_db)): ...
        """)
    assert depends(g) == {("app.py::h", "app.py::get_db")}
    assert dep_unresolved(g) == {}
    assert_no_dangling(g)


def test_cross_file_reference_forms():
    g = build(
        {
            "app/__init__.py": "",
            "app/deps.py": "def get_db(): ...\n",
            "a.py": """
                from fastapi import Depends
                from app.deps import get_db
                def h(x=Depends(get_db)): ...
                """,
            "b.py": """
                from fastapi import Depends
                from app import deps
                def h(x=Depends(deps.get_db)): ...
                """,
            "c.py": """
                from fastapi import Depends
                import app.deps as dm
                def h(x=Depends(dm.get_db)): ...
                """,
            "d.py": """
                from fastapi import Depends
                import app.deps
                def h(x=Depends(app.deps.get_db)): ...
                """,
        }
    )
    assert depends(g) == {(f"{x}.py::h", "app/deps.py::get_db") for x in "abcd"}
    assert dep_unresolved(g) == {}
    assert_no_dangling(g)


def test_dependency_reexported_through_package_init():
    g = build(
        {
            "pkg/__init__.py": "from .deps import get_db\n",
            "pkg/deps.py": "def get_db(): ...\n",
            "app.py": """
                from fastapi import Depends
                from pkg import get_db
                def h(x=Depends(get_db)): ...
                """,
        }
    )
    assert depends(g) == {("app.py::h", "pkg/deps.py::get_db")}


def test_default_annotated_and_decorator_forms_all_produce_edges():
    g = one("""
        from typing import Annotated
        from fastapi import APIRouter, Depends, Security

        router = APIRouter()

        def get_db(): ...
        def current(): ...
        def audit(): ...

        @router.get("/x", dependencies=[Depends(audit)])
        def h(db: Annotated[object, Depends(get_db)], u=Security(current)): ...
        """)
    assert depends(g) == {
        ("app.py::h", "app.py::get_db"),
        ("app.py::h", "app.py::current"),
        ("app.py::h", "app.py::audit"),
    }


def test_dependency_chain():
    g = one("""
        from fastapi import Depends

        def get_db(): ...
        def current_user(db=Depends(get_db)): ...
        def h(u=Depends(current_user)): ...
        """)
    assert depends(g) == {
        ("app.py::h", "app.py::current_user"),
        ("app.py::current_user", "app.py::get_db"),
    }


def test_class_dependency_targets_class_and_init():
    g = one("""
        from fastapi import Depends

        class Auth:
            def __init__(self): ...
            def __call__(self): ...

        def h(a=Depends(Auth)): ...
        """)
    assert depends(g) == {
        ("app.py::h", "app.py::Auth"),
        ("app.py::h", "app.py::Auth.__init__"),
    }


def test_aliased_and_qualified_fastapi_names():
    g = one("""
        import fastapi
        from fastapi import Depends as D
        from fastapi.params import Security as S

        def get_db(): ...
        def a(x=fastapi.Depends(get_db)): ...
        def b(x=D(get_db)): ...
        def c(x=S(get_db)): ...
        """)
    assert depends(g) == {(f"app.py::{n}", "app.py::get_db") for n in "abc"}


def test_non_fastapi_depends_lookalikes_make_no_edge_and_no_diagnostic():
    g = build(
        {
            "x.py": """
                class Depends: ...
                def get_db(): ...
                def h(a=Depends(get_db)): ...
                """,
            "y.py": """
                from other import Depends
                def get_db(): ...
                def h(a=Depends(get_db)): ...
                """,
        }
    )
    assert depends(g) == set()
    assert dep_unresolved(g) == {}


def test_unresolved_dependency_reasons():
    g = one("""
        from fastapi import Depends
        from sqlalchemy import get_session

        def h(a=Depends(get_session), b=Depends(missing), c=Depends(helpers.x)): ...
        """)
    assert depends(g) == set()
    assert dep_unresolved(g) == {
        ("app.py::h", "get_session"): R.EXTERNAL,
        ("app.py::h", "missing"): R.NOT_FOUND,
        ("app.py::h", "helpers.x"): R.UNKNOWN_RECEIVER,
    }


def test_nested_function_resolves_in_enclosing_scope():
    g = one("""
        from fastapi import Depends

        def outer():
            def dep(): ...
            def inner(x=Depends(dep)): ...
            return inner

        def top(x=Depends(dep)): ...
        """)
    assert depends(g) == {("app.py::outer.inner", "app.py::outer.dep")}
    assert dep_unresolved(g) == {("app.py::top", "dep"): R.NOT_FOUND}


def test_method_default_resolves_module_level_name():
    g = one("""
        from fastapi import Depends

        def helper(): ...

        class C:
            def m(self, x=Depends(helper)): ...
        """)
    assert depends(g) == {("app.py::C.m", "app.py::helper")}


def test_facts_drive_edges_and_removal_clears_them():
    src = src_of("""
        from fastapi import Depends
        def get_db(): ...
        def h(x=Depends(get_db)): ...
        """)
    builder = GraphBuilder()
    builder.add(parse_python_source(src, "app.py"))  # no facts: no edges
    assert depends(builder.build()) == set()

    builder.add(parse_python_source(src, "app.py"), parse_fastapi_source(src, "app.py"))
    assert depends(builder.build()) == {("app.py::h", "app.py::get_db")}

    builder.remove("app.py")
    assert depends(builder.build()) == set()


def test_sample_fixture_has_no_dependency_edges():
    builder = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        builder.add(parse_python_file(p, rel), parse_fastapi_file(p, rel))
    g = builder.build()
    assert depends(g) == set()
    assert [u for u in g.unresolved if u.kind == "depends"] == []
