from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, NamedTuple

from pydantic import TypeAdapter

from ripple.graph.builder import CodeGraph
from ripple.graph.schema import (
    AccessMode,
    Edge,
    EdgeType,
    Evidence,
    Node,
    NodeType,
)
from ripple.graph.store import GraphStore

_NODE = TypeAdapter(Node)

_NODES_BY_ID = " UNION ALL ".join(
    f"MATCH (n:`{label.value}`) WHERE n.id IN $ids "
    f"RETURN '{label.value}' AS label, properties(n) AS props"
    for label in NodeType
)


class Direction(StrEnum):
    OUT = "out"
    IN = "in"


class Step(NamedTuple):
    """One kind of hop a traversal may take."""

    edge_type: EdgeType
    direction: Direction


class Neighbor(NamedTuple):
    from_id: str
    edge: Edge
    node: Node


class Reached(NamedTuple):
    node: Node
    hops: int
    via: Edge
    parent: str


def node_from_row(label: str, props: dict[str, Any]) -> Node:
    return _NODE.validate_python({**props, "type": NodeType(label)})


def edge_from_row(
    edge_type: str,
    src: str,
    dst: str,
    access: str | None,
    via: str | None,
) -> Edge:
    return Edge(
        type=EdgeType(edge_type),
        src=src,
        dst=dst,
        access=AccessMode(access) if access else None,
        via=Evidence(via) if via else None,
    )


def _neighbor_key(n: Neighbor) -> tuple[str, ...]:
    e = n.edge
    return (
        n.from_id,
        e.type.value,
        e.src,
        e.dst,
        e.access.value if e.access else "",
        e.via.value if e.via else "",
        n.node.id,
    )


def _match_tier(node: Node, name: str) -> int:
    if node.id == name:
        return 0
    if getattr(node, "qualname", None) == name:
        return 1
    if node.name == name:
        return 2
    return 3


class GraphQuery:
    def __init__(self, store: GraphStore) -> None:
        self._store = store

    # lookups
    def nodes(self, ids: Iterable[str]) -> dict[str, Node]:
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return {}
        rows = self._store.execute(_NODES_BY_ID, {"ids": wanted})
        found = (node_from_row(r["label"], r["props"]) for r in rows)
        return {n.id: n for n in found}

    def node(self, node_id: str) -> Node | None:
        return self.nodes([node_id]).get(node_id)

    def find_nodes(
        self,
        name: str,
        types: Iterable[NodeType] | None = None,
        limit: int = 50,
    ) -> list[Node]:
        if not name:
            return []
        labels = list(types) if types is not None else list(NodeType)
        query = " UNION ALL ".join(
            f"MATCH (n:`{label.value}`) "
            "WHERE n.id = $q OR n.qualname = $q OR n.name = $q "
            "OR n.path = $q OR n.path ENDS WITH ('/' + $q) "
            f"RETURN '{label.value}' AS label, properties(n) AS props"
            for label in labels
        )
        if not query:
            return []
        rows = self._store.execute(query, {"q": name})
        found = [node_from_row(r["label"], r["props"]) for r in rows]
        found.sort(key=lambda n: (_match_tier(n, name), n.id))
        return found[:limit]

    # traversal
    def neighbors(self, nodes: Iterable[Node], steps: Iterable[Step]) -> list[Neighbor]:
        """One hop from every given node along every given step, sorted."""
        ids_by_label: dict[str, list[str]] = defaultdict(list)
        for n in {n.id: n for n in nodes}.values():
            ids_by_label[n.type.value].append(n.id)

        types_by_direction: dict[Direction, list[EdgeType]] = defaultdict(list)
        for s in dict.fromkeys(steps):
            types_by_direction[s.direction].append(s.edge_type)

        out: list[Neighbor] = []
        for label, ids in ids_by_label.items():
            for direction, edge_types in types_by_direction.items():
                rels = "|".join(f"`{t.value}`" for t in edge_types)
                if direction is Direction.OUT:
                    pattern = f"(a:`{label}`)-[r:{rels}]->(b)"
                else:
                    pattern = f"(a:`{label}`)<-[r:{rels}]-(b)"
                rows = self._store.execute(
                    f"MATCH {pattern} WHERE a.id IN $ids "
                    "RETURN type(r) AS type, a.id AS anchor, b.id AS other, "
                    "r.access AS access, r.via AS via, "
                    "labels(b)[0] AS label, properties(b) AS props",
                    {"ids": ids},
                )
                for row in rows:
                    src, dst = (
                        (row["anchor"], row["other"])
                        if direction is Direction.OUT
                        else (row["other"], row["anchor"])
                    )
                    out.append(
                        Neighbor(
                            from_id=row["anchor"],
                            edge=edge_from_row(
                                row["type"], src, dst, row["access"], row["via"]
                            ),
                            node=node_from_row(row["label"], row["props"]),
                        )
                    )
        out.sort(key=_neighbor_key)
        return out

    def reachable(
        self, starts: Iterable[Node], steps: Iterable[Step], max_hops: int
    ) -> list[Reached]:
        step_list = list(dict.fromkeys(steps))
        frontier = list({n.id: n for n in starts}.values())
        seen = {n.id for n in frontier}
        out: list[Reached] = []
        for hops in range(1, max_hops + 1):
            if not frontier or not step_list:
                break
            next_frontier: list[Node] = []
            for nb in self.neighbors(frontier, step_list):
                if nb.node.id in seen:
                    continue
                seen.add(nb.node.id)
                out.append(Reached(nb.node, hops, nb.edge, nb.from_id))
                next_frontier.append(nb.node)
            frontier = next_frontier
        return out

    # whole graph
    def load_graph(self) -> CodeGraph:
        graph = CodeGraph()
        for r in self._store.execute(
            "MATCH (n) RETURN labels(n)[0] AS label, properties(n) AS props"
        ):
            graph.add_node(node_from_row(r["label"], r["props"]))
        for r in self._store.execute(
            "MATCH (a)-[r]->(b) "
            "RETURN type(r) AS type, a.id AS src, b.id AS dst, "
            "r.access AS access, r.via AS via"
        ):
            graph.add_edge(
                edge_from_row(r["type"], r["src"], r["dst"], r["access"], r["via"])
            )
        return graph
