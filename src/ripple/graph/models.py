from __future__ import annotations

from collections.abc import Mapping

from ripple.graph.calls import CallResolver
from ripple.graph.schema import (
    ClassNode,
    DBFacts,
    DbIssue,
    ModelDecl,
    ModelResolution,
    ParsedFile,
    ResolvedModel,
)
from ripple.graph.schema import DbIssueReason as Why


def resolve_models(
    facts: Mapping[str, DBFacts],
    parsed: Mapping[str, ParsedFile],
    calls: CallResolver,
) -> ModelResolution:
    return _Resolver(facts, parsed, calls).run()


class _Resolver:
    def __init__(
        self,
        facts: Mapping[str, DBFacts],
        parsed: Mapping[str, ParsedFile],
        calls: CallResolver,
    ) -> None:
        self._calls = calls
        self._classes: dict[str, ClassNode] = {
            n.id: n
            for pf in parsed.values()
            for n in pf.nodes
            if isinstance(n, ClassNode)
        }
        self._decls: dict[str, ModelDecl] = {}
        for path in sorted(facts):
            for decl in facts[path].models:
                if decl.class_id in self._classes:
                    self._decls.setdefault(decl.class_id, decl)
        self._bases: dict[str, list[str]] = {}

    def run(self) -> ModelResolution:
        issues = [
            DbIssue(
                file_path=d.file_path,
                line=d.line,
                subject=d.class_id,
                reason=Why.NON_LITERAL_TABLENAME,
            )
            for d in self._decls.values()
            if d.non_literal and not d.abstract
        ]
        models: list[ResolvedModel] = []
        for cid in sorted(self._classes):
            table = self._table_of(cid, frozenset())
            if table is None:
                continue
            models.append(
                ResolvedModel(
                    class_id=cid,
                    file_path=self._classes[cid].file_path,
                    table=table,
                    own=cid in self._decls,
                )
            )
        return ModelResolution(models=models, issues=issues)

    def _table_of(self, cid: str, stack: frozenset[str]) -> str | None:
        if cid in stack:
            return None
        decl = self._decls.get(cid)
        if decl is not None:
            if decl.abstract or decl.table is None:
                return None
            return decl.table.lower()

        here = stack | {cid}
        for base in self._base_ids(cid):
            table = self._table_of(base, here)
            if table is not None:
                return table
        return None

    def _base_ids(self, cid: str) -> list[str]:
        cached = self._bases.get(cid)
        if cached is None:
            cls = self._classes[cid]
            cached = self._calls.base_classes(cls.file_path, cls.qualname)
            self._bases[cid] = cached
        return cached
