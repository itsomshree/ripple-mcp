import textwrap

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.calls import CallResolver
from ripple.graph.resolver import ModuleResolver
from ripple.graph.schema import EdgeType
from ripple.graph.schema import UnresolvedReason as R
from ripple.parsers.fastapi_parser import parse_fastapi_source
from ripple.parsers.python_parser import parse_python_source


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def build(files: dict[str, str]) -> CodeGraph:
    builder = GraphBuilder()
    for path, text in files.items():
        src = src_of(text)
        builder.add(parse_python_source(src, path), parse_fastapi_source(src, path))
    return builder.build()


def calls_from(g: CodeGraph, src: str) -> set[str]:
    return {e.dst for e in g.edges_of(EdgeType.CALLS) if e.src == src}


def depends(g: CodeGraph) -> set[tuple[str, str]]:
    return {(e.src, e.dst) for e in g.edges_of(EdgeType.DEPENDS_ON)}


def unresolved(g: CodeGraph, kind: str = "call") -> dict[tuple[str, str], R]:
    return {(u.caller_id, u.target): u.reason for u in g.unresolved if u.kind == kind}


LIMITER = """
    class Limiter:
        def check(self): ...

    limiter = Limiter()
    """


# resolving through an instance
def test_module_instance_method_call():
    g = build(
        {
            "app.py": LIMITER
            + """
    def f():
        limiter.check()
    """
        }
    )
    assert calls_from(g, "app.py::f") == {"app.py::Limiter.check"}
    assert unresolved(g) == {}


def test_function_local_instance_method_call():
    g = build(
        {
            "app.py": """
            class Builder:
                def add(self): ...
                def build(self): ...

            def f():
                b = Builder()
                b.add()
                b.build()
            """
        }
    )
    assert calls_from(g, "app.py::f") == {
        "app.py::Builder",
        "app.py::Builder.add",
        "app.py::Builder.build",
    }


def test_function_local_instance_with_a_qualified_constructor():
    g = build(
        {
            "m.py": "class Builder:\n    def add(self): ...\n",
            "a.py": "import m\ndef f():\n    b = m.Builder()\n    b.add()\n",
        }
    )
    assert "m.py::Builder.add" in calls_from(g, "a.py::f")


def test_instance_imported_from_another_file():
    g = build(
        {
            "app/__init__.py": "",
            "app/lim.py": LIMITER,
            "a.py": "from app.lim import limiter\ndef go():\n    limiter.check()\n",
            "b.py": "import app.lim\ndef go():\n    app.lim.limiter.check()\n",
            "c.py": "from app import lim\ndef go():\n    lim.limiter.check()\n",
            "d.py": "from app.lim import limiter as lm\ndef go():\n    lm.check()\n",
        }
    )
    for x in "abcd":
        assert calls_from(g, f"{x}.py::go") == {"app/lim.py::Limiter.check"}


def test_instance_reexported_through_a_package_init():
    g = build(
        {
            "pkg/__init__.py": "from .lim import limiter\n",
            "pkg/lim.py": LIMITER,
            "x.py": "from pkg import limiter\ndef go():\n    limiter.check()\n",
        }
    )
    assert calls_from(g, "x.py::go") == {"pkg/lim.py::Limiter.check"}


def test_methods_inherited_from_in_repo_bases():
    g = build(
        {
            "base.py": "class Base:\n    def save(self): ...\n",
            "svc.py": """
                from base import Base

                class S(Base): ...

                s = S()

                def go():
                    s.save()
                """,
        }
    )
    assert calls_from(g, "svc.py::go") == {"base.py::Base.save"}


def test_conditional_module_instance_resolves():
    g = build(
        {
            "app.py": """
            class T:
                def go(self): ...

            if DEBUG:
                t = T()

            def f():
                t.go()
            """
        }
    )
    assert calls_from(g, "app.py::f") == {"app.py::T.go"}


def test_closures_and_inner_shadowing():
    g = build(
        {
            "app.py": """
            class A:
                def run(self): ...

            def outer():
                a = A()

                def inner():
                    a.run()

                def other(a):
                    a.run()
            """
        }
    )
    assert calls_from(g, "app.py::outer.inner") == {"app.py::A.run"}
    assert calls_from(g, "app.py::outer.other") == set()


# calling an instance
def test_calling_an_instance_targets_dunder_call_only():
    g = build(
        {
            "app.py": """
            class Limiter:
                def __init__(self, n): ...
                def __call__(self): ...

            limiter = Limiter(60)

            def f():
                limiter()
            """
        }
    )
    assert calls_from(g, "app.py::f") == {"app.py::Limiter.__call__"}


def test_depends_on_an_instance_links_its_dunder_call():
    g = build(
        {
            "app/__init__.py": "",
            "app/limits.py": """
                class RateLimiter:
                    def __init__(self, limit): ...
                    def __call__(self): ...

                limiter = RateLimiter(60)
                """,
            "app/api.py": """
                from fastapi import APIRouter, Depends
                from app.limits import limiter

                router = APIRouter()

                @router.get("/x", dependencies=[Depends(limiter)])
                def a(): ...

                def b(x=Depends(limiter)): ...
                """,
        }
    )
    call = "app/limits.py::RateLimiter.__call__"
    assert depends(g) == {("app/api.py::a", call), ("app/api.py::b", call)}
    assert unresolved(g, "depends") == {}


