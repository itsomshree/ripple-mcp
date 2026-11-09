from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from ripple.analysis.impact import GraphView, Group, ImpactResult, classify
from ripple.graph.schema import (
    APIEndpointNode,
    ClassNode,
    DBTableNode,
    EdgeType,
    FunctionNode,
)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class RiskConfig:
    w_endpoints: float = 0.35
    w_tables: float = 0.25
    w_coverage: float = 0.25
    w_fan_in: float = 0.15

    cap_endpoints: int = 8
    cap_tables: int = 4
    cap_fan_in: int = 10

    medium_at: float = 25.0
    high_at: float = 50.0
    critical_at: float = 75.0

    def __post_init__(self) -> None:
        weights = (self.w_endpoints, self.w_tables, self.w_coverage, self.w_fan_in)
        if any(w < 0 for w in weights) or not math.isclose(
            sum(weights), 1.0, abs_tol=1e-9
        ):
            raise ValueError("risk weights must be non-negative and sum to 1")
        if min(self.cap_endpoints, self.cap_tables, self.cap_fan_in) < 1:
            raise ValueError("risk caps must be >= 1")
        if not 0 < self.medium_at < self.high_at < self.critical_at <= 100:
            raise ValueError("risk thresholds must be ascending within (0, 100]")


DEFAULT_CONFIG = RiskConfig()


class RiskFactor(BaseModel):
    name: str
    raw: float | None
    normalized: float
    weight: float
    contribution: float
    explanation: str


class RiskAssessment(BaseModel):
    score: float
    level: RiskLevel
    factors: list[RiskFactor]
    caveats: list[str] = Field(default_factory=list)


# pure maths
def saturate(n: float, cap: int) -> float:
    """Diminishing returns: 0 at 0, 1 at `cap` and beyond"""
    if n <= 0:
        return 0.0
    return min(1.0, math.log1p(n) / math.log1p(cap))


def coverage_gap(covered: int, considered: int) -> float | None:
    """Uncovered fraction, or None when there is nothing to judge"""
    if considered <= 0:
        return None
    return 1.0 - covered / considered


def level_for(score: float, config: RiskConfig = DEFAULT_CONFIG) -> RiskLevel:
    if score >= config.critical_at:
        return RiskLevel.CRITICAL
    if score >= config.high_at:
        return RiskLevel.HIGH
    if score >= config.medium_at:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _factor(
    name: str, raw: float | None, normalized: float, weight: float, why: str
) -> RiskFactor:
    return RiskFactor(
        name=name,
        raw=raw,
        normalized=normalized,
        weight=weight,
        contribution=weight * normalized * 100,
        explanation=why,
    )


def build_factors(
    *,
    endpoints: int,
    tables: int,
    covered: int,
    considered: int,
    fan_in: int,
    config: RiskConfig = DEFAULT_CONFIG,
) -> list[RiskFactor]:
    gap = coverage_gap(covered, considered)
    if gap is None:
        coverage_why = "no endpoints or start nodes to check"
    else:
        coverage_why = f"{covered} of {considered} covered by tests"

    return [
        _factor(
            "endpoints",
            endpoints,
            saturate(endpoints, config.cap_endpoints),
            config.w_endpoints,
            f"{_plural(endpoints, 'endpoint')} affected",
        ),
        _factor(
            "tables",
            tables,
            saturate(tables, config.cap_tables),
            config.w_tables,
            f"{_plural(tables, 'table')} touched by the changed code",
        ),
        _factor(
            "coverage_gap",
            gap,
            gap or 0.0,
            config.w_coverage,
            coverage_why,
        ),
        _factor(
            "fan_in",
            fan_in,
            saturate(fan_in, config.cap_fan_in),
            config.w_fan_in,
            f"{_plural(fan_in, 'direct dependent')}",
        ),
    ]


