from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from ripple.graph.schema import (
    CallRef,
    ClassNode,
    Edge,
    EdgeType,
    FileNode,
    FunctionNode,
    ImportRef,
    Node,
    ParsedFile,
    file_id,
    symbol_id,
)


def parse_python_file(abs_path: Path, rel_path: str) -> ParsedFile:
    raw = abs_path.read_bytes()
    return parse_python_source(
        raw.decode("utf-8", errors="replace"),
        rel_path,
        content_hash=hashlib.sha256(raw).hexdigest(),
    )


def parse_python_source(
    source: str, path: str, content_hash: str | None = None
) -> ParsedFile:
    path = PurePosixPath(path.replace("\\", "/")).as_posix()
    content_hash = content_hash or hashlib.sha256(source.encode("utf-8")).hexdigest()

    file_node = FileNode(
        id=file_id(path),
        name=PurePosixPath(path).name,
        path=path,
        content_hash=content_hash,
        is_test=_is_test_path(path),
    )

    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return ParsedFile(
            path=path, nodes=[file_node], error=f"{type(exc).__name__}: {exc}"
        )

    collector = _Collector(path)
    collector.visit(tree)

    return ParsedFile(
        path=path,
        nodes=[file_node, *collector.nodes],
        edges=collector.edges,
        calls=collector.calls,
        imports=collector.imports,
    )


def _is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        p.name.startswith("test_")
        or p.name.endswith("_test.py")
        or p.name == "conftest.py"
        or "tests" in p.parts[:-1]
    )


def _dotted_name(expr: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return ".".join(reversed(parts))
    return None


@dataclass(frozen=True)
class _Scope:
    kind: Literal["class", "function"]
    qualname: str
    id: str


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.file_id = file_id(path)
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.calls: list[CallRef] = []
        self.imports: list[ImportRef] = []
        self._scope: list[_Scope] = []
        self._seen: set[str] = set()

    def _qualname(self, name: str) -> str:
        return f"{self._scope[-1].qualname}.{name}" if self._scope else name

    def _current_caller(self) -> str:
        for scope in reversed(self._scope):
            if scope.kind == "function":
                return scope.id
        return self.file_id

    def _register(self, sid: str) -> bool:
        if sid in self._seen:
            return False
        self._seen.add(sid)
        return True

    def _defined_in(self, sid: str) -> None:
        self.edges.append(Edge(type=EdgeType.DEFINED_IN, src=sid, dst=self.file_id))

    @staticmethod
    def _span_start(
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    ) -> int:
        return min([node.lineno, *(d.lineno for d in node.decorator_list)])

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)

        qualname = self._qualname(node.name)
        sid = symbol_id(self.path, qualname)
        if self._register(sid):
            self.nodes.append(
                ClassNode(
                    id=sid,
                    name=node.name,
                    qualname=qualname,
                    file_path=self.path,
                    line_start=self._span_start(node),
                    line_end=node.end_lineno or node.lineno,
                    bases=[ast.unparse(b) for b in node.bases],
                )
            )
            self._defined_in(sid)

        self._scope.append(_Scope("class", qualname, sid))
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool
    ) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)

        enclosing = self._scope[-1] if self._scope else None
        qualname = self._qualname(node.name)
        sid = symbol_id(self.path, qualname)

        if self._register(sid):
            self.nodes.append(
                FunctionNode(
                    id=sid,
                    name=node.name,
                    qualname=qualname,
                    file_path=self.path,
                    line_start=self._span_start(node),
                    line_end=node.end_lineno or node.lineno,
                    is_async=is_async,
                    class_name=(
                        enclosing.qualname
                        if enclosing and enclosing.kind == "class"
                        else None
                    ),
                    decorators=[ast.unparse(d) for d in node.decorator_list],
                )
            )
            self._defined_in(sid)

        self._scope.append(_Scope("function", qualname, sid))
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(
                ImportRef(
                    module=alias.name,
                    alias=alias.asname,
                    line=node.lineno,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module == "__future__":
            return
        self.imports.append(
            ImportRef(
                module=node.module or "",
                names=[(a.name, a.asname) for a in node.names],
                level=node.level,
                line=node.lineno,
            )
        )

    def visit_Call(self, node: ast.Call) -> None:
        target = _dotted_name(node.func)
        if target is not None:
            self.calls.append(
                CallRef(
                    caller_id=self._current_caller(), target=target, line=node.lineno
                )
            )
        self.generic_visit(node)
