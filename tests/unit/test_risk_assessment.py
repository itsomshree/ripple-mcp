import textwrap
from pathlib import Path

import pytest

from ripple.analysis.impact import CodeGraphView, impact
from ripple.analysis.risk import RiskLevel, assemble, assess_risk, build_factors
from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import Edge, FileNode, FunctionNode
from ripple.graph.schema import EdgeType as T
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file, parse_fastapi_source
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file, parse_python_source

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
U = "app/services/user_service.py::UserService"
TU = "tests/test_users.py::"


@pytest.fixture(scope="module")
def view():
    b = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        b.add(
            parse_python_file(p, rel),
            parse_fastapi_file(p, rel),
            parse_db_file(p, rel),
            parse_coverage_file(p, rel),
        )
    return CodeGraphView(b.build())


def assess(view, node_id, **kw):
    kw.setdefault("max_hops", 8)
    return assess_risk(view, impact(view, [view.node(node_id)], **kw))


def by_name(a):
    return {f.name: f for f in a.factors}


def has(a, text):
    return any(text in c for c in a.caveats)


# sample fixture
def test_service_method(view):
    a = assess(view, f"{U}.get")
    f = by_name(a)
    assert (f["endpoints"].raw, f["tables"].raw, f["fan_in"].raw) == (2, 1, 2)
    assert f["coverage_gap"].raw == 0.0
    assert f["coverage_gap"].explanation == "3 of 3 covered by tests"
    expected = assemble(
        build_factors(endpoints=2, tables=1, covered=3, considered=3, fan_in=2)
    )
    assert a.score == expected.score
    assert a.level is RiskLevel.MEDIUM
    assert a.caveats == []


def test_model_class_is_riskier_than_a_service_method(view):
    model = assess(view, "app/models.py::User")
    f = by_name(model)
    assert (f["endpoints"].raw, f["tables"].raw, f["fan_in"].raw) == (5, 1, 4)
    assert f["coverage_gap"].explanation == "2 of 6 covered by tests"
    assert model.level is RiskLevel.HIGH
    assert model.score > assess(view, f"{U}.get").score


def test_file_start_counts_callers_of_its_symbols(view):
    f = by_name(assess(view, "app/services/user_service.py"))
    assert f["fan_in"].raw == 5
    assert f["tables"].raw == 1


def test_table_start_counts_itself_and_its_direct_users(view):
    f = by_name(assess(view, "table: users"))
    assert f["tables"].raw == 1
    assert f["fan_in"].raw == 5  # four methods + the User model


def test_untested_leaf_endpoint(view):
    a = assess(view, "endpoint::GET /health")
    f = by_name(a)
    assert f["endpoints"].raw == 1
    assert f["coverage_gap"].raw == 1.0
    assert f["fan_in"].raw == 0
    assert has(a, "No dependents found")


def test_test_function_start_has_no_coverage_to_judge(view):
    a = assess(view, f"{TU}test_get_user_service")
    cov = by_name(a)["coverage_gap"]
    assert cov.raw is None
    assert "no endpoints" in cov.explanation


# handcrafted graphs
def _fn(path, q):
    return FunctionNode(
        id=f"{path}::{q}", name=q, qualname=q, file_path=path, line_start=1, line_end=2
    )


def _calls(pairs):
    g = CodeGraph()
    g.add_node(FileNode(id="m.py", name="m.py", path="m.py", content_hash="x"))
    for name in sorted({x for p in pairs for x in p}):
        g.add_node(_fn("m.py", name))
    for caller, callee in pairs:
        g.add_edge(Edge(type=T.CALLS, src=f"m.py::{caller}", dst=f"m.py::{callee}"))
    return CodeGraphView(g)


def assess_calls(pairs, start, **kw):
    v = _calls(pairs)
    return assess_risk(v, impact(v, [v.node(f"m.py::{start}")], **kw))


def test_untested_function_nobody_calls():
    a = assess_calls([("B", "A")], "B")
    f = by_name(a)
    assert f["fan_in"].raw == 0
    assert f["coverage_gap"].explanation == "0 of 1 covered by tests"
    assert has(a, "No dependents found")


def test_callers_without_tests():
    a = assess_calls([(f"c{i}", "A") for i in range(10)], "A")
    assert by_name(a)["fan_in"].raw == 10
    assert a.caveats == ["No tests reach the changed code."]


def test_truncation_is_a_caveat_and_lowers_the_count():
    a = assess_calls([(f"c{i}", "A") for i in range(10)], "A", max_results=4)
    assert by_name(a)["fan_in"].raw == 4
    assert has(a, "result cap")


def test_depth_limit_is_a_caveat():
    pairs = [("B", "A"), ("C", "B"), ("D", "C")]
    a = assess_calls(pairs, "A", max_hops=2)
    assert has(a, "beyond 2 hops")


# endpoint flags
def _app_view(src):
    text = textwrap.dedent(src).lstrip("\n")
    b = GraphBuilder()
    b.add(parse_python_source(text, "app.py"), parse_fastapi_source(text, "app.py"))
    return CodeGraphView(b.build())


def test_unmounted_endpoint_is_flagged():
    v = _app_view("""
        from fastapi import APIRouter
        router = APIRouter(prefix="/orphan")

        @router.get("/x")
        def h(): ...
        """)
    a = assess_risk(v, impact(v, [v.node("endpoint::GET /orphan/x")]))
    assert has(a, "never included in an app")
    assert not has(a, "incomplete route")


def test_inexact_endpoint_is_flagged():
    v = _app_view("""
        from fastapi import FastAPI, APIRouter
        app = FastAPI()
        router = APIRouter(prefix=PREFIX)

        @router.get("/x")
        def h(): ...

        app.include_router(router)
        """)
    a = assess_risk(v, impact(v, [v.node("endpoint::GET /x")]))
    assert has(a, "incomplete route")
    assert not has(a, "never included in an app")
