"""Tests for repopedia.search (no network)."""

import json
from pathlib import Path

import pytest

from repopedia.index import index_repo
from repopedia.search import BM25, hybrid_search, tokenize
from repopedia.store import open_store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def pyrepo_db(tmp_path):
    db = tmp_path / "graph.db"
    index_repo(FIXTURES / "pyrepo", db_path=db, quiet=True)
    return db


class TestTokenize:
    def test_snake_and_dots(self):
        assert tokenize("pkg.core.Engine") == ["pkg", "core", "engine"]

    def test_camel_case(self):
        assert tokenize("handleRetry") == ["handle", "retry"]
        assert tokenize("HTMLParser") == ["html", "parser"]

    def test_kebab(self):
        assert tokenize("my-module") == ["my", "module"]


class TestBM25:
    def test_ranks_exact_match_first(self):
        bm = BM25([["alpha", "beta"], ["alpha"], ["gamma"]])
        scores = bm.scores(["alpha", "beta"])
        assert scores[0] > scores[1] > scores[2] == 0.0

    def test_no_match_zero(self):
        bm = BM25([["alpha"]])
        assert bm.scores(["zzz"]) == [0.0]


class TestHybridSearch:
    def test_top1_lexical(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            res = hybrid_search(s, "pkg.util.helper")
        assert res, "expected results"
        assert res[0]["qualified_name"] == "pkg.util.helper"
        assert res[0]["via"] == "lexical"

    def test_graph_expansion_pulls_neighbors(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            res = hybrid_search(s, "helper")
        by_name = {r["qualified_name"]: r for r in res}
        assert "pkg.util.helper" in by_name
        # callers of helper come along via the graph even if BM25 missed them
        graph_hits = {n for n, r in by_name.items() if r["via"] == "graph"}
        assert graph_hits & {"pkg.core.Engine.run", "pkg.util.parse"}, \
            f"expected a graph neighbor, got via=graph: {graph_hits}"

    def test_file_line_present(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            res = hybrid_search(s, "Engine")
        assert res
        for r in res:
            assert r["file"] and r["line_start"] >= 1

    def test_json_serializable(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            json.dumps(hybrid_search(s, "helper"))

    def test_empty_query_no_crash(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            assert hybrid_search(s, "zzzznotathing") == []
