from __future__ import annotations

import builtins
import re
from collections.abc import Mapping
from dataclasses import dataclass

from ripple.graph.resolver import Binding, ModuleResolver
from ripple.graph.schema import (
    CallRef,
    ClassNode,
    FunctionNode,
    ParsedFile,
    SymbolNode,
    file_id,
    symbol_id,
)
from ripple.graph.schema import UnresolvedReason as R

_BUILTINS = frozenset(dir(builtins))
_DOTTED = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")
_TASK_DECORATORS = frozenset({"task", "shared_task"})
_TASK_CALLS = frozenset({"delay", "apply_async"})


@dataclass(frozen=True)
class _Sym:
    file: str
    qual: str


@dataclass(frozen=True)
class _Mod:
    module: str


@dataclass(frozen=True)
class _Inst:
    """A name bound once to `Class(...)`: an instance of an in-repo class"""

    file: str
    qual: str


@dataclass(frozen=True)
class _Fail:
    reason: R


_Entity = _Sym | _Mod | _Inst | _Fail


def _is_task(node: SymbolNode) -> bool:
    """A function decorated with Celery's `task` or `shared_task`"""
    return isinstance(node, FunctionNode) and any(
        d.partition("(")[0].rpartition(".")[2] in _TASK_DECORATORS
        for d in node.decorators
    )


