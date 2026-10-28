from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

from ripple.graph.schema import DBFacts, ModelDecl, symbol_id


def parse_models_file(abs_path: Path, rel_path: str) -> list[ModelDecl]:
    source = abs_path.read_bytes().decode("utf-8", errors="replace")
    return parse_models_source(source, rel_path)


def parse_models_source(source: str, path: str) -> list[ModelDecl]:
    path = PurePosixPath(path.replace("\\", "/")).as_posix()
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return []
    collector = _ModelCollector(path)
    collector.visit(tree)
    return collector.models


def parse_db_file(abs_path: Path, rel_path: str) -> DBFacts:
    source = abs_path.read_bytes().decode("utf-8", errors="replace")
    return parse_db_source(source, rel_path)


def parse_db_source(source: str, path: str) -> DBFacts:
    path = PurePosixPath(path.replace("\\", "/")).as_posix()
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return DBFacts(path=path)

    models = _ModelCollector(path)
    models.visit(tree)
    return DBFacts(path=path, models=models.models)


def _class_attr(stmt: ast.stmt) -> tuple[str, ast.AST] | None:
    if (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
    ):
        return stmt.targets[0].id, stmt.value
    if (
        isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and stmt.value is not None
    ):
        return stmt.target.id, stmt.value
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return stmt.name, stmt
    return None


def _model_decl(path: str, class_id: str, node: ast.ClassDef) -> ModelDecl | None:
    table: str | None = None
    non_literal = False
    abstract = False
    tablename_line: int | None = None
    abstract_line: int | None = None

    for stmt in node.body:
        attr = _class_attr(stmt)
        if attr is None:
            continue
        name, value = attr
        if name == "__tablename__":
            tablename_line = stmt.lineno
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                table, non_literal = value.value, False
            else:
                table, non_literal = None, True
        elif name == "__abstract__":
            abstract = isinstance(value, ast.Constant) and value.value is True
            abstract_line = stmt.lineno

    line = tablename_line if tablename_line is not None else abstract_line
    if line is None or not (abstract or tablename_line is not None):
        return None
    return ModelDecl(
        class_id=class_id,
        file_path=path,
        line=line,
        table=table,
        abstract=abstract,
        non_literal=non_literal,
    )


class _ModelCollector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.models: list[ModelDecl] = []
        self._scope: list[str] = []
        self._seen: set[str] = set()

    def _qualname(self, name: str) -> str:
        return f"{self._scope[-1]}.{name}" if self._scope else name

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualname = self._qualname(node.name)
        sid = symbol_id(self.path, qualname)
        if sid not in self._seen:
            self._seen.add(sid)
            decl = _model_decl(self.path, sid, node)
            if decl is not None:
                self.models.append(decl)
        self._scope.append(qualname)
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(self._qualname(node.name))
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()
