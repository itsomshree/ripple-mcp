# Graph schema

Defined in `src/ripple/graph/schema.py`. The graph is built in memory by
`GraphBuilder` (`CodeGraph`) and persisted to Neo4j in a later step.

## Nodes

| Type | ID format | Notes |
|---|---|---|
| `File` | repo-relative POSIX path, e.g. `src/ripple/server.py` | `is_test` is set from the path (`test_*.py`, `*_test.py`, `conftest.py`, or a `tests/` directory) |
| `Function` | `<path>::<qualname>`, e.g. `app/users.py::UserService.create` | Methods and nested functions use dotted qualnames; `class_name` is set for methods |
| `Class` | `<path>::<qualname>` | `bases` holds the raw base expressions |
| `APIEndpoint` | `endpoint::<METHOD> <route>` | Full path after router prefixes, e.g. `endpoint::GET /api/orders/{order_id}`. An empty path becomes `/`. `exact` and `mounted` flags, see below |
| `DBTable` | `table: <lowercase name>` | One per table that a model maps to or that SQL/ORM code touches. Schema prefixes are dropped (`public.users` is `table: users`) |
| `Test` | `<path>::<qualname>` | A pytest test function or method, promoted from `Function` in pass 7. Discovery follows pytest's rules: files named `test_*.py` or `*_test.py`; functions starting with `test`, at module level or in a `Test*` class without `__init__` or a `unittest.TestCase` subclass; `@pytest.fixture` functions are excluded; nested functions and nested classes are not detected |

Line spans (`line_start`, `line_end`) start at the first decorator.

## Edges

| Type | Direction | Status |
|---|---|---|
| `DEFINED_IN` | `Function` / `Class` → `File` | Implemented |
| `IMPORTS` | `File` → `File` | Implemented |
| `CALLS` | `File` / `Function` → `Function` / `Class` | Implemented |
| `HANDLES` | `APIEndpoint` → `Function` | Implemented |
| `DEPENDS_ON` | `Function` → `Function` / `Class` | Implemented |
| `MAPS_TO` | `Class` → `DBTable` | Implemented |
| `QUERIES` | `Function` / `File` → `DBTable` | Implemented. Carries `access` |
| `TESTS` | `Test` → `Function` / `Class` / `APIEndpoint` | Implemented. Carries `via` |

Edges have two optional attributes. `access` (`read` or `write`) is set only by
`QUERIES`. `via` (`call`, `http` or `name`) is set only by `TESTS`. Both are
part of an edge's identity, so a function that both reads and writes a table
has two `QUERIES` edges to it.

### IMPORTS

Only imports that resolve to a file inside the repo create an edge.
External modules are ignored. Modules are named relative to the source roots
(`src/` and the repo root). If two files claim the same module name, no edge is
created. `from pkg import X` creates an edge to `pkg/__init__.py` (or to the
submodule when `X` is a module), not to the file that defines `X`; the
`__init__.py` has its own edge to the defining file, so impact reaches the
importer transitively.

### CALLS

A `File` source means a module-level call (including decorators and default
arguments). Instantiating a class emits an edge to the `Class` and, if one
exists in the class or its in-repo bases, to its `__init__`. Resolution is
exact-only: no type inference. Multiple call sites between the same pair
collapse into one edge.

Calls that can't be resolved are not edges. They are kept on
`CodeGraph.unresolved` with a reason:

| Reason | Meaning |
|---|---|
| `external` | Lands in code outside the repo (or in an inherited external method) |
| `builtin` | Python builtin |
| `unknown_receiver` | Receiver type unknown, e.g. `self.repo.save()` or `svc.create()` |
| `ambiguous` | Module name matches more than one file |
| `not_found` | Name or member isn't defined anywhere we can see |

Each entry carries `kind` (`call` by default, `depends` for unresolved
`DEPENDS_ON` targets) so diagnostics can tell them apart.

### HANDLES and APIEndpoint

Routes are found by a separate AST walk (`parsers/fastapi_parser.py`) and
resolved to full paths in `graph/routes.py`, then added by `GraphBuilder` as
pass 4. Facts are passed with `GraphBuilder.add(parsed, facts)`; a file added
without facts contributes no routes.

- Receivers must resolve to a `FastAPI()` / `APIRouter()` declared in the repo.
  Anything else gets no endpoint and is reported on `CodeGraph.route_issues`.
