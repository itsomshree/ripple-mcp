from pathlib import Path

import pytest

from ripple.scanner.repo_scanner import ScanError, scan_repo

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_fastapi_repo"


def test_scan_repo_writes_the_graph_to_neo4j(store):
    result = scan_repo(FIXTURE, store)
    nodes, edges = store.counts()
    assert nodes == result.summary.nodes
    assert edges == result.summary.edges
    assert (sum(nodes.values()), sum(edges.values())) == (48, 79)


def test_a_failed_scan_leaves_the_stored_graph_untouched(store, tmp_path):
    scan_repo(FIXTURE, store)
    before = store.counts()
    for bad in (tmp_path / "missing", tmp_path):
        with pytest.raises(ScanError):
            scan_repo(bad, store)
    assert store.counts() == before
