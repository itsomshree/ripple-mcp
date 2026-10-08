import textwrap
from pathlib import Path

import pytest

from ripple.graph.builder import GraphBuilder
from ripple.graph.schema import EdgeType, UnresolvedReason
from ripple.parsers.fastapi_parser import parse_routes_file, parse_routes_source
from ripple.parsers.python_parser import parse_python_file, parse_python_source

PATH = "app/routes.py"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"


def routes(src: str, path: str = PATH):
    return parse_routes_source(textwrap.dedent(src).lstrip("\n"), path)


def triples(src: str):
    return [(r.method, r.path, r.handler_id.partition("::")[2]) for r in routes(src)]


@pytest.mark.parametrize(
    "verb", ["get", "post", "put", "delete", "patch", "options", "head", "trace"]
)
def test_all_http_methods(verb):
    r = routes(f'@app.{verb}("/x")\ndef h(): ...\n')
    assert [(x.method, x.path, x.receiver) for x in r] == [(verb.upper(), "/x", "app")]


def test_route_fields_and_handler_id():
    (r,) = routes("""
        @router.get("/users/{user_id}")
        async def get_user(user_id: int): ...
        """)
    assert r.handler_id == f"{PATH}::get_user"
    assert (r.receiver, r.method, r.path, r.line) == (
        "router",
        "GET",
        "/users/{user_id}",
        1,
    )


def test_path_keyword_and_extra_kwargs():
    assert triples("""
        @app.post(path="/items", response_model=Item, status_code=201)
        def create(): ...
        """) == [("POST", "/items", "create")]


def test_empty_path_is_valid():
    assert triples('@router.get("")\ndef index(): ...\n') == [("GET", "", "index")]


def test_dotted_receiver():
    (r,) = routes('@api.v1.router.get("/x")\ndef h(): ...\n')
    assert r.receiver == "api.v1.router"


def test_stacked_decorators_emit_one_route_each_with_own_line():
    r = routes("""
        @app.get("/a")
        @app.get("/b")
        @app.post("/c")
        def h(): ...
        """)
    assert [(x.method, x.path, x.line) for x in r] == [
        ("GET", "/a", 1),
        ("GET", "/b", 2),
        ("POST", "/c", 3),
    ]
    assert len({x.handler_id for x in r}) == 1


def test_multiline_decorator_line_is_decorator_start():
    (r,) = routes("""
        x = 1

        @app.get(
            "/x",
            tags=["t"],
        )
        def h(): ...
        """)
    assert r.line == 3


def test_non_route_decorators_are_ignored():
    assert routes("""
        @property
        def a(self): ...

        @app.middleware("http")
        async def b(request, call_next): ...

        @app.on_event("startup")
        def c(): ...

        @app.get
        def d(): ...

        @cache.clear("/x")
        def e(): ...

        @handlers["x"].get("/y")
        def f(): ...
        """) == []


def test_non_literal_paths_are_skipped():
    assert routes("""
        PREFIX = "/x"

        @app.get(PREFIX)
        def a(): ...

        @app.get(f"/users/{PREFIX}")
        def b(): ...

        @app.get(PREFIX + "/y")
        def c(): ...

        @app.get()
        def d(): ...
        """) == []


def test_class_methods_are_skipped_but_nested_functions_are_not():
    assert triples("""
        class Views:
            @router.get("/m")
            def method(self): ...

        def factory():
            @app.get("/inner")
            def inner(): ...
        """) == [("GET", "/inner", "factory.inner")]


def test_function_inside_method_is_a_route():
    assert triples("""
        class C:
            def m(self):
                @app.get("/x")
                def h(): ...
        """) == [("GET", "/x", "C.m.h")]


def test_syntax_error_yields_no_routes():
    assert routes("@app.get('/x')\ndef broken(:\n") == []


def test_handler_ids_match_python_parser_nodes():
    src = textwrap.dedent("""
        @app.get("/a")
        def top(): ...

        class C:
            def m(self):
                @app.get("/b")
                async def inner(): ...

        def factory():
            @router.delete("/c")
            def deep(): ...
        """).lstrip("\n")
    node_ids = {n.id for n in parse_python_source(src, PATH).nodes}
    found = parse_routes_source(src, PATH)
    assert len(found) == 3
    assert {r.handler_id for r in found} <= node_ids


def fixture_files():
    return sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix())


def test_sample_fixture_routes():
    found = []
    for p in fixture_files():
        found += parse_routes_file(p, p.relative_to(FIXTURE).as_posix())
    assert [(r.method, r.receiver, r.path, r.handler_id) for r in found] == [
        ("GET", "app", "/health", "app/main.py::health"),
        ("GET", "router", "/{order_id}", "app/routers/orders.py::get_order"),
        ("POST", "router", "", "app/routers/orders.py::place_order"),
        ("GET", "router", "", "app/routers/users.py::list_users"),
        ("GET", "router", "/{user_id}", "app/routers/users.py::get_user"),
        ("POST", "router", "", "app/routers/users.py::create_user"),
        ("DELETE", "router", "/{user_id}", "app/routers/users.py::delete_user"),
    ]


def test_sample_fixture_handlers_reach_services_in_call_graph():
    builder = GraphBuilder()
    for p in fixture_files():
        builder.add(parse_python_file(p, p.relative_to(FIXTURE).as_posix()))
    g = builder.build()
    calls = {(e.src, e.dst) for e in g.edges_of(EdgeType.CALLS)}
    users = "app/services/user_service.py::UserService"
    assert ("app/routers/users.py::get_user", f"{users}.get") in calls
    assert ("app/routers/users.py::delete_user", f"{users}.delete") in calls
    assert (
        "app/services/order_service.py::OrderService.place",
        f"{users}.get",
    ) in calls
    assert (
        "app/routers/orders.py::place_order",
        "app/services/order_service.py::OrderService.place",
    ) in calls


def test_sample_fixture_has_no_dangling_internal_calls():
    builder = GraphBuilder()
    for p in fixture_files():
        builder.add(parse_python_file(p, p.relative_to(FIXTURE).as_posix()))
    g = builder.build()
    missing = [
        (u.caller_id, u.target)
        for u in g.unresolved
        if u.reason == UnresolvedReason.NOT_FOUND
    ]
    assert missing == []
    calls = {(e.src, e.dst) for e in g.edges_of(EdgeType.CALLS)}
    assert (
        "app/routers/users.py::create_user",
        "app/services/user_service.py::UserService.create",
    ) in calls
