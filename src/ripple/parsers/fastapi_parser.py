from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from ripple.graph.schema import (
    FastAPIFacts,
    MountRef,
    RouterDecl,
    RouteRef,
    RouterKind,
    symbol_id,
)

HTTP_METHODS = frozenset(
    {"get", "post", "put", "delete", "patch", "options", "head", "trace"}
)

_CTORS = {"FastAPI": RouterKind.APP, "APIRouter": RouterKind.ROUTER}


def parse_routes_file(abs_path: Path, rel_path: str) -> list[RouteRef]:
    source = abs_path.read_bytes().decode("utf-8", errors="replace")
    return parse_routes_source(source, rel_path)


def parse_routes_source(source: str, path: str) -> list[RouteRef]:
    path = PurePosixPath(path.replace("\\", "/")).as_posix()
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return []
    collector = _RouteCollector(path)
    collector.visit(tree)
    return collector.routes


def _dotted_name(expr: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return ".".join(reversed(parts))
    return None


def _literal_path(call: ast.Call) -> str | None:
    node: ast.expr | None = call.args[0] if call.args else None
    if node is None:
        node = next((kw.value for kw in call.keywords if kw.arg == "path"), None)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _route_from_decorator(dec: ast.expr) -> tuple[str, str, str] | None:
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return None
    method = dec.func.attr
    if method not in HTTP_METHODS:
        return None
    receiver = _dotted_name(dec.func.value)
    path = _literal_path(dec)
    if receiver is None or path is None:
        return None
    return receiver, method.upper(), path


def parse_fastapi_file(abs_path: Path, rel_path: str) -> FastAPIFacts:
    source = abs_path.read_bytes().decode("utf-8", errors="replace")
    return parse_fastapi_source(source, rel_path)


def parse_fastapi_source(source: str, path: str) -> FastAPIFacts:
    path = PurePosixPath(path.replace("\\", "/")).as_posix()
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return FastAPIFacts(path=path)

    routes = _RouteCollector(path)
    routes.visit(tree)
    decls = _DeclCollector(path)
    decls.visit(tree)
    return FastAPIFacts(
        path=path,
        routes=routes.routes,
        routers=decls.routers,
        mounts=decls.mounts,
    )


def _prefix_kwarg(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "prefix":
            v = kw.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                return v.value
            return None
    return ""


def _router_arg(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    return next((kw.value for kw in call.keywords if kw.arg == "router"), None)


@dataclass(frozen=True)
class _Scope:
    kind: Literal["class", "function"]
    qualname: str


class _RouteCollector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.routes: list[RouteRef] = []
        self._scope: list[_Scope] = []

    def _qualname(self, name: str) -> str:
        return f"{self._scope[-1].qualname}.{name}" if self._scope else name

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(_Scope("class", self._qualname(node.name)))
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualname = self._qualname(node.name)
        in_class = bool(self._scope) and self._scope[-1].kind == "class"

        if not in_class:
            for dec in node.decorator_list:
                parsed = _route_from_decorator(dec)
                if parsed is None:
                    continue
                receiver, method, route = parsed
                self.routes.append(
                    RouteRef(
                        handler_id=symbol_id(self.path, qualname),
                        receiver=receiver,
                        method=method,
                        path=route,
                        line=dec.lineno,
                    )
                )

        self._scope.append(_Scope("function", qualname))
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()


class _DeclCollector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.routers: list[RouterDecl] = []
        self.mounts: list[MountRef] = []
        self._aliases: dict[str, RouterKind] = {}

    def _skip(self, node: ast.AST) -> None:
        return None

    visit_FunctionDef = _skip
    visit_AsyncFunctionDef = _skip
    visit_ClassDef = _skip
    visit_Lambda = _skip

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and (node.module or "").partition(".")[0] == "fastapi":
            for a in node.names:
                kind = _CTORS.get(a.name)
                if kind is not None and a.asname:
                    self._aliases[a.asname] = kind

    def _kind_of(self, ctor: str) -> RouterKind | None:
        return self._aliases.get(ctor) or _CTORS.get(ctor.rpartition(".")[2])

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._declare(node.targets[0].id, node.value, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._declare(node.target.id, node.value, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._maybe_mount(node)
        self.generic_visit(node)

    def _declare(self, var: str, value: ast.expr, line: int) -> None:
        if not isinstance(value, ast.Call):
            return
        ctor = _dotted_name(value.func)
        kind = self._kind_of(ctor) if ctor else None
        if ctor is None or kind is None:
            return
        self.routers.append(
            RouterDecl(
                file_path=self.path,
                var=var,
                ctor=ctor,
                kind=kind,
                prefix=_prefix_kwarg(value) if kind is RouterKind.ROUTER else "",
                line=line,
            )
        )

    def _maybe_mount(self, call: ast.Call) -> None:
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "include_router":
            return
        parent = _dotted_name(func.value)
        arg = _router_arg(call)
        child = _dotted_name(arg) if arg is not None else None
        if parent is None or child is None:
            return
        self.mounts.append(
            MountRef(
                file_path=self.path,
                parent=parent,
                child=child,
                prefix=_prefix_kwarg(call),
                line=call.lineno,
            )
        )
