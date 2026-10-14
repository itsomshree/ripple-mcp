from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from ripple.graph.schema import ParsedFile, SymbolNode

DEFAULT_SOURCE_ROOTS: tuple[str, ...] = ("src", "")


def _strip_root(path: str, root: str) -> str | None:
    if not root:
        return path
    prefix = root.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else None


def _dotted(rel_path: str) -> str | None:
    p = PurePosixPath(rel_path)
    if p.suffix != ".py":
        return None
    parts = list(p.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or None


class ModuleIndex:
    def __init__(
        self,
        paths: Iterable[str],
        source_roots: tuple[str, ...] = DEFAULT_SOURCE_ROOTS,
    ) -> None:
        self._by_name: dict[str, set[str]] = defaultdict(set)
        self._primary: dict[str, str] = {}
        self._packages: set[str] = set()
        self._prefixes: set[str] = set()

        for path in paths:
            if PurePosixPath(path).name == "__init__.py":
                self._packages.add(path)
            for root in source_roots:
                rel = _strip_root(path, root)
                name = _dotted(rel) if rel is not None else None
                if name is None:
                    continue
                self._by_name[name].add(path)
                self._primary.setdefault(path, name)
                parts = name.split(".")
                for i in range(1, len(parts) + 1):
                    self._prefixes.add(".".join(parts[:i]))

    def resolve(self, module: str) -> str | None:
        candidates = self._by_name.get(module)
        if candidates and len(candidates) == 1:
            return next(iter(candidates))
        return None

    def has_package(self, dotted: str) -> bool:
        return dotted in self._prefixes

    def module_of(self, path: str) -> str | None:
        return self._primary.get(path)

    def package_of(self, path: str) -> str | None:
        primary = self._primary.get(path)
        if primary is None:
            return None
        if path in self._packages:
            return primary
        return primary.rpartition(".")[0]

    def absolute(self, path: str, module: str, level: int) -> str | None:
        if level == 0:
            return module
        pkg = self.package_of(path)
        if pkg is None:
            return None
        parts = pkg.split(".") if pkg else []
        keep = len(parts) - (level - 1)
        if keep < 1:
            return None
        base = parts[:keep]
        if module:
            base.append(module)
        return ".".join(base)

    def is_ambiguous(self, module: str) -> bool:
        return len(self._by_name.get(module, ())) > 1


@dataclass(frozen=True)
class Binding:
    kind: Literal["module", "symbol"]
    module: str
    file: str | None
    symbol: str | None = None
    name: str | None = field(default=None, compare=False)


@dataclass
class FileImports:
    bindings: dict[str, Binding] = field(default_factory=dict)
    depends_on: set[str] = field(default_factory=set)


class ModuleResolver:
    def __init__(
        self,
        parsed: Mapping[str, ParsedFile],
        source_roots: tuple[str, ...] = DEFAULT_SOURCE_ROOTS,
    ) -> None:
        self._parsed = parsed
        self.index = ModuleIndex(parsed, source_roots)
        self.top_level: dict[str, set[str]] = {}
        self._from_imports: dict[str, dict[str, tuple[str, str]]] = {}
        self._stars: dict[str, list[str]] = {}
        self._cache: dict[str, FileImports] = {}

        for path, pf in parsed.items():
            self.top_level[path] = {
                n.qualname
                for n in pf.nodes
                if isinstance(n, SymbolNode) and "." not in n.qualname
            }
            froms: dict[str, tuple[str, str]] = {}
            stars: list[str] = []
            for imp in pf.imports:
                if not imp.names:
                    continue
                mod = self.index.absolute(path, imp.module, imp.level)
                if mod is None:
                    continue
                for name, asname in imp.names:
                    if name == "*":
                        stars.append(mod)
                    else:
                        froms[asname or name] = (mod, name)
            self._from_imports[path] = froms
            self._stars[path] = stars

    # public
    def imports(self, path: str) -> FileImports:
        cached = self._cache.get(path)
        if cached is None:
            cached = self._cache[path] = self._resolve_imports(path)
        return cached

    def resolve_from(
        self,
        module: str,
        name: str,
        _seen: set[tuple[str, str]] | None = None,
    ) -> Binding:
        seen = _seen if _seen is not None else set()
        mod_file = self.index.resolve(module)

        if (module, name) in seen:
            return Binding("symbol", module, mod_file, None, name)
        seen.add((module, name))

        if mod_file is not None:
            if name in self.top_level[mod_file]:
                return Binding("symbol", module, mod_file, name, name)
            reexport = self._from_imports[mod_file].get(name)
            if reexport is not None:
                return self.resolve_from(*reexport, _seen=seen)
            for star in self._stars[mod_file]:
                found = self.resolve_from(star, name, seen)
                if found.file is not None and (
                    found.kind == "module" or found.symbol is not None
                ):
                    return found

        sub = f"{module}.{name}" if module else name
        sub_file = self.index.resolve(sub)
        if sub_file is not None:
            return Binding("module", sub, sub_file)
        if mod_file is not None:
            return Binding("symbol", module, mod_file, None, name)
        return Binding("symbol", module, None, name, name)  # external

    # internals
    def _resolve_imports(self, path: str) -> FileImports:
        result = FileImports()
        for imp in self._parsed[path].imports:
            if imp.names:
                mod = self.index.absolute(path, imp.module, imp.level)
                if mod is not None:
                    self._bind_from(mod, imp.names, result)
            else:
                self._bind_import(imp.module, imp.alias, result)
        result.depends_on.discard(path)
        return result

    def _bind_import(self, module: str, alias: str | None, out: FileImports) -> None:
        target = self.index.resolve(module)
        if target is not None:
            out.depends_on.add(target)
        if alias:
            out.bindings[alias] = Binding("module", module, target)
        else:  # `import a.b.c` binds only the head name `a`
            head = module.partition(".")[0]
            out.bindings[head] = Binding("module", head, self.index.resolve(head))

    def _bind_from(
        self,
        module: str,
        names: list[tuple[str, str | None]],
        out: FileImports,
    ) -> None:
        mod_file = self.index.resolve(module)
        if mod_file is not None:
            out.depends_on.add(mod_file)

        for name, asname in names:
            if name == "*":
                if mod_file is not None:
                    for sym in sorted(self.top_level[mod_file]):
                        if not sym.startswith("_"):
                            out.bindings[sym] = Binding("symbol", module, mod_file, sym)
                continue
            binding = self.resolve_from(module, name)
            out.bindings[asname or name] = binding
            if binding.kind == "module" and binding.file is not None:
                out.depends_on.add(binding.file)
