"""Scan a repository and write its graph to Neo4j"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from neo4j.exceptions import DriverError, Neo4jError
from pydantic import ValidationError

from ripple.config import get_settings
from ripple.graph.store import GraphStore
from ripple.scanner.repo_scanner import ScanError, build_graph, scan_repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-path", type=Path, help="repository to scan (default: REPO_PATH)"
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip files whose repo-relative path matches (repeatable); "
        "'*' also matches '/', so 'tests/fixtures/*' covers nested files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the graph and print the summary without writing to Neo4j",
    )
    args = parser.parse_args(argv)

    settings = None
    if args.repo_path is None or not args.dry_run:
        try:
            settings = get_settings()
        except ValidationError as exc:
            print(f"configuration error (see .env.example): {exc}", file=sys.stderr)
            return 2
    repo = args.repo_path or (settings.repo_path if settings else None)
    if repo is None:
        parser.error("no repo path: pass --repo-path or set REPO_PATH")

    logging.basicConfig(
        level=settings.log_level if settings else "INFO",
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.dry_run:
            result = build_graph(repo, exclude=args.exclude)
        else:
            with GraphStore.from_settings(settings) as store:
                store.verify()
                result = scan_repo(repo, store, exclude=args.exclude)
    except ScanError as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        return 1
    except (DriverError, Neo4jError, OSError) as exc:
        print(f"could not reach Neo4j: {exc}", file=sys.stderr)
        return 1

    print(result.summary.format())
    return 0


if __name__ == "__main__":
    sys.exit(main())
