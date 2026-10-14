import textwrap
from pathlib import Path

from ripple.graph.resolver import ModuleResolver
from ripple.graph.routes import resolve_routes
from ripple.graph.schema import RouteIssueReason as Why
from ripple.graph.schema import RouteResolution
from ripple.parsers.fastapi_parser import parse_fastapi_file, parse_fastapi_source
from ripple.parsers.python_parser import parse_python_file, parse_python_source

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"

HEAD = "        from fastapi import FastAPI, APIRouter\n        app = FastAPI()\n"


def resolve(files: dict[str, str]) -> RouteResolution:
    srcs = {p: textwrap.dedent(s).lstrip("\n") for p, s in files.items()}
    parsed = {p: parse_python_source(s, p) for p, s in srcs.items()}
    facts = {p: parse_fastapi_source(s, p) for p, s in srcs.items()}
    return resolve_routes(facts, ModuleResolver(parsed))


def one(src: str) -> RouteResolution:
    return resolve({"app.py": src})


def paths(res: RouteResolution):
    return [(r.method, r.path) for r in res.routes]


def issues(res: RouteResolution):
    return [(i.subject, i.reason) for i in res.issues]


def test_sample_fixture_full_paths():
    files = sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix())
    rels = [p.relative_to(FIXTURE).as_posix() for p in files]
    parsed = {r: parse_python_file(p, r) for p, r in zip(files, rels)}
    facts = {r: parse_fastapi_file(p, r) for p, r in zip(files, rels)}
    res = resolve_routes(facts, ModuleResolver(parsed))
    assert [(r.method, r.path, r.handler_id) for r in res.routes] == [
        ("GET", "/health", "app/main.py::health"),
        ("GET", "/api/orders/{order_id}", "app/routers/orders.py::get_order"),
        ("POST", "/api/orders", "app/routers/orders.py::place_order"),
        ("GET", "/users", "app/routers/users.py::list_users"),
        ("GET", "/users/{user_id}", "app/routers/users.py::get_user"),
        ("POST", "/users", "app/routers/users.py::create_user"),
        ("DELETE", "/users/{user_id}", "app/routers/users.py::delete_user"),
    ]
    assert all(r.exact and r.mounted for r in res.routes)
    assert res.issues == []


def test_same_file_router_mounted_with_prefix():
    res = one(
        HEAD
        + """
        router = APIRouter(prefix="/users")

        @router.get("/{id}")
        def get(id): ...

        @app.get("/health")
        def health(): ...

        app.include_router(router, prefix="/api")
        """
    )
    assert paths(res) == [("GET", "/api/users/{id}"), ("GET", "/health")]
    assert all(r.exact and r.mounted for r in res.routes)


def test_nested_routers_compose_outermost_first():
    res = one(
        HEAD
        + """
        v1 = APIRouter(prefix="/v1")
        users = APIRouter(prefix="/users")

        @users.get("/{id}")
        def get(id): ...

        v1.include_router(users)
        app.include_router(v1, prefix="/api")
        """
    )
    assert paths(res) == [("GET", "/api/v1/users/{id}")]


def test_cross_file_mount_forms():
    router = "from fastapi import APIRouter\nrouter = APIRouter(prefix='/{n}')\n"
    route = "@router.get('/x')\ndef {h}(): ...\n"
    res = resolve(
        {
            "app/__init__.py": "",
            "app/routers/__init__.py": "",
            "app/routers/a.py": router.format(n="a") + route.format(h="ax"),
            "app/routers/b.py": router.format(n="b") + route.format(h="bx"),
            "app/routers/c.py": router.format(n="c") + route.format(h="cx"),
            "app/main.py": """
                from fastapi import FastAPI
                from app.routers import a
                from app.routers.b import router as b_router
                import app.routers.c

                application = FastAPI()
                application.include_router(a.router)
                application.include_router(b_router, prefix="/api")
                application.include_router(app.routers.c.router)
                """,
        }
    )
    assert paths(res) == [("GET", "/a/x"), ("GET", "/api/b/x"), ("GET", "/c/x")]
    assert res.issues == []