- Prefixes are literal-only. Router prefixes and `include_router` prefixes are
  composed outermost first.
- The relation is many-to-many: a handler with stacked decorators, or a router
  mounted twice, yields several endpoints. Two handlers with the same method
  and path share one endpoint node with two `HANDLES` edges.
- `exact` is false when any prefix on the path was non-literal or the mount
  chain had a cycle, so `route` may be incomplete. `mounted` is false when the
  router is never included in an app, and also after a cycle. When several
  routes collapse onto one endpoint, both flags are ANDed.

Diagnostics on `CodeGraph.route_issues`: `unknown_receiver`,
`unresolved_mount`, `non_literal_prefix`, `mount_cycle`.

### DEPENDS_ON

FastAPI dependencies are functions that are passed, not called, so they leave
no `CALLS` edge. `DEPENDS_ON` links the function that declares a dependency to
the dependency itself. Changing `get_db` therefore reaches every function that
declares `Depends(get_db)`, and through `HANDLES` the endpoints behind them.
Chains (`h → current_user → get_db`) fall out of the same edges.

Recognised forms, all attributed to the function that owns the signature:

- parameter defaults: `def h(db=Depends(get_db))`
- `Annotated` metadata: `def h(db: Annotated[Session, Depends(get_db)])`
- route decorators: `@router.get("/x", dependencies=[Depends(auth)])`

`Security(...)` and `dependency=` keyword targets work like `Depends`. The
constructor must resolve to `fastapi.Depends` / `fastapi.Security` through the
file's imports, so aliases (`Depends as D`) and qualified names
(`fastapi.Depends`) are accepted, while a local class or another package's
`Depends` is ignored without a diagnostic.

Targets resolve with the same rules as `CALLS`, but in the scope the
expression is evaluated in: the function enclosing the owner, or the module
for top-level functions. A class target emits an edge to the `Class` and, if
one exists, its `__init__`. Targets that don't resolve are kept on
`CodeGraph.unresolved` with `kind="depends"` and the same reasons as calls.

### DBTable and MAPS_TO

Models are found by `parsers/db_parser.py` and resolved in `graph/models.py`.
Detection is marker-based: a class is a model if its own body assigns a
`__tablename__` or `__abstract__`. The base class is not checked.

- A literal `__tablename__` maps the class to that table, lowercased.
- `__abstract__ = True` stops inheritance: the class and its subclasses get no
  table unless a subclass declares its own.
- A subclass without its own `__tablename__` inherits its parent's table
  (single-table inheritance). A subclass with its own `__tablename__` gets that
  table (joined-table inheritance). Every such class gets its own `MAPS_TO`
  edge, so `Employee → people` and `Person → people` both exist.
- Bases are walked in declaration order and in-repo mixins without a table are
  skipped, the first base that yields a table wins.
- A non-literal `__tablename__` (computed value, `@declared_attr`) creates no
  edge, is not inherited by subclasses, and is reported as `non_literal_tablename`
  (abstract classes are exempt).

A `DBTable` node exists for every table that either a model maps to or any
`QUERIES` edge targets, so SQL-only tables (no model) are included.

### QUERIES

`QUERIES` links the code that touches a table to its `DBTable`. The source is
the innermost enclosing `Function`, or the `File` for module-level SQL. It is
built by `GraphBuilder` as pass 6 from `GraphBuilder.add(parsed, facts, db)`;
a file added without `db` facts contributes no tables, models or queries.

There are two ways a use is found. Both end up as the same edge, keyed by
(owner, table, access), so a read reached through SQL and through the ORM is
one edge.

**1. SQL string sinks** (`parsers/db_parser.py`, tables extracted by
`parsers/sql_parser.py`, dialect `postgres`).

- A sink is a call whose last name segment is `execute`, `executemany`,
  `executescript`, `exec_driver_sql`, `text`, `fetch`, `fetchrow`, `fetchval`,
  `read_sql` or `read_sql_query`. The first positional argument (or `sql=`,
  `query=`, `statement=`, `text=`) is the SQL. `text(...)` and `SQL(...)`
  wrappers are unwrapped.
- Strings are not scanned outside sinks, so docstrings and log messages are
  ignored. A literal that isn't DML (`CREATE TABLE`, a URL) is ignored without
  a diagnostic.
