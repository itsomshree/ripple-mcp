import textwrap

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import EdgeType, Evidence
from ripple.parsers.fastapi_parser import parse_fastapi_source
from ripple.parsers.pytest_parser import parse_coverage_source
from ripple.parsers.python_parser import parse_python_source

SVC = "app/svc.py"
H = "tests/helpers.py"
T = "tests/test_a.py"

BASE = {"app/__init__.py": "", SVC: "def get(): ...\n"}


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def builder_for(files: dict[str, str]) -> GraphBuilder:
    builder = GraphBuilder()
    for path, text in files.items():
        builder.add(parse_python_source(src_of(text), path))
    return builder


def build(files: dict[str, str]) -> CodeGraph:
    return builder_for(files).build()


def covers(g: CodeGraph) -> list[tuple[str, str, Evidence | None]]:
    return [(e.src, e.dst, e.via) for e in g.edges_of(EdgeType.TESTS)]


def assert_no_dangling(g: CodeGraph) -> None:
    for e in g.edges:
        assert e.src in g.nodes and e.dst in g.nodes, e


GET_BY_A = [(f"{T}::test_a", f"{SVC}::get", Evidence.CALL)]


APP = "app/main.py"
HEALTH = "endpoint::GET /health"
GET_USER = "endpoint::GET /users/{user_id}"

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


def build_http(files: dict[str, str]) -> CodeGraph:
    builder = GraphBuilder()
    for path, text in files.items():
        src = src_of(text)
        builder.add(
            parse_python_source(src, path),
            parse_fastapi_source(src, path),
            cov=parse_coverage_source(src, path),
        )
    return builder.build()


def test_helper_chain_reaches_source():
    g = build(
        {
            **BASE,
            H: """
                from app.svc import get

                def make():
                    get()

                def outer():
                    make()
                """,
            T: """
                from tests.helpers import outer

                def test_a():
                    outer()
                """,
        }
    )
    assert covers(g) == GET_BY_A
    assert_no_dangling(g)


def test_helper_cycle_terminates():
    g = build(
        {
            **BASE,
            H: """
                from app.svc import get

                def a():
                    b()
                    get()

                def b():
                    a()
                """,
            T: "from tests.helpers import a\ndef test_a():\n    a()\n",
        }
    )
    assert covers(g) == GET_BY_A


def test_shared_helper_is_credited_to_every_test_that_reaches_it():
    g = build(
        {
            **BASE,
            H: "from app.svc import get\ndef make():\n    get()\n",
            T: """
                from tests.helpers import make

                def test_a():
                    make()

                def test_b():
                    make()

                def test_c():
                    pass
                """,
        }
    )
    assert covers(g) == [
        (f"{T}::test_a", f"{SVC}::get", Evidence.CALL),
        (f"{T}::test_b", f"{SVC}::get", Evidence.CALL),
    ]


def test_self_helper_method_on_a_test_class():
    g = build(
        {
            **BASE,
            T: """
                from app.svc import get

                class TestSvc:
                    def _login(self):
                        get()

                    def test_a(self):
                        self._login()
                """,
        }
    )
    assert covers(g) == [(f"{T}::TestSvc.test_a", f"{SVC}::get", Evidence.CALL)]


def test_instantiated_helper_class_propagates_through_init():
    g = build(
        {
            **BASE,
            T: """
                from app.svc import get

                class Client:
                    def __init__(self):
                        get()

                def test_a():
                    Client()
                """,
        }
    )
    assert covers(g) == GET_BY_A
    assert f"{T}::Client" in g.nodes


def test_uncalled_helpers_and_module_level_calls_are_not_coverage():
    g = build(
        {
            **BASE,
            H: "from app.svc import get\nget()\ndef unused():\n    get()\n",
            T: "import tests.helpers\ndef test_a():\n    pass\n",
        }
    )
    assert covers(g) == []


def test_walk_does_not_enter_other_tests():
    g = build(
        {
            **BASE,
            T: """
                from app.svc import get

                def test_inner():
                    get()

                def test_outer():
                    test_inner()
                """,
        }
    )
    assert covers(g) == [(f"{T}::test_inner", f"{SVC}::get", Evidence.CALL)]


