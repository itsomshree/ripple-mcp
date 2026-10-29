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
| `Test` | `<path>::<qualname>` | Planned (step 5). Until then, test functions are plain `Function` nodes in files with `is_test` set |

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
| `TESTS` | `Test` → `Function` / `APIEndpoint` | Planned (step 5) |

Edges have an optional `access` attribute (`read` or `write`). Only `QUERIES`
sets it. It is part of an edge's identity, so a function that both reads and
writes a table has two `QUERIES` edges to it.

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
  dotted chain is captured (`models.User.id`) and `CallCallResolver.class_of`
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