from __future__ import annotations

import builtins
import re
from collections.abc import Mapping
from dataclasses import dataclass

from ripple.graph.resolver import Binding, ModuleResolver
from ripple.graph.schema import (
    CallRef,
    ClassNode,
    ParsedFile,
    SymbolNode,
)
from ripple.graph.schema import UnresolvedReason as R

_BUILTINS = frozenset(dir(builtins))
_DOTTED = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")


@dataclass(frozen=True)
class _Sym:
    file: str
    qual: str


@dataclass(frozen=True)
class _Mod:
    module: str


@dataclass(frozen=True)
class _Fail:
    reason: R


_Entity = _Sym | _Mod | _Fail


class CallResolver:
    def __init__(
        self, parsed: Mapping[str, ParsedFile], modules: ModuleResolver
    ) -> None:
        self._mods = modules
        self._syms: dict[str, dict[str, SymbolNode]] = {
            path: {n.qualname: n for n in pf.nodes if isinstance(n, SymbolNode)}
            for path, pf in parsed.items()
        }

    def resolve(self, path: str, ref: CallRef) -> tuple[list[str], R | None]:
        """Returns (target node ids, None) or ([], reason)."""
        parts = ref.target.split(".")
        caller_qual = ref.caller_id.partition("::")[2] or None

        if parts[0] in ("self", "cls") and caller_qual:
            ent = self._resolve_self(path, parts, caller_qual)
        else:
            ent = self._resolve_chain(path, parts, caller_qual)
        return self._targets(ent)

    # entry points
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
        for part in parts[1:]:
            if isinstance(ent, _Fail):
                break
            ent = self._step(ent, part)
        return ent

    def _targets(self, ent: _Entity) -> tuple[list[str], R | None]:
        if isinstance(ent, _Fail):
            return [], ent.reason
        if isinstance(ent, _Mod):
            return [], R.NOT_FOUND
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

        if name in self._mods.top_level[path]:
            return _Sym(path, name)

        binding = self._mods.imports(path).bindings.get(name)
        if binding is not None:
            return self._from_binding(binding)

        if name in _BUILTINS:
            return _Fail(R.BUILTIN)
        return _Fail(R.UNKNOWN_RECEIVER if dotted else R.NOT_FOUND)

    def _step(self, ent: _Mod | _Sym, part: str) -> _Entity:
        if isinstance(ent, _Mod):
            sub = f"{ent.module}.{part}"
            if self._mods.index.is_ambiguous(sub):
                return _Fail(R.AMBIGUOUS)
            return self._from_binding(self._mods.resolve_from(ent.module, part))

        node = self._syms[ent.file][ent.qual]
        if isinstance(node, ClassNode):
            member, external = self._lookup_member(ent.file, ent.qual, part, set())
            if member is not None:
                return _Sym(member.file_path, member.qualname)
            return _Fail(R.EXTERNAL if external else R.NOT_FOUND)
        return _Fail(R.NOT_FOUND)

    def _from_binding(self, b: Binding) -> _Entity:
        if b.kind == "module":
            return _Mod(b.module)
        if b.file is None:
            return _Fail(self._module_failure(b.module))
        if b.symbol is None:
            return _Fail(R.UNKNOWN_RECEIVER)
        return _Sym(b.file, b.symbol)

    def _module_failure(self, module: str) -> R:
        idx = self._mods.index
        if idx.is_ambiguous(module):
            return R.AMBIGUOUS
        if idx.has_package(module):
            return R.NOT_FOUND
        return R.EXTERNAL

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