def test_coverage_stops_at_the_first_source_symbol():
    g = build(
        {
            "app/__init__.py": "",
            SVC: "def other(): ...\ndef get():\n    other()\n",
            H: "from app.svc import get\ndef make():\n    get()\n",
            T: "from tests.helpers import make\ndef test_a():\n    make()\n",
        }
    )
    assert covers(g) == GET_BY_A


def test_direct_and_helper_paths_to_one_target_collapse_to_one_edge():
    g = build(
        {
            **BASE,
            H: "from app.svc import get\ndef make():\n    get()\n",
            T: """
                from app.svc import get
                from tests.helpers import make

                def test_a():
                    get()
                    make()
                """,
        }
    )
    assert covers(g) == GET_BY_A


def test_rebuilds_are_deterministic_and_removing_the_helper_clears_edges():
    files = {
        **BASE,
        H: "from app.svc import get\ndef make():\n    get()\n",
        T: "from tests.helpers import make\ndef test_a():\n    make()\n",
    }
    builder = builder_for(files)
    first = builder.build()
    assert covers(first) == GET_BY_A
    again = builder.build()
    assert again.edges == first.edges
    assert set(again.nodes) == set(first.nodes)

    builder.remove(H)
    g = builder.build()
    assert covers(g) == []
    assert_no_dangling(g)


def test_http_call_through_a_helper_links_the_endpoint():
    g = build_http(
        {
            "app/__init__.py": "",
            APP: MAIN,
            H: 'def ping(client):\n    client.get("/health")\n',
            T: """
                from tests.helpers import ping

                def test_a(client):
                    ping(client)
                """,
        }
    )
    assert covers(g) == [(f"{T}::test_a", HEALTH, Evidence.HTTP)]
    assert_no_dangling(g)


def test_http_call_through_a_helper_chain():
    g = build_http(
        {
            "app/__init__.py": "",
            APP: MAIN,
            H: """
                def ping(client):
                    client.get("/health")

                def outer(client):
                    ping(client)
                """,
            T: """
                from tests.helpers import outer

                def test_a(client):
                    outer(client)
                """,
        }
    )
    assert covers(g) == [(f"{T}::test_a", HEALTH, Evidence.HTTP)]


def test_shared_http_helper_is_credited_to_every_test_that_reaches_it():
    g = build_http(
        {
            "app/__init__.py": "",
            APP: MAIN,
            H: 'def ping(client):\n    client.get("/health")\n',
            T: """
                from tests.helpers import ping

                def test_a(client):
                    ping(client)

                def test_b(client):
                    ping(client)

                def test_c(client):
                    pass
                """,
        }
    )
    assert covers(g) == [
        (f"{T}::test_a", HEALTH, Evidence.HTTP),
        (f"{T}::test_b", HEALTH, Evidence.HTTP),
    ]


def test_helper_with_a_direct_call_and_an_http_call_yields_both_kinds():
    g = build_http(
        {
            "app/__init__.py": "",
            APP: MAIN,
            H: """
                from app.main import get_user

                def both(client):
                    get_user(1)
                    client.get("/users/1")
                """,
            T: """
                from tests.helpers import both

                def test_a(client):
                    both(client)
                """,
        }
    )
    assert covers(g) == [
        (f"{T}::test_a", f"{APP}::get_user", Evidence.CALL),
        (f"{T}::test_a", GET_USER, Evidence.HTTP),
    ]


def test_uncalled_http_helper_links_nothing():
    g = build_http(
        {
            "app/__init__.py": "",
            APP: MAIN,
            H: 'def ping(client):\n    client.get("/health")\n',
            T: "def test_a(client):\n    pass\n",
        }
    )
    assert covers(g) == []


def test_http_call_in_a_non_test_helper_is_not_attributed_to_the_test():
    g = build_http(
        {
            "app/__init__.py": "",
            APP: MAIN,
            "app/util.py": 'def ping(c):\n    c.get("/health")\n',
            T: """
                from app.util import ping

                def test_a(client):
                    ping(client)
                """,
        }
    )
    assert covers(g) == [(f"{T}::test_a", "app/util.py::ping", Evidence.CALL)]
