"""Measure what an incremental rescan saves on a real repo"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from ripple.graph.diff import diff_graphs
from ripple.graph.schema import FileNode
from ripple.scanner.incremental import IncrementalScanner
from ripple.scanner.repo_scanner import SKIP_DIRS, ScanError

T = TypeVar("T")
PROBE = "\n\ndef _ripple_bench_probe():\n    pass\n"


def timed(fn: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - started


def repeat(n: int, fn: Callable[[], T]) -> tuple[T, float]:
    runs = [timed(fn) for _ in range(n)]
    return runs[-1][0], sorted(t for _, t in runs)[n // 2]


def ignore(directory: str, names: list[str]) -> set[str]:
    skip = {n for n in names if n in SKIP_DIRS or n.endswith(".egg-info")}
    skip |= {n for n in names if (Path(directory) / n / "pyvenv.cfg").is_file()}
    return skip


def pick_file(graph, requested: str | None) -> str:
    if requested:
        return requested
    candidates = sorted(
        n.id
        for n in graph.nodes.values()
        if isinstance(n, FileNode)
        and not n.is_test
        and not n.id.endswith("__init__.py")
    )
    if not candidates:
        raise ScanError("no non-test source file to edit; pass --file")
    return candidates[len(candidates) // 2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-path", type=Path, required=True)
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    parser.add_argument("--repeat", type=int, default=5, help="runs per measurement")
    parser.add_argument(
        "--file", help="repo-relative file to edit (default: a middle one)"
    )
    parser.add_argument(
        "--neo4j",
        action="store_true",
        help="also time store writes (WIPES the database)",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        shutil.copytree(
            args.repo_path.expanduser(),
            repo,
            ignore=ignore,
            symlinks=True,
        )
        scanner = IncrementalScanner(repo, exclude=args.exclude)
        n = args.repeat

        def cold_scan():
            scanner.reset()
            return scanner.refresh()

        cold, t_cold = repeat(n, cold_scan)
        _, t_idle = repeat(n, scanner.refresh)
        _, t_build = repeat(n, scanner._builder.build)

        rel = pick_file(cold.result.graph, args.file)

        def edited_scan():
            with (repo / rel).open("a", encoding="utf-8") as f:
                f.write(PROBE)
            return scanner.refresh()

        edit, t_edit = repeat(n, edited_scan)
        delta, t_diff = repeat(
            n, lambda: diff_graphs(cold.result.graph, edit.result.graph)
        )

        g = cold.result.graph
        print(
            f"repo: {args.repo_path}  ({cold.result.summary.files_scanned} files, "
            f"{len(g.nodes)} nodes, {len(g.edges)} edges)"
        )
        print(
            f"edited: {rel}  ->  {edit.changes.changed} changed, "
            f"delta {delta.counts()}\n"
        )
        print(f" (median of {n} runs)")
        rows = [
            ("cold scan (read + parse + build)", t_cold),
            ("idle rescan (read + hash + build)", t_idle),
            ("  of which build() alone", t_build),
            ("  so parsing the whole repo is about", t_cold - t_idle),
            ("one-file rescan (read + hash + parse 1 + build)", t_edit),
            ("diff old vs new graph", t_diff),
        ]
        for label, seconds in rows:
            print(f"  {label:<52}{seconds * 1000:9.1f} ms")

        if args.neo4j:
            from ripple.graph.store import GraphStore
            from ripple.graph.sync import sync_graph

            with GraphStore.from_settings() as store:
                store.verify()
                _, t_replace = timed(lambda: store.replace_graph(cold.result.graph))
                _, t_noop = timed(lambda: sync_graph(store, cold.result.graph))
                _, t_sync = timed(lambda: sync_graph(store, edit.result.graph))
            print("\n  neo4j")
            for label, seconds in (
                ("replace_graph (full wipe + write)", t_replace),
                ("sync_graph, nothing changed (load + diff)", t_noop),
                ("sync_graph, one-file change", t_sync),
            ):
                print(f"  {label:<52}{seconds * 1000:9.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