# what is not resolved, and why
def test_external_instance_is_reported_external():
    g = build(
        {
            "app.py": """
            from fastapi import Depends
            from fastapi.security import HTTPBearer

            bearer = HTTPBearer()

            def h(c=Depends(bearer)): ...

            def g():
                bearer.foo()
            """
        }
    )
    assert depends(g) == set()
    assert unresolved(g, "depends") == {("app.py::h", "bearer"): R.EXTERNAL}
    assert unresolved(g)[("app.py::g", "bearer.foo")] == R.EXTERNAL


def test_inherited_external_members_are_external():
    g = build(
        {
            "app.py": """
            from pydantic import BaseModel

            class S(BaseModel): ...

            s = S()

            def f():
                s.model_dump()
            """
        }
    )
    assert unresolved(g)[("app.py::f", "s.model_dump")] == R.EXTERNAL


def test_factory_result_is_not_resolved():
    g = build(
        {
            "app.py": """
            class C:
                def run(self): ...

            def make():
                return C()

            x = make()

            def f():
                x.run()
                x()
            """
        }
    )
    assert calls_from(g, "app.py::f") == set()
    assert unresolved(g)[("app.py::f", "x.run")] == R.UNKNOWN_RECEIVER
    assert unresolved(g)[("app.py::f", "x")] == R.UNKNOWN_RECEIVER


def test_missing_members_on_an_instance():
    g = build(
        {
            "app.py": """
            class P:
                def run(self): ...

            p = P()

            def f():
                p()
                p.nope()
                p.attr.run()
            """
        }
    )
    u = unresolved(g)
    assert u[("app.py::f", "p")] == R.NOT_FOUND
    assert u[("app.py::f", "p.nope")] == R.NOT_FOUND
    assert u[("app.py::f", "p.attr.run")] == R.UNKNOWN_RECEIVER


def test_reassigned_names_are_not_instances():
    g = build(
        {
            "app.py": """
            class A:
                def run(self): ...

            class B:
                def run(self): ...

            x = A()
            x = B()

            def f():
                x.run()

            def g():
                y = A()
                y = B()
                y.run()
            """
        }
    )
    assert calls_from(g, "app.py::f") == set()
    assert calls_from(g, "app.py::g") == {"app.py::A", "app.py::B"}
    assert unresolved(g)[("app.py::f", "x.run")] == R.UNKNOWN_RECEIVER
    assert unresolved(g)[("app.py::g", "y.run")] == R.UNKNOWN_RECEIVER


def test_instance_cycles_terminate():
    g = build(
        {
            "app.py": """
            a = b()
            b = a()

            def f():
                a.x()
            """
        }
    )
    assert unresolved(g)[("app.py::f", "a.x")] == R.UNKNOWN_RECEIVER


# shadowing
def test_a_parameter_shadows_a_module_level_instance():
    g = build(
        {
            "app.py": LIMITER
            + """
    def f(limiter):
        limiter.check()

    def g():
        limiter.check()
    """
        }
    )
    assert calls_from(g, "app.py::f") == set()
    assert calls_from(g, "app.py::g") == {"app.py::Limiter.check"}
    assert unresolved(g)[("app.py::f", "limiter.check")] == R.UNKNOWN_RECEIVER


def test_a_parameter_shadows_a_module_level_function():
    g = build(
        {
            "app.py": """
            def helper(): ...

            def f(helper):
                helper()

            def g():
                helper()
            """
        }
    )
    assert calls_from(g, "app.py::f") == set()
    assert calls_from(g, "app.py::g") == {"app.py::helper"}
    assert unresolved(g)[("app.py::f", "helper")] == R.UNKNOWN_RECEIVER


def test_other_local_bindings_shadow_too():
    g = build(
        {
            "app.py": """
            def helper(): ...

            def f(items):
                for helper in items:
                    helper()
            """
        }
    )
    assert calls_from(g, "app.py::f") == set()
    assert unresolved(g)[("app.py::f", "helper")] == R.UNKNOWN_RECEIVER


def test_function_local_imports_do_not_shadow():
    g = build({"app.py": "def f():\n    import json\n    json.dumps(1)\n"})
    assert unresolved(g) == {("app.py::f", "json.dumps"): R.EXTERNAL}


# ORM lookups share the same head resolution
def test_class_of_ignores_instances_and_respects_shadowing():
    parsed = {
        "m.py": parse_python_source("class User: ...\nu = User()\n", "m.py"),
        "a.py": parse_python_source(
            "from m import User, u\ndef f(User): ...\ndef g(): ...\n", "a.py"
        ),
    }
    cr = CallResolver(parsed, ModuleResolver(parsed))
    assert cr.class_of("a.py", "User", "g") == ("m.py::User", 1)
    assert cr.class_of("a.py", "User", "f") is None
    assert cr.class_of("a.py", "u", "g") is None