- The argument is read as SQL if it is a string literal (adjacent and `+`
  concatenated literals are folded), or a name bound exactly once to such a
  literal, either in the same function (or an enclosing one) or at module
  level in the same file. A name that is also a parameter, reassigned, or
  otherwise stored in the scope is not resolved.
- `INSERT` / `UPDATE` / `DELETE` / `MERGE` targets are `write`; every other
  table, including `FROM` / `JOIN` / `USING` and subqueries, is `read`. CTE
  names are not tables.
- Dynamic SQL (f-string, `%`, `+`, `.format`) is reported as `non_literal_sql`
  only when its visible leading literal looks like SQL. Anything else, such as
  `execute(sql_var)` or `execute(select(User))`, is silent.

**2. ORM references** (`ModelRef` facts, resolved in `graph/usage.py`).

- A function uses a model if its body references the class by name. The whole
  dotted chain is captured (`models.User.id`) and `CallResolver.class_of`
  finds the class it reaches, through imports, aliases and `__init__`
  re-exports. This covers `select(User)`, `session.query(User)`,
  `User.id == x`, `.join(Order)`: references that leave no `CALLS` edge.
- Only function bodies are scanned. Decorators (`response_model=User`),
  defaults, annotations, class bodies (`ForeignKey(User.id)`) and module-level
  statements are not. Nested functions own their own references.
- Any reference is a `read`. It is a `write` when the model is the first
  argument of `insert` / `update` / `delete` / `bulk_insert_mappings` /
  `bulk_update_mappings`, the model of a `query(M)` chain that ends in
  `.update()` or `.delete()`, or when the chain is called and resolves exactly
  to the class (`User(...)`, `models.User(...)`). `User.get(1)` and
  `User.query.filter(...)` stay reads.
- The model must resolve to a table through the rules above, so a reference
  to a plain class or an unknown name is dropped silently. A reference to a
  single-table subclass uses the parent's table.

There is no `Function → Class` edge for ORM references. Impact analysis from a
`Class` is expected to follow `MAPS_TO` to its table and then `QUERIES`
backwards, which over-approximates (raw-SQL users of the same table, other
subclasses) in the safe direction.

Each use records whether it came from SQL or the ORM (`TableUse.source`), but
the graph keeps only the collapsed edge.

Diagnostics on `CodeGraph.db_issues`: `non_literal_tablename` (from model
resolution, listed first), then per file in path order `non_literal_sql` and
`unparseable_sql`.

### TESTS

`TESTS` links a test to the code it exercises. It is built by `GraphBuilder` as
passes 7 (promote tests) and 8 (edges). A test can reach a target in two ways,
and a test/target pair keeps only its strongest evidence (`call` over `http`
over `name`).

**1. Calls (`via=call`).** A `CALLS` edge from a test to a `Function` or
`Class` outside the test files. Instantiating a class also links its
`__init__`, as for `CALLS`.

**2. HTTP (`via=http`).** A call in a function body of the form
`client.get("/x")` (any receiver, any verb in the HTTP method set) or
`client.request("GET", "/x")`, matched to `APIEndpoint` nodes by method and
path. Paths may be literals or f-strings, where each hole becomes a wildcard.
Query strings and fragments are stripped. Absolute URLs and paths built from
variables are skipped. Matching is per segment: a literal beats a `{param}`;
when a wildcard call ties between several routes, every tied endpoint is
linked; `{p:path}` matches any remaining tail.

**Helper propagation.** A helper is a `Function` in a test file that is not a
test: `conftest.py` and `tests/` helper modules, plain helpers in test files,
nested functions, `self._x()` methods on test classes, and `__init__` of
helper classes. A test is credited with everything its helpers reach, however
deep, so `test -> make() -> get()` yields `test -> get`, and an `http` fact
inside a helper is credited to every test that reaches the helper. The walk
stops at the first non-test symbol (coverage is not followed from source to
source; impact analysis does that), never enters another test, ignores
module-level calls in helper files, and tolerates cycles. Links reached through
a helper keep `via=call` or `via=http`; the number of hops is not recorded.

## Persistence (Neo4j)

`GraphStore` (`src/ripple/graph/store.py`) writes a `CodeGraph` to Neo4j.
Connection settings come from `ripple.config` (`NEO4J_URI`, `NEO4J_USER`,
`NEO4J_PASSWORD`).

