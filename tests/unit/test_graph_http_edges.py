import textwrap

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import EdgeType, Evidence
from ripple.parsers.fastapi_parser import parse_fastapi_source
from ripple.parsers.pytest_parser import parse_coverage_source
from ripple.parsers.python_parser import parse_python_source

APP = "app/main.py"
T = "tests/test_api.py"

MAIN = """
from fastapi import FastAPI, APIRouter

app = FastAPI()
router = APIRouter(prefix="/users")

@router.get("/{user_id}")
def get_user(user_id): ...

@app.get("/health")
def health(): ...

app.include_router(router)
"""

GET_USER = "endpoint::GET /users/{user_id}"
HEALTH = "endpoint::GET /health"


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def add(builder: GraphBuilder, path: str, text: str, cov: bool = True) -> None:
    src = src_of(text)
    builder.add(
        parse_python_source(src, path),
        parse_fastapi_source(src, path),
        cov=parse_coverage_source(src, path) if cov else None,
    )


def builder_for(files: dict[str, str], cov: bool = True) -> GraphBuilder:
    builder = GraphBuilder()
    for path, text in files.items():
        add(builder, path, text, cov)
    return builder


def build(files: dict[str, str], cov: bool = True) -> CodeGraph:
    return builder_for(files, cov).build()


def covers(g: CodeGraph) -> list[tuple[str, str, Evidence | None]]:
    return [(e.src, e.dst, e.via) for e in g.edges_of(EdgeType.TESTS)]


def assert_no_dangling(g: CodeGraph) -> None:
    for e in g.edges:
        assert e.src in g.nodes and e.dst in g.nodes, e


# matching into edges
def test_client_calls_link_tests_to_endpoints():
    g = build(
        {
            APP: MAIN,
            T: """
                def test_get_user(client):
                    client.get("/users/1")

                def test_health(client):
                    client.get("/health")
                """,
        }
    )
    assert covers(g) == [
        (f"{T}::test_get_user", GET_USER, Evidence.HTTP),
        (f"{T}::test_health", HEALTH, Evidence.HTTP),
    ]
    assert_no_dangling(g)


def test_unmatched_paths_and_methods_make_no_edge():
    g = build(
        {
            APP: MAIN,
            T: """
                def test_x(client):
                    client.get("/nope")
                    client.post("/health")
                    client.get("/users")
                    client.delete("/users/1")
                """,
        }
    )
    assert covers(g) == []


def test_repeated_hits_on_one_endpoint_collapse_to_one_edge():
    g = build(
        {
            APP: MAIN,
            T: """
                def test_x(client):
                    client.get("/users/1")
                    client.get("/users/2")
                    client.request("GET", "/users/3")
                """,
        }
    )
    assert covers(g) == [(f"{T}::test_x", GET_USER, Evidence.HTTP)]


def test_most_specific_route_wins_and_hole_calls_link_every_match():
    g = build(
        {
            APP: """
                from fastapi import FastAPI
                app = FastAPI()

                @app.get("/users/me")
                def me(): ...

                @app.get("/users/{uid}")
                def by_id(uid): ...
                """,
            T: """
                def test_literal(client):
                    client.get("/users/me")

                def test_dynamic(client, uid):
                    client.get(f"/users/{uid}")
                """,
        }
    )
    me, by_id = "endpoint::GET /users/me", "endpoint::GET /users/{uid}"
    assert covers(g) == [
        (f"{T}::test_dynamic", me, Evidence.HTTP),
        (f"{T}::test_dynamic", by_id, Evidence.HTTP),
        (f"{T}::test_literal", me, Evidence.HTTP),
    ]


def test_test_class_methods_are_sources():
    g = build(
        {
            APP: MAIN,
            T: """
                class TestApi:
                    def test_health(self, client):
                        client.get("/health")
                """,
        }
    )
    assert covers(g) == [(f"{T}::TestApi.test_health", HEALTH, Evidence.HTTP)]


def test_only_tests_and_the_helpers_they_reach_are_sources():
    g = build(
        {
            APP: MAIN,
            "app/util.py": 'def ping(c):\n    c.get("/health")\n',
            T: """
                import pytest

                @pytest.fixture
                def warmed(client):
                    client.get("/health")

                def fetch(client):
                    client.get("/health")

                def test_nested(client):
                    def run():
                        client.get("/health")
                    run()

                def test_ok(client):
                    client.get("/users/1")
                """,
        }
    )
    assert covers(g) == [
        (f"{T}::test_nested", HEALTH, Evidence.HTTP),
        (f"{T}::test_ok", GET_USER, Evidence.HTTP),
    ]
    assert_no_dangling(g)


def test_direct_call_and_http_evidence_coexist_on_different_targets():
    g = build(
        {
            "app/__init__.py": "",
            APP: MAIN,
            T: """
                from app.main import get_user

                def test_both(client):
                    get_user(1)
                    client.get("/users/1")
                """,
        }
    )
    assert covers(g) == [
        (f"{T}::test_both", f"{APP}::get_user", Evidence.CALL),
        (f"{T}::test_both", GET_USER, Evidence.HTTP),
    ]


# endpoint flags
def test_unmounted_router_endpoint_matches_its_own_route():
    g = build(
        {
            "app.py": """
                from fastapi import APIRouter
                router = APIRouter(prefix="/orphan")

                @router.get("/x")
                def h(): ...
                """,
            T: 'def test_x(client):\n    client.get("/orphan/x")\n',
        }
    )
    assert covers(g) == [(f"{T}::test_x", "endpoint::GET /orphan/x", Evidence.HTTP)]
    assert g.nodes["endpoint::GET /orphan/x"].mounted is False


def test_inexact_endpoint_matches_its_recorded_route():
    g = build(
        {
            "app.py": """
                from fastapi import FastAPI, APIRouter
                app = FastAPI()
                router = APIRouter(prefix=PREFIX)

                @router.get("/x")
                def h(): ...

                app.include_router(router)
                """,
            T: 'def test_x(client):\n    client.get("/x")\n',
        }
    )
    assert covers(g) == [(f"{T}::test_x", "endpoint::GET /x", Evidence.HTTP)]
    assert g.nodes["endpoint::GET /x"].exact is False


# builder semantics
FILES = {
    APP: MAIN,
    T: 'def test_health(client):\n    client.get("/health")\n',
}


def test_files_added_without_coverage_facts_contribute_no_edges():
    g = build(FILES, cov=False)
    assert HEALTH in g.nodes
    assert covers(g) == []


def test_readd_without_facts_and_removal_clear_edges():
    builder = builder_for(FILES)
    assert len(covers(builder.build())) == 1

    add(builder, T, FILES[T], cov=False)
    assert covers(builder.build()) == []

    add(builder, T, FILES[T])
    assert len(covers(builder.build())) == 1

    builder.remove(T)
    g = builder.build()
    assert covers(g) == []
    assert T not in g.nodes
    assert_no_dangling(g)


def test_facts_for_missing_owners_are_ignored():
    builder = GraphBuilder()
    add(builder, APP, MAIN)
    src = 'def test_x(client):\n    client.get("/health")\n'
    builder.add(parse_python_source("x = 1\n", T), cov=parse_coverage_source(src, T))
    assert covers(builder.build()) == []


def test_rebuilds_are_deterministic():
    builder = builder_for(
        {
            APP: MAIN,
            T: """
                def test_a(client):
                    client.get("/users/1")
                    client.get("/health")
                """,
        }
    )
    first = builder.build()
    assert len(covers(first)) == 2
    again = builder.build()
    assert again.edges == first.edges
    assert set(again.nodes) == set(first.nodes)
    assert_no_dangling(again)
