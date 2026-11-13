from __future__ import annotations

import shlex

from pydantic import Field

from ripple.analysis.affected_tests import (
    DEFAULT_MAX_HOPS,
    AffectedTest,
    AffectedTests,
    HelperRef,
    find_affected_tests,
)
from ripple.analysis.request_flow import UnsupportedStart
from ripple.tools._common import (
    DEFAULT_LIST_LIMIT,
    Entity,
    GraphAccess,
    Problem,
    ToolResult,
    ToolStatus,
    clamp_hops,
    clamp_limit,
    entity_of,
    resolve,
)

MAX_IDS_IN_COMMAND = 20


class AffectedTestsResult(ToolResult):
    entity: Entity | None = None
    tests: list[AffectedTest] = Field(default_factory=list)
    tests_total: int = 0
    more_tests: bool = False
    helpers: list[HelperRef] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    pytest_command: str | None = None
    max_hops: int | None = None
    truncated: bool = False
    depth_limited: bool = False
    caveats: list[str] = Field(default_factory=list)


def pytest_command(found: AffectedTests) -> str | None:
    if not found.tests:
        return None
    if len(found.tests) <= MAX_IDS_IN_COMMAND:
        targets = [t.pytest_id for t in found.tests]
    else:
        targets = found.files
    return "pytest " + " ".join(shlex.quote(t) for t in targets)


def affected_tests(
    access: GraphAccess,
    entity: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    include_importers: bool = False,
    limit: int = DEFAULT_LIST_LIMIT,
) -> AffectedTestsResult:
    node = resolve(access, entity)
    if isinstance(node, Problem):
        return node.into(AffectedTestsResult)

    hops, cap = clamp_hops(max_hops), clamp_limit(limit)
    try:
        found = find_affected_tests(
            access.view, [node], max_hops=hops, include_importers=include_importers
        )
    except UnsupportedStart as exc:
        return AffectedTestsResult(status=ToolStatus.UNSUPPORTED, message=str(exc))

    return AffectedTestsResult(
        entity=entity_of(node),
        tests=found.tests[:cap],
        tests_total=len(found.tests),
        more_tests=len(found.tests) > cap,
        helpers=found.helpers[:cap],
        files=found.files,
        pytest_command=pytest_command(found),
        max_hops=hops,
        truncated=found.truncated,
        depth_limited=found.depth_limited,
        caveats=found.caveats,
    )
