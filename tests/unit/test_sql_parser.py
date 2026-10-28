import pytest

from ripple.parsers.sql_parser import extract_table_accesses, looks_like_sql


def acc(sql: str):
    r = extract_table_accesses(sql)
    assert r.error is None, r.error
    return [(a.table, a.access) for a in r.accesses]


# classification
def test_select_join_and_subquery_are_reads():
    assert acc(
        "select o.id from orders o join users u on u.id = o.user_id "
        "where o.id in (select order_id from items)"
    ) == [("items", "read"), ("orders", "read"), ("users", "read")]


def test_comma_join_and_self_join_dedupe():
    assert acc("SELECT * FROM a, b") == [("a", "read"), ("b", "read")]
    assert acc("SELECT * FROM users u1 JOIN users u2 ON u1.id = u2.id") == [
        ("users", "read")
    ]


def test_insert_with_and_without_column_list():
    assert acc("INSERT INTO orders (user_id) VALUES (%s)") == [("orders", "write")]
    assert acc("INSERT INTO orders VALUES (1, 2)") == [("orders", "write")]


def test_insert_select_splits_target_and_source():
    assert acc("INSERT INTO archive SELECT * FROM orders WHERE id = 1") == [
        ("archive", "write"),
        ("orders", "read"),
    ]


def test_update_and_update_from():
    assert acc("UPDATE users SET name = :name WHERE id = :id") == [("users", "write")]
    assert acc(
        "UPDATE orders o SET status = 'x' FROM users u WHERE u.id = o.user_id"
    ) == [("orders", "write"), ("users", "read")]


def test_delete_using_and_same_table_subquery():
    assert acc("DELETE FROM orders USING users WHERE users.id = orders.user_id") == [
        ("orders", "write"),
        ("users", "read"),
    ]
    assert acc("DELETE FROM users WHERE id IN (SELECT id FROM users WHERE x)") == [
        ("users", "read"),
        ("users", "write"),
    ]


def test_merge():
    assert acc(
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET x = s.x"
    ) == [("s", "read"), ("t", "write")]


def test_multiple_statements():
    assert acc("INSERT INTO a VALUES (1); SELECT * FROM b") == [
        ("a", "write"),
        ("b", "read"),
    ]


# names
def test_cte_names_are_not_tables():
    assert acc("WITH recent AS (SELECT * FROM orders) SELECT * FROM recent") == [
        ("orders", "read")
    ]


def test_write_inside_cte_is_found():
    assert acc(
        "WITH moved AS (DELETE FROM queue WHERE id = 1 RETURNING *) "
        "INSERT INTO done SELECT * FROM moved"
    ) == [("done", "write"), ("queue", "write")]


def test_schema_prefix_quotes_and_case_are_normalized():
    assert acc('SELECT * FROM Public."Users"') == [("users", "read")]


# placeholders
@pytest.mark.parametrize("p", ["%s", "%(id)s", ":id", "$1", "?"])
def test_placeholder_styles_parse(p):
    assert acc(f"SELECT * FROM users WHERE id = {p}") == [("users", "read")]


def test_postgres_cast_is_not_a_placeholder():
    assert acc("SELECT id::int FROM users WHERE ts > :since") == [("users", "read")]


# filtering and errors
@pytest.mark.parametrize(
    "text, expected",
    [
        ("SELECT 1", True),
        ("  -- note\n select 1", True),
        ("/* x */ WITH a AS (SELECT 1) SELECT * FROM a", True),
        ("selection", False),
        ("update_user", False),
        ("updates", False),
        ("hello world", False),
        ("", False),
    ],
)
def test_looks_like_sql(text, expected):
    assert looks_like_sql(text) is expected


@pytest.mark.parametrize("text", ["hello world", "", "CREATE TABLE x (id int)"])
def test_non_dml_text_is_ignored_without_error(text):
    r = extract_table_accesses(text)
    assert r.accesses == [] and r.error is None


def test_unparseable_sql_is_reported_not_raised():
    r = extract_table_accesses("SELECT * FROM users WHERE (id = 1")
    assert r.accesses == [] and r.error is not None
