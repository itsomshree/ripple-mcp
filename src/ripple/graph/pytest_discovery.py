from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath

from ripple.graph.schema import ClassNode, FunctionNode, ParsedFile

_FUNC_PREFIX = "test"
_CLASS_PREFIX = "Test"


def _collectable(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def find_tests(parsed: Mapping[str, ParsedFile]) -> list[FunctionNode]:
    out: list[FunctionNode] = []
    for path in sorted(parsed):
        if not _collectable(path):
            continue
        pf = parsed[path]
        symbols = {
            n.qualname: n for n in pf.nodes if isinstance(n, (FunctionNode, ClassNode))
        }
        out.extend(
            n
            for n in pf.nodes
            if isinstance(n, FunctionNode) and _is_test_function(n, symbols)
        )
    return out


def _is_test_function(
    fn: FunctionNode, symbols: Mapping[str, FunctionNode | ClassNode]
) -> bool:
    if not fn.name.startswith(_FUNC_PREFIX) or _is_fixture(fn.decorators):
        return False
    if fn.class_name is None:
        return "." not in fn.qualname
    cls = symbols.get(fn.class_name)
    return (
        isinstance(cls, ClassNode)
        and "." not in cls.qualname
        and _is_test_class(cls, symbols)
    )


def _is_test_class(
    cls: ClassNode, symbols: Mapping[str, FunctionNode | ClassNode]
) -> bool:
    if any(base.rpartition(".")[2] == "TestCase" for base in cls.bases):
        return True
    return (
        cls.name.startswith(_CLASS_PREFIX) and f"{cls.qualname}.__init__" not in symbols
    )


def _is_fixture(decorators: list[str]) -> bool:
    return any(d.partition("(")[0].rpartition(".")[2] == "fixture" for d in decorators)
