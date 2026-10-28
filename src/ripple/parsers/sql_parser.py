from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ripple.graph.schema import AccessMode, SqlExtraction, TableAccess

DIALECT = "postgres"

_LEAD = r"(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*"
_SQL_START = re.compile(
    rf"^{_LEAD}(?:with|select|insert|update|delete|merge)\b",
    re.IGNORECASE | re.DOTALL,
)

_PLACEHOLDER = re.compile(r"%\(\w+\)s|%s|(?<![:\w]):[A-Za-z_]\w*|\?|\$\d+")

_WRITES = (exp.Insert, exp.Update, exp.Delete, exp.Merge)


def looks_like_sql(text: str) -> bool:
    return _SQL_START.match(text) is not None


def extract_table_accesses(sql: str) -> SqlExtraction:
    if not looks_like_sql(sql):
        return SqlExtraction()
    try:
        statements = sqlglot.parse(_PLACEHOLDER.sub("NULL", sql), read=DIALECT)
    except (SqlglotError, RecursionError) as exc:
        message = str(exc).partition("\n")[0]
        return SqlExtraction(error=f"{type(exc).__name__}: {message}")

    found: set[TableAccess] = set()
    for stmt in statements:
        if stmt is not None:
            found |= _accesses(stmt)
    return SqlExtraction(accesses=sorted(found, key=lambda a: (a.table, a.access)))


def _accesses(stmt: exp.Expr) -> set[TableAccess]:
    if not isinstance(stmt, (exp.Query, *_WRITES)):
        return set()

    ctes = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    targets = {
        id(target)
        for write in stmt.find_all(*_WRITES)
        if (target := _write_target(write)) is not None
    }

    out: set[TableAccess] = set()
    for table in stmt.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue
        name = table.name.lower()
        if not table.db and name in ctes:
            continue
        mode = AccessMode.WRITE if id(table) in targets else AccessMode.READ
        out.add(TableAccess(table=name, access=mode))
    return out


def _write_target(stmt: exp.Expr) -> exp.Table | None:
    target = stmt.this
    if isinstance(target, exp.Schema):
        target = target.this
    return target if isinstance(target, exp.Table) else None
