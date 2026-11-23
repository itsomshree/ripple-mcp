# Architecture

RippleMCP answers "what breaks if I change X" for a Python/FastAPI/PostgreSQL
repo. It scans the repo into a graph, stores the graph in Neo4j, and exposes
read-only analysis tools over MCP. The graph model and its limits are in
`docs/graph_schema.md`; this file covers how the pieces fit together.

## Pipeline

```
scanner -> parsers -> graph builder -> store (Neo4j) -> analysis -> tools -> MCP server
```

| Layer | Module | Job |
|---|---|---|
| Scanner | `scanner/repo_scanner.py`, `scanner/incremental.py` | Walk the repo and parse each file once. `IncrementalScanner` keeps the parsed facts between scans, so a rescan reparses only files whose content changed. `build_graph` builds in memory; `scan_repo` also replaces the stored graph |
| Parsers | `parsers/` | Turn one file into facts: definitions, calls, imports, instances, routes, models, SQL, HTTP calls in tests |
| Graph | `graph/` | Resolve facts into nodes and edges (`builder.py`), persist and query them (`store.py`, `query.py`) |
| Analysis | `analysis/` | Impact, request flow, affected tests, risk score. Works on a `GraphView`, so the same code runs on the in-memory graph and on Neo4j |
| Tools | `tools/` | Thin wrappers that resolve the user's entity string and shape results for a client |
| Services | `services.py` | What the tools run against: `Neo4jServices` in production, `InMemoryServices` for tests and development |
| Server | `server.py` | Registers the six tools with FastMCP and runs it |

## Tools

| Tool | Read-only | Purpose |
|---|---|---|
| `what_breaks_if_i_change` | yes | Blast radius and risk score for a function, class, file, table or endpoint |
| `trace_request_flow` | yes | Forward from an endpoint or function to the code and tables it reaches |
| `affected_tests` | yes | Tests to run after a change, with a ready `pytest` command |
| `status` | yes | Store health, graph counts, last scan summary |
| `scan_repo` | no | Rebuild the graph from the configured repo. Replaces the stored graph |
| `rescan` | no | Bring the graph up to date after edits: reparse changed files, write only the difference. Not destructive |

Defaults: impact follows 5 hops, request flow 6, affected tests 8. Hops are
clamped to 1-15 and list sizes to 1-100. Every result lists at most 25 items
per group by default and reports the total, so a cap is visible.

## Error contract

A request that cannot be answered is data, not an exception, so a client can
act on it.

| Situation | Result |
|---|---|
| Entity name matches several nodes | `status: "ambiguous"` with `candidates`; call again with one id |
| Nothing matches | `status: "not_found"`, with close candidates if any |
| Start node is not valid for the tool (a table for `trace_request_flow`, a test for `affected_tests`) | `status: "unsupported"` |
| Graph is empty | `status: "not_scanned"`, "call scan_repo first" |
| A scan is running | `status: "scan_in_progress"` |

Failures of the server itself raise a tool error: `ScanError` (no repo
configured, path missing, no Python files) with its message, and any Neo4j or
OS error with a generic "graph store is unavailable" message. The details go to
the log, not to the client.

Results also carry `caveats`. They say when a number is a lower bound
(truncated, depth-limited), when a route may be incomplete, or when "no
dependents" is not proof of safety. The server instructions tell clients to
relay them.

## Scans and concurrency

Tools are synchronous and FastMCP runs them in a thread pool, so a long scan
does not block the event loop. `ScanGate` allows one scan (`scan_repo` or
`rescan`) at a time:

- queries and a second scan during a scan return `scan_in_progress`
- `status` always answers, with `scanning: true`

`scan_repo` is wipe-then-write, so a Neo4j failure part-way leaves a partial
graph until the next scan. `rescan` writes in one transaction and diffs
against what is actually stored, so a failure leaves the previous graph intact
and the next `rescan` repairs any earlier damage.

### Rescans

`rescan` hashes every file, reparses the ones whose bytes changed, rebuilds
the graph from all cached facts and writes only the difference to Neo4j. The
rebuild is global because resolution is cross-file: an edit in one file can
add or remove edges that start in another. Details in
`docs/graph_schema.md`, "Incremental rescans".

The parse cache lives in memory. The first `rescan` after a server start
(or after `scan_repo`, which does not fill the cache) parses every file once;
it is still correct, because the result is diffed against the store.

## Running it

Transport is stdio. stdout carries only MCP messages (a test checks this);
logs go to stderr. Configuration comes from the environment or `.env`:

| Variable | Meaning |
|---|---|
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` | Graph store (the password is required) |
| `REPO_PATH` | Repo the server scans, a path on the machine running it |
| `SCAN_EXCLUDE` | Comma-separated globs of repo-relative paths to skip |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `MCP_AUTH_TOKEN` | Reserved for the remote HTTP server; unused over stdio |

Local setup: `docker compose up -d neo4j`, then `uv run python -m ripple.server`.
`scripts/scan_repo.py --dry-run` builds the graph and prints the summary
without Neo4j, which is the quickest way to see how a repo parses.

### Claude Desktop

Add the server to `claude_desktop_config.json` (use the absolute path of `uv`;
desktop apps do not see your shell `PATH`):

```json
{
  "mcpServers": {
    "ripple": {
      "command": "/home/you/.local/bin/uv",
      "args": ["run", "--directory", "/path/to/ripple-mcp",
               "python", "-m", "ripple.server"]
    }
  }
}
```

`--directory` makes the server read the `.env` in the project. The server process 
loads its code once, so quit and reopen the app after changing code or `.env`, then 
run `rescan`: the restarted server has no parse cache, so it reparses everything and 
rewrites whatever the new resolver changed.

## Using the tools well

- Start with `status`, then `scan_repo` if the graph is empty. After that,
- If a method-level question comes back with no dependents, ask about the
  owning class. Callers usually instantiate the class directly, and a class
  start also covers its methods.
- "No dependents found" is not "safe": see the blind spots in
  `docs/graph_schema.md`.
- `affected_tests` gives pytest ids relative to the scanned repo; run pytest
  from that root. For a core class the list is nearly the whole suite, and
  running everything is simpler.
- After a resolver change, restart the server and `rescan`. Exclude
  directories that hold generated or migration code (`SCAN_EXCLUDE`).