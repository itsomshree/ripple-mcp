# sample_fastapi_repo

Small synthetic FastAPI project used as parser/graph test input. It is parsed
as source only: never imported, never executed (`fastapi` and `sqlalchemy` are
not installed in the dev environment).

## Layout

| File | Contents |
|---|---|
| `app/main.py` | `FastAPI` app, `/health`, mounts both routers |
| `app/routers/users.py` | `users` router (`prefix="/users"`), calls `UserService` |
| `app/routers/orders.py` | `orders` router (`prefix="/orders"`), calls `OrderService` |
| `app/services/user_service.py` | `UserService`, ORM access only |
| `app/services/order_service.py` | `OrderService`, raw SQL only; `place` calls `UserService.get` |
| `app/models.py` | `Base`, `User` (`users`), `Order` (`orders`) |
| `app/db.py` | `get_session()`, called by every service method |
| `tests/conftest.py` | `client` fixture (never linked: fixtures are not propagated), `make_user` helper calling `UserService.create` |
| `tests/test_users.py` | 3 tests: direct `UserService.get`, HTTP `GET /users/1`, `make_user` helper |
| `tests/test_orders.py` | 2 tests: HTTP `POST /api/orders`, direct `OrderService.place` |

## Routes

Declared path, before router prefixes:

| Method | Path | Handler |
|---|---|---|
| GET | `/health` | `app/main.py::health` |
| GET | `` | `app/routers/users.py::list_users` |
| GET | `/{user_id}` | `app/routers/users.py::get_user` |
| POST | `` | `app/routers/users.py::create_user` |
| DELETE | `/{user_id}` | `app/routers/users.py::delete_user` |
| GET | `/{order_id}` | `app/routers/orders.py::get_order` |
| POST | `` | `app/routers/orders.py::place_order` |

Full resolved paths (after router prefixes and mounts), asserted in
`tests/unit/test_fastapi_routes.py` and `tests/unit/test_graph_endpoints.py`:

| Method | Full path |
|---|---|
| GET | `/health` |
| GET | `/users` |
| GET | `/users/{user_id}` |
| POST | `/users` |
| DELETE | `/users/{user_id}` |
| GET | `/api/orders/{order_id}` |
| POST | `/api/orders` |

Router prefixes: `users` router declares `prefix="/users"`; `orders` router
declares `prefix="/orders"` and is mounted with `prefix="/api"`.

## Models and table access

Models: `User` maps to `users`, `Order` maps to `orders`. `Base` has no
`__tablename__`, so it is not a model.

| Owner | Table | Access | Source |
|---|---|---|---|
| `UserService.list_all` | users | read | orm |
| `UserService.get` | users | read | orm |
| `UserService.create` | users | write | orm |
| `UserService.delete` | users | read + write | orm |
| `OrderService.get` | orders | read | sql |
| `OrderService.place` | orders | write | sql |

`users` is reached only through the ORM and `orders` only through raw SQL, so
the fixture covers both detection paths. `UserService.delete` has a read edge
(`User.id`) and a write edge (`query(User)...delete()`).

Asserted in `tests/unit/test_db_models.py`, `tests/unit/test_db_usage_resolve.py`
and `tests/integration/test_end_to_end_scan.py`.

## Tests and coverage

| Test | Target | via |
|---|---|---|
| `test_get_user_service` | `UserService.get` | call |
| `test_get_user_endpoint` | `GET /users/{user_id}` | http |
| `test_create_user_via_helper` | `UserService.create` (through `make_user` in `conftest.py`) | call |
| `test_place_order_service` | `OrderService.place` | call |
| `test_place_order_endpoint` | `POST /api/orders` | http |

The three paths are direct call, HTTP, and helper propagation. `client` is a
fixture injected by name, so nothing links to it. Asserted in
`tests/integration/test_end_to_end_scan.py`.

## Expected graph

| Nodes | | Edges | |
|---|---|---|---|
| File | 13 | DEFINED_IN | 26 |
| Class | 5 | IMPORTS | 14 |
| Function | 16 | CALLS | 18 |
| APIEndpoint | 7 | HANDLES | 7 |
| DBTable | 2 | MAPS_TO | 2 |
| Test | 5 | QUERIES | 7 |
| | | TESTS | 5 |

The fixture has no `Depends(...)` use (no `DEPENDS_ON` edges) and produces no
route or DB diagnostics.
