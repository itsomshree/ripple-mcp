from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ripple.graph.calls import CallResolver
from ripple.graph.coverage import CoverageLinks, EndpointIndex, direct_call_targets
from ripple.graph.models import resolve_models
from ripple.graph.pytest_discovery import find_tests
from ripple.graph.resolver import DEFAULT_SOURCE_ROOTS, ModuleResolver
from ripple.graph.routes import resolve_routes
from ripple.graph.schema import (
    APIEndpointNode,
    CallRef,
    CoverageFacts,
    DBFacts,
    DbIssue,
    DBTableNode,
    Edge,
    EdgeType,
    Evidence,
    FastAPIFacts,
    Node,
    ParsedFile,
    RouteIssue,
    TestNode,
    UnresolvedCall,
    UnresolvedReason,
    endpoint_id,
    file_id,
    symbol_id,
    table_id,
)
from ripple.graph.usage import resolve_usage

_DEP_CTORS = frozenset({"Depends", "Security"})


def _enclosing_scope(path: str, owner_id: str) -> str:
    parent = owner_id.partition("::")[2].rpartition(".")[0]
    return symbol_id(path, parent) if parent else file_id(path)


def _table_node(graph: CodeGraph, name: str) -> str:
    name = name.lower()
    graph.add_node(DBTableNode(id=table_id(name), name=name))
    return table_id(name)


@dataclass
class CodeGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: set[Edge] = field(default_factory=set)
    unresolved: list[UnresolvedCall] = field(default_factory=list)
    route_issues: list[RouteIssue] = field(default_factory=list)
    db_issues: list[DbIssue] = field(default_factory=list)

    def add_node(self, node: Node) -> None:
        self.nodes.setdefault(node.id, node)

    def add_edge(self, edge: Edge) -> None:
        self.edges.add(edge)

    def edges_of(self, type_: EdgeType) -> list[Edge]:
        return sorted(
            (e for e in self.edges if e.type == type_),
            key=lambda e: (
                e.src,
                e.dst,
                e.access.value if e.access else "",
                e.via.value if e.via else "",
            ),
        )

    def unresolved_by_reason(self) -> Counter[UnresolvedReason]:
        return Counter(u.reason for u in self.unresolved)


