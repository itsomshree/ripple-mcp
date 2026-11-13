from __future__ import annotations

from pydantic import BaseModel, Field

from ripple.analysis.impact import DEFAULT_MAX_HOPS, ImpactEntry, impact
from ripple.analysis.risk import RiskAssessment, assess_risk
from ripple.tools._common import (
    DEFAULT_LIST_LIMIT,
    Entity,
    GraphAccess,
    ItemGroup,
    Problem,
    ToolResult,
    clamp_hops,
    clamp_limit,
    entity_of,
    make_group,
    make_item,
    resolve,
)


class FactorOut(BaseModel):
    name: str
    raw: float | None
    contribution: float
    explanation: str


class RiskOut(BaseModel):
    score: float
    level: str
    factors: list[FactorOut]
    caveats: list[str]


class WhatBreaksResult(ToolResult):
    entity: Entity | None = None
    context: list[Entity] = Field(default_factory=list)
    risk: RiskOut | None = None
    functions: ItemGroup | None = None
    classes: ItemGroup | None = None
    endpoints: ItemGroup | None = None
    tables: ItemGroup | None = None
    tests: ItemGroup | None = None
    files: ItemGroup | None = None
    max_hops: int | None = None
    truncated: bool = False
    depth_limited: bool = False


def _group(entries: list[ImpactEntry], limit: int) -> ItemGroup:
    return make_group(
        [make_item(e.node, hops=e.hops, parent=e.parent, via=e.via) for e in entries],
        limit,
    )


def _risk(a: RiskAssessment) -> RiskOut:
    return RiskOut(
        score=a.score,
        level=a.level.value,
        factors=[
            FactorOut(
                name=f.name,
                raw=f.raw,
                contribution=round(f.contribution, 1),
                explanation=f.explanation,
            )
            for f in a.factors
        ],
        caveats=a.caveats,
    )


def what_breaks_if_i_change(
    access: GraphAccess,
    entity: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    include_importers: bool = False,
    limit: int = DEFAULT_LIST_LIMIT,
) -> WhatBreaksResult:
    node = resolve(access, entity)
    if isinstance(node, Problem):
        return node.into(WhatBreaksResult)

    hops, cap = clamp_hops(max_hops), clamp_limit(limit)
    result = impact(
        access.view, [node], max_hops=hops, include_importers=include_importers
    )
    return WhatBreaksResult(
        entity=entity_of(node),
        context=[entity_of(n) for n in result.context],
        risk=_risk(assess_risk(access.view, result)),
        functions=_group(result.functions, cap),
        classes=_group(result.classes, cap),
        endpoints=_group(result.endpoints, cap),
        tables=_group(result.tables, cap),
        tests=_group(result.tests, cap),
        files=_group(result.files, cap),
        max_hops=hops,
        truncated=result.truncated,
        depth_limited=result.depth_limited,
    )
