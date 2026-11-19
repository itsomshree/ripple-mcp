import textwrap

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import EdgeType
from ripple.graph.schema import UnresolvedReason as R
from ripple.parsers.python_parser import parse_python_source


def build(files: dict[str, str]) -> CodeGraph:
    builder = GraphBuilder()
    for path, text in files.items():
        builder.add(parse_python_source(textwrap.dedent(text).lstrip("\n"), path))
    return builder.build()


def calls_from(g: CodeGraph, src: str) -> set[str]:
    return {e.dst for e in g.edges_of(EdgeType.CALLS) if e.src == src}


def unresolved(g: CodeGraph) -> dict[tuple[str, str], R]:
    return {(u.caller_id, u.target): u.reason for u in g.unresolved}


def test_delay_and_apply_async_link_the_task_function():
    g = build(
        {
            "app.py": """
            from celery import shared_task

            @shared_task
            def work(x): ...

            def f():
                work.delay(1)
                work.apply_async(args=[1])
            """
        }
    )
    assert calls_from(g, "app.py::f") == {"app.py::work"}


def test_task_decorated_through_an_app_object_and_called_from_other_files():
    g = build(
        {
            "app/__init__.py": "",
            "app/celery_app.py": "from celery import Celery\ncelery_app = Celery('x')\n",
            "app/tasks.py": """
                from app.celery_app import celery_app

                @celery_app.task(name="run")
                def run(pid, request_id=None): ...
                """,
            "app/svc.py": """
                from app.tasks import run

                def submit():
                    run.delay("1", request_id="r")
                """,
            "app/svc2.py": """
                import app.tasks as t

                def go():
                    t.run.delay(1)
                """,
        }
    )
    assert calls_from(g, "app/svc.py::submit") == {"app/tasks.py::run"}
    assert calls_from(g, "app/svc2.py::go") == {"app/tasks.py::run"}


def test_only_delay_and_apply_async_on_decorated_functions_resolve():
    g = build(
        {
            "app.py": """
            from celery import shared_task

            @shared_task
            def work(): ...

            def plain(): ...

            def f():
                work.s(1)
                plain.delay()
            """
        }
    )
    assert calls_from(g, "app.py::f") == set()
    u = unresolved(g)
    assert u[("app.py::f", "work.s")] == R.NOT_FOUND
    assert u[("app.py::f", "plain.delay")] == R.NOT_FOUND


def test_a_parameter_named_like_a_task_does_not_link():
    g = build(
        {
            "app.py": """
            from celery import shared_task

            @shared_task
            def work(): ...

            def f(work):
                work.delay()
            """
        }
    )
    assert calls_from(g, "app.py::f") == set()
