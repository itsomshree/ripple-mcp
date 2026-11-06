from pathlib import Path

import pytest
from neo4j.exceptions import DriverError, Neo4jError
from pydantic import ValidationError

from ripple.graph.builder import GraphBuilder
from ripple.graph.store import GraphStore
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file

SAMPLE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"


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


@pytest.fixture(scope="session")
def sample_graph():
    b = GraphBuilder()
    for p in sorted(SAMPLE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(SAMPLE).as_posix()
        b.add(
            parse_python_file(p, rel),
            parse_fastapi_file(p, rel),
            parse_db_file(p, rel),
            parse_coverage_file(p, rel),
        )
    return b.build()
