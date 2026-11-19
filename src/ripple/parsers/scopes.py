from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def own_nodes(body: Iterable[ast.stmt]) -> Iterator[ast.AST]:
    """The nodes of one scope. A nested def, class or lambda is yielded but not entered"""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPE_NODES):
            continue
        stack.extend(ast.iter_child_nodes(node))


def parameter_names(args: ast.arguments) -> list[str]:
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    names += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
    return names


@dataclass(frozen=True)
class Assignment:
    value: ast.expr
    line: int


@dataclass
class Bindings:
    names: frozenset[str]
    single: dict[str, Assignment] = field(default_factory=dict)


def collect_bindings(
    body: Iterable[ast.stmt],
    params: Iterable[str] = (),
    extra: Iterable[str] = (),
) -> Bindings:
    """Every name a scope binds, and the ones bound exactly once by assignment.

    Imports are not in `names`: the resolver reads imports file-wide, so a
    function-local import must not shadow itself. They do count towards
    "bound once", so an imported and assigned name is never an instance.
    `extra` are additional bindings the scope cannot see itself (module-level
    names that a function rebinds through `global`). Names a scope declares
    `global` or `nonlocal` belong to another scope and are left out.
    Rebinding through `nonlocal` in a nested function is not seen by the
    enclosing function: a known gap"""
    counts: Counter[str] = Counter(params)
    counts.update(extra)
    imported: Counter[str] = Counter()
    outer: set[str] = set()
    assigned: dict[str, Assignment] = {}

    for node in own_nodes(body):
        if isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Load):
                counts[node.id] += 1
        elif isinstance(node, _DEFS):
            counts[node.name] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name.partition(".")[0]] += 1
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            if node.name:
                counts[node.name] += 1
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                counts[node.rest] += 1
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            outer.update(node.names)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            assigned[node.targets[0].id] = Assignment(node.value, node.lineno)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assigned[node.target.id] = Assignment(node.value, node.lineno)

    return Bindings(
        names=frozenset(n for n in counts if n not in outer),
        single={
            v: a
            for v, a in assigned.items()
            if counts[v] + imported[v] == 1 and v not in outer
        },
    )
