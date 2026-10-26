from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ripple.graph.calls import CallResolver
from ripple.graph.resolver import DEFAULT_SOURCE_ROOTS, ModuleResolver
from ripple.graph.routes import resolve_routes
from ripple.graph.schema import (
    APIEndpointNode,
    Edge,
    EdgeType,
    FastAPIFacts,
    Node,
    ParsedFile,
    RouteIssue,
    UnresolvedCall,
    UnresolvedReason,
    endpoint_id,
    file_id,
)


@dataclass
class CodeGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: set[Edge] = field(default_factory=set)
    unresolved: list[UnresolvedCall] = field(default_factory=list)
    route_issues: list[RouteIssue] = field(default_factory=list)

    def add_node(self, node: Node) -> None:
        self.nodes.setdefault(node.id, node)

    def add_edge(self, edge: Edge) -> None:
        self.edges.add(edge)

    def edges_of(self, type_: EdgeType) -> list[Edge]:
        return sorted(
            (e for e in self.edges if e.type == type_),
            key=lambda e: (e.src, e.dst),
        )

    def unresolved_by_reason(self) -> Counter[UnresolvedReason]:
        return Counter(u.reason for u in self.unresolved)


class GraphBuilder:
    def __init__(self, source_roots: tuple[str, ...] = DEFAULT_SOURCE_ROOTS) -> None:
        self._source_roots = source_roots
        self._parsed: dict[str, ParsedFile] = {}
        self._facts: dict[str, FastAPIFacts] = {}

    def add(self, parsed: ParsedFile, facts: FastAPIFacts | None = None) -> None:
        self._parsed[parsed.path] = parsed
        if facts is None:
            self._facts.pop(parsed.path, None)
        else:
            self._facts[parsed.path] = facts

    def remove(self, path: str) -> None:
        self._parsed.pop(path, None)
        self._facts.pop(path, None)

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
        return graph