def assemble(
    factors: list[RiskFactor],
    caveats: list[str] | None = None,
    config: RiskConfig = DEFAULT_CONFIG,
) -> RiskAssessment:
    score = round(min(100.0, max(0.0, sum(f.contribution for f in factors))), 1)
    return RiskAssessment(
        score=score,
        level=level_for(score, config),
        factors=factors,
        caveats=list(caveats or []),
    )


# wiring
_FAN_IN_EDGES = frozenset(
    {EdgeType.CALLS, EdgeType.DEPENDS_ON, EdgeType.QUERIES, EdgeType.MAPS_TO}
)
_FAN_IN_GROUPS = frozenset({Group.FUNCTIONS, Group.CLASSES})
_COVERABLE = frozenset({Group.FUNCTIONS, Group.CLASSES, Group.ENDPOINTS})


def assess_risk(
    view: GraphView,
    result: ImpactResult,
    config: RiskConfig = DEFAULT_CONFIG,
) -> RiskAssessment:
    endpoints = _endpoints(result)
    covered, considered = _coverage(view, result, endpoints)
    factors = build_factors(
        endpoints=len(endpoints),
        tables=len(_tables(result)),
        covered=covered,
        considered=considered,
        fan_in=_fan_in(result),
        config=config,
    )
    return assemble(factors, _caveats(result, list(endpoints.values())), config)


def _endpoints(result: ImpactResult) -> dict[str, APIEndpointNode]:
    nodes = [e.node for e in result.endpoints] + list(result.starts)
    return {n.id: n for n in nodes if isinstance(n, APIEndpointNode)}


def _tables(result: ImpactResult) -> set[str]:
    nodes = [e.node for e in result.tables] + list(result.starts)
    return {n.id for n in nodes if isinstance(n, DBTableNode)}


def _fan_in(result: ImpactResult) -> int:
    """Dependents attached directly to the changed surface"""
    surface = {s.id for s in result.starts}
    for e in result.entries.values():
        if e.via.type == EdgeType.DEFINED_IN or e.group == Group.TABLES:
            surface.add(e.node.id)
    return sum(
        1
        for e in result.entries.values()
        if e.group in _FAN_IN_GROUPS
        and e.via.type in _FAN_IN_EDGES
        and e.parent in surface
    )


def _coverage(
    view: GraphView,
    result: ImpactResult,
    endpoints: dict[str, APIEndpointNode],
) -> tuple[int, int]:
    """(covered, considered) over the start nodes and affected endpoints"""
    ids = set(endpoints)
    for s in result.starts:
        if (
            isinstance(s, (FunctionNode, ClassNode, APIEndpointNode))
            and classify(view, s) in _COVERABLE
        ):
            ids.add(s.id)
    if not ids:
        return 0, 0
    hit = {e.dst for e in view.edges(ids, {EdgeType.TESTS}, reverse=True)}
    return len(hit & ids), len(ids)


def _caveats(result: ImpactResult, endpoints: list[APIEndpointNode]) -> list[str]:
    out: list[str] = []
    if result.truncated:
        out.append("Result hit the result cap; the counts are lower bounds.")
    if result.depth_limited:
        out.append(
            f"More dependents exist beyond {result.max_hops} hops; "
            "the counts are lower bounds."
        )
    inexact = sum(not ep.exact for ep in endpoints)
    if inexact:
        out.append(
            f"{_plural(inexact, 'affected endpoint')} may have an incomplete "
            "route (non-literal prefix or mount cycle)."
        )
    unmounted = sum(not ep.mounted for ep in endpoints)
    if unmounted:
        out.append(
            f"{_plural(unmounted, 'affected endpoint')} never included in an app."
        )
    if not result.entries:
        out.append(
            "No dependents found. That does not prove the change is safe: "
            "inheritance, callbacks, fixtures and annotation references "
            "are not tracked."
        )
    elif not result.tests:
        out.append("No tests reach the changed code.")
    return out
