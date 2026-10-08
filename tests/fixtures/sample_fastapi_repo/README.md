# sample_fastapi_repo

Small synthetic FastAPI project used as parser/graph test input. It is parsed
as source only: never imported, never executed (`fastapi` is not installed in
the dev environment).

Expected routes (declared path, before router prefixes):

| Method | Path | Handler |
|---|---|---|
| GET | `/health` | `app/main.py::health` |
| GET | `` | `app/routers/users.py::list_users` |
| GET | `/{user_id}` | `app/routers/users.py::get_user` |
| POST | `` | `app/routers/users.py::create_user` |
| DELETE | `/{user_id}` | `app/routers/users.py::delete_user` |
| GET | `/{order_id}` | `app/routers/orders.py::get_order` |
| POST | `` | `app/routers/orders.py::place_order` |

Router prefixes: `users` router declares `prefix="/users"`; `orders` router
declares `prefix="/orders"` and is mounted with `prefix="/api"`.

Steps 4 and 5 will add models, raw SQL and tests here, so counts in
integration tests will change deliberately at those steps.