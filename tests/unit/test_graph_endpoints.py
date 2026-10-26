import textwrap
from pathlib import Path

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import APIEndpointNode, EdgeType
from ripple.graph.schema import RouteIssueReason as Why
from ripple.parsers.fastapi_parser import parse_fastapi_file, parse_fastapi_source
from ripple.parsers.python_parser import parse_python_file, parse_python_source

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"

HEAD = "        from fastapi import FastAPI, APIRouter\n        app = FastAPI()\n"


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def add(builder: GraphBuilder, path: str, src: str) -> None:
    builder.add(parse_python_source(src, path), parse_fastapi_source(src, path))


def build(files: dict[str, str]) -> CodeGraph:
    builder = GraphBuilder()
    for path, text in files.items():
        add(builder, path, src_of(text))
    return builder.build()


def one(text: str) -> CodeGraph:
    return build({"app.py": text})


def endpoints(g: CodeGraph) -> dict[str, APIEndpointNode]:
    return {n.id: n for n in g.nodes.values() if isinstance(n, APIEndpointNode)}


def handles(g: CodeGraph) -> set[tuple[str, str]]:
    return {(e.src, e.dst) for e in g.edges_of(EdgeType.HANDLES)}


def assert_no_dangling(g: CodeGraph) -> None:
    for e in g.edges:
        assert e.src in g.nodes and e.dst in g.nodes, e


def fixture_builder() -> GraphBuilder:
    builder = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        builder.add(parse_python_file(p, rel), parse_fastapi_file(p, rel))
    return builder


EXPECTED = {
    ("GET", "/health"): "app/main.py::health",
    ("GET", "/api/orders/{order_id}"): "app/routers/orders.py::get_order",
    ("POST", "/api/orders"): "app/routers/orders.py::place_order",
    ("GET", "/users"): "app/routers/users.py::list_users",
    ("GET", "/users/{user_id}"): "app/routers/users.py::get_user",
    ("POST", "/users"): "app/routers/users.py::create_user",
    ("DELETE", "/users/{user_id}"): "app/routers/users.py::delete_user",
}


def test_sample_fixture_endpoints_and_handles():
    builder = fixture_builder()
    g = builder.build()

    assert set(endpoints(g)) == {f"endpoint::{m} {p}" for m, p in EXPECTED}
    assert handles(g) == {(f"endpoint::{m} {p}", h) for (m, p), h in EXPECTED.items()}
    for (m, p), node in zip(
        EXPECTED, (endpoints(g)[f"endpoint::{m} {p}"] for m, p in EXPECTED)
    ):
        assert (node.name, node.method, node.route) == (f"{m} {p}", m, p)
        assert node.exact and node.mounted
    assert g.route_issues == []
    assert_no_dangling(g)

    again = builder.build()
    assert again.edges == g.edges and set(again.nodes) == set(g.nodes)


def test_router_mounted_twice_gives_two_endpoints_one_handler():
    g = one(
        HEAD
        + """
        router = APIRouter()

        @router.get("/x")
        def h(): ...

        app.include_router(router, prefix="/v1")
        app.include_router(router, prefix="/v2")
        """
    )
    assert set(endpoints(g)) == {"endpoint::GET /v1/x", "endpoint::GET /v2/x"}
    assert handles(g) == {
        ("endpoint::GET /v1/x", "app.py::h"),
        ("endpoint::GET /v2/x", "app.py::h"),
    }
    assert_no_dangling(g)


def test_same_method_and_path_from_two_handlers_shares_one_node():
    g = one(
        HEAD
        + """
        @app.get("/x")
        def a(): ...

        @app.get("/x")
        def b(): ...
        """
    )
    assert set(endpoints(g)) == {"endpoint::GET /x"}
    assert handles(g) == {
        ("endpoint::GET /x", "app.py::a"),
        ("endpoint::GET /x", "app.py::b"),
    }


def test_non_literal_prefix_marks_endpoint_inexact_and_records_issue():
    g = one(
        HEAD
        + """
        router = APIRouter(prefix=PREFIX)

        @router.get("/x")
        def h(): ...

        app.include_router(router)
        """
    )
    node = endpoints(g)["endpoint::GET /x"]
    assert (node.exact, node.mounted) == (False, True)
    assert [(i.subject, i.reason) for i in g.route_issues] == [
        ("router", Why.NON_LITERAL_PREFIX)
    ]


def test_unmounted_router_endpoint_is_flagged_unmounted():
    g = one("""
        from fastapi import APIRouter
        router = APIRouter(prefix="/orphan")

        @router.get("/x")
        def h(): ...
        """)
    node = endpoints(g)["endpoint::GET /orphan/x"]
    assert (node.exact, node.mounted) == (True, False)


def test_flags_merge_conservatively_when_routes_collide():
    g = one(
        HEAD
        + """
        router = APIRouter(prefix=P)

        @app.get("/x")
        def a(): ...

        @router.get("/x")
        def b(): ...

        app.include_router(router)
        """
    )
    assert set(endpoints(g)) == {"endpoint::GET /x"}
    node = endpoints(g)["endpoint::GET /x"]
    assert (node.exact, node.mounted) == (False, True)
    assert len(handles(g)) == 2


def test_empty_full_path_is_normalized_to_slash():
    g = one(
        HEAD
        + """
        router = APIRouter()

        @router.get("")
        def index(): ...

        app.include_router(router)
        """
    )
    node = endpoints(g)["endpoint::GET /"]
    assert (node.name, node.route) == ("GET /", "/")
    assert handles(g) == {("endpoint::GET /", "app.py::index")}


def test_unknown_receiver_makes_no_endpoint_but_is_reported():
    g = one("""
        cache = make_cache()

        @cache.get("/x")
        def a(): ...
        """)
    assert endpoints(g) == {} and handles(g) == set()
    assert [(i.subject, i.reason) for i in g.route_issues] == [
        ("app.py::a", Why.UNKNOWN_RECEIVER)
    ]


def test_remove_and_readd_without_facts_clear_endpoints():
    src = src_of(
        HEAD
        + """
        @app.get("/x")
        def h(): ...
        """
    )
    builder = GraphBuilder()
    add(builder, "app.py", src)
    assert set(endpoints(builder.build())) == {"endpoint::GET /x"}

    builder.add(parse_python_source(src, "app.py"))
    g = builder.build()
    assert endpoints(g) == {} and handles(g) == set()
    assert "app.py::h" in g.nodes

    add(builder, "app.py", src)
    builder.remove("app.py")
    g = builder.build()
    assert endpoints(g) == {} and "app.py::h" not in g.nodes
