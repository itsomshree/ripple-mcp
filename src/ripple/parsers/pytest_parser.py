from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from ripple.graph.schema import CoverageFacts, HttpCallRef, symbol_id
from ripple.parsers.fastapi_parser import HTTP_METHODS

_PATH_KWARGS = frozenset({"url", "path"})
_METHOD_KWARGS = frozenset({"method"})
_QUERY = re.compile(r"[?#].*", re.DOTALL)


def parse_coverage_file(abs_path: Path, rel_path: str) -> CoverageFacts:
    source = abs_path.read_bytes().decode("utf-8", errors="replace")
    return parse_coverage_source(source, rel_path)


def parse_coverage_source(source: str, path: str) -> CoverageFacts:
    path = PurePosixPath(path.replace("\\", "/")).as_posix()
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return CoverageFacts(path=path)
    collector = _HttpCollector(path)
    collector.visit(tree)
    return CoverageFacts(path=path, http=collector.calls)


def _arg(call: ast.Call, index: int, kwargs: frozenset[str]) -> ast.expr | None:
    if len(call.args) > index:
        if any(isinstance(a, ast.Starred) for a in call.args[: index + 1]):
            return None
        return call.args[index]
    return next((kw.value for kw in call.keywords if kw.arg in kwargs), None)


def _pattern(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        text = expr.value
    elif isinstance(expr, ast.JoinedStr):
        text = "".join(
            (
                v.value
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
                else "{}"
            )
            for v in expr.values
        )
    else:
        return None
    text = _QUERY.sub("", text)
    return text if text.startswith("/") else None


def _http_call(call: ast.Call) -> tuple[str, str] | None:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in HTTP_METHODS:
        method = func.attr.upper()
        path_expr = _arg(call, 0, _PATH_KWARGS)
    elif func.attr == "request":
        verb = _arg(call, 0, _METHOD_KWARGS)
        if not (
            isinstance(verb, ast.Constant)
            and isinstance(verb.value, str)
            and verb.value.lower() in HTTP_METHODS
        ):
            return None
        method = verb.value.upper()
        path_expr = _arg(call, 1, _PATH_KWARGS)
    else:
        return None
    path = _pattern(path_expr) if path_expr is not None else None
    return (method, path) if path is not None else None


@dataclass(frozen=True)
class _Scope:
    kind: Literal["class", "function"]
    qualname: str


class _HttpCollector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list[HttpCallRef] = []
        self._scope: list[_Scope] = []
        self._seen: set[tuple[str, str, str]] = set()

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
        self._scope.append(_Scope("function", self._qualname(node.name)))
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if self._scope and self._scope[-1].kind == "function":
            found = _http_call(node)
            if found is not None:
                method, path = found
                owner = symbol_id(self.path, self._scope[-1].qualname)
                key = (owner, method, path)
                if key not in self._seen:
                    self._seen.add(key)
                    self.calls.append(
                        HttpCallRef(
                            owner_id=owner, method=method, path=path, line=node.lineno
                        )
                    )
        self.generic_visit(node)
