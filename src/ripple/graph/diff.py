from __future__ import annotations

from dataclasses import dataclass

from ripple.graph.builder import CodeGraph
from ripple.graph.schema import Edge, Node


@dataclass(frozen=True)
class EdgeChange:
    edge: Edge
    src_label: str
    dst_label: str


@dataclass(frozen=True)
class GraphDelta:
    nodes_added: tuple[Node, ...] = ()
    nodes_changed: tuple[Node, ...] = ()
    nodes_removed: tuple[Node, ...] = ()
    edges_added: tuple[EdgeChange, ...] = ()
    edges_removed: tuple[EdgeChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(self.counts().values())

    def counts(self) -> dict[str, int]:
        return {
            "nodes_added": len(self.nodes_added),
            "nodes_changed": len(self.nodes_changed),
            "nodes_removed": len(self.nodes_removed),
            "edges_added": len(self.edges_added),
            "edges_removed": len(self.edges_removed),
        }


def _edge_order(e: Edge) -> tuple[str, ...]:
    return (
        e.type.value,
        e.src,
        e.dst,
        e.access.value if e.access else "",
        e.via.value if e.via else "",
    )


def _changes(edges: set[Edge], graph: CodeGraph) -> tuple[EdgeChange, ...]:
    return tuple(
        EdgeChange(
            edge=e,
            src_label=graph.nodes[e.src].type.value,
            dst_label=graph.nodes[e.dst].type.value,
        )
        for e in sorted(edges, key=_edge_order)
    )


def diff_graphs(old: CodeGraph, new: CodeGraph) -> GraphDelta:
    added: list[Node] = []
    changed: list[Node] = []
    removed: list[Node] = []
    retyped: set[str] = set()

    for nid in sorted(old.nodes.keys() | new.nodes.keys()):
        before, after = old.nodes.get(nid), new.nodes.get(nid)
        if before is None:
            assert after is not None
            added.append(after)
        elif after is None:
            removed.append(before)
        elif before.type != after.type:
            removed.append(before)
            added.append(after)
            retyped.add(nid)
        elif before != after:
            changed.append(after)

    def touches(e: Edge) -> bool:
        return e.src in retyped or e.dst in retyped

    gone = (old.edges - new.edges) | {e for e in old.edges if touches(e)}
    fresh = (new.edges - old.edges) | {e for e in new.edges if touches(e)}
    return GraphDelta(
        nodes_added=tuple(added),
        nodes_changed=tuple(changed),
        nodes_removed=tuple(removed),
        edges_added=_changes(fresh, new),
        edges_removed=_changes(gone, old),
    )
