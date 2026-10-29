import textwrap
from pathlib import Path

from ripple.graph.calls import CallResolver
from ripple.graph.models import resolve_models
from ripple.graph.resolver import ModuleResolver
from ripple.graph.schema import UsageResolution
from ripple.graph.usage import resolve_usage
from ripple.parsers.db_parser import parse_db_file, parse_db_source
from ripple.parsers.python_parser import parse_python_file, parse_python_source

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
MODEL = 'class User(Base):\n    __tablename__ = "users"\n'


def _calls(files: dict[str, str]) -> CallResolver:
    parsed = {
        p: parse_python_source(textwrap.dedent(s).lstrip("\n"), p)
        for p, s in files.items()
    }
    return CallResolver(parsed, ModuleResolver(parsed))


def resolve(files: dict[str, str]) -> UsageResolution:
    srcs = {p: textwrap.dedent(s).lstrip("\n") for p, s in files.items()}
    parsed = {p: parse_python_source(s, p) for p, s in srcs.items()}
    facts = {p: parse_db_source(s, p) for p, s in srcs.items()}
    calls = CallResolver(parsed, ModuleResolver(parsed))
    return resolve_usage(facts, calls, resolve_models(facts, parsed, calls))


def got(res: UsageResolution):
    return [(u.owner_id, u.table, u.access, u.source) for u in res.uses]


# class_of
def test_class_of_reports_consumed_segments():
    cr = _calls(
        {
            "m.py": "class User: ...\ndef helper(): ...\n",
            "a.py": "import m\nfrom m import User, helper\ndef f(): ...\n",
        }
    )
    assert cr.class_of("a.py", "User", "f") == ("m.py::User", 1)
    assert cr.class_of("a.py", "User.id.x", "f") == ("m.py::User", 1)
    assert cr.class_of("a.py", "m.User.id", "f") == ("m.py::User", 2)
    assert cr.class_of("a.py", "m", None) is None
    assert cr.class_of("a.py", "helper", "f") is None
    assert cr.class_of("a.py", "m.helper", "f") is None
    assert cr.class_of("a.py", "unknown", "f") is None


# ORM resolution
def test_read_write_and_call_classification():
    res = resolve(
        {
            "app/models.py": MODEL
            + textwrap.dedent("""
            def get(s):
                return s.query(User).all()

            def create(s):
                s.add(User(name="x"))

            def rename(s):
                s.query(User).filter(User.id == 1).update({"name": "y"})

            def classmethod_use(s):
                return User.get(1)
            """)
        }
    )
    m = "app/models.py"
    assert got(res) == [
        (f"{m}::classmethod_use", "users", "read", "orm"),
        (f"{m}::create", "users", "write", "orm"),
        (f"{m}::get", "users", "read", "orm"),
        (f"{m}::rename", "users", "read", "orm"),
        (f"{m}::rename", "users", "write", "orm"),
    ]


def test_cross_file_reference_forms():
    res = resolve(
        {
            "app/__init__.py": "",
            "app/models.py": MODEL,
            "a.py": "from app.models import User\ndef f():\n    return User\n",
            "b.py": "from app.models import User as U\ndef f():\n    return U\n",
            "c.py": "from app import models\ndef f():\n    return models.User\n",
            "d.py": "import app.models as m\ndef f():\n    return m.User\n",
            "e.py": "import app.models\ndef f():\n    return app.models.User.id\n",
        }
    )
    assert got(res) == [(f"{x}.py::f", "users", "read", "orm") for x in "abcde"]


def test_exact_module_qualified_call_is_a_write_but_classmethod_is_not():
    res = resolve(
        {
            "app/__init__.py": "",
            "app/models.py": MODEL,
            "x.py": (
                "from app import models\n"
                "def f():\n    models.User(name='x')\n"
                "def g():\n    models.User.get(1)\n"
            ),
        }
    )
    assert got(res) == [
        ("x.py::f", "users", "write", "orm"),
        ("x.py::g", "users", "read", "orm"),
    ]


def test_reexport_through_package_init():
    res = resolve(
        {
            "pkg/__init__.py": "from .models import User\n",
            "pkg/models.py": MODEL,
            "x.py": "from pkg import User\ndef f():\n    return User\n",
        }
    )
    assert got(res) == [("x.py::f", "users", "read", "orm")]


def test_single_table_inheritance_maps_subclass_to_parent_table():
    res = resolve(
        {
            "m.py": (
                "class Person(Base):\n    __tablename__ = 'people'\n"
                "class Employee(Person): ...\n"
                "def f():\n    return Employee\n"
            )
        }
    )
    assert got(res) == [("m.py::f", "people", "read", "orm")]


def test_non_model_and_unresolved_refs_are_dropped():
    res = resolve(
        {
            "m.py": MODEL
            + "class Plain: ...\ndef f():\n    Plain()\n    unknown()\n    len([1])\n"
        }
    )
    assert res.uses == []


# SQL pass-through
def test_sql_and_orm_coexist_and_dedupe_to_the_first_line():
    res = resolve(
        {
            "m.py": MODEL
            + textwrap.dedent("""
            def f(c, s):
                c.execute("SELECT * FROM users")
                c.execute("SELECT * FROM users WHERE id = 1")
                s.query(User).all()
            """)
        }
    )
    assert got(res) == [
        ("m.py::f", "users", "read", "orm"),
        ("m.py::f", "users", "read", "sql"),
    ]
    assert {u.source: u.line for u in res.uses} == {"orm": 7, "sql": 5}


def test_module_level_sql_has_the_file_as_owner():
    res = resolve({"m.py": 'cur.execute("SELECT * FROM a")\n'})
    assert got(res) == [("m.py", "a", "read", "sql")]


# determinism + fixture
def test_result_is_independent_of_file_order():
    files = {
        "app/__init__.py": "",
        "app/models.py": MODEL,
        "a.py": "from app.models import User\ndef f():\n    return User\n",
        "b.py": "import app.models as m\ndef g():\n    m.User()\n",
    }
    assert resolve(files) == resolve(dict(reversed(files.items())))


def test_sample_fixture_has_no_usage_yet():
    files = sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix())
    rels = [p.relative_to(FIXTURE).as_posix() for p in files]
    parsed = {r: parse_python_file(p, r) for p, r in zip(files, rels)}
    facts = {r: parse_db_file(p, r) for p, r in zip(files, rels)}
    calls = CallResolver(parsed, ModuleResolver(parsed))
    res = resolve_usage(facts, calls, resolve_models(facts, parsed, calls))
    assert res.uses == []
