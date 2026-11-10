from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, Field

from ripple.analysis.impact import GraphView, Group, ImpactStep, classify
from ripple.graph.schema import (
    AccessMode,
    APIEndpointNode,
    DBTableNode,
    Edge,
    EdgeType,
    FunctionNode,
    Node,
)

DEFAULT_MAX_HOPS = 6
DEFAULT_MAX_RESULTS = 200

_FOLLOW = frozenset({EdgeType.CALLS, EdgeType.DEPENDS_ON, EdgeType.HANDLES})
_QUERIES = frozenset({EdgeType.QUERIES})


class UnsupportedStart(ValueError):
    """The start node is not something a request flow can begin at"""


class TableAccessRef(BaseModel):
    table: str
    access: AccessMode


class FlowStep(BaseModel):
    node: Node
    hops: int
    parent: str
    via: Edge
    tables: list[TableAccessRef] = Field(default_factory=list)


class TableTouch(BaseModel):
    table: str
    name: str
    hops: int
    access: list[AccessMode]
    touched_by: list[str]


class FlowResult(BaseModel):
    start: Node
    start_tables: list[TableAccessRef] = Field(default_factory=list)
    steps: list[FlowStep] = Field(default_factory=list)
    tables: list[TableTouch] = Field(default_factory=list)
    max_hops: int = DEFAULT_MAX_HOPS
    truncated: bool = False
    depth_limited: bool = False
    caveats: list[str] = Field(default_factory=list)

    def path_to(self, node_id: str) -> list[ImpactStep]:
        by_id = {s.node.id: s for s in self.steps}
        if node_id not in by_id:
            if node_id == self.start.id:
                return []
            raise KeyError(node_id)
        out: list[ImpactStep] = []
        cur = node_id
        while cur in by_id:
            s = by_id[cur]
            out.append(ImpactStep(src=s.parent, dst=cur, edge=s.via))
            cur = s.parent
        return out[::-1]


def can_trace(view: GraphView, node: Node) -> bool:
    if isinstance(node, APIEndpointNode):
        return True
    return isinstance(node, FunctionNode) and classify(view, node) is Group.FUNCTIONS


def trace_flow(
    view: GraphView,
    start: Node,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> FlowResult:
    if not can_trace(view, start):
        raise UnsupportedStart(
            f"{start.id} is a {start.type.value}; a request flow starts from "
            "an endpoint or a function"
        )
    steps, truncated, depth_limited = _walk(view, start, max_hops, max_results)
    result = FlowResult(
        start=start,
        steps=steps,
        max_hops=max_hops,
        truncated=truncated,
        depth_limited=depth_limited,
    )
    _attach_tables(view, result)
    result.caveats = _caveats(result)
    return result


def _walk(
    view: GraphView, start: Node, max_hops: int, max_results: int
) -> tuple[list[FlowStep], bool, bool]:
    steps: list[FlowStep] = []
    seen = {start.id}
    frontier = {start.id}
    truncated = False
    for hops in range(1, max_hops + 1):
        if not frontier:
            break
        new: list[str] = []
        for e in view.edges(frontier, _FOLLOW, reverse=False):
            if e.dst in seen:
                continue
            node = view.node(e.dst)
            if node is None:
                continue
            if len(steps) >= max_results:
                truncated = True
                break
            seen.add(e.dst)
            steps.append(FlowStep(node=node, hops=hops, parent=e.src, via=e))
            new.append(e.dst)
        if truncated:
            break
        frontier = new

    depth_limited = False
    if frontier and not truncated:
        depth_limited = any(
            e.dst not in seen and view.node(e.dst) is not None
            for e in view.edges(frontier, _FOLLOW, reverse=False)
        )
    steps.sort(key=lambda s: (s.hops, s.node.id))
    return steps, truncated, depth_limited


def _attach_tables(view: GraphView, result: FlowResult) -> None:
    hops = {result.start.id: 0} | {s.node.id: s.hops for s in result.steps}
    per_owner: dict[str, list[TableAccessRef]] = defaultdict(list)
    modes: dict[str, set[AccessMode]] = defaultdict(set)
    owners: dict[str, set[str]] = defaultdict(set)
    names: dict[str, str] = {}

    for e in view.edges(list(hops), _QUERIES, reverse=False):
        table = view.node(e.dst)
        if not isinstance(table, DBTableNode) or e.access is None:
            continue
        per_owner[e.src].append(TableAccessRef(table=e.dst, access=e.access))
        modes[e.dst].add(e.access)
        owners[e.dst].add(e.src)
        names[e.dst] = table.name

    result.start_tables = per_owner.get(result.start.id, [])
    for step in result.steps:
        step.tables = per_owner.get(step.node.id, [])
    result.tables = sorted(
        (
            TableTouch(
                table=t,
                name=names[t],
                hops=min(hops[o] for o in owners[t]),
                access=sorted(modes[t], key=lambda m: m.value),
                touched_by=sorted(owners[t], key=lambda o: (hops[o], o)),
            )
            for t in owners
        ),
        key=lambda t: (t.hops, t.table),
    )


def _caveats(result: FlowResult) -> list[str]:
    out: list[str] = []
    if result.truncated:
        out.append("Result hit the result cap; the flow is a lower bound")
    if result.depth_limited:
        out.append(
            f"The flow continues beyond {result.max_hops} hops; "
            "the result is a lower bound."
        )
    start = result.start
    if isinstance(start, APIEndpointNode):
        if not start.exact:
            out.append(
                "The endpoint route may be incomplete "
                "(non-literal prefix or mount cycle)."
            )
        if not start.mounted:
            out.append("The endpoint's router is never included in an app.")
    if not result.tables:
        out.append(
            "No table access found along the flow. ORM relationship "
            "traversal, SQL built from imported constants and calls through "
            "local variables are not detected."
        )
    return out
