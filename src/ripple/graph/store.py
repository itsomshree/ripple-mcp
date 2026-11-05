from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Sequence
from typing import Any, Self

from neo4j import Driver, GraphDatabase

from ripple.config import Settings, get_settings
from ripple.graph.builder import CodeGraph
from ripple.graph.schema import NodeType


class GraphStore:
    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        database: str | None = None,
    ) -> None:
        self._driver: Driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> GraphStore:
        s = settings or get_settings()
        return cls(s.neo4j_uri, s.neo4j_user, s.neo4j_password.get_secret_value())

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._driver.close()

    def verify(self) -> None:
        """Raises if the server is unreachable or the credentials are wrong"""
        self._driver.verify_connectivity()

    def execute(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run one auto-commit query and return its rows as dicts."""
        with self._driver.session(database=self._database) as session:
            return session.run(query, params or {}).data()

    # schema and maintenance
    def ensure_schema(self) -> None:
        """One uniqueness constraint on `id` per node label"""
        for label in NodeType:
            name = f"{label.value.lower()}_id"
            query = (
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label.value}) REQUIRE n.id IS UNIQUE"
            )
            self.execute(query)

    def clear(self) -> None:
        """Delete every node and relationship, in batches"""
        self.execute(
            "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS"
        )

    def counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """(node count per label, relationship count per type)."""
        nodes = self.execute("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c")
        edges = self.execute("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS c")
        return (
            {r["label"]: r["c"] for r in nodes},
            {r["type"]: r["c"] for r in edges},
        )

    # writing
    def replace_graph(self, graph: CodeGraph) -> None:
        """Full replace: schema, wipe, write"""
        self.ensure_schema()
        self.clear()
        self.write_graph(graph)

    def write_graph(self, graph: CodeGraph) -> None:
        """MERGE every node, then every edge. Safe to run twice"""
        self._write_nodes(graph)
        self._write_edges(graph)

    def _write_nodes(self, graph: CodeGraph) -> None:
        by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node_id in sorted(graph.nodes):
            node = graph.nodes[node_id]
            by_label[node.type.value].append(
                {"id": node.id, "props": node.to_properties()}
            )
        for label in sorted(by_label):
            query = (
                f"UNWIND $rows AS r MERGE (n:`{label}` {{id: r.id}}) SET n += r.props"
            )
            for batch in _batches(by_label[label]):
                self.execute(query, {"rows": batch})

    def _write_edges(self, graph: CodeGraph) -> None:
        groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
        for e in sorted(
            graph.edges,
            key=lambda e: (
                e.type.value,
                e.src,
                e.dst,
                e.access.value if e.access else "",
                e.via.value if e.via else "",
            ),
        ):
            src, dst = graph.nodes.get(e.src), graph.nodes.get(e.dst)
            if src is None or dst is None:
                continue  # the builder never emits dangling edges; stay safe
            groups[(e.type.value, src.type.value, dst.type.value)].append(
                {
                    "src": e.src,
                    "dst": e.dst,
                    "access": e.access.value if e.access else "",
                    "via": e.via.value if e.via else "",
                }
            )
        for (etype, src_label, dst_label), rows in sorted(groups.items()):
            query = (
                "UNWIND $rows AS r "
                f"MATCH (a:`{src_label}` {{id: r.src}}) "
                f"MATCH (b:`{dst_label}` {{id: r.dst}}) "
                f"MERGE (a)-[:`{etype}` {{access: r.access, via: r.via}}]->(b)"
            )
            for batch in _batches(rows):
                self.execute(query, {"rows": batch})


def _batches(rows: Sequence[Any], size: int = 1000) -> Iterator[Sequence[Any]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]
