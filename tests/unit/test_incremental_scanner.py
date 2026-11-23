import random
from pathlib import Path

import pytest

from ripple.graph.schema import EdgeType
from ripple.scanner import incremental
from ripple.scanner.incremental import IncrementalScanner
from ripple.scanner.repo_scanner import ScanError, build_graph

BASE = {
    "app/__init__.py": "",
    "app/svc.py": "def get(): ...\n",
    "tests/test_svc.py": "from app.svc import get\ndef test_get():\n    get()\n",
}


def put(root: Path, rel: str, text: str | None) -> None:
    p = root / rel
    if text is None:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode())


def make(root: Path, files: dict[str, str] = BASE) -> None:
    for rel, text in files.items():
        put(root, rel, text)


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.py")}


def calls(result) -> set[tuple[str, str]]:
    return {(e.src, e.dst) for e in result.graph.edges_of(EdgeType.CALLS)}


def assert_equivalent(inc, fresh) -> None:
    assert inc.graph.nodes == fresh.graph.nodes
    assert inc.graph.edges == fresh.graph.edges
    assert inc.graph.unresolved == fresh.graph.unresolved
    assert inc.graph.route_issues == fresh.graph.route_issues
    assert inc.graph.db_issues == fresh.graph.db_issues
    zero = {"duration_seconds": 0.0}
    assert inc.summary.model_copy(update=zero) == fresh.summary.model_copy(update=zero)


@pytest.fixture
def parsed_paths(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = incremental.parse_bytes

    def spy(raw, rel, digest=None):
        seen.append(rel)
        return real(raw, rel, digest)

    monkeypatch.setattr(incremental, "parse_bytes", spy)
    return seen


# behaviour
def test_first_refresh_parses_everything_and_an_idle_one_parses_nothing(
    tmp_path, parsed_paths
):
    make(tmp_path)
    scanner = IncrementalScanner(tmp_path)

    first = scanner.refresh()
    assert first.changes.added == tuple(sorted(BASE))
    assert first.changes.unchanged == 0
    assert sorted(parsed_paths) == sorted(BASE)

    parsed_paths.clear()
    second = scanner.refresh()
    assert (second.changes.added, second.changes.changed, second.changes.removed) == (
        (),
        (),
        (),
    )
    assert second.changes.unchanged == len(BASE)
    assert parsed_paths == []
    assert_equivalent(second.result, first.result)


def test_only_a_file_whose_bytes_changed_is_reparsed(tmp_path, parsed_paths):
    make(tmp_path)
    scanner = IncrementalScanner(tmp_path)
    scanner.refresh()
    parsed_paths.clear()

    put(tmp_path, "app/svc.py", BASE["app/svc.py"])
    assert scanner.refresh().changes.changed == ()
    assert parsed_paths == []

    put(tmp_path, "app/svc.py", "def get(): ...\ndef put(): ...\n")
    r = scanner.refresh()
    assert r.changes.changed == ("app/svc.py",)
    assert parsed_paths == ["app/svc.py"]


def test_an_edit_in_the_target_file_updates_edges_that_start_elsewhere(
    tmp_path, parsed_paths
):
    make(tmp_path)
    scanner = IncrementalScanner(tmp_path)
    caller, target = "tests/test_svc.py::test_get", "app/svc.py::get"
    assert (caller, target) in calls(scanner.refresh().result)
    parsed_paths.clear()

    put(tmp_path, "app/svc.py", "def fetch(): ...\n")
    r = scanner.refresh()
    assert parsed_paths == ["app/svc.py"]
    assert (caller, target) not in calls(r.result)
    assert_equivalent(r.result, build_graph(tmp_path))


def test_removed_and_renamed_files(tmp_path):
    make(tmp_path)
    scanner = IncrementalScanner(tmp_path)
    scanner.refresh()

    (tmp_path / "app/svc.py").rename(tmp_path / "app/service.py")
    r = scanner.refresh()
    assert (r.changes.added, r.changes.removed) == (
        ("app/service.py",),
        ("app/svc.py",),
    )
    assert "app/svc.py" not in r.result.graph.nodes
    assert_equivalent(r.result, build_graph(tmp_path))


def test_a_syntax_error_is_reported_until_the_file_is_fixed(tmp_path):
    make(tmp_path)
    scanner = IncrementalScanner(tmp_path)
    scanner.refresh()

    put(tmp_path, "app/svc.py", "def broken(:\n")
    s = scanner.refresh().result.summary
    assert [e.path for e in s.parse_errors] == ["app/svc.py"]

    s = scanner.refresh().result.summary
    assert s.parse_error_count == 1

    put(tmp_path, "app/svc.py", "def get(): ...\n")
    assert scanner.refresh().result.summary.parse_error_count == 0


def test_a_failed_refresh_leaves_the_cache_intact(tmp_path, parsed_paths):
    make(tmp_path)
    scanner = IncrementalScanner(tmp_path)
    scanner.refresh()

    for rel in BASE:
        put(tmp_path, rel, None)
    with pytest.raises(ScanError, match="no readable Python files"):
        scanner.refresh()

    make(tmp_path)
    parsed_paths.clear()
    r = scanner.refresh()
    assert r.changes.unchanged == len(BASE)
    assert parsed_paths == []


# property: incremental == fresh
MAIN = (
    "from fastapi import FastAPI\n"
    "app = FastAPI()\n\n"
    '@app.get("/health")\n'
    "def health(): ...\n"
)
MAIN_USERS = (
    MAIN + "\nfrom app.repo import load\n\n"
    '@app.get("/users")\n'
    "def users():\n    load()\n"
)

VARIANTS: dict[str, list[str | None]] = {
    "app/__init__.py": ["", "X = 1\n"],
    "app/svc.py": [
        None,
        "def get(): ...\n",
        "def get(): ...\ndef put(): ...\n",
        "class Svc:\n    def get(self): ...\n",
        "def broken(:\n",
    ],
    "app/models.py": [
        None,
        'class User(Base):\n    __tablename__ = "users"\n',
        'class User(Base):\n    __tablename__ = "people"\n',
        "class User: ...\n",
    ],
    "app/repo.py": [
        None,
        "from app.models import User\ndef load(s):\n    return s.query(User).all()\n",
        'def load(c):\n    c.execute("SELECT * FROM users")\n',
        "from app.svc import get\ndef load():\n    get()\n",
    ],
    "app/main.py": [None, MAIN, MAIN_USERS],
    "tests/conftest.py": [
        None,
        "from app.svc import get\ndef make():\n    get()\n",
    ],
    "tests/test_main.py": [
        None,
        "def test_health(client):\n    client.get('/health')\n",
        "from tests.conftest import make\ndef test_make():\n    make()\n",
        "from app.svc import get\ndef test_get():\n    get()\n",
    ],
}


@pytest.mark.parametrize("seed", range(30))
def test_incremental_equals_a_fresh_build_after_random_edits(tmp_path, seed):
    rng = random.Random(seed)
    for rel, options in VARIANTS.items():
        put(tmp_path, rel, rng.choice(options))
    scanner = IncrementalScanner(tmp_path)
    scanner.refresh()

    for _ in range(25):
        before = snapshot(tmp_path)
        for _ in range(rng.randint(1, 3)):
            rel = rng.choice(sorted(VARIANTS))
            put(tmp_path, rel, rng.choice(VARIANTS[rel]))
        after = snapshot(tmp_path)

        got = scanner.refresh()
        assert_equivalent(got.result, build_graph(tmp_path))
        assert got.changes.added == tuple(sorted(after.keys() - before.keys()))
        assert got.changes.removed == tuple(sorted(before.keys() - after.keys()))
        assert got.changes.changed == tuple(
            sorted(p for p in before.keys() & after.keys() if before[p] != after[p])
        )
