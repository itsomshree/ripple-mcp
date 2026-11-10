from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from ripple.graph.builder import CodeGraph
from ripple.graph.query import match_tier
from ripple.graph.schema import Node, NodeType
from ripple.parsers.fastapi_parser import HTTP_METHODS

DEFAULT_LIMIT = 10
_SUGGESTIONS = 5

_ENDPOINT = re.compile(rf"^({'|'.join(sorted(HTTP_METHODS))})\s+(/\S*)$", re.IGNORECASE)
_SEGMENTS = re.compile(r"::|[./\s]+")


class NodeFinder(Protocol):
    def find_nodes(
        self,
        name: str,
        types: Iterable[NodeType] | None = None,
        limit: int = 50,
    ) -> list[Node]: ...


class ResolveStatus(StrEnum):
    OK = "ok"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class Resolution(BaseModel):
    status: ResolveStatus
    query: str
    node: Node | None = None
    candidates: list[Node] = Field(default_factory=list)


def _matches(node: Node, q: str) -> bool:
    path = getattr(node, "path", None)
    return (
        node.id == q
        or getattr(node, "qualname", None) == q
        or node.name == q
        or (path is not None and path.endswith("/" + q))
    )


class GraphFinder:
    def __init__(self, graph: CodeGraph) -> None:
        self._nodes = graph.nodes

    def find_nodes(
        self,
        name: str,
        types: Iterable[NodeType] | None = None,
        limit: int = 50,
    ) -> list[Node]:
        if not name:
            return []
        wanted = set(types) if types is not None else None
        found = [
            n
            for n in self._nodes.values()
            if (wanted is None or n.type in wanted) and _matches(n, name)
        ]
        found.sort(key=lambda n: (match_tier(n, name), n.id))
        return found[:limit]


def _variants(query: str) -> list[str]:
    q = query.strip().replace("\\", "/")
    m = _ENDPOINT.match(q)
    if m is None:
        return [q]
    canon = f"{m[1].upper()} {m[2]}"
    out = [canon]
    if len(m[2]) > 1 and m[2].endswith("/"):
        out.append(canon.rstrip("/"))
    return out


def _suggest(
    finder: NodeFinder, q: str, types: Iterable[NodeType] | None
) -> list[Node]:
    kinds = list(types) if types is not None else None
    for seg in reversed(_SEGMENTS.split(q)):
        if seg and seg != q:
            found = finder.find_nodes(seg, kinds, _SUGGESTIONS)
            if found:
                return found
    return []


def resolve_entity(
    finder: NodeFinder,
    query: str,
    *,
    types: Iterable[NodeType] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Resolution:
    kinds = list(types) if types is not None else None
    q = query.strip()
    if not q:
        return Resolution(status=ResolveStatus.NOT_FOUND, query=query)

    found: list[Node] = []
    used = q
    for used in _variants(q):
        found = finder.find_nodes(used, kinds)
        if found:
            break

    if not found:
        return Resolution(
            status=ResolveStatus.NOT_FOUND,
            query=q,
            candidates=_suggest(finder, q, kinds),
        )

    best = match_tier(found[0], used)
    top = [n for n in found if match_tier(n, used) == best]
    if len(top) == 1:
        return Resolution(status=ResolveStatus.OK, query=q, node=top[0])
    return Resolution(status=ResolveStatus.AMBIGUOUS, query=q, candidates=top[:limit])
