from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from ripple.graph.schema import (
    AccessMode,
    DBFacts,
    DbIssue,
    DbIssueReason,
    ModelDecl,
    ModelRef,
    TableUse,
    file_id,
    symbol_id,
)
from ripple.parsers.sql_parser import extract_table_accesses, looks_like_sql


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


def parse_refs_source(source: str, path: str) -> list[ModelRef]:
    path = PurePosixPath(path.replace("\\", "/")).as_posix()
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return []
    return _collect_refs(tree, path)


def parse_usage_source(source: str, path: str) -> tuple[list[TableUse], list[DbIssue]]:
    path = PurePosixPath(path.replace("\\", "/")).as_posix()
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return [], []
    return _collect_usage(tree, path)


def parse_db_source(source: str, path: str) -> DBFacts:
    path = PurePosixPath(path.replace("\\", "/")).as_posix()
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return DBFacts(path=path)

    models = _ModelCollector(path)
    models.visit(tree)
    uses, issues = _collect_usage(tree, path)
    return DBFacts(
        path=path,
        models=models.models,
        uses=uses,
        refs=_collect_refs(tree, path),
        issues=issues,
    )


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


# SQL sinks
_SINKS = frozenset(
    {
        "execute",
        "executemany",
        "executescript",
        "exec_driver_sql",
        "text",
        "fetch",
        "fetchrow",
        "fetchval",
        "read_sql",
        "read_sql_query",
    }
)
_WRAPPERS = frozenset({"text", "SQL"})
_SQL_KWARGS = frozenset({"sql", "query", "statement", "text"})


def _collect_usage(tree: ast.Module, path: str) -> tuple[list[TableUse], list[DbIssue]]:
    collector = _UsageCollector(path, _collect_names(tree.body))
    collector.visit(tree)
    return collector.uses, collector.issues


def _const_str(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left, right = _const_str(expr.left), _const_str(expr.right)
        if left is not None and right is not None:
            return left + right
    return None


def _leading_literal(expr: ast.expr) -> str | None:
    s = _const_str(expr)
    if s is not None:
        return s
    if isinstance(expr, ast.JoinedStr):
        first = expr.values[0] if expr.values else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Mod)):
        return _leading_literal(expr.left)
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "format"
    ):
        return _leading_literal(expr.func.value)
    return None


