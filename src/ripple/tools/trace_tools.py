from __future__ import annotations

from pydantic import BaseModel, Field

from ripple.analysis.request_flow import (
    DEFAULT_MAX_HOPS,
    TableAccessRef,
    UnsupportedStart,
    trace_flow,
)
from ripple.tools._common import (
    Entity,
    GraphAccess,
    Item,
    Problem,
    ToolResult,
    ToolStatus,
    clamp_hops,
    clamp_limit,
    entity_of,
    make_item,
    resolve,
)

DEFAULT_FLOW_LIMIT = 50


class TableAccessOut(BaseModel):
    table: str
    access: str


class FlowItem(Item):
    tables: list[TableAccessOut] = Field(default_factory=list)


class FlowGroup(BaseModel):
    total: int
    items: list[FlowItem]
    more: bool


class TableTouchOut(BaseModel):
    table: str
    name: str
    hops: int
    access: list[str]
    touched_by: list[str]


class TraceResult(ToolResult):
    start: Entity | None = None
    start_tables: list[TableAccessOut] = Field(default_factory=list)
    steps: FlowGroup | None = None
    tables: list[TableTouchOut] = Field(default_factory=list)
    max_hops: int | None = None
    truncated: bool = False
    depth_limited: bool = False
    caveats: list[str] = Field(default_factory=list)


def _tables(refs: list[TableAccessRef]) -> list[TableAccessOut]:
    return [TableAccessOut(table=r.table, access=r.access.value) for r in refs]


def trace_request_flow(
    access: GraphAccess,
    endpoint: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    limit: int = DEFAULT_FLOW_LIMIT,
) -> TraceResult:
    node = resolve(access, endpoint)
    if isinstance(node, Problem):
        return node.into(TraceResult)

    hops, cap = clamp_hops(max_hops), clamp_limit(limit)
    try:
        flow = trace_flow(access.view, node, max_hops=hops)
    except UnsupportedStart as exc:
        return TraceResult(status=ToolStatus.UNSUPPORTED, message=str(exc))

    items = [
        FlowItem(
            **make_item(s.node, hops=s.hops, parent=s.parent, via=s.via).model_dump(),
            tables=_tables(s.tables),
        )
        for s in flow.steps
    ]
    return TraceResult(
        start=entity_of(node),
        start_tables=_tables(flow.start_tables),
        steps=FlowGroup(total=len(items), items=items[:cap], more=len(items) > cap),
        tables=[
            TableTouchOut(
                table=t.table,
                name=t.name,
                hops=t.hops,
                access=[m.value for m in t.access],
                touched_by=t.touched_by,
            )
            for t in flow.tables
        ],
        max_hops=hops,
        truncated=flow.truncated,
        depth_limited=flow.depth_limited,
        caveats=flow.caveats,
    )
