import textwrap

from ripple.graph.builder import CodeGraph, GraphBuilder
from ripple.graph.schema import DbIssueReason as Why
from ripple.graph.schema import DBTableNode, EdgeType
from ripple.parsers.db_parser import parse_db_source
from ripple.parsers.python_parser import parse_python_source


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def builder_for(files: dict[str, str]) -> GraphBuilder:
    builder = GraphBuilder()
    for path, text in files.items():
        src = src_of(text)
        builder.add(parse_python_source(src, path), db=parse_db_source(src, path))
    return builder


def build(files: dict[str, str]) -> CodeGraph:
    return builder_for(files).build()


def tables(g: CodeGraph) -> dict[str, DBTableNode]:
    return {n.id: n for n in g.nodes.values() if isinstance(n, DBTableNode)}


def edges(g: CodeGraph, type_: EdgeType):
    return [(e.src, e.dst, e.access) for e in g.edges_of(type_)]


def assert_no_dangling(g: CodeGraph) -> None:
    for e in g.edges:
        assert e.src in g.nodes and e.dst in g.nodes, e


def test_model_and_queries_smoke():
    g = build(
        {
            "m.py": """
            class User(Base):
                __tablename__ = "users"

            def get(s):
                return s.query(User).all()

            def create(s):
                s.add(User())
                s.execute(text("DELETE FROM users"))
            """
        }
    )
    assert "table: users" in g.nodes
    assert edges(g, EdgeType.MAPS_TO) == [("m.py::User", "table: users", None)]
    assert edges(g, EdgeType.QUERIES) == [
        ("m.py::create", "table: users", "write"),
        ("m.py::get", "table: users", "read"),
    ]
    assert_no_dangling(g)


# nodes
def test_tables_exist_for_models_and_queries_alike():
    g = build(
        {
            "m.py": """
            class User(Base):
                __tablename__ = "users"

            class Audit(Base):
                __tablename__ = "audit"

            def f(c):
                c.execute("SELECT * FROM users")
                c.execute("SELECT * FROM events")
            """
        }
    )
    assert sorted(tables(g)) == ["table: audit", "table: events", "table: users"]
    assert tables(g)["table: users"].name == "users"
    assert edges(g, EdgeType.MAPS_TO) == [
        ("m.py::Audit", "table: audit", None),
        ("m.py::User", "table: users", None),
    ]
    assert edges(g, EdgeType.QUERIES) == [
        ("m.py::f", "table: events", "read"),
        ("m.py::f", "table: users", "read"),
    ]


def test_table_names_are_normalized_across_sources():
    g = build(
        {
            "m.py": """
            class User(Base):
                __tablename__ = "Users"

            def f(c):
                c.execute("SELECT * FROM public.USERS")
            """
        }
    )
    assert sorted(tables(g)) == ["table: users"]
    assert edges(g, EdgeType.QUERIES) == [("m.py::f", "table: users", "read")]


# MAPS_TO
def test_inherited_and_joined_tables_map_each_class():
    g = build(
        {
            "m.py": """
            class Person(Base):
                __tablename__ = "people"

            class Employee(Person): ...

            class Manager(Employee):
                __tablename__ = "managers"

            class Plain: ...

            def f():
                return Employee
            """
        }
    )
    assert edges(g, EdgeType.MAPS_TO) == [
        ("m.py::Employee", "table: people", None),
        ("m.py::Manager", "table: managers", None),
        ("m.py::Person", "table: people", None),
    ]
    assert edges(g, EdgeType.QUERIES) == [("m.py::f", "table: people", "read")]


# QUERIES
def test_read_and_write_to_one_table_are_separate_edges():
    g = build(
        {
            "m.py": """
            def f(s):
                s.execute("SELECT * FROM t")
                s.execute("UPDATE t SET x = 1")
            """
        }
    )
    assert edges(g, EdgeType.QUERIES) == [
        ("m.py::f", "table: t", "read"),
        ("m.py::f", "table: t", "write"),
    ]
    assert edges(g, EdgeType.MAPS_TO) == []


