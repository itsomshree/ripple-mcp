from __future__ import annotations

from collections.abc import Mapping

from ripple.graph.calls import CallResolver
from ripple.graph.schema import (
    AccessMode,
    DBFacts,
    ModelResolution,
    TableUse,
    UsageResolution,
)

_Key = tuple[str, str, AccessMode, str]


def resolve_usage(
    facts: Mapping[str, DBFacts],
    calls: CallResolver,
    models: ModelResolution,
) -> UsageResolution:
    table_of = {m.class_id: m.table for m in models.models}
    found: dict[_Key, TableUse] = {}

    def add(use: TableUse) -> None:
        key = (use.owner_id, use.table, use.access, use.source)
        current = found.get(key)
        if current is None or use.line < current.line:
            found[key] = use

    for path in sorted(facts):
        for use in facts[path].uses:
            add(use)
        for ref in facts[path].refs:
            scope = ref.owner_id.partition("::")[2] or None
            hit = calls.class_of(path, ref.ref, scope)
            if hit is None:
                continue
            class_id, consumed = hit
            table = table_of.get(class_id)
            if table is None:
                continue
            access = ref.access
            if ref.called and consumed == ref.ref.count(".") + 1:
                access = AccessMode.WRITE
            add(
                TableUse(
                    owner_id=ref.owner_id,
                    table=table,
                    access=access,
                    line=ref.line,
                    source="orm",
                )
            )
    return UsageResolution(uses=[found[k] for k in sorted(found)])
