import pytest
from neo4j.exceptions import ConstraintError

from ripple.graph.schema import NodeType


def test_ensure_schema_is_idempotent(store):
    store.ensure_schema()
    store.ensure_schema()
    names = {r["name"] for r in store.execute("SHOW CONSTRAINTS YIELD name")}
    assert {f"{label.value.lower()}_id" for label in NodeType} <= names


def test_id_is_unique_per_label(store):
    store.execute("CREATE (:File {id: 'a.py'})")
    with pytest.raises(ConstraintError):
        store.execute("CREATE (:File {id: 'a.py'})")
    assert store.counts() == ({"File": 1}, {})


def test_clear_removes_nodes_and_relationships(store):
    store.execute("CREATE (:File {id: 'x'})-[:IMPORTS]->(:File {id: 'y'})")
    assert store.counts()[1] == {"IMPORTS": 1}
    store.clear()
    assert store.counts() == ({}, {})