def test_sql_and_orm_reads_collapse_to_one_edge():
    g = build(
        {
            "m.py": """
            class User(Base):
                __tablename__ = "users"

            def f(c, s):
                c.execute("SELECT * FROM users")
                s.query(User).all()
            """
        }
    )
    assert edges(g, EdgeType.QUERIES) == [("m.py::f", "table: users", "read")]


def test_module_level_sql_is_queried_by_the_file():
    g = build({"m.py": 'cur.execute("INSERT INTO log VALUES (1)")\n'})
    assert "m.py" in g.nodes
    assert edges(g, EdgeType.QUERIES) == [("m.py", "table: log", "write")]


def test_cross_file_orm_usage_resolves_through_imports():
    g = build(
        {
            "app/__init__.py": "",
            "app/models.py": 'class User(Base):\n    __tablename__ = "users"\n',
            "svc.py": "from app.models import User\ndef f(s):\n    s.add(User())\n",
        }
    )
    assert edges(g, EdgeType.MAPS_TO) == [("app/models.py::User", "table: users", None)]
    assert edges(g, EdgeType.QUERIES) == [("svc.py::f", "table: users", "write")]
    assert_no_dangling(g)


# diagnostics
def test_db_issues_list_model_issues_first_then_per_file_extraction_issues():
    g = build(
        {
            "b.py": 'def f(c):\n    c.execute("SELECT * FROM users WHERE (id = 1")\n',
            "a.py": (
                "class W(Base):\n    __tablename__ = make()\n\n"
                'def g(c, t):\n    c.execute(f"SELECT * FROM {t}")\n'
            ),
        }
    )
    assert [(i.reason, i.file_path, i.line, i.subject) for i in g.db_issues] == [
        (Why.NON_LITERAL_TABLENAME, "a.py", 2, "a.py::W"),
        (Why.NON_LITERAL_SQL, "a.py", 5, "a.py::g"),
        (Why.UNPARSEABLE_SQL, "b.py", 2, "b.py::f"),
    ]
    assert tables(g) == {}
    assert edges(g, EdgeType.QUERIES) == []


# robustness and builder semantics
def test_no_dangling_edges_and_deterministic_rebuilds():
    builder = builder_for(
        {
            "app/__init__.py": "",
            "app/models.py": (
                'class User(Base):\n    __tablename__ = "users"\n'
                "class Admin(User): ...\n"
            ),
            "svc.py": (
                "from app.models import User, Admin\n"
                "def a(s):\n    s.query(User).all()\n    s.add(Admin())\n"
                'def b(c):\n    c.execute("DELETE FROM sessions")\n'
            ),
        }
    )
    first = builder.build()
    assert_no_dangling(first)
    again = builder.build()
    assert again.edges == first.edges
    assert set(again.nodes) == set(first.nodes)
    assert again.db_issues == first.db_issues


def test_facts_drive_tables_and_removal_clears_them():
    src = src_of("""
        class User(Base):
            __tablename__ = "users"

        def f(s):
            s.query(User).all()
        """)
    builder = GraphBuilder()
    builder.add(parse_python_source(src, "m.py"))
    g = builder.build()
    assert tables(g) == {}
    assert edges(g, EdgeType.MAPS_TO) == [] and edges(g, EdgeType.QUERIES) == []

    builder.add(parse_python_source(src, "m.py"), db=parse_db_source(src, "m.py"))
    g = builder.build()
    assert sorted(tables(g)) == ["table: users"]
    assert len(edges(g, EdgeType.QUERIES)) == 1

    builder.add(parse_python_source(src, "m.py"))
    assert tables(builder.build()) == {}

    builder.add(parse_python_source(src, "m.py"), db=parse_db_source(src, "m.py"))
    builder.remove("m.py")
    g = builder.build()
    assert tables(g) == {} and "m.py" not in g.nodes


def test_facts_for_symbols_missing_from_the_parse_are_ignored():
    db_src = 'def f(c):\n    c.execute("SELECT * FROM t")\n'
    builder = GraphBuilder()
    builder.add(
        parse_python_source("x = 1\n", "m.py"), db=parse_db_source(db_src, "m.py")
    )
    g = builder.build()
    assert edges(g, EdgeType.QUERIES) == [] and tables(g) == {}
