import textwrap

import pytest

from ripple.graph.schema import DbIssueReason as Why
from ripple.parsers.db_parser import parse_db_source, parse_usage_source
from ripple.parsers.python_parser import parse_python_source

PATH = "app/repo.py"


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def facts(text: str):
    return parse_db_source(src_of(text), PATH)


def uses(text: str):
    return [
        (u.owner_id.partition("::")[2] or "<file>", u.table, u.access, u.line)
        for u in facts(text).uses
    ]


# sinks
@pytest.mark.parametrize(
    "call",
    [
        "cur.execute",
        "cur.executemany",
        "cur.executescript",
        "conn.exec_driver_sql",
        "await conn.fetch",
        "await conn.fetchrow",
        "await conn.fetchval",
        "pd.read_sql",
        "pd.read_sql_query",
        "text",
    ],
)
def test_sink_names(call):
    src = f'async def f(cur, conn):\n    x = {call}("SELECT * FROM users")\n'
    assert uses(src) == [("f", "users", "read", 2)]


def test_receiver_may_be_any_expression():
    assert uses("""
        def f(conn):
            conn.cursor().execute("SELECT * FROM a")
        """) == [("f", "a", "read", 2)]


def test_write_and_read_modes_and_lines():
    assert uses("""
        def save(cur):
            cur.execute("INSERT INTO orders (user_id) VALUES (%s)", (1,))
            cur.execute("INSERT INTO archive SELECT * FROM orders")
        """) == [
        ("save", "orders", "write", 2),
        ("save", "archive", "write", 3),
        ("save", "orders", "read", 3),
    ]


def test_line_is_the_call_line():
    assert uses('''
        def f(c):
            c.execute(
                """
                SELECT * FROM a
                """
            )
        ''') == [("f", "a", "read", 2)]


def test_text_and_sql_wrappers_are_unwrapped_once():
    assert uses("""
        def f(s):
            s.execute(text("SELECT * FROM a"))
            s.execute(sql.SQL("SELECT * FROM b"))
            s.execute(sql=text("SELECT * FROM c"))
        """) == [
        ("f", "a", "read", 2),
        ("f", "b", "read", 3),
        ("f", "c", "read", 4),
    ]


def test_adjacent_and_concatenated_literals_fold():
    assert uses("""
        def f(cur):
            cur.execute("SELECT * FROM " "a")
            cur.execute("SELECT * FROM " + "b")
        """) == [("f", "a", "read", 2), ("f", "b", "read", 3)]


# constants
def test_name_bound_once_to_a_literal_resolves():
    assert uses("""
        TOP = "SELECT * FROM top_t"

        def f(cur):
            q = "SELECT * FROM local_t"
            cur.execute(q)
            cur.execute(TOP)
        """) == [("f", "local_t", "read", 5), ("f", "top_t", "read", 6)]


def test_reassigned_shadowed_or_param_names_do_not_resolve():
    assert (
        uses("""
        Q = "SELECT * FROM t"
        R = "SELECT * FROM r"
        R = "SELECT * FROM r2"

        def a(cur, Q):
            cur.execute(Q)

        def b(cur):
            q = "SELECT * FROM x"
            q = "SELECT * FROM y"
            cur.execute(q)
            cur.execute(R)

        def c(cur):
            Q = make()
            cur.execute(Q)
        """)
        == []
    )


def test_nested_function_sees_enclosing_local_literal():
    assert uses("""
        def outer(cur):
            q = "SELECT * FROM t"
            def inner():
                cur.execute(q)
            return inner
        """) == [("outer.inner", "t", "read", 4)]


# owners
def test_module_and_class_body_sinks_belong_to_the_file():
    assert uses("""
        cur.execute("SELECT * FROM a")

        class Repo:
            cur.execute("SELECT * FROM b")

            def get(self):
                self.cur.execute("SELECT * FROM c")
        """) == [
        ("<file>", "a", "read", 1),
        ("<file>", "b", "read", 4),
        ("Repo.get", "c", "read", 7),
    ]


def test_owner_ids_match_python_parser_nodes():
    text = src_of("""
        c.execute("SELECT * FROM a")

        def top(c):
            c.execute("SELECT * FROM b")

        class C:
            def m(self):
                def inner(c):
                    c.execute("SELECT * FROM d")
        """)
    node_ids = {n.id for n in parse_python_source(text, PATH).nodes}
    found = parse_db_source(text, PATH).uses
    assert len(found) == 3
    assert {u.owner_id for u in found} <= node_ids


# what is ignored
def test_sql_looking_text_outside_sinks_is_ignored():
    f = facts('''
        def f(log):
            """Update the user record."""
            log.info("SELECT * FROM users")
            x = "DELETE FROM users"
        ''')
    assert f.uses == [] and f.issues == []


@pytest.mark.parametrize(
    "arg",
    ['"http://example.com"', '"CREATE TABLE x (id int)"', '"hello"', '""'],
)
def test_non_dml_literals_in_sinks_are_silent(arg):
    f = facts(f"def f(c):\n    c.fetch({arg})\n")
    assert f.uses == [] and f.issues == []


def test_decorators_defaults_and_bases_are_not_scanned():
    f = facts("""
        @cur.execute("SELECT * FROM a")
        def f(x=cur.execute("SELECT * FROM b")): ...
        """)
    assert f.uses == [] and f.issues == []


# diagnostics
@pytest.mark.parametrize(
    "arg",
    [
        'f"SELECT * FROM {t}"',
        '"SELECT * FROM %s" % t',
        '"SELECT * FROM {}".format(t)',
        '"SELECT * FROM " + t',
        '"SELECT * FROM " + t + " WHERE x"',
    ],
)
def test_dynamic_sql_with_visible_prefix_is_reported(arg):
    f = facts(f"def f(c, t):\n    c.execute({arg})\n")
    assert f.uses == []
    assert [(i.file_path, i.line, i.subject, i.reason) for i in f.issues] == [
        (PATH, 2, f"{PATH}::f", Why.NON_LITERAL_SQL)
    ]


@pytest.mark.parametrize(
    "arg",
    ["sql", "select(User)", "build()", 'f"{prefix}SELECT 1"', "t + 'x'", "self.query"],
)
def test_other_dynamic_arguments_are_silent(arg):
    f = facts(f"def f(c, sql, t, prefix, select, User, build):\n    c.execute({arg})\n")
    assert f.uses == [] and f.issues == []


def test_unparseable_literal_is_reported_without_uses():
    f = facts('def f(c):\n    c.execute("SELECT * FROM users WHERE (id = 1")\n')
    assert f.uses == []
    assert [i.reason for i in f.issues] == [Why.UNPARSEABLE_SQL]


# bundle + robustness
def test_syntax_error_yields_no_usage():
    assert parse_usage_source("c.execute('SELECT * FROM a')\ndef broken(:\n", PATH) == (
        [],
        [],
    )


def test_bundle_carries_usage_alongside_models():
    f = facts("""
        class A(Base):
            __tablename__ = "a"

        def f(c):
            c.execute("SELECT * FROM a")
        """)
    assert [m.table for m in f.models] == ["a"]
    assert [u.table for u in f.uses] == ["a"]
