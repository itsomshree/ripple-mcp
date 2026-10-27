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
| `DBTable` | `table: <lowercase name>` | Planned (step 4) |
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
| `QUERIES` | `Function` → `DBTable` | Planned (step 4) |
| `TESTS` | `Test` → `Function` / `APIEndpoint` | Planned (step 5) |

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