from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache

from ripple.graph.schema import (
    APIEndpointNode,
    ClassNode,
    Edge,
    Evidence,
    FileNode,
    FunctionNode,
    Node,
    TestNode,
)

_RANK = {Evidence.CALL: 3, Evidence.HTTP: 2, Evidence.NAME: 1}


class CoverageLinks:
    def __init__(self) -> None:
        self._best: dict[tuple[str, str], Evidence] = {}

    def add(self, test_id: str, target_id: str, via: Evidence) -> None:
        key = (test_id, target_id)
        current = self._best.get(key)
        if current is None or _RANK[via] > _RANK[current]:
            self._best[key] = via

    def items(self) -> list[tuple[str, str, Evidence]]:
        return [(t, d, v) for (t, d), v in sorted(self._best.items())]


def direct_call_targets(
    nodes: Mapping[str, Node], calls: Iterable[Edge]
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for e in calls:
        if not isinstance(nodes.get(e.src), TestNode):
            continue
        target = nodes.get(e.dst)
        if not isinstance(target, (FunctionNode, ClassNode)):
            continue
        home = nodes.get(target.file_path)
        if isinstance(home, FileNode) and home.is_test:
            continue
        out.append((e.src, e.dst))
    return out


_WILD = re.compile(r"\{[^{}]*\}")
_TAIL = re.compile(r"\{[^{}:]*:path\}")
_HOLE = "\x00"


def _split(path: str) -> tuple[str, ...]:
    stripped = path.rstrip("/")
    return tuple(stripped.split("/")[1:]) if stripped else ()


def _is_wild(seg: str) -> bool:
    return _WILD.fullmatch(seg) is not None


def _is_literal(seg: str) -> bool:
    return "{" not in seg


@lru_cache(maxsize=1024)
def _regex(seg: str) -> re.Pattern[str]:
    return re.compile("[^/]+".join(re.escape(p) for p in _WILD.split(seg)))


def _segments_match(a: str, b: str) -> bool:
    if _is_wild(a) or _is_wild(b):
        return True
    if _is_literal(a) and _is_literal(b):
        return a == b
    for pattern, other in ((a, b), (b, a)):
        if not _is_literal(pattern) and _regex(pattern).fullmatch(
            _WILD.sub(_HOLE, other)
        ):
            return True
    return False


def _score(call: tuple[str, ...], route: tuple[str, ...]) -> int | None:
    score = 0
    for c, r in zip(call, route):
        if not _segments_match(c, r):
            return None
        if _is_literal(c) and _is_literal(r):
            score += 1
    return score


@dataclass(frozen=True)
class _Route:
    id: str
    segs: tuple[str, ...]


class EndpointIndex:
    def __init__(self, endpoints: Iterable[APIEndpointNode]) -> None:
        self._fixed: dict[tuple[str, int], list[_Route]] = defaultdict(list)
        self._tail: dict[str, list[_Route]] = defaultdict(list)
        for ep in endpoints:
            segs = _split(ep.route)
            if segs and _TAIL.fullmatch(segs[-1]):
                self._tail[ep.method].append(_Route(ep.id, segs[:-1]))
            else:
                self._fixed[(ep.method, len(segs))].append(_Route(ep.id, segs))

    def match(self, method: str, path: str) -> list[str]:
        call = _split(path)
        pool = [
            *self._fixed.get((method, len(call)), ()),
            *(r for r in self._tail.get(method, ()) if len(call) > len(r.segs)),
        ]
        scored = [
            (score, r.id) for r in pool if (score := _score(call, r.segs)) is not None
        ]
        if not scored:
            return []
        best = max(s for s, _ in scored)
        return sorted({eid for s, eid in scored if s == best})
