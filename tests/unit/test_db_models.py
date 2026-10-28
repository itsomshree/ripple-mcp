import textwrap
from pathlib import Path

from ripple.graph.calls import CallResolver
from ripple.graph.models import resolve_models
from ripple.graph.resolver import ModuleResolver
from ripple.graph.schema import DbIssueReason as Why
from ripple.graph.schema import ModelResolution
from ripple.parsers.db_parser import parse_db_file, parse_db_source
from ripple.parsers.python_parser import parse_python_file, parse_python_source

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"
P = "app/models.py"


def resolve(files: dict[str, str]) -> ModelResolution:
    srcs = {p: textwrap.dedent(s).lstrip("\n") for p, s in files.items()}
    parsed = {p: parse_python_source(s, p) for p, s in srcs.items()}
    facts = {p: parse_db_source(s, p) for p, s in srcs.items()}
    return resolve_models(facts, parsed, CallResolver(parsed, ModuleResolver(parsed)))


def one(src: str) -> ModelResolution:
    return resolve({P: src})


def tables(res: ModelResolution):
    """(qualname, table, own) for single-file tests."""
    return [(m.class_id.partition("::")[2], m.table, m.own) for m in res.models]


def full(res: ModelResolution):
    return [(m.class_id, m.table, m.own) for m in res.models]


def test_own_table_is_lowercased_and_unmapped_classes_are_skipped():
    res = one("""
        class Base(DeclarativeBase): pass
        class Plain:
            x = 1
        class User(Base):
            __tablename__ = "Users"
        """)
    assert tables(res) == [("User", "users", True)]
    assert res.issues == []


def test_single_table_inheritance_shares_the_parent_table():
    res = one("""
        class Base(DeclarativeBase): pass
        class Person(Base):
            __tablename__ = "people"
        class Employee(Person): pass
        class Manager(Employee): pass
        """)
    assert tables(res) == [
        ("Employee", "people", False),
        ("Manager", "people", False),
        ("Person", "people", True),
    ]


def test_joined_table_child_gets_its_own_table():
    res = one("""
        class Person(Base):
            __tablename__ = "people"
        class Employee(Person):
            __tablename__ = "employees"
        """)
    assert tables(res) == [("Employee", "employees", True), ("Person", "people", True)]


def test_abstract_base_stops_inheritance():
    res = one("""
        class Base(DeclarativeBase): pass
        class Timestamped(Base):
            __abstract__ = True
        class Event(Timestamped):
            __tablename__ = "events"
        class Plain(Timestamped):
            x = 1
        """)
    assert tables(res) == [("Event", "events", True)]


def test_mixin_without_table_is_skipped_in_bases_order():
    res = one("""
        class Audit:
            created = 1
        class User(Audit, Base):
            __tablename__ = "users"
        class Admin(Audit, User): pass
        """)
    assert tables(res) == [("Admin", "users", False), ("User", "users", True)]


def test_non_literal_tablename_is_reported_and_not_inherited():
    res = one("""
        class Person(Base):
            __tablename__ = "people"
        class Weird(Person):
            __tablename__ = make_name()
        class Child(Weird): pass
        """)
    assert tables(res) == [("Person", "people", True)]
    assert [(i.subject, i.reason) for i in res.issues] == [
        (f"{P}::Weird", Why.NON_LITERAL_TABLENAME)
    ]
    assert res.issues[0].file_path == P


def test_abstract_class_with_non_literal_tablename_is_silent():
    res = one("""
        class A(Base):
            __abstract__ = True
            __tablename__ = make()
        """)
    assert res.models == [] and res.issues == []


def test_cross_file_and_qualified_bases():
    res = resolve(
        {
            "app/__init__.py": "",
            "app/base.py": "class Person(Base):\n    __tablename__ = 'people'\n",
            "app/models.py": "from app.base import Person\nclass Employee(Person): ...\n",
            "app/other.py": "import app.base as b\nclass Manager(b.Person): ...\n",
        }
    )
    assert full(res) == [
        ("app/base.py::Person", "people", True),
        ("app/models.py::Employee", "people", False),
        ("app/other.py::Manager", "people", False),
    ]


def test_base_cycle_terminates():
    res = one("""
        class A(B): pass
        class B(A): pass
        """)
    assert res.models == []


def test_files_without_facts_contribute_no_models():
    parsed = {P: parse_python_source('class A(Base):\n    __tablename__ = "a"\n', P)}
    res = resolve_models({}, parsed, CallResolver(parsed, ModuleResolver(parsed)))
    assert res.models == [] and res.issues == []


def test_base_classes_in_declaration_order_skipping_external():
    parsed = {
        "m.py": parse_python_source(
            "from pydantic import BaseModel\n"
            "class A: pass\n"
            "class B: pass\n"
            "class C(BaseModel, B, A): pass\n"
            "def f(): pass\n",
            "m.py",
        )
    }
    calls = CallResolver(parsed, ModuleResolver(parsed))
    assert calls.base_classes("m.py", "C") == ["m.py::B", "m.py::A"]
    assert calls.base_classes("m.py", "A") == []
    assert calls.base_classes("m.py", "f") == []
    assert calls.base_classes("m.py", "nope") == []


def test_sample_fixture_has_no_models_yet():
    files = sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix())
    rels = [p.relative_to(FIXTURE).as_posix() for p in files]
    parsed = {r: parse_python_file(p, r) for p, r in zip(files, rels)}
    facts = {r: parse_db_file(p, r) for p, r in zip(files, rels)}
    res = resolve_models(facts, parsed, CallResolver(parsed, ModuleResolver(parsed)))
    assert res.models == [] and res.issues == []