class CallResolver:
    def __init__(
        self, parsed: Mapping[str, ParsedFile], modules: ModuleResolver
    ) -> None:
        self._mods = modules
        self._syms: dict[str, dict[str, SymbolNode]] = {
            path: {n.qualname: n for n in pf.nodes if isinstance(n, SymbolNode)}
            for path, pf in parsed.items()
        }
        self._instances: dict[tuple[str, str], _Entity] = {}
        self._resolving: set[tuple[str, str]] = set()

    def resolve(self, path: str, ref: CallRef) -> tuple[list[str], R | None]:
        """Returns (target node ids, None) or ([], reason)"""
        parts = ref.target.split(".")
        caller_qual = ref.caller_id.partition("::")[2] or None

        if parts[0] in ("self", "cls") and caller_qual:
            ent = self._resolve_self(path, parts, caller_qual)
        else:
            ent = self._resolve_chain(path, parts, caller_qual)
        return self._targets(ent)

    # entry points
    def base_classes(self, path: str, cls_qual: str) -> list[str]:
        """Node ids of a class's in-repo base classes, in declaration order"""
        cls = self._syms.get(path, {}).get(cls_qual)
        if not isinstance(cls, ClassNode):
            return []
        out: list[str] = []
        for raw in cls.bases:
            base = self._resolve_base(path, raw)
            if not isinstance(base, _Sym):
                continue
            node = self._syms[base.file][base.qual]
            if isinstance(node, ClassNode):
                out.append(node.id)
        return out

    def class_of(
        self, path: str, dotted: str, scope_qual: str | None
    ) -> tuple[str, int] | None:
        parts = dotted.split(".")
        ent = self._resolve_head(path, parts[0], scope_qual, dotted=len(parts) > 1)
        consumed = 1
        while True:
            if isinstance(ent, (_Fail, _Inst)):
                return None
            if isinstance(ent, _Sym):
                node = self._syms[ent.file][ent.qual]
                return (node.id, consumed) if isinstance(node, ClassNode) else None
            if consumed >= len(parts):
                return None
            ent = self._step(ent, parts[consumed], more=consumed < len(parts) - 1)
            consumed += 1

    def _resolve_self(self, path: str, parts: list[str], caller_qual: str) -> _Entity:
        cls = self._enclosing_class(path, caller_qual)
        if cls is None or len(parts) != 2:
            return _Fail(R.UNKNOWN_RECEIVER)
        member, external = self._lookup_member(path, cls, parts[1], set())
        if member is None:
            return _Fail(R.EXTERNAL if external else R.NOT_FOUND)
        return _Sym(member.file_path, member.qualname)

    def _resolve_chain(
        self, path: str, parts: list[str], caller_qual: str | None
    ) -> _Entity:
        ent = self._resolve_head(path, parts[0], caller_qual, dotted=len(parts) > 1)
        last = len(parts) - 1
        for i, part in enumerate(parts[1:], start=1):
            if isinstance(ent, _Fail):
                break
            ent = self._step(ent, part, more=i < last)
        return ent

    def _targets(self, ent: _Entity) -> tuple[list[str], R | None]:
        if isinstance(ent, _Fail):
            return [], ent.reason
        if isinstance(ent, _Mod):
            return [], R.NOT_FOUND
        if isinstance(ent, _Inst):
            call, external = self._lookup_member(ent.file, ent.qual, "__call__", set())
            if call is not None:
                return [call.id], None
            return [], R.EXTERNAL if external else R.NOT_FOUND
        node = self._syms[ent.file][ent.qual]
        ids = [node.id]
        if isinstance(node, ClassNode):
            init, _ = self._lookup_member(ent.file, ent.qual, "__init__", set())
            if init is not None and init.id != node.id:
                ids.append(init.id)
        return ids, None

    # name resolution
    def _resolve_head(
        self, path: str, name: str, caller_qual: str | None, dotted: bool
    ) -> _Entity:
        syms = self._syms[path]

        if caller_qual:
            parts = caller_qual.split(".")
            for k in range(len(parts), 0, -1):
                scope = ".".join(parts[:k])
                if isinstance(syms.get(scope), ClassNode):
                    continue
                cand = f"{scope}.{name}"
                if cand in syms:
                    return _Sym(path, cand)
                scope_id = symbol_id(path, scope)
                inst = self._instance(path, scope_id, name)
                if inst is not None:
                    return inst
                if name in self._mods.bound.get(scope_id, frozenset()):
                    return _Fail(R.UNKNOWN_RECEIVER)

        inst = self._instance(path, file_id(path), name)
        if inst is not None:
            return inst

        if name in self._mods.top_level[path]:
            return _Sym(path, name)

        binding = self._mods.imports(path).bindings.get(name)
        if binding is not None:
            return self._from_binding(binding)

        if name in _BUILTINS:
            return _Fail(R.BUILTIN)
        return _Fail(R.UNKNOWN_RECEIVER if dotted else R.NOT_FOUND)

    def _step(self, ent: _Mod | _Sym | _Inst, part: str, more: bool = False) -> _Entity:
        if isinstance(ent, _Mod):
            sub = f"{ent.module}.{part}"
            if self._mods.index.is_ambiguous(sub):
                return _Fail(R.AMBIGUOUS)
            return self._from_binding(self._mods.resolve_from(ent.module, part))

        if isinstance(ent, _Inst):
            member, external = self._lookup_member(ent.file, ent.qual, part, set())
            if member is not None:
                return _Sym(member.file_path, member.qualname)
            if external:
                return _Fail(R.EXTERNAL)
            return _Fail(R.UNKNOWN_RECEIVER if more else R.NOT_FOUND)

        node = self._syms[ent.file][ent.qual]
        if isinstance(node, ClassNode):
            member, external = self._lookup_member(ent.file, ent.qual, part, set())
            if member is not None:
                return _Sym(member.file_path, member.qualname)
            return _Fail(R.EXTERNAL if external else R.NOT_FOUND)
        if not more and part in _TASK_CALLS and _is_task(node):
            return ent
        return _Fail(R.NOT_FOUND)

    def _from_binding(self, b: Binding) -> _Entity:
        if b.kind == "module":
            return _Mod(b.module)
        if b.file is None:
            return _Fail(self._module_failure(b.module))
        if b.symbol is None:
            if b.name is not None:
                inst = self._instance(b.file, file_id(b.file), b.name)
                if inst is not None:
                    return inst
            return _Fail(R.UNKNOWN_RECEIVER)
        return _Sym(b.file, b.symbol)

    def _module_failure(self, module: str) -> R:
        idx = self._mods.index
        if idx.is_ambiguous(module):
            return R.AMBIGUOUS
        if idx.has_package(module):
            return R.NOT_FOUND
        return R.EXTERNAL

    # instances
    def _instance(self, path: str, scope_id: str, name: str) -> _Entity | None:
        """What a name bound once to a call is, or None if it is not such a name"""
        ref = self._mods.instances.get(scope_id, {}).get(name)
        if ref is None:
            return None
        key = (scope_id, name)
        cached = self._instances.get(key)
        if cached is not None:
            return cached
        if key in self._resolving:
            return _Fail(R.UNKNOWN_RECEIVER)

        self._resolving.add(key)
        try:
            scope_qual = scope_id.partition("::")[2] or None
            made = self._resolve_chain(path, ref.ctor.split("."), scope_qual)
        finally:
            self._resolving.discard(key)

        result = self._instance_of(made)
        self._instances[key] = result
        return result

    def _instance_of(self, made: _Entity) -> _Entity:
        if isinstance(made, _Sym):
            if isinstance(self._syms[made.file][made.qual], ClassNode):
                return _Inst(made.file, made.qual)
            return _Fail(R.UNKNOWN_RECEIVER)
        if isinstance(made, _Fail) and made.reason == R.EXTERNAL:
            return made
        return _Fail(R.UNKNOWN_RECEIVER)

    # classes
    def _enclosing_class(self, path: str, caller_qual: str) -> str | None:
        parts = caller_qual.split(".")
        for k in range(len(parts) - 1, 0, -1):
            qual = ".".join(parts[:k])
            if isinstance(self._syms[path].get(qual), ClassNode):
                return qual
        return None

    def _resolve_base(self, path: str, raw: str) -> _Entity:
        name = raw.partition("[")[0].strip()
        if not _DOTTED.fullmatch(name):
            return _Fail(R.NOT_FOUND)
        return self._resolve_chain(path, name.split("."), None)

    def _lookup_member(
        self, path: str, cls_qual: str, name: str, seen: set[tuple[str, str]]
    ) -> tuple[SymbolNode | None, bool]:
        key = (path, cls_qual)
        if key in seen:
            return None, False
        seen.add(key)

        member = self._syms[path].get(f"{cls_qual}.{name}")
        if member is not None:
            return member, False

        cls = self._syms[path][cls_qual]
        if not isinstance(cls, ClassNode):
            return None, False

        external = False
        for raw in cls.bases:
            base = self._resolve_base(path, raw)
            if isinstance(base, _Sym) and isinstance(
                self._syms[base.file][base.qual], ClassNode
            ):
                member, ext = self._lookup_member(base.file, base.qual, name, seen)
                if member is not None:
                    return member, False
                external = external or ext
            elif not (isinstance(base, _Fail) and base.reason == R.BUILTIN):
                external = True
        return None, external