class GraphBuilder:
    def __init__(self, source_roots: tuple[str, ...] = DEFAULT_SOURCE_ROOTS) -> None:
        self._source_roots = source_roots
        self._parsed: dict[str, ParsedFile] = {}
        self._facts: dict[str, FastAPIFacts] = {}
        self._db: dict[str, DBFacts] = {}
        self._cov: dict[str, CoverageFacts] = {}

    def add(
        self,
        parsed: ParsedFile,
        facts: FastAPIFacts | None = None,
        db: DBFacts | None = None,
        cov: CoverageFacts | None = None,
    ) -> None:
        self._parsed[parsed.path] = parsed
        if facts is None:
            self._facts.pop(parsed.path, None)
        else:
            self._facts[parsed.path] = facts
        if db is None:
            self._db.pop(parsed.path, None)
        else:
            self._db[parsed.path] = db
        if cov is None:
            self._cov.pop(parsed.path, None)
        else:
            self._cov[parsed.path] = cov

    def remove(self, path: str) -> None:
        self._parsed.pop(path, None)
        self._facts.pop(path, None)
        self._db.pop(path, None)
        self._cov.pop(path, None)

    def build(self) -> CodeGraph:
        graph = CodeGraph()
        paths = sorted(self._parsed)

        # pass 1: ingest nodes and DEFINED_IN edges
        for path in paths:
            pf = self._parsed[path]
            for node in pf.nodes:
                graph.add_node(node)
            for edge in pf.edges:
                graph.add_edge(edge)

        # pass 2: IMPORTS edges
        modules = ModuleResolver(self._parsed, self._source_roots)
        for path in paths:
            for dep in sorted(modules.imports(path).depends_on):
                graph.add_edge(
                    Edge(type=EdgeType.IMPORTS, src=file_id(path), dst=file_id(dep))
                )

        # pass 3: CALLS edges
        calls = CallResolver(self._parsed, modules)
        for path in paths:
            for ref in self._parsed[path].calls:
                targets, reason = calls.resolve(path, ref)
                for target in targets:
                    graph.add_edge(
                        Edge(type=EdgeType.CALLS, src=ref.caller_id, dst=target)
                    )
                if reason is not None:
                    graph.unresolved.append(
                        UnresolvedCall(
                            caller_id=ref.caller_id,
                            target=ref.target,
                            line=ref.line,
                            reason=reason,
                        )
                    )

        # pass 4: APIEndpoint nodes + HANDLES edges
        resolution = resolve_routes(self._facts, modules)
        graph.route_issues.extend(resolution.issues)
        for r in resolution.routes:
            if r.handler_id not in graph.nodes:
                continue
            route = r.path or "/"
            eid = endpoint_id(r.method, route)
            existing = graph.nodes.get(eid)
            if isinstance(existing, APIEndpointNode):
                existing.exact = existing.exact and r.exact
                existing.mounted = existing.mounted and r.mounted
            else:
                graph.add_node(
                    APIEndpointNode(
                        id=eid,
                        name=f"{r.method} {route}",
                        method=r.method,
                        route=route,
                        exact=r.exact,
                        mounted=r.mounted,
                    )
                )
            graph.add_edge(Edge(type=EdgeType.HANDLES, src=eid, dst=r.handler_id))

        # pass 5: DEPENDS_ON edges
        for path in sorted(self._facts):
            for dep in self._facts[path].depends:
                ref = modules.external_ref(path, dep.ctor)
                if (
                    ref is None
                    or ref[0].partition(".")[0] != "fastapi"
                    or ref[1] not in _DEP_CTORS
                    or dep.owner_id not in graph.nodes
                ):
                    continue
                targets, reason = calls.resolve(
                    path,
                    CallRef(
                        caller_id=_enclosing_scope(path, dep.owner_id),
                        target=dep.target,
                        line=dep.line,
                    ),
                )
                for target in targets:
                    graph.add_edge(
                        Edge(
                            type=EdgeType.DEPENDS_ON,
                            src=dep.owner_id,
                            dst=target,
                        )
                    )
                if reason is not None:
                    graph.unresolved.append(
                        UnresolvedCall(
                            caller_id=dep.owner_id,
                            target=dep.target,
                            line=dep.line,
                            reason=reason,
                            kind="depends",
                        )
                    )

        # pass 6: DBTable nodes + MAPS_TO / QUERIES edges
        if self._db:
            models = resolve_models(self._db, self._parsed, calls)
            usage = resolve_usage(self._db, calls, models)

            graph.db_issues.extend(models.issues)
            for path in sorted(self._db):
                graph.db_issues.extend(self._db[path].issues)

            for m in models.models:
                if m.class_id in graph.nodes:
                    graph.add_edge(
                        Edge(
                            type=EdgeType.MAPS_TO,
                            src=m.class_id,
                            dst=_table_node(graph, m.table),
                        )
                    )
            for use in usage.uses:
                if use.owner_id in graph.nodes:
                    graph.add_edge(
                        Edge(
                            type=EdgeType.QUERIES,
                            src=use.owner_id,
                            dst=_table_node(graph, use.table),
                            access=use.access,
                        )
                    )

        # pass 7: promote test functions to Test nodes
        for fn in find_tests(self._parsed):
            graph.nodes[fn.id] = TestNode(
                id=fn.id,
                name=fn.name,
                qualname=fn.qualname,
                file_path=fn.file_path,
                line_start=fn.line_start,
                line_end=fn.line_end,
                is_async=fn.is_async,
                class_name=fn.class_name,
            )

        # pass 8: TESTS edges (strongest evidence per test/target pair)
        links = CoverageLinks()
        for test_id, target_id in direct_call_targets(
            graph.nodes, graph.edges_of(EdgeType.CALLS)
        ):
            links.add(test_id, target_id, Evidence.CALL)

        if self._cov:
            index = EndpointIndex(
                n for n in graph.nodes.values() if isinstance(n, APIEndpointNode)
            )
            for path in sorted(self._cov):
                for call in self._cov[path].http:
                    if not isinstance(graph.nodes.get(call.owner_id), TestNode):
                        continue
                    for eid in index.match(call.method, call.path):
                        links.add(call.owner_id, eid, Evidence.HTTP)

        for test_id, target_id, via in links.items():
            graph.add_edge(
                Edge(type=EdgeType.TESTS, src=test_id, dst=target_id, via=via)
            )
        return graph
