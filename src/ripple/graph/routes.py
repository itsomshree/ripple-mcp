from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from ripple.graph.resolver import ModuleResolver
from ripple.graph.schema import (
    FastAPIFacts,
    MountRef,
    ResolvedRoute,
    RouteIssue,
    RouterDecl,
    RouteRef,
    RouteResolution,
    RouterKind,
)
from ripple.graph.schema import RouteIssueReason as Why

_EXPECTED = {RouterKind.APP: "FastAPI", RouterKind.ROUTER: "APIRouter"}
_Key = tuple[str, str]


@dataclass(frozen=True)
class _Path:
    prefix: str
    exact: bool
    mounted: bool


def resolve_routes(
    facts: Mapping[str, FastAPIFacts], modules: ModuleResolver
) -> RouteResolution:
    return _Resolver(facts, modules).run()


class _Resolver:
    def __init__(self, facts: Mapping[str, FastAPIFacts], modules: ModuleResolver):
        self._facts = facts
        self._mods = modules
        self._decls: dict[_Key, RouterDecl] = {}
        self._parents: dict[_Key, list[tuple[_Key, str | None]]] = defaultdict(list)
        self._issues: list[RouteIssue] = []
        self._cycles: set[_Key] = set()

    def run(self) -> RouteResolution:
        paths = sorted(self._facts)
        for path in paths:
            for d in self._facts[path].routers:
                if self._is_fastapi_ctor(path, d):
                    self._decls[(path, d.var)] = d
                    if d.prefix is None:
                        self._issue(path, d.line, d.var, Why.NON_LITERAL_PREFIX)
        for path in paths:
            for m in self._facts[path].mounts:
                self._add_mount(path, m)

        routes: list[ResolvedRoute] = []
        seen: set[tuple[str, str, str]] = set()
        for path in paths:
            for r in self._facts[path].routes:
                routes.extend(self._resolve_route(path, r, seen))
        return RouteResolution(routes=routes, issues=self._issues)

    def _is_fastapi_ctor(self, path: str, d: RouterDecl) -> bool:
        ref = self._mods.external_ref(path, d.ctor)
        if ref is None:
            return False
        origin, name = ref
        return origin.partition(".")[0] == "fastapi" and name == _EXPECTED[d.kind]

    def _locate(self, path: str, expr: str) -> _Key | None:
        parts = expr.split(".")
        if len(parts) == 1 and (path, parts[0]) in self._decls:
            return path, parts[0]
        b = self._mods.imports(path).bindings.get(parts[0])
        if b is None:
            return None
        for part in parts[1:]:
            if b.kind != "module":
                return None
            b = self._mods.resolve_from(b.module, part)
        if b.kind != "symbol" or b.file is None or b.name is None:
            return None
        return b.file, b.name

    def _ref(self, path: str, expr: str) -> _Key | None:
        key = self._locate(path, expr)
        return key if key in self._decls else None

    def _add_mount(self, path: str, m: MountRef) -> None:
        subject = f"{m.parent} <- {m.child}"
        parent, child = self._ref(path, m.parent), self._ref(path, m.child)
        if parent is None or child is None:
            self._issue(path, m.line, subject, Why.UNRESOLVED_MOUNT)
            return
        if m.prefix is None:
            self._issue(path, m.line, subject, Why.NON_LITERAL_PREFIX)
        self._parents[child].append((parent, m.prefix))

    def _prefixes(self, key: _Key, stack: frozenset[_Key]) -> list[_Path]:
        decl = self._decls[key]
        own, own_exact = decl.prefix or "", decl.prefix is not None
        edges = self._parents.get(key)
        if not edges:
            return [_Path(own, own_exact, decl.kind is RouterKind.APP)]

        here = stack | {key}
        out: list[_Path] = []
        for parent, mount_prefix in edges:
            if parent in here:
                self._cycle(key)
                continue
            for up in self._prefixes(parent, here):
                out.append(
                    _Path(
                        up.prefix + (mount_prefix or "") + own,
                        up.exact and own_exact and mount_prefix is not None,
                        up.mounted,
                    )
                )
        return out or [_Path(own, False, False)]

    def _resolve_route(
        self, path: str, r: RouteRef, seen: set[tuple[str, str, str]]
    ) -> list[ResolvedRoute]:
        key = self._ref(path, r.receiver)
        if key is None:
            self._issue(path, r.line, r.handler_id, Why.UNKNOWN_RECEIVER)
            return []
        out: list[ResolvedRoute] = []
        for p in self._prefixes(key, frozenset()):
            full = p.prefix + r.path
            ident = (r.handler_id, r.method, full)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(
                ResolvedRoute(
                    handler_id=r.handler_id,
                    method=r.method,
                    path=full,
                    declared_path=r.path,
                    line=r.line,
                    exact=p.exact,
                    mounted=p.mounted,
                )
            )
        return out

    def _issue(self, path: str, line: int, subject: str, reason: Why) -> None:
        self._issues.append(
            RouteIssue(file_path=path, line=line, subject=subject, reason=reason)
        )

    def _cycle(self, key: _Key) -> None:
        if key not in self._cycles:
            self._cycles.add(key)
            self._issue(key[0], self._decls[key].line, key[1], Why.MOUNT_CYCLE)
