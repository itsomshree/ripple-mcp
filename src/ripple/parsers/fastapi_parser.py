from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from ripple.graph.schema import RouteRef, symbol_id

HTTP_METHODS = frozenset(
    {"get", "post", "put", "delete", "patch", "options", "head", "trace"}
)


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


@dataclass(frozen=True)
class _Scope:
    kind: Literal["class", "function"]
    qualname: str


class _RouteCollector(ast.NodeVisitor):
    # Scope handling mirrors python_parser._Collector so handler ids line up.
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

        if not in_class:  # FastAPI routes are plain functions, not methods
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
