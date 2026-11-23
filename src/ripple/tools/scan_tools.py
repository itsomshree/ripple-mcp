from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import Field

from ripple.scanner.incremental import RescanSummary
from ripple.scanner.repo_scanner import ScanSummary
from ripple.services import ScanInProgress, Services
from ripple.tools._common import ToolResult, ToolStatus

_S = TypeVar("_S", bound=ScanSummary)


class ScanToolResult(ToolResult):
    summary: ScanSummary | None = None


class RescanToolResult(ToolResult):
    summary: RescanSummary | None = None


class StatusResult(ToolResult):
    store: str
    store_ok: bool
    store_error: str | None = None
    repo: str | None = None
    scanning: bool = False
    graph_empty: bool = True
    nodes: dict[str, int] = Field(default_factory=dict)
    edges: dict[str, int] = Field(default_factory=dict)
    last_scan: ScanSummary | None = None
    last_scan_at: str | None = None


def public_summary(summary: _S) -> _S:
    """The summary as a client may see it: the repo's name, not the server path"""
    return summary.model_copy(update={"repo_path": Path(summary.repo_path).name})


def run_scan(services: Services) -> ScanToolResult:
    """Full scan of the server-configured repo"""
    try:
        summary = services.scan()
    except ScanInProgress:
        return ScanToolResult(
            status=ToolStatus.SCAN_IN_PROGRESS,
            message="A scan is already running. Try again in a moment.",
        )
    return ScanToolResult(summary=public_summary(summary))


def run_rescan(services: Services) -> RescanToolResult:
    """Incremental scan: reparse changed files, write only the graph difference"""
    try:
        summary = services.rescan()
    except ScanInProgress:
        return RescanToolResult(
            status=ToolStatus.SCAN_IN_PROGRESS,
            message="A scan is already running. Try again in a moment.",
        )
    unchanged = not any(summary.graph_delta.values())
    return RescanToolResult(
        summary=public_summary(summary),
        message=(
            "No graph changes: the stored graph already matches the repository."
            if unchanged
            else None
        ),
    )


def get_status(services: Services) -> StatusResult:
    s = services.status()
    empty = not s.nodes
    return StatusResult(
        store=s.store,
        store_ok=s.store_ok,
        store_error=s.store_error,
        repo=s.repo,
        scanning=s.scanning,
        graph_empty=empty,
        nodes=s.nodes,
        edges=s.edges,
        last_scan=public_summary(s.last_scan) if s.last_scan else None,
        last_scan_at=s.last_scan_at,
        message=(
            "The graph is empty. Call scan_repo to build it."
            if s.store_ok and empty
            else None
        ),
    )