def _callee_tail(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _sql_arg(call: ast.Call) -> ast.expr | None:
    if call.args:
        first = call.args[0]
        return None if isinstance(first, ast.Starred) else first
    return next((kw.value for kw in call.keywords if kw.arg in _SQL_KWARGS), None)


def _own_nodes(body: Iterable[ast.stmt]) -> Iterator[ast.AST]:
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        stack.extend(ast.iter_child_nodes(node))


@dataclass
class _Names:
    lits: dict[str, str]
    stores: Counter[str]


def _collect_names(body: Iterable[ast.stmt], params: Iterable[str] = ()) -> _Names:
    stores: Counter[str] = Counter(params)
    lits: dict[str, str] = {}
    for node in _own_nodes(body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            stores[node.id] += 1
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            s = _const_str(node.value)
            if s is not None:
                lits[node.targets[0].id] = s
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            s = _const_str(node.value)
            if s is not None:
                lits[node.target.id] = s
    return _Names({k: v for k, v in lits.items() if stores[k] == 1}, stores)


@dataclass
class _UseScope:
    kind: Literal["class", "function"]
    qualname: str
    names: _Names | None = None


class _UsageCollector(ast.NodeVisitor):
    def __init__(self, path: str, module_names: _Names) -> None:
        self.path = path
        self.uses: list[TableUse] = []
        self.issues: list[DbIssue] = []
        self._module = module_names
        self._scope: list[_UseScope] = []
        self._claimed: set[int] = set()

    def _qualname(self, name: str) -> str:
        return f"{self._scope[-1].qualname}.{name}" if self._scope else name

    def _owner(self) -> str:
        for scope in reversed(self._scope):
            if scope.kind == "function":
                return symbol_id(self.path, scope.qualname)
        return file_id(self.path)

    def _lookup_const(self, name: str) -> str | None:
        for scope in reversed(self._scope):
            if (
                scope.kind == "function"
                and scope.names is not None
                and name in scope.names.stores
            ):
                return scope.names.lits.get(name)

        if name in self._module.stores:
            return self._module.lits.get(name)

        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(_UseScope("class", self._qualname(node.name)))
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        a = node.args
        params = [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
        params += [p.arg for p in (a.vararg, a.kwarg) if p is not None]
        self._scope.append(
            _UseScope(
                "function", self._qualname(node.name), _collect_names(node.body, params)
            )
        )
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if id(node) not in self._claimed:
            self._maybe_sink(node)
        self.generic_visit(node)

    def _maybe_sink(self, call: ast.Call) -> None:
        if _callee_tail(call.func) not in _SINKS:
            return
        arg = _sql_arg(call)
        if isinstance(arg, ast.Call) and _callee_tail(arg.func) in _WRAPPERS:
            self._claimed.add(id(arg))
            arg = _sql_arg(arg)
        if arg is not None:
            self._record(arg, call.lineno)

    def _record(self, arg: ast.expr, line: int) -> None:
        owner = self._owner()
        sql = _const_str(arg)
        if sql is None and isinstance(arg, ast.Name):
            sql = self._lookup_const(arg.id)

        if sql is not None:
            result = extract_table_accesses(sql)
            if result.error is not None:
                self._issue(line, owner, DbIssueReason.UNPARSEABLE_SQL)
            for acc in result.accesses:
                self.uses.append(
                    TableUse(
                        owner_id=owner, table=acc.table, access=acc.access, line=line
                    )
                )
            return

        lead = _leading_literal(arg)
        if lead is not None and looks_like_sql(lead):
            self._issue(line, owner, DbIssueReason.NON_LITERAL_SQL)

    def _issue(self, line: int, owner: str, reason: DbIssueReason) -> None:
        self.issues.append(
            DbIssue(file_path=self.path, line=line, subject=owner, reason=reason)
        )


# ORM references
_SELF_NAMES = frozenset({"self", "cls"})
_WRITE_CALLS = frozenset(
    {"insert", "update", "delete", "bulk_insert_mappings", "bulk_update_mappings"}
)
_CHAIN_WRITES = frozenset({"update", "delete"})


def _collect_refs(tree: ast.Module, path: str) -> list[ModelRef]:
    collector = _RefCollector(path)
    collector.visit(tree)
    return collector.refs


def _dotted_name(expr: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return ".".join(reversed(parts))
    return None


def _query_model_arg(expr: ast.expr) -> ast.expr | None:
    while True:
        if isinstance(expr, ast.Call):
            if _callee_tail(expr.func) == "query":
                arg = expr.args[0] if expr.args else None
                if arg is not None and _dotted_name(arg) is not None:
                    return arg
                return None
            if not isinstance(expr.func, ast.Attribute):
                return None
            expr = expr.func.value
        elif isinstance(expr, ast.Attribute):
            expr = expr.value
        else:
            return None


def _write_targets(call: ast.Call) -> list[ast.expr]:
    out: list[ast.expr] = []
    if (
        _callee_tail(call.func) in _WRITE_CALLS
        and call.args
        and _dotted_name(call.args[0]) is not None
    ):
        out.append(call.args[0])
    if isinstance(call.func, ast.Attribute) and call.func.attr in _CHAIN_WRITES:
        arg = _query_model_arg(call.func.value)
        if arg is not None:
            out.append(arg)
    return out


class _RefCollector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.refs: list[ModelRef] = []
        self._scope: list[_UseScope] = []
        self._writes: set[int] = set()
        self._called: set[int] = set()
        self._seen: set[tuple[str, str, AccessMode, bool]] = set()

    def _qualname(self, name: str) -> str:
        return f"{self._scope[-1].qualname}.{name}" if self._scope else name

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(_UseScope("class", self._qualname(node.name)))
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(_UseScope("function", self._qualname(node.name)))
        for stmt in node.body:
            self.visit(stmt)
        self._scope.pop()

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        if _dotted_name(node.func) is not None:
            self._called.add(id(node.func))
        self._writes.update(id(t) for t in _write_targets(node))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._record(node, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        dotted = _dotted_name(node)
        if dotted is not None and isinstance(node.ctx, ast.Load):
            self._record(node, dotted)
        else:
            self.generic_visit(node)

    def _record(self, node: ast.Name | ast.Attribute, dotted: str) -> None:
        if not self._scope or self._scope[-1].kind != "function":
            return
        if dotted.partition(".")[0] in _SELF_NAMES:
            return
        owner = symbol_id(self.path, self._scope[-1].qualname)
        access = AccessMode.WRITE if id(node) in self._writes else AccessMode.READ
        called = id(node) in self._called
        key = (owner, dotted, access, called)
        if key in self._seen:
            return
        self._seen.add(key)
        self.refs.append(
            ModelRef(
                owner_id=owner,
                ref=dotted,
                access=access,
                line=node.lineno,
                called=called,
            )
        )
