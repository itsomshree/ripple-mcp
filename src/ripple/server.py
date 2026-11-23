from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Annotated, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from neo4j.exceptions import DriverError, Neo4jError
from pydantic import Field, ValidationError

from ripple.analysis.affected_tests import DEFAULT_MAX_HOPS as TESTS_HOPS
from ripple.analysis.impact import DEFAULT_MAX_HOPS as IMPACT_HOPS
from ripple.analysis.request_flow import DEFAULT_MAX_HOPS as FLOW_HOPS
from ripple.config import get_settings
from ripple.scanner.repo_scanner import ScanError
from ripple.services import Neo4jServices, ScanInProgress, Services
from ripple.tools._common import (
    DEFAULT_LIST_LIMIT,
    GraphAccess,
    Problem,
    ToolResult,
    ToolStatus,
)
from ripple.tools.affected_tests_tools import AffectedTestsResult
from ripple.tools.affected_tests_tools import affected_tests as affected_tests_impl
from ripple.tools.impact_tools import (
    WhatBreaksResult,
)
from ripple.tools.impact_tools import (
    what_breaks_if_i_change as what_breaks_if_i_change_impl,
)
from ripple.tools.scan_tools import (
    RescanToolResult,
    ScanToolResult,
    StatusResult,
    get_status,
    run_rescan,
    run_scan,
)
from ripple.tools.trace_tools import DEFAULT_FLOW_LIMIT, TraceResult
from ripple.tools.trace_tools import trace_request_flow as trace_request_flow_impl
from ripple.utils.logging import configure_logging

logger = logging.getLogger(__name__)

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
REPLACES_GRAPH = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}
UPDATES_GRAPH = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

EntityArg = Annotated[
    str,
    Field(
        description=(
            "What to analyse: a function or class name (`UserService.get`), "
            "a file path (`services/user_service.py`), a table name (`users`), "
            "or an endpoint (`POST /api/orders`). If the name is ambiguous the "
            "result lists candidates; call again with one candidate's id."
        )
    ),
]
HopsArg = Annotated[int, Field(description="How far to follow dependencies (1-15).")]
LimitArg = Annotated[int, Field(description="Max items listed per group (1-100).")]

_R = TypeVar("_R", bound=ToolResult)


def _store_error(exc: Exception) -> ToolError:
    logger.warning("graph store error: %s: %s", type(exc).__name__, exc)
    return ToolError(
        "The graph store is unavailable. Check that Neo4j is running and that "
        "the server's NEO4J_* settings are correct."
    )


def _on_graph(
    services: Services, result_type: type[_R], call: Callable[[GraphAccess], _R]
) -> _R:
    try:
        return call(services.graph())
    except ScanInProgress:
        return Problem(
            status=ToolStatus.SCAN_IN_PROGRESS,
            message="A scan is replacing the graph. Try again in a moment.",
        ).into(result_type)
    except (DriverError, Neo4jError, OSError) as exc:
        raise _store_error(exc) from exc


def build_server(services: Services) -> FastMCP:
    mcp = FastMCP(
        "ripple-mcp",
        instructions=(
            "Change-impact analysis for a Python/FastAPI/PostgreSQL repo. "
            "Call `status` first; if the graph is empty, call `scan_repo`. "
            "After the repo changes, call `rescan`: it reparses only changed "
            "files and updates only what differs. `scan_repo` is the full "
            "reset. Then use `what_breaks_if_i_change`, `trace_request_flow` "
            "and `affected_tests`. Results carry `caveats`: relay them, because "
            "they say when an answer is only a lower bound."
        ),
    )

    @mcp.tool(annotations=READ_ONLY)
    def what_breaks_if_i_change(
        entity: EntityArg,
        max_hops: HopsArg = IMPACT_HOPS,
        include_importers: bool = False,
        limit: LimitArg = DEFAULT_LIST_LIMIT,
    ) -> WhatBreaksResult:
        """
        Blast radius of changing a function, class, file, table or endpoint.

        Returns a risk score with its factors, plus the affected functions,
        classes, endpoints, tables, tests and files, nearest first. Each item
        says how many hops away it is and which edge reached it.
        """
        return _on_graph(
            services,
            WhatBreaksResult,
            lambda a: what_breaks_if_i_change_impl(
                a,
                entity,
                max_hops=max_hops,
                include_importers=include_importers,
                limit=limit,
            ),
        )

    @mcp.tool(annotations=READ_ONLY)
    def trace_request_flow(
        endpoint: EntityArg,
        max_hops: HopsArg = FLOW_HOPS,
        limit: LimitArg = DEFAULT_FLOW_LIMIT,
    ) -> TraceResult:
        """
        Follow an endpoint (or application function) forward to the code and
        database tables it reaches.

        Hops are distance from the start, not execution order. Tables list
        read/write access and which functions touch them.
        """
        return _on_graph(
            services,
            TraceResult,
            lambda a: trace_request_flow_impl(
                a, endpoint, max_hops=max_hops, limit=limit
            ),
        )

    @mcp.tool(annotations=READ_ONLY)
    def affected_tests(
        entity: EntityArg,
        max_hops: HopsArg = TESTS_HOPS,
        include_importers: bool = False,
        limit: LimitArg = DEFAULT_LIST_LIMIT,
    ) -> AffectedTestsResult:
        """
        Which tests to run after changing something.

        Returns runnable tests nearest first, each with its pytest id and
        whether it reaches the code by a direct call or an HTTP request, plus a
        ready-to-run `pytest_command`. Run pytest from the scanned repo root.
        """
        return _on_graph(
            services,
            AffectedTestsResult,
            lambda a: affected_tests_impl(
                a,
                entity,
                max_hops=max_hops,
                include_importers=include_importers,
                limit=limit,
            ),
        )

    @mcp.tool(annotations=REPLACES_GRAPH)
    def scan_repo() -> ScanToolResult:
        """
        Scan the server's configured repository and replace the stored graph.

        Takes a few seconds on a large repo. Queries are refused with
        status `scan_in_progress` while it runs.
        """
        try:
            return run_scan(services)
        except ScanError as exc:
            raise ToolError(str(exc)) from exc
        except (DriverError, Neo4jError, OSError) as exc:
            raise _store_error(exc) from exc

    @mcp.tool(annotations=UPDATES_GRAPH)
    def rescan() -> RescanToolResult:
        """
        Bring the graph up to date after the repository changed.

        Reparses only files whose content changed and writes only the
        difference to the store, so it is much cheaper than `scan_repo` on a
        large repo. The first rescan after a server restart parses every file
        once, then writes nothing if the graph already matches. Use
        `scan_repo` instead to rebuild from scratch. Queries are refused with
        status `scan_in_progress` while it runs.
        """
        try:
            return run_rescan(services)
        except ScanError as exc:
            raise ToolError(str(exc)) from exc
        except (DriverError, Neo4jError, OSError) as exc:
            raise _store_error(exc) from exc

    @mcp.tool(annotations=READ_ONLY)
    def status() -> StatusResult:
        """Graph store health, node/edge counts and the last scan's summary."""
        return get_status(services)

    return mcp


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"configuration error (see .env.example): {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    configure_logging(settings.log_level)
    services = Neo4jServices(settings)
    try:
        build_server(services).run(show_banner=False)
    finally:
        services.close()


if __name__ == "__main__":
    main()
