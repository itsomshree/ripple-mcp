import textwrap

import pytest

from ripple.graph.schema import CoverageFacts
from ripple.parsers.fastapi_parser import HTTP_METHODS
from ripple.parsers.pytest_parser import parse_coverage_file, parse_coverage_source
from ripple.parsers.python_parser import parse_python_source

PATH = "tests/test_api.py"


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def facts(text: str, path: str = PATH) -> CoverageFacts:
    return parse_coverage_source(src_of(text), path)


def rows(text: str):
    return [(c.owner_id.partition("::")[2], c.method, c.path) for c in facts(text).http]


# sinks
@pytest.mark.parametrize("verb", sorted(HTTP_METHODS))
def test_all_http_verbs(verb):
    assert rows(f"def f(client):\n    client.{verb}('/x')\n") == [
        ("f", verb.upper(), "/x")
    ]


def test_call_fields_and_owner_id():
    (c,) = facts("""
        def test_x(client):
            pass
            client.get("/users")
        """).http
    assert (c.owner_id, c.method, c.path, c.line) == (
        f"{PATH}::test_x",
        "GET",
        "/users",
        3,
    )


def test_path_and_url_keywords():
    assert rows("""
        def f(client):
            client.get(url="/a")
            client.post(path="/b")
            client.put(headers={}, url="/c")
        """) == [("f", "GET", "/a"), ("f", "POST", "/b"), ("f", "PUT", "/c")]


def test_request_form():
    assert rows("""
        def f(client, verb):
            client.request("GET", "/a")
            client.request(method="post", url="/b")
            client.request("GET", url="/c")
            client.request("BOGUS", "/d")
            client.request(verb, "/e")
            client.request("GET")
        """) == [("f", "GET", "/a"), ("f", "POST", "/b"), ("f", "GET", "/c")]


def test_receiver_may_be_any_expression_and_await_is_seen():
    assert rows("""
        async def f(self):
            self.client.get("/a")
            httpx.AsyncClient().get("/b")
            await self.client.post("/c")
            client.get("/d").json()
        """) == [
        ("f", "GET", "/a"),
        ("f", "GET", "/b"),
        ("f", "POST", "/c"),
        ("f", "GET", "/d"),
    ]


# paths
def test_fstring_holes_become_wildcards():
    assert rows("""
        def f(client, uid, oid):
            client.get(f"/users/{uid}")
            client.get(f"/users/{uid}/orders/{oid}")
            client.get(f"/files/{uid}.json")
        """) == [
        ("f", "GET", "/users/{}"),
        ("f", "GET", "/users/{}/orders/{}"),
        ("f", "GET", "/files/{}.json"),
    ]


def test_query_and_fragment_are_stripped():
    assert rows("""
        def f(client, i):
            client.get("/x?a=1")
            client.get("/y#top")
            client.get(f"/z?id={i}")
            client.get("/?a=1")
            client.get("?a=1")
        """) == [
        ("f", "GET", "/x"),
        ("f", "GET", "/y"),
        ("f", "GET", "/z"),
        ("f", "GET", "/"),
    ]


def test_route_templates_pass_through_unchanged():
    assert rows('def f(c):\n    c.get("/users/{user_id}")\n') == [
        ("f", "GET", "/users/{user_id}")
    ]


def test_unsupported_forms_are_skipped():
    assert rows("""
        def f(client, url, base, args):
            client.get(url)
            client.get("users")
            client.get("http://testserver/x")
            client.get(base + "/x")
            client.get(f"{base}/x")
            client.get()
            client.get(*args)
            client.fetch("/fetch")
            get("/bare")
            client.get("/ok")
        """) == [("f", "GET", "/ok")]


# scoping
def test_only_function_bodies_are_scanned():
    assert rows("""
        @app.get("/decorator")
        def route(x=client.get("/default")):
            client.get("/body")

        client.get("/module")

        class C:
            attr = client.get("/class_body")

            def m(self):
                client.get("/method")

        def outer():
            client.get("/outer")

            def inner():
                client.get("/inner")

            return [client.get("/comp") for _ in range(2)], (
                lambda: client.get("/lam")
            )
        """) == [
        ("route", "GET", "/body"),
        ("C.m", "GET", "/method"),
        ("outer", "GET", "/outer"),
        ("outer.inner", "GET", "/inner"),
        ("outer", "GET", "/comp"),
        ("outer", "GET", "/lam"),
    ]


def test_route_declared_inside_a_test_is_not_a_client_call():
    assert (
        rows("""
        def test_x():
            app = FastAPI()

            @app.get("/inline")
            def h(): ...
        """)
        == []
    )


def test_repeats_collapse_to_the_first_line():
    f = facts("""
        def f(c):
            c.get("/x")
            c.get("/x")
            c.post("/x")
            c.get("/y")
        """)
    assert [(c.method, c.path, c.line) for c in f.http] == [
        ("GET", "/x", 2),
        ("POST", "/x", 4),
        ("GET", "/y", 5),
    ]


# robustness
def test_empty_source():
    assert facts("") == CoverageFacts(path=PATH)


def test_windows_path_is_normalized():
    f = parse_coverage_source("def f(c):\n    c.get('/x')\n", "tests\\t.py")
    assert f.path == "tests/t.py"
    assert f.http[0].owner_id == "tests/t.py::f"


def test_syntax_error_yields_empty_facts():
    assert facts("def f(c):\n    c.get('/x')\ndef broken(:\n") == CoverageFacts(
        path=PATH
    )


def test_parse_file_handles_non_utf8(tmp_path):
    p = tmp_path / "test_x.py"
    p.write_bytes(b'# caf\xe9\ndef f(c):\n    c.get("/x")\n')
    f = parse_coverage_file(p, "test_x.py")
    assert [(c.method, c.path) for c in f.http] == [("GET", "/x")]


def test_owner_ids_match_python_parser_nodes():
    text = src_of("""
        def top(c):
            c.get("/a")

        class C:
            def m(self, c):
                def inner():
                    c.get("/b")

                c.get("/c")
        """)
    node_ids = {n.id for n in parse_python_source(text, PATH).nodes}
    found = parse_coverage_source(text, PATH).http
    assert len(found) == 3
    assert {c.owner_id for c in found} <= node_ids
