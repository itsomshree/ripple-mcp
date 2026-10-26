import textwrap

from ripple.parsers.fastapi_parser import parse_depends_source, parse_fastapi_source
from ripple.parsers.python_parser import parse_python_source

PATH = "app/deps.py"


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def deps(text: str):
    return parse_depends_source(src_of(text), PATH)


def triples(text: str):
    return [(d.owner_id.partition("::")[2], d.ctor, d.target) for d in deps(text)]


def test_parameter_default_fields():
    (d,) = deps("def h(db=Depends(get_db)): ...\n")
    assert d.owner_id == f"{PATH}::h"
    assert (d.ctor, d.target, d.line) == ("Depends", "get_db", 1)


def test_positional_and_keyword_only_defaults():
    assert triples("def h(a=Depends(x), *, b=Depends(y)): ...\n") == [
        ("h", "Depends", "x"),
        ("h", "Depends", "y"),
    ]


def test_async_function():
    assert triples("async def h(db=Depends(get_db)): ...\n") == [
        ("h", "Depends", "get_db")
    ]


def test_annotated_metadata():
    assert triples("""
        def h(
            db: Annotated[Session, Depends(get_db)],
            user: Annotated[User, Depends(current_user), Other()],
            z: typing.Annotated[int, Depends(f)],
        ): ...
        """) == [
        ("h", "Depends", "get_db"),
        ("h", "Depends", "current_user"),
        ("h", "Depends", "f"),
    ]


def test_security_and_dependency_keyword():
    assert triples("""
        def h(u=Security(get_user, scopes=["a"]), d=Depends(dependency=get_db)): ...
        """) == [("h", "Security", "get_user"), ("h", "Depends", "get_db")]


def test_dotted_targets_and_ctors():
    assert triples("""
        def h(a=Depends(deps.get_db), b=fastapi.Depends(auth.check)): ...
        """) == [
        ("h", "Depends", "deps.get_db"),
        ("h", "fastapi.Depends", "auth.check"),
    ]


def test_aliased_fastapi_imports_are_recognised():
    assert triples("""
        from fastapi import Depends as D
        from fastapi.params import Depends as P

        def h(a=D(f), b=P(g)): ...
        """) == [("h", "D", "f"), ("h", "P", "g")]


def test_non_fastapi_alias_and_unknown_names_are_ignored():
    assert (
        triples("""
        from other import Depends as D

        def a(x=D(f)): ...
        def b(x=E(f)): ...
        """)
        == []
    )


def test_local_depends_lookalike_is_still_emitted():
    assert triples("""
        class Depends: ...

        def h(a=Depends(f)): ...
        """) == [("h", "Depends", "f")]


def test_route_decorator_dependencies_list_and_tuple():
    assert triples("""
        @router.get("/x", dependencies=[Depends(auth), Depends(rate_limit)])
        def a(): ...

        @app.post("/y", dependencies=(Depends(audit),))
        def b(): ...
        """) == [
        ("a", "Depends", "auth"),
        ("a", "Depends", "rate_limit"),
        ("b", "Depends", "audit"),
    ]


def test_stacked_routes_and_multiline_decorator_line():
    found = deps("""
        @app.get("/a", dependencies=[Depends(a)])
        @app.get(
            "/b",
            dependencies=[Depends(b)],
        )
        def h(): ...
        """)
    assert [(d.target, d.line) for d in found] == [("a", 1), ("b", 4)]
    assert len({d.owner_id for d in found}) == 1


def test_non_route_decorators_and_non_literal_dependencies_are_ignored():
    assert (
        triples("""
        @cache.clear("/x", dependencies=[Depends(x)])
        def a(): ...

        @app.get("/y", dependencies=DEPS)
        def b(): ...

        @app.get("/z", dependencies=[make()])
        def c(): ...
        """)
        == []
    )


def test_class_methods_collect_parameters_but_not_decorator_dependencies():
    assert triples("""
        class C:
            def m(self, x=Depends(f)): ...

            @router.get("/n", dependencies=[Depends(g)])
            def n(self): ...
        """) == [("C.m", "Depends", "f")]


def test_nested_function_owner():
    assert triples("""
        def outer():
            def inner(x=Depends(f)): ...
        """) == [("outer.inner", "Depends", "f")]


def test_unsupported_forms_produce_no_facts():
    assert (
        triples("""
        def a(x=Depends()): ...
        def b(x=Depends(lambda: 1)): ...
        def c(x=Depends(make())): ...
        def d(x=Depends(handlers["k"])): ...
        def e(x=Query(DEFAULT)): ...
        def f(x=make_default()): ...
        def g(x: Annotated[int, Query()]): ...
        def h(x: Annotated[int]): ...
        def i(x: List[Depends(f)]): ...
        """)
        == []
    )


def test_calls_outside_signatures_are_ignored():
    assert (
        triples("""
        shared = Depends(f)

        def h():
            x = Depends(g)
        """)
        == []
    )


def test_syntax_error_yields_nothing():
    assert deps("def h(x=Depends(f)): ...\ndef broken(:\n") == []


def test_bundle_matches_parse_depends_source():
    text = "def h(x=Depends(f)): ...\n"
    assert parse_fastapi_source(text, PATH).depends == parse_depends_source(text, PATH)


def test_owner_ids_match_python_parser_nodes():
    text = src_of("""
        def top(a=Depends(f)): ...

        class C:
            def m(self, b=Depends(g)):
                def inner(c=Depends(h)): ...

        @app.get("/x", dependencies=[Depends(k)])
        async def route(): ...
        """)
    node_ids = {n.id for n in parse_python_source(text, PATH).nodes}
    found = parse_depends_source(text, PATH)
    assert len(found) == 4
    assert {d.owner_id for d in found} <= node_ids
