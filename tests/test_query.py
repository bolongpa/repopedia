"""Tests for repopedia.query (no network)."""

import json

import pytest

from repopedia.index import index_repo
from repopedia.query import (
    UnknownSymbol,
    blast_radius,
    callees,
    callers,
    file_symbols,
    inheritance_chain,
)
from repopedia.store import open_store

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def pyrepo_db(tmp_path):
    db = tmp_path / "graph.db"
    index_repo(FIXTURES / "pyrepo", db_path=db, quiet=True)
    return db


class TestCallersCallees:
    def test_callers(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            got = {c["qualified_name"] for c in callers(s, "pkg.util.helper")}
        assert got == {"pkg.core.Engine.run", "pkg.util.parse"}

    def test_callers_short_name(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            got = {c["qualified_name"] for c in callers(s, "helper")}
        assert got == {"pkg.core.Engine.run", "pkg.util.parse"}

    def test_callees_resolved(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            got = callees(s, "pkg.core.Engine.run")
        by_name = {c["qualified_name"] for c in got if c.get("resolved")}
        assert by_name == {"pkg.util.helper", "pkg.core.Engine.log"}
        assert all(c["via"] == "calls" for c in got)

    def test_unknown_symbol(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            with pytest.raises(UnknownSymbol):
                callers(s, "no.such.thing")

    def test_results_are_json_serializable(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            json.dumps(callers(s, "pkg.util.helper"))
            json.dumps(callees(s, "pkg.core.Engine.run"))
            json.dumps(blast_radius(s, "pkg.util.helper"))
            json.dumps(inheritance_chain(s, "pkg.core.Engine"))
            json.dumps(file_symbols(s, "pkg/util.py"))


class TestBlastRadius:
    def test_depths_and_via(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            res = blast_radius(s, "pkg.util.helper")
        by_name = {r["qualified_name"]: r for r in res}
        assert by_name["pkg.util.helper"]["depth"] == 0
        assert by_name["pkg.util.helper"]["via"] == "self"
        # direct callers, depth 1
        assert by_name["pkg.core.Engine.run"]["depth"] == 1
        assert by_name["pkg.core.Engine.run"]["via"] == "calls(1)"
        assert by_name["pkg.util.parse"]["depth"] == 1
        # transitive caller, depth 2
        assert by_name["pkg.core.launch"]["depth"] == 2
        assert by_name["pkg.core.launch"]["via"] == "calls(2)"
        # import-driven: pkg/core.py imports pkg.util
        assert by_name["pkg.core"]["via"] == "imports"
        assert by_name["pkg.core"]["depth"] == 1

    def test_depth_limit(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            res = blast_radius(s, "pkg.util.helper", depth=1)
        names = {r["qualified_name"] for r in res}
        assert "pkg.core.launch" not in names  # depth 2, cut off
        assert "pkg.core.Engine.run" in names

    def test_ordered_by_depth_then_name(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            res = blast_radius(s, "pkg.util.helper")
        keys = [(r["depth"], r["qualified_name"]) for r in res]
        assert keys == sorted(keys)


class TestInheritance:
    def test_chain(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            chain = inheritance_chain(s, "pkg.core.Engine")
        assert [c["qualified_name"] for c in chain] == ["pkg.core.Base"]

    def test_non_class_raises(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            with pytest.raises(UnknownSymbol):
                inheritance_chain(s, "pkg.util.helper")


class TestFileSymbols:
    def test_symbols(self, pyrepo_db):
        with open_store(pyrepo_db) as s:
            got = {x["qualified_name"] for x in file_symbols(s, "pkg/util.py")}
        assert got == {"pkg.util.helper", "pkg.util.parse"}
