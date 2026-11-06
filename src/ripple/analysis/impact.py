from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from ripple.graph.builder import CodeGraph
from ripple.graph.schema import (
    APIEndpointNode,
    ClassNode,
    DBTableNode,
    Edge,
    EdgeType,
    FileNode,
    FunctionNode,
    Node,
    TestNode,
)

DEFAULT_MAX_HOPS = 5
DEFAULT_MAX_RESULTS = 500


class Group(StrEnum):
    FUNCTIONS = "functions"
    CLASSES = "classes"
    ENDPOINTS = "endpoints"
    TABLES = "tables"
    TESTS = "tests"
    FILES = "files"


class GraphView(Protocol):
    def node(self, node_id: str) -> Node | None: ...

    def edges(
        self, node_ids: Collection[str], types: Collection[EdgeType], *, reverse: bool
    ) -> list[Edge]:
        """Edges whose src is in node_ids, sorted"""
        ...


def edge_key(e: Edge) -> tuple[str, ...]:
    return (
        e.type.value,
        e.src,
        e.dst,
        e.access.value if e.access else "",
        e.via.value if e.via else "",
    )


class CodeGraphView:
    """GraphView over an in-memory CodeGraph"""

    def __init__(self, graph: CodeGraph) -> None:
        self._g = graph
        self._out: dict[tuple[EdgeType, str], list[Edge]] = defaultdict(list)
        self._in: dict[tuple[EdgeType, str], list[Edge]] = defaultdict(list)
        for e in graph.edges:
            self._out[(e.type, e.src)].append(e)
            self._in[(e.type, e.dst)].append(e)

    def node(self, node_id: str) -> Node | None:
        return self._g.nodes.get(node_id)

    def edges(
        self, node_ids: Collection[str], types: Collection[EdgeType], *, reverse: bool
    ) -> list[Edge]:
        index = self._in if reverse else self._out
        found = [e for t in types for nid in node_ids for e in index.get((t, nid), ())]
        return sorted(found, key=edge_key)


# result models
class ImpactEntry(BaseModel):
    node: Node
    group: Group
    hops: int
    parent: str
    via: Edge


class ImpactStep(BaseModel):
    src: str
    dst: str
    edge: Edge


class ImpactResult(BaseModel):
    starts: list[Node]
    context: list[Node] = Field(default_factory=list)
    entries: dict[str, ImpactEntry] = Field(default_factory=dict)
    max_hops: int = DEFAULT_MAX_HOPS
    truncated: bool = False
    depth_limited: bool = False

    def _in(self, group: Group) -> list[ImpactEntry]:
        return sorted(
            (e for e in self.entries.values() if e.group == group),
            key=lambda e: (e.hops, e.node.id),
        )

    @property
    def functions(self) -> list[ImpactEntry]:
        return self._in(Group.FUNCTIONS)

    @property
    def classes(self) -> list[ImpactEntry]:
        return self._in(Group.CLASSES)

    @property
    def endpoints(self) -> list[ImpactEntry]:
        return self._in(Group.ENDPOINTS)

    @property
    def tables(self) -> list[ImpactEntry]:
        return self._in(Group.TABLES)

    @property
    def tests(self) -> list[ImpactEntry]:
        return self._in(Group.TESTS)

    @property
    def files(self) -> list[ImpactEntry]:
        return self._in(Group.FILES)

    def path_to(self, node_id: str) -> list[ImpactStep]:
        """Steps from a start node to node_id"""
        if node_id not in self.entries:
            if any(s.id == node_id for s in self.starts):
                return []
            raise KeyError(node_id)
        steps: list[ImpactStep] = []
        cur = node_id
        while cur in self.entries:
            e = self.entries[cur]
            steps.append(ImpactStep(src=e.parent, dst=cur, edge=e.via))
            cur = e.parent
        return steps[::-1]


def classify(view: GraphView, node: Node) -> Group:
    match node:
        case TestNode():
            return Group.TESTS
        case FileNode():
            return Group.FILES
        case APIEndpointNode():
            return Group.ENDPOINTS
        case DBTableNode():
            return Group.TABLES
        case FunctionNode() | ClassNode():
            home = view.node(node.file_path)
            if isinstance(home, FileNode) and home.is_test:
                return Group.TESTS
            return Group.FUNCTIONS if isinstance(node, FunctionNode) else Group.CLASSES
    raise TypeError(type(node))


# seeds
@dataclass(frozen=True)
class Seed:
    node_id: str
    parent: str
    via: Edge
    hops: int = 1


def seeds_for(
    view: GraphView,
    start: Node,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    include_importers: bool = False,
) -> list[Seed]:
    """Extra nodes a change to `start` affects before the generic spread runs"""
    seeds: list[Seed] = []
    if isinstance(start, ClassNode):
        seeds += _class_seeds(view, start)
    elif isinstance(start, DBTableNode):
        for e in view.edges(
            [start.id], {EdgeType.QUERIES, EdgeType.MAPS_TO}, reverse=True
        ):
            seeds.append(Seed(e.src, start.id, e))
    elif isinstance(start, FileNode):
        for e in view.edges([start.id], {EdgeType.DEFINED_IN}, reverse=True):
            seeds.append(Seed(e.src, start.id, e))
        seeds += _importer_seeds(view, start.id, start.id, max_hops)
    if include_importers and isinstance(start, (FunctionNode, ClassNode, TestNode)):
        seeds += _importer_seeds(view, start.file_path, start.id, max_hops)
    return _dedupe(seeds, exclude={start.id})


