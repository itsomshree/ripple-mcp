from __future__ import annotations

from ripple.graph.builder import CodeGraph
from ripple.graph.diff import GraphDelta, diff_graphs
from ripple.graph.query import GraphQuery
from ripple.graph.store import GraphStore


def sync_graph(store: GraphStore, graph: CodeGraph) -> GraphDelta:
    store.ensure_schema()
    current = GraphQuery(store).load_graph()
    delta = diff_graphs(current, graph)
    store.apply_delta(delta)
    return delta