- Node labels are the `NodeType` values and relationship types are the
  `EdgeType` values. Each label has a uniqueness constraint on `id`
  (`ensure_schema`, idempotent).
- Node properties come from `to_properties()`: `None` values are dropped,
  `type` becomes the label, and list fields (`decorators`, `bases`) are stored
  as string lists.
- Relationships are MERGEd on (source, destination, type, `access`, `via`).
  Neo4j cannot MERGE on null, so an absent `access` or `via` is stored as `""`;
  readers must map `""` back to `None`.
- Edges are written in groups by (type, source label, destination label) so
  each `MATCH` uses the per-label `id` index.
- `write_graph` is idempotent. `replace_graph` is a full replace
  (`ensure_schema`, `clear`, `write_graph`); it is what a full scan uses.

Known limits: `clear` wipes the whole database, there is no per-file patching
yet (planned for incremental rescans), and queries run in auto-commit sessions
without retries.

## Impact analysis

`analysis/impact.py` answers "what is affected if I change X". `impact(view,
starts, max_hops=5, max_results=500, include_importers=False)` takes resolved
nodes; turning strings like `POST /users` into nodes is the job of the MCP
tools. It reads the graph through a two-method `GraphView` protocol, with an
in-memory implementation (`CodeGraphView`) and a Neo4j one
(`analysis/neo4j_view.py`, one `GraphQuery.neighbors` call per hop plus a node
cache).

### What a change to each kind of node means

| Start | Seeds (affected at hop 1 unless noted) |
|---|---|
| `Function` / `Test` | none; the spread below starts from the node itself |
| `Class` | its methods, its `MAPS_TO` table, and at hop 2 every function or class that queries or maps to that table (ORM users leave no `CALLS` edge) |
| `DBTable` | functions and files that `QUERIES` it, classes that `MAPS_TO` it |
| `APIEndpoint` | none; tests that hit it come from the spread. Its handler is returned as `context`, not as affected |
| `File` | every symbol defined in it, and files that import it, transitively, one hop per `IMPORTS` level (this covers `__init__.py` re-exports) |

Importer files are added for function and class starts only with
`include_importers=True`: importing a file does not mean using the changed
symbol, so by default that fringe stays out.

### How it spreads

From the start nodes and seeds, the traversal follows reverse `CALLS`,
`DEPENDS_ON`, `HANDLES` and `TESTS`. A changed function reaches its callers,
the endpoints they handle, and the tests that hit or call any of them. It is a
level-by-level BFS: each node is reported once, at its shortest distance, so
cycles and diamonds are safe. Seeds join at their own hop, and a seed's edge
wins a tie with a spread edge at the same hop.

### Affected tables

