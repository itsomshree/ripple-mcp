import pytest
from neo4j.exceptions import DriverError, Neo4jError
from pydantic import ValidationError

from ripple.graph.store import GraphStore


@pytest.fixture(scope="module")
def store():
    try:
        s = GraphStore.from_settings()
        s.verify()
    except (ValidationError, DriverError, Neo4jError, OSError) as exc:
        pytest.skip(f"Neo4j not reachable: {exc}")
    s.ensure_schema()
    s.clear()
    yield s
    s.clear()
    s.close()
