from __future__ import annotations

from pydantic import BaseModel, Field

from ripple.analysis.impact import (
    DEFAULT_MAX_RESULTS,
    GraphView,
    ImpactEntry,
    ImpactResult,
    impact,
)
from ripple.analysis.request_flow import UnsupportedStart
from ripple.graph.schema import (
    ClassNode,
    EdgeType,
    Evidence,
    FunctionNode,
    Node,
    TestNode,
)

DEFAULT_MAX_HOPS = 8


class AffectedTest(BaseModel):
    id: str
    name: str
    file: str
    pytest_id: str
    hops: int
    evidence: Evidence | None
    path: list[str]


class HelperRef(BaseModel):
    id: str
    file: str
    hops: int


class AffectedTests(BaseModel):
    starts: list[Node]
    tests: list[AffectedTest] = Field(default_factory=list)
    helpers: list[HelperRef] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    max_hops: int = DEFAULT_MAX_HOPS
    truncated: bool = False
    depth_limited: bool = False
    caveats: list[str] = Field(default_factory=list)


def pytest_id(test: TestNode) -> str:
    parts = [test.file_path]
    if test.class_name:
        parts.append(test.class_name)
    parts.append(test.name)
    return "::".join(parts)


def _evidence(entry: ImpactEntry) -> Evidence | None:
    if entry.via.type == EdgeType.CALLS:
        return Evidence.CALL
    return entry.via.via


def _path(result: ImpactResult, node_id: str) -> list[str]:
    steps = result.path_to(node_id)
    return [steps[0].src, *(s.dst for s in steps)]


def affected_from_impact(result: ImpactResult) -> AffectedTests:
    tests: list[AffectedTest] = []
    helpers: list[HelperRef] = []
    for entry in result.tests:
        node = entry.node
        if isinstance(node, TestNode):
            tests.append(
                AffectedTest(
                    id=node.id,
                    name=node.name,
                    file=node.file_path,
                    pytest_id=pytest_id(node),
                    hops=entry.hops,
                    evidence=_evidence(entry),
                    path=_path(result, node.id),
                )
            )
        elif isinstance(node, (FunctionNode, ClassNode)):
            helpers.append(HelperRef(id=node.id, file=node.file_path, hops=entry.hops))

    tests.sort(key=lambda t: (t.hops, t.pytest_id))
    return AffectedTests(
        starts=result.starts,
        tests=tests,
        helpers=helpers,
        files=list(dict.fromkeys(t.file for t in tests)),
        max_hops=result.max_hops,
        truncated=result.truncated,
        depth_limited=result.depth_limited,
        caveats=_caveats(result, tests),
    )


def find_affected_tests(
    view: GraphView,
    starts: list[Node],
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    max_results: int = DEFAULT_MAX_RESULTS,
    include_importers: bool = False,
) -> AffectedTests:
    for s in starts:
        if isinstance(s, TestNode):
            raise UnsupportedStart(
                f"{s.id} is already a test; ask about the code it covers instead"
            )
    return affected_from_impact(
        impact(
            view,
            starts,
            max_hops=max_hops,
            max_results=max_results,
            include_importers=include_importers,
        )
    )


def _caveats(result: ImpactResult, tests: list[AffectedTest]) -> list[str]:
    out: list[str] = []
    if result.truncated:
        out.append("Result hit the result cap; the test list is a lower bound.")
    if result.depth_limited:
        out.append(
            f"More dependents exist beyond {result.max_hops} hops; "
            "the test list is a lower bound."
        )
    if not tests:
        out.append(
            "No tests reach the changed code. Tests that reach it only "
            "through pytest fixtures are not linked."
        )
    return out