def test_router_reexported_through_package_init():
    res = resolve(
        {
            "pkg/__init__.py": "from .r import router\n",
            "pkg/r.py": (
                "from fastapi import APIRouter\n"
                "router = APIRouter(prefix='/r')\n"
                "@router.get('/x')\ndef h(): ...\n"
            ),
            "main.py": (
                "from fastapi import FastAPI\nimport pkg\n"
                "app = FastAPI()\napp.include_router(pkg.router)\n"
            ),
        }
    )
    assert paths(res) == [("GET", "/r/x")]


def test_aliased_fastapi_imports():
    res = one("""
        import fastapi as f
        from fastapi import APIRouter as R

        app = f.FastAPI()
        r = R(prefix="/r")

        @r.get("/x")
        def h(): ...

        app.include_router(r)
        """)
    assert paths(res) == [("GET", "/r/x")]


def test_router_mounted_twice_yields_two_endpoints_and_dedupes_repeats():
    res = one(
        HEAD
        + """
        router = APIRouter()

        @router.get("/x")
        def h(): ...

        app.include_router(router, prefix="/v1")
        app.include_router(router, prefix="/v2")
        app.include_router(router, prefix="/v1")
        """
    )
    assert paths(res) == [("GET", "/v1/x"), ("GET", "/v2/x")]
    assert len({r.handler_id for r in res.routes}) == 1


def test_unmounted_router_keeps_own_prefix_and_is_flagged():
    res = one("""
        from fastapi import APIRouter
        router = APIRouter(prefix="/orphan")

        @router.get("/x")
        def h(): ...
        """)
    (r,) = res.routes
    assert (r.path, r.exact, r.mounted) == ("/orphan/x", True, False)


def test_unknown_and_fake_receivers_get_no_endpoint():
    res = one("""
        from fastapi import FastAPI
        app = FastAPI()
        cache = make_cache()

        class APIRouter: ...
        fake = APIRouter()

        @cache.get("/x")
        def a(): ...

        @app.get("/ok")
        def b(): ...

        @fake.get("/y")
        def c(): ...
        """)
    assert paths(res) == [("GET", "/ok")]
    assert issues(res) == [
        ("app.py::a", Why.UNKNOWN_RECEIVER),
        ("app.py::c", Why.UNKNOWN_RECEIVER),
    ]


def test_non_literal_router_prefix_is_inexact():
    res = one(
        HEAD
        + """
        router = APIRouter(prefix=PREFIX)

        @router.get("/x")
        def h(): ...

        app.include_router(router)
        """
    )
    (r,) = res.routes
    assert (r.path, r.exact, r.mounted) == ("/x", False, True)
    assert issues(res) == [("router", Why.NON_LITERAL_PREFIX)]


def test_non_literal_mount_prefix_is_inexact():
    res = one(
        HEAD
        + """
        router = APIRouter(prefix="/r")

        @router.get("/x")
        def h(): ...

        app.include_router(router, prefix=P)
        """
    )
    (r,) = res.routes
    assert (r.path, r.exact) == ("/r/x", False)
    assert issues(res) == [("app <- router", Why.NON_LITERAL_PREFIX)]


def test_mount_cycle_terminates_and_is_reported_once():
    res = one("""
        from fastapi import APIRouter
        a = APIRouter(prefix="/a")
        b = APIRouter(prefix="/b")
        a.include_router(b)
        b.include_router(a)

        @a.get("/x")
        def h(): ...
        """)
    assert len(res.routes) == 1
    assert res.routes[0].exact is False and res.routes[0].mounted is False
    assert [i.reason for i in res.issues] == [Why.MOUNT_CYCLE]


def test_unresolved_mount_is_reported_but_routes_still_resolve():
    res = one(
        HEAD
        + """
        @app.get("/ok")
        def h(): ...

        app.include_router(missing.router)
        """
    )
    assert paths(res) == [("GET", "/ok")]
    assert issues(res) == [("app <- missing.router", Why.UNRESOLVED_MOUNT)]
