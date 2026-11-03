import pytest

from ripple.graph.coverage import EndpointIndex
from ripple.graph.schema import APIEndpointNode, endpoint_id


def nodes(*routes: str, **flags) -> list[APIEndpointNode]:
    out = []
    for r in routes:
        method, _, route = r.partition(" ")
        out.append(
            APIEndpointNode(
                id=endpoint_id(method, route),
                name=r,
                method=method,
                route=route,
                **flags,
            )
        )
    return out


def match(routes: list[str], method: str, path: str) -> list[str]:
    return EndpointIndex(nodes(*routes)).match(method, path)


def ids(*routes: str) -> list[str]:
    return sorted(f"endpoint::{r}" for r in routes)


@pytest.mark.parametrize(
    "routes, method, path, expected",
    [
        # literals and parameters
        (["GET /users"], "GET", "/users", ["GET /users"]),
        (["GET /users/{id}"], "GET", "/users/42", ["GET /users/{id}"]),
        (
            ["GET /users/{id}/orders/{oid}"],
            "GET",
            "/users/1/orders/2",
            ["GET /users/{id}/orders/{oid}"],
        ),
        (["GET /users/{id:int}"], "GET", "/users/1", ["GET /users/{id:int}"]),
        # holes and templates on the call side
        (["GET /users/{id}"], "GET", "/users/{}", ["GET /users/{id}"]),
        (["GET /users/{id}"], "GET", "/users/{user_id}", ["GET /users/{id}"]),
        (["GET /users/me"], "GET", "/users/{}", ["GET /users/me"]),
        # method and shape
        (["GET /x"], "POST", "/x", []),
        (["GET /users/{id}"], "GET", "/users", []),
        (["GET /users/{id}"], "GET", "/users/1/extra", []),
        (["GET /users"], "GET", "/orders", []),
        (["GET /users/me"], "GET", "/users/you", []),
        # trailing slash and root
        (["GET /users"], "GET", "/users/", ["GET /users"]),
        (["GET /users/"], "GET", "/users", ["GET /users/"]),
        (["GET /"], "GET", "/", ["GET /"]),
        (["GET /"], "GET", "/x", []),
        # mixed segments
        (
            ["GET /files/{name}.json"],
            "GET",
            "/files/a.json",
            ["GET /files/{name}.json"],
        ),
        (["GET /files/{name}.json"], "GET", "/files/a.txt", []),
        (
            ["GET /files/{name}.json"],
            "GET",
            "/files/{}.json",
            ["GET /files/{name}.json"],
        ),
        (["GET /files/{name}.json"], "GET", "/files/{}.txt", []),
        (["GET /files/{name}.json"], "GET", "/files/{}", ["GET /files/{name}.json"]),
        (["GET /v{n}/items"], "GET", "/v2/items", ["GET /v{n}/items"]),
        (["GET /v{n}/items"], "GET", "/x2/items", []),
        # path converter
        (["GET /static/{p:path}"], "GET", "/static/a", ["GET /static/{p:path}"]),
        (["GET /static/{p:path}"], "GET", "/static/a/b/c", ["GET /static/{p:path}"]),
        (["GET /static/{p:path}"], "GET", "/static", []),
        (["GET /static/{p:path}"], "GET", "/other/a", []),
        (["GET /static/{p:path}"], "POST", "/static/a", []),
    ],
)
def test_matching(routes, method, path, expected):
    assert match(routes, method, path) == ids(*expected)


# specificity and ties
def test_literal_beats_parameter():
    routes = ["GET /users/me", "GET /users/{id}"]
    assert match(routes, "GET", "/users/me") == ids("GET /users/me")
    assert match(routes, "GET", "/users/42") == ids("GET /users/{id}")


def test_hole_call_ties_between_literal_and_parameter():
    routes = ["GET /users/me", "GET /users/{id}"]
    assert match(routes, "GET", "/users/{}") == ids("GET /users/me", "GET /users/{id}")


def test_specificity_is_counted_per_segment():
    routes = ["GET /a/{x}/{y}", "GET /a/b/{y}", "GET /a/{x}/c"]
    assert match(routes, "GET", "/a/b/c") == ids("GET /a/b/{y}", "GET /a/{x}/c")


def test_fixed_route_beats_path_converter():
    routes = ["GET /static/{p:path}", "GET /static/logo.png"]
    assert match(routes, "GET", "/static/logo.png") == ids("GET /static/logo.png")
    assert match(routes, "GET", "/static/other.css") == ids("GET /static/{p:path}")


# index behaviour
def test_empty_index_matches_nothing():
    assert EndpointIndex([]).match("GET", "/x") == []


def test_result_does_not_depend_on_input_order():
    routes = ["GET /users/me", "GET /users/{id}", "GET /health"]
    forward = EndpointIndex(nodes(*routes)).match("GET", "/users/{}")
    backward = EndpointIndex(nodes(*reversed(routes))).match("GET", "/users/{}")
    assert forward == backward == ids("GET /users/me", "GET /users/{id}")


def test_inexact_and_unmounted_endpoints_still_match_their_route():
    index = EndpointIndex(nodes("GET /x", exact=False, mounted=False))
    assert index.match("GET", "/x") == ids("GET /x")