def context_for(view: GraphView, start: Node) -> list[Node]:
    """Nodes shown for orientation but not counted as affected"""
    if not isinstance(start, APIEndpointNode):
        return []
    handlers = view.edges([start.id], {EdgeType.HANDLES}, reverse=False)
    return [n for e in handlers if (n := view.node(e.dst)) is not None]


def _class_seeds(view: GraphView, cls: ClassNode) -> list[Seed]:
    out: list[Seed] = []
    for e in view.edges([cls.file_path], {EdgeType.DEFINED_IN}, reverse=True):
        n = view.node(e.src)
        if isinstance(n, (FunctionNode, TestNode)) and n.class_name == cls.qualname:
            out.append(
                Seed(n.id, cls.id, Edge(type=EdgeType.DEFINED_IN, src=n.id, dst=cls.id))
            )
    for e in view.edges([cls.id], {EdgeType.MAPS_TO}, reverse=False):
        out.append(Seed(e.dst, cls.id, e))
        for r in view.edges(
            [e.dst], {EdgeType.QUERIES, EdgeType.MAPS_TO}, reverse=True
        ):
            out.append(Seed(r.src, e.dst, r, hops=2))
    return out


def _importer_seeds(
    view: GraphView, file_id: str, parent: str, max_hops: int
) -> list[Seed]:
    """Files importing file_id"""
    out: list[Seed] = []
    seen = {file_id}
    frontier = [file_id]
    hops = 1
    while frontier and hops <= max_hops:
        nxt: list[str] = []
        for e in view.edges(frontier, {EdgeType.IMPORTS}, reverse=True):
            if e.src in seen:
                continue
            seen.add(e.src)
            nxt.append(e.src)
            out.append(Seed(e.src, parent if hops == 1 else e.dst, e, hops))
        frontier, hops = nxt, hops + 1
    return out


def _dedupe(seeds: list[Seed], exclude: set[str]) -> list[Seed]:
    best: dict[str, Seed] = {}
    for s in seeds:
        if s.node_id in exclude:
            continue
        cur = best.get(s.node_id)
        if cur is None or s.hops < cur.hops:
            best[s.node_id] = s
    return [best[k] for k in sorted(best)]


SPREAD = (EdgeType.CALLS, EdgeType.DEPENDS_ON, EdgeType.HANDLES, EdgeType.TESTS)
_OWN = {EdgeType.QUERIES, EdgeType.MAPS_TO}


def impact(
    view: GraphView,
    starts: list[Node],
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    max_results: int = DEFAULT_MAX_RESULTS,
    include_importers: bool = False,
) -> ImpactResult:
    starts = list({n.id: n for n in starts}.values())
    start_ids = {s.id for s in starts}
    result = ImpactResult(
        starts=starts,
        context=[c for s in starts for c in context_for(view, s)],
        max_hops=max_hops,
    )
    if max_hops < 1:
        return result
    entries = result.entries

    def admit(node_id: str, parent: str, edge: Edge, hops: int) -> bool:
        if node_id in start_ids or node_id in entries or hops > max_hops:
            return False
        node = view.node(node_id)
        if node is None:
            return False
        if len(entries) >= max_results:
            result.truncated = True
            return False
        entries[node_id] = ImpactEntry(
            node=node, group=classify(view, node), hops=hops, parent=parent, via=edge
        )
        return True

    seeds: list[Seed] = []
    for s in starts:
        seeds += seeds_for(
            view, s, max_hops=max_hops, include_importers=include_importers
        )
    by_hop: dict[int, list[Seed]] = defaultdict(list)
    for sd in seeds:
        by_hop[sd.hops].append(sd)

    base = {
        s.id: 0
        for s in starts
        if isinstance(s, (FunctionNode, TestNode, ClassNode, FileNode))
    }
    base |= {sd.node_id: sd.hops for sd in seeds if sd.via.type == EdgeType.DEFINED_IN}
    own = [(base[e.src] + 1, e) for e in view.edges(list(base), _OWN, reverse=False)]
    for hops, e in sorted(own, key=lambda p: p[0]):
        admit(e.dst, e.src, e, hops)

    frontier = sorted(start_ids)
    last_seed_hop = max(by_hop, default=0)
    for h in range(1, max_hops + 1):
        if not frontier and h > last_seed_hop:
            break
        new: list[str] = []
        for sd in by_hop.get(h, ()):
            if admit(sd.node_id, sd.parent, sd.via, h):
                new.append(sd.node_id)
        for e in view.edges(frontier, SPREAD, reverse=True):
            if admit(e.src, e.dst, e, h):
                new.append(e.src)
        if result.truncated:
            break
        frontier = new
    if not result.truncated:
        result.depth_limited = any(sd.hops > max_hops for sd in seeds) or any(
            e.src not in start_ids and e.src not in entries
            for e in view.edges(frontier, SPREAD, reverse=True)
        )
    return result