Only tables the changed code itself touches: forward `QUERIES` and `MAPS_TO`
from the start nodes and from symbols they expand to (a class's methods, a
file's symbols). Callers' tables are not added, otherwise every change would
appear to touch the busiest table. A `DBTable` start is not listed as its own
impact.

### Result

`ImpactResult` carries `starts`, `context`, and `entries`: one `ImpactEntry`
per affected node with its `group`, `hops`, `parent` and the `via` edge that
reached it. `functions`, `classes`, `endpoints`, `tables`, `tests` and `files`
are views over `entries`, and `path_to(id)` rebuilds the steps from a start to
any entry so tools can explain why something is affected. Functions and
classes defined in test files (`make_user` in `conftest.py`) are grouped with
`tests`. The class-to-method step in `path_to` uses a synthesized
`DEFINED_IN(method -> class)` edge, because the graph stores no such edge.

Two flags say when the answer is partial:

- `truncated`: the result cap was hit. Farthest hops are cut first.
- `depth_limited`: more nodes are reachable beyond `max_hops`.

### Blind spots

Impact is only as good as the edges. These changes are missed:

- Inheritance. There is no edge for `class A(Base)`, so changing `Base` does
  not reach subclasses or their callers. An `INHERITS` edge is the natural
  addition.
- Type-annotation references (`def f(u: User)`).
- Functions passed as callbacks, apart from FastAPI `Depends`.
- Tests that reach code only through pytest fixtures.
- Router-level and app-level `dependencies=[...]`.
- Calls through local variables (`unknown_receiver`).

Class-level table expansion over-approximates on purpose: raw-SQL users of the
same table and other models on it are included.

## Entity resolution

`analysis/entity.py` turns a user-supplied string into one node:
`resolve_entity(finder, query, types=None, limit=10)` returns a `Resolution`
with status `ok`, `ambiguous` or `not_found`. The finder is anything with
`GraphQuery.find_nodes`'s shape: `GraphQuery` itself, or `GraphFinder` over an
in-memory `CodeGraph` (same matching and ranking, kept in step by an
integration parity test).

- Matches are ranked by tier: exact id, then qualname, then name, then path
  (a full path, or a suffix on a `/` boundary).
- It never guesses. If several nodes tie at the best tier the result is
  `ambiguous` and lists them (`get` matches both `UserService.get` and
  `OrderService.get`).
- `not_found` carries close suggestions, found by searching again on the last
  path or dotted segment that matches something.
- Endpoint-looking input is canonicalised (`post  /x` becomes `POST /x`) and a
  trailing slash falls back to the route without one. Backslashes in paths
  are normalised.
- An empty graph is not detected here. The tool layer checks for it.

## Request flow

`analysis/request_flow.py` answers "what does this request reach".
`trace_flow(view, start, max_hops=6, max_results=200)` goes forward from an
endpoint or a function and reports every function and class it can reach,
plus the tables they touch. It reads the graph through the same `GraphView`
protocol as impact analysis, so it behaves the same on the in-memory and Neo4j
views.

**Starts.** An endpoint expands through `HANDLES`: its handlers are hop 1
(unlike impact analysis, where they are only context), and a shared endpoint
can have several. A non-test function is hop 0 and its callees are hop 1.
Classes, tables, files, tests and test helpers raise `UnsupportedStart`.

**Spread.** Level-by-level BFS along `CALLS`, `DEPENDS_ON` and `HANDLES`.
Each node is reported once at its shortest distance, with its `parent` and
`via` edge, so cycles and diamonds are safe and `path_to(id)` can explain why
a node is in the flow.

**Reachability, not execution order.** Hops are distance from the start, not
a call stack. FastAPI runs dependencies before the handler, but both can sit
at the same hop.

**Tables.** `QUERIES` edges are collected from every reached node (and from a
function start) but not followed. The result has the tables each step touches
(`FlowStep.tables`, `FlowResult.start_tables`) and one `TableTouch` per table
with merged `read` / `write` access, the distance of the closest toucher, and
the touching nodes, closest first.

**Result.** `FlowResult` carries `start`, `steps`, `tables`, `truncated`,
`depth_limited` and `caveats`. Caveats appear only when relevant: the result
is truncated or depth-limited, the start endpoint is inexact or unmounted, or
no table access was found (ORM relationship traversal, SQL built from imported
constants and calls through local variables are not detected).

## Risk scoring

`analysis/risk.py` turns an `ImpactResult` into a score and a level:
`assess_risk(view, result, config=DEFAULT_CONFIG) -> RiskAssessment`. It reads
the graph only through `GraphView` (one `TESTS` lookup), so it behaves the
same on the in-memory and Neo4j views.

### Factors

| Factor | Raw value | Saturates at | Weight |
|---|---|---|---|
| `endpoints` | affected endpoints, plus an endpoint start | 8 | 0.35 |
| `tables` | affected tables, plus a table start | 4 | 0.25 |
| `coverage_gap` | uncovered fraction of the start nodes and affected endpoints | linear | 0.25 |
| `fan_in` | dependents attached directly to the changed surface | 10 | 0.15 |

Counts go through a saturating curve, `min(1, ln(1 + n) / ln(1 + cap))`, so
the first endpoint matters more than the tenth. The score is the weighted sum
× 100, rounded to one decimal; the level comes from the rounded score:
below 25 is `low`, below 50 `medium`, below 75 `high`, otherwise `critical`.
Weights, caps and thresholds live in the frozen `RiskConfig`, which validates
itself (weights sum to 1, thresholds ascend).

Start nodes count as affected for endpoints and tables: `ImpactResult` leaves
them out of its own groups, but changing `POST /users` or a table is itself
the change.

**Coverage.** "Considered" is the non-test `Function` / `Class` / `APIEndpoint`
start nodes plus the affected endpoints. "Covered" means an incoming `TESTS`
edge (`call` or `http`). Tests link to the first source symbol and to
endpoints, so affected callers in between are not judged. If nothing is
considered (a file, table or test start with no affected endpoints), the
factor is `n/a` and contributes 0.

**Fan-in.** The changed surface is the start nodes, the symbols a start expands
to (`DEFINED_IN` entries for a file or class) and affected tables. Fan-in counts
function and class entries reached through `CALLS`, `DEPENDS_ON`, `QUERIES` or
`MAPS_TO` whose parent is in that surface. For a function start this is its
direct callers; for a file start, the callers of its symbols.

### Caveats

Incomplete data never changes the score silently. It appears in
`RiskAssessment.caveats` instead:

- `truncated` or `depth_limited`: the counts are lower bounds.
- Affected endpoints with `exact=False` (possibly incomplete route) or
  `mounted=False` (never included in an app).
- No dependents found: not proof of safety, since inheritance, callbacks,
  fixtures and annotation references are not tracked.
- Dependents found but no test reaches the changed code.

### Limits

- Table access is not weighted: `ImpactEntry` keeps one `via` per table, so a
  change that writes a table scores like one that only reads it. Weighting
  writes needs an extra `QUERIES` lookup.
- A fully uncovered start contributes the whole coverage weight (25 points),
  which is exactly the `medium` threshold, so any untested change is at least
  `medium`, even a leaf nothing calls.
- Endpoints count equally whatever the method (`GET` vs `DELETE`).
- Coverage is only as good as the `TESTS` edges. Fixture-only coverage is not
  linked, so it reads as uncovered, which errs towards higher risk.
- Weights and caps are untuned heuristics; adjust them through `RiskConfig`.


## Known gaps

- `super().method()` is recorded only as a call to `super`.
- Only FastAPI dependencies are handled as passed-not-called functions. Other
  callbacks (`background_tasks.add_task(f)`, `map(f, xs)`) leave no edge.
- Not yet covered for dependencies: router-level and app-level
  `dependencies=[...]`, module-level aliases such as
  `DbSession = Annotated[Session, Depends(get_db)]`, bare `Depends()` (type
  taken from the annotation), and `Depends(lambda: ...)` or other non-dotted
  targets.
- Dependency expressions on methods resolve from module level, so a sibling
  method referenced by bare name (legal in a class body) reports `not_found`.
- Calls through local variables are `unknown_receiver`.
- Models are found only through `__tablename__` / `__abstract__` in the class
  body. Classes mapped with `__table__ = Table(...)`, Core `Table("users",
  metadata)` objects, and imperative mapping are not detected. Nested model
  classes (`Outer.Inner`) are only found through the inner class's own name.
- Table names drop schema prefixes, so `public.users` and `audit.users` are
  the same node.
- SQL held anywhere other than a single-assignment literal in the same file is
  not read: constants imported from another module, class attributes
  (`self.QUERY`), parameters, and strings built from variables. Only the
  dynamic cases with a visible SQL prefix are reported.
- DDL (`CREATE`, `ALTER`, `DROP`) is ignored. `text` is treated as a sink
  everywhere, so an unrelated `text("Select an option")` can raise a spurious
  `unparseable_sql`.
- ORM usage is syntactic. Relationship traversal (`user.orders`),
  `session.delete(obj)`, and model instances held in variables leave no edge.
  Reads over-approximate: any reference in a function body counts, including
  `isinstance(x, User)`. Writes are narrow and by name only
  (`d.update(defaults)` marks `defaults`, which never resolves to a model).
- A local variable or parameter named like a model class is not detected as
  shadowing it.
  - Pytest fixtures are not propagated. A fixture is injected by parameter name,
  not called, so source reached only through a fixture (`client` built from the
  app) is not linked to the tests that use it. `Evidence.NAME` is reserved for
  name-based links and is not produced yet.
- Test coverage is over-approximate in one way: any helper call counts, even on
  a branch that does not run. It is under-approximate in another: calls through
  local variables are `unknown_receiver`, so `c = Client(); c.run()` links
  `Client` but not `run`.
- `via` does not distinguish a direct call from one reached through helpers.
- Impact analysis follows only edges the graph has; see "Blind spots" under
  "Impact analysis" (inheritance, annotation references and callbacks are the
  main misses).
  - Entity resolution matches an endpoint only by its declared route
  (`GET /users/{user_id}`). A concrete path such as `GET /users/42` does not
  resolve; it would need `EndpointIndex` and all endpoints loaded.