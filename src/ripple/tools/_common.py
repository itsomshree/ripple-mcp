from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, Field

from ripple.analysis.entity import (
    GraphFinder,
    NodeFinder,
    ResolveStatus,
    resolve_entity,
)
from ripple.analysis.impact import CodeGraphView, GraphView
from ripple.analysis.neo4j_view import GraphQueryView
from ripple.graph.builder import CodeGraph
from ripple.graph.query import GraphQuery
from ripple.graph.schema import Edge, Node, NodeType
from ripple.graph.store import GraphStore

DEFAULT_LIST_LIMIT = 25
MAX_HOPS_LIMIT = 15
MAX_LIST_LIMIT = 100


class ToolStatus(StrEnum):
    OK = "ok"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    NOT_SCANNED = "not_scanned"
    SCAN_IN_PROGRESS = "scan_in_progress"


class Entity(BaseModel):
    id: str
    kind: str
    name: str


class Item(BaseModel):
    id: str
    kind: str
    hops: int
    via: str | None = None
    parent: str | None = None
    line: int | None = None


class ItemGroup(BaseModel):
    total: int
    items: list[Item]  # nearest first, capped
    more: bool


class ToolResult(BaseModel):
    status: ToolStatus = ToolStatus.OK
    message: str | None = None
    candidates: list[Entity] = Field(default_factory=list)


_R = TypeVar("_R", bound=ToolResult)


class Problem(ToolResult):
    """A ToolResult that says something is wrong with the request."""

    def into(self, cls: type[_R]) -> _R:
        return cls.model_validate(self.model_dump())


# access to a graph
@dataclass(frozen=True)
class GraphAccess:
    view: GraphView
    finder: NodeFinder
    is_empty: Callable[[], bool]

    @classmethod
    def from_graph(cls, graph: CodeGraph) -> GraphAccess:
        return cls(CodeGraphView(graph), GraphFinder(graph), lambda: not graph.nodes)

    @classmethod
    def from_store(cls, store: GraphStore) -> GraphAccess:
        query = GraphQuery(store)
        return cls(GraphQueryView(query), query, lambda: not store.counts()[0])


# shaping
def entity_of(node: Node) -> Entity:
    return Entity(id=node.id, kind=node.type.value, name=node.name)


def make_item(node: Node, *, hops: int, parent: str | None, via: Edge | None) -> Item:
    return Item(
        id=node.id,
        kind=node.type.value,
        hops=hops,
        via=via.type.value if via is not None else None,
        parent=parent,
        line=getattr(node, "line_start", None),
    )


def make_group(items: list[Item], limit: int = DEFAULT_LIST_LIMIT) -> ItemGroup:
    limit = max(0, limit)
    return ItemGroup(total=len(items), items=items[:limit], more=len(items) > limit)


def clamp_hops(hops: int) -> int:
    return max(1, min(hops, MAX_HOPS_LIMIT))


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


# resolving the user's entity string
def resolve(
    access: GraphAccess,
    query: str,
    *,
    types: Iterable[NodeType] | None = None,
) -> Node | Problem:
    r = resolve_entity(access.finder, query, types=types)
    if r.status is ResolveStatus.OK and r.node is not None:
        return r.node

    candidates = [entity_of(n) for n in r.candidates]
    if r.status is ResolveStatus.AMBIGUOUS:
        return Problem(
            status=ToolStatus.AMBIGUOUS,
            message=(
                f"{query!r} matches several nodes; call again with the id of "
                "one of the candidates."
            ),
            candidates=candidates,
        )
    if access.is_empty():
        return Problem(
            status=ToolStatus.NOT_SCANNED,
            message="The graph is empty. Call scan_repo first.",
        )
    message = f"Nothing matches {query!r}."
    if candidates:
        message += " Did you mean one of the candidates?"
    return Problem(status=ToolStatus.NOT_FOUND, message=message, candidates=candidates)


def unsupported(message: str) -> Problem:
    return Problem(status=ToolStatus.UNSUPPORTED, message=message)
