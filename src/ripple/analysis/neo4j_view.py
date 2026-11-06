from __future__ import annotations

from collections.abc import Collection

from ripple.analysis.impact import edge_key
from ripple.graph.query import Direction, GraphQuery, Step
from ripple.graph.schema import Edge, EdgeType, Node


class GraphQueryView:
    def __init__(self, query: GraphQuery) -> None:
        self._q = query
        self._cache: dict[str, Node] = {}
        self._missing: set[str] = set()

    def _load(self, ids: list[str]) -> None:
        need = [
            i
            for i in dict.fromkeys(ids)
            if i not in self._cache and i not in self._missing
        ]
        if need:
            found = self._q.nodes(need)
            self._cache.update(found)
            self._missing.update(i for i in need if i not in found)

    def node(self, node_id: str) -> Node | None:
        self._load([node_id])
        return self._cache.get(node_id)

    def edges(
        self, node_ids: Collection[str], types: Collection[EdgeType], *, reverse: bool
    ) -> list[Edge]:
        ids = list(node_ids)
        self._load(ids)
        anchors = [self._cache[i] for i in ids if i in self._cache]
        direction = Direction.IN if reverse else Direction.OUT
        steps = [Step(t, direction) for t in sorted(types, key=lambda t: t.value)]
        found = self._q.neighbors(anchors, steps)
        for nb in found:
            self._cache[nb.node.id] = nb.node
        return sorted({nb.edge for nb in found}, key=edge_key)
