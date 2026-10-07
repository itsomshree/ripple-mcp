# Graph schema

Defined in `src/ripple/graph/schema.py`. The graph is built in memory by
`GraphBuilder` (`CodeGraph`) and persisted to Neo4j in a later step.

## Nodes

| Type | ID format | Notes |
|---|---|---|
| `File` | repo-relative POSIX path, e.g. `src/ripple/server.py` | `is_test` is set from the path (`test_*.py`, `*_test.py`, `conftest.py`, or a `tests/` directory) |
| `Function` | `<path>::<qualname>`, e.g. `app/users.py::UserService.create` | Methods and nested functions use dotted qualnames; `class_name` is set for methods |
| `Class` | `<path>::<qualname>` | `bases` holds the raw base expressions |
| `APIEndpoint` | `endpoint::<METHOD> <route>` | Planned (step 3) |
| `DBTable` | `table: <lowercase name>` | Planned (step 4) |
| `Test` | `<path>::<qualname>` | Planned (step 5). Until then, test functions are plain `Function` nodes in files with `is_test` set |

Line spans (`line_start`, `line_end`) start at the first decorator.

## Edges

| Type | Direction | Status |
|---|---|---|
| `DEFINED_IN` | `Function` / `Class` → `File` | Implemented |
| `IMPORTS` | `File` → `File` | Implemented |
| `CALLS` | `File` / `Function` → `Function` / `Class` | Implemented |
| `HANDLES` | `APIEndpoint` → `Function` | Planned (step 3) |
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

## Known gaps

- `super().method()` is recorded only as a call to `super`.
- Functions passed rather than called (e.g. `Depends(get_db)`) leave no edge yet (step 3).
- Calls through local variables are `unknown_receiver`.