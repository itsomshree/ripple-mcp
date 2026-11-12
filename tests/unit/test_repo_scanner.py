import os
from pathlib import Path

import pytest

from ripple.graph.builder import GraphBuilder
from ripple.graph.schema import NodeType
from ripple.parsers.db_parser import parse_db_file
from ripple.parsers.fastapi_parser import parse_fastapi_file
from ripple.parsers.pytest_parser import parse_coverage_file
from ripple.parsers.python_parser import parse_python_file
from ripple.scanner.repo_scanner import (
    MAX_LISTED_ERRORS,
    ScanError,
    build_graph,
    scan_repo,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"


def write(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())


def file_ids(graph) -> set[str]:
    return {n.id for n in graph.nodes.values() if n.type == NodeType.FILE}


def hand_wired_graph():
    b = GraphBuilder()
    for p in sorted(FIXTURE.rglob("*.py"), key=lambda p: p.as_posix()):
        rel = p.relative_to(FIXTURE).as_posix()
        b.add(
            parse_python_file(p, rel),
            parse_fastapi_file(p, rel),
            parse_db_file(p, rel),
            parse_coverage_file(p, rel),
        )
    return b.build()


class FakeStore:
    def __init__(self):
        self.graphs = []

    def replace_graph(self, graph):
        self.graphs.append(graph)


# sample fixture
def test_scanning_the_fixture_matches_the_hand_wired_builder():
    scanned = build_graph(FIXTURE).graph
    expected = hand_wired_graph()
    assert scanned.nodes == expected.nodes
    assert scanned.edges == expected.edges
    assert (len(scanned.nodes), len(scanned.edges)) == (48, 79)


def test_summary_counts_the_fixture():
    s = build_graph(FIXTURE).summary
    assert s.files_scanned == 13
    assert s.nodes == {
        "File": 13,
        "Class": 5,
        "Function": 16,
        "APIEndpoint": 7,
        "DBTable": 2,
        "Test": 5,
    }
    assert s.edges == {
        "DEFINED_IN": 26,
        "IMPORTS": 14,
        "CALLS": 18,
        "HANDLES": 7,
        "MAPS_TO": 2,
        "QUERIES": 7,
        "TESTS": 5,
    }
    assert s.parse_error_count == 0
    assert s.route_issues == {} and s.db_issues == {}
    assert s.duration_seconds >= 0


def test_scans_are_deterministic():
    a, b = build_graph(FIXTURE), build_graph(FIXTURE)
    assert a.graph.nodes == b.graph.nodes and a.graph.edges == b.graph.edges
    zero = {"duration_seconds": 0.0}
    assert a.summary.model_copy(update=zero) == b.summary.model_copy(update=zero)


# discovery
def test_skips_vcs_cache_and_virtualenv_dirs(tmp_path):
    write(
        tmp_path,
        {
            "app/a.py": "x = 1\n",
            "app/__pycache__/c.py": "x = 1\n",
            ".git/hooks/h.py": "x = 1\n",
            "node_modules/pkg/n.py": "x = 1\n",
            ".venv/lib/v.py": "x = 1\n",
            "myenv/pyvenv.cfg": "home = /usr\n",
            "myenv/lib/m.py": "x = 1\n",
            "pkg.egg-info/e.py": "x = 1\n",
            "env/real.py": "x = 1\n",
        },
    )
    assert file_ids(build_graph(tmp_path).graph) == {"app/a.py", "env/real.py"}


def test_exclude_globs_match_nested_paths_and_are_counted(tmp_path):
    write(
        tmp_path,
        {"app/a.py": "x = 1\n", "gen/x/y.py": "x = 1\n", "gen/z.py": "x = 1\n"},
    )
    r = build_graph(tmp_path, exclude=["gen/*"])
    assert file_ids(r.graph) == {"app/a.py"}
    assert r.summary.files_excluded == 2


def test_oversized_files_are_skipped_and_counted(tmp_path):
    write(tmp_path, {"small.py": "x = 1\n", "big.py": "x = 1\n" * 100})
    r = build_graph(tmp_path, max_file_bytes=100)
    assert file_ids(r.graph) == {"small.py"}
    assert r.summary.files_too_large == 1


def test_symlinks_are_not_followed(tmp_path):
    write(tmp_path, {"outside/o.py": "x = 1\n", "repo/a.py": "x = 1\n"})
    try:
        os.symlink(tmp_path / "outside", tmp_path / "repo" / "link")
        os.symlink(tmp_path / "repo" / "a.py", tmp_path / "repo" / "alias.py")
    except OSError:
        pytest.skip("symlinks not supported here")
    assert file_ids(build_graph(tmp_path / "repo").graph) == {"a.py"}


# bad input
def test_bad_files_do_not_stop_the_scan(tmp_path):
    write(
        tmp_path,
        {
            "ok.py": "def f(): pass\n",
            "broken.py": "def broken(:\n",
            "latin.py": b"# caf\xe9\ndef g(): pass\n",
        },
    )
    r = build_graph(tmp_path)
    assert r.summary.files_scanned == 3
    assert r.summary.parse_error_count == 1
    assert [e.path for e in r.summary.parse_errors] == ["broken.py"]
    assert r.summary.parse_errors[0].error.startswith("SyntaxError")
    assert "broken.py" in r.graph.nodes and "latin.py::g" in r.graph.nodes


def test_listed_parse_errors_are_capped_but_counted(tmp_path):
    total = MAX_LISTED_ERRORS + 5
    write(tmp_path, {f"bad{i:02}.py": "def broken(:\n" for i in range(total)})
    s = build_graph(tmp_path).summary
    assert s.parse_error_count == total
    assert len(s.parse_errors) == MAX_LISTED_ERRORS
    assert "and 5 more" in s.format()


# safety
def test_missing_path_raises_and_never_touches_the_store(tmp_path):
    store = FakeStore()
    with pytest.raises(ScanError, match="not a directory"):
        scan_repo(tmp_path / "nope", store)
    assert store.graphs == []


def test_repo_without_python_files_raises_and_never_touches_the_store(tmp_path):
    write(tmp_path, {"README.md": "hi", "node_modules/x.py": "x = 1\n"})
    store = FakeStore()
    with pytest.raises(ScanError, match="no readable Python files"):
        scan_repo(tmp_path, store)
    assert store.graphs == []


def test_successful_scan_replaces_the_graph_once(tmp_path):
    write(tmp_path, {"a.py": "def f(): pass\n"})
    store = FakeStore()
    r = scan_repo(tmp_path, store)
    assert store.graphs == [r.graph]
    assert r.summary.files_scanned == 1


# rendering
def test_format_lists_counts_and_parse_errors(tmp_path):
    write(tmp_path, {"ok.py": "x = 1\n", "broken.py": "def broken(:\n"})
    text = build_graph(tmp_path).summary.format()
    assert "files: 2 scanned" in text
    assert "parse errors: 1" in text
    assert "broken.py: SyntaxError" in text
