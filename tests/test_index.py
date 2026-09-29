"""End-to-end indexing tests on the fixture repos (no network)."""

import json
import sqlite3
from pathlib import Path

import pytest

from repopedia.index import index_repo
from repopedia.store import open_store

FIXTURES = Path(__file__).parent / "fixtures"


def index_fixture(name: str, tmp_path: Path, language: str = "all"):
    db = tmp_path / "graph.db"
    result = index_repo(FIXTURES / name, db_path=db, language=language, quiet=True)
    return result, db


def qual_map(db_path: Path) -> dict[str, dict]:
    with open_store(db_path) as s:
        rows = s._conn.execute("SELECT * FROM nodes").fetchall()
        return {r["qualified_name"]: dict(r) for r in rows}


def edges_of(db_path: Path, kind: str) -> list[dict]:
    with open_store(db_path) as s:
        rows = s._conn.execute(
            """SELECT e.kind, s1.qualified_name AS src_q, s2.qualified_name AS dst_q, e.data
               FROM edges e JOIN nodes s1 ON s1.id = e.src
               LEFT JOIN nodes s2 ON s2.id = e.dst WHERE e.kind = ?""",
            (kind,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"])
            out.append(d)
        return out


# ---------------------------------------------------------------- python
class TestPythonRepo:
    def test_summary_and_broken_file_skipped(self, tmp_path):
        result, _ = index_fixture("pyrepo", tmp_path)
        assert result.files_parsed == 3  # __init__, util, core (broken.py skipped)
        assert any("broken.py" in s and "syntax" in s for s in result.skipped)
        assert result.symbols == 8

    def test_symbols_with_qualified_names_and_spans(self, tmp_path):
        _, db = index_fixture("pyrepo", tmp_path)
        qm = qual_map(db)
        expected = {
            "pkg.util.helper": ("function", 1, 3),
            "pkg.util.parse": ("function", 6, 7),
            "pkg.core.Base": ("class", 5, 7),
            "pkg.core.Base.name": ("method", 6, 7),
            "pkg.core.Engine": ("class", 10, 17),
            "pkg.core.Engine.run": ("method", 11, 14),
            "pkg.core.Engine.log": ("method", 16, 17),
            "pkg.core.launch": ("function", 20, 22),
        }
        for qual, (kind, ls, le) in expected.items():
            assert qual in qm, f"missing {qual}"
            assert qm[qual]["kind"] == kind
            assert (qm[qual]["line_start"], qm[qual]["line_end"]) == (ls, le), qual

    def test_calls_resolve_within_repo(self, tmp_path):
        _, db = index_fixture("pyrepo", tmp_path)
        calls = {(c["src_q"], c["dst_q"]): c["data"] for c in edges_of(db, "calls")}
        # helper(task) inside Engine.run -> pkg.util.helper (cross-file)
        assert calls[("pkg.core.Engine.run", "pkg.util.helper")]["raw"] == "helper"
        # self.log -> the unique method named log
        assert calls[("pkg.core.Engine.run", "pkg.core.Engine.log")]["raw"] == "self.log"
        # Engine() instantiation -> the class node
        assert calls[("pkg.core.launch", "pkg.core.Engine")]["raw"] == "Engine"
        # engine.run -> unique method named run
        assert calls[("pkg.core.launch", "pkg.core.Engine.run")]["raw"] == "engine.run"
        # print() has no repo target: dst NULL, raw kept
        unresolved = [c for c in edges_of(db, "calls") if c["dst_q"] is None]
        assert any(c["data"]["raw"] == "print" for c in unresolved)

    def test_inherits_resolves(self, tmp_path):
        _, db = index_fixture("pyrepo", tmp_path)
        inh = edges_of(db, "inherits")
        assert len(inh) == 1
        assert (inh[0]["src_q"], inh[0]["dst_q"]) == ("pkg.core.Engine", "pkg.core.Base")

    def test_imports_resolve_to_files(self, tmp_path):
        _, db = index_fixture("pyrepo", tmp_path)
        imp = {(c["src_q"], c["dst_q"]): c["data"] for c in edges_of(db, "imports")}
        # from pkg.util import helper -> pkg/util.py
        assert imp[("pkg.core", "pkg.util")]["module"] == "pkg.util"
        # import os -> stdlib, dst NULL but module recorded
        assert imp[("pkg.core", None)]["module"] == "os"


# ------------------------------------------------------------- typescript
class TestTypeScriptRepo:
    def test_summary_and_broken_file_skipped(self, tmp_path):
        result, _ = index_fixture("tsrepo", tmp_path)
        assert result.files_parsed == 2
        assert any("broken.ts" in s for s in result.skipped)
        assert result.symbols == 8

    def test_symbols_with_qualified_names_and_spans(self, tmp_path):
        _, db = index_fixture("tsrepo", tmp_path)
        qm = qual_map(db)
        expected = {
            "src.util.helper": ("function", 1, 3),
            "src.util.parse": ("function", 5, 7),
            "src.service.Base": ("class", 3, 7),
            "src.service.Base.name": ("method", 4, 6),
            "src.service.Service": ("class", 9, 19),
            "src.service.Service.run": ("method", 10, 14),
            "src.service.Service.log": ("method", 16, 18),
            "src.service.launch": ("function", 21, 24),
        }
        for qual, (kind, ls, le) in expected.items():
            assert qual in qm, f"missing {qual}"
            assert qm[qual]["kind"] == kind
            assert (qm[qual]["line_start"], qm[qual]["line_end"]) == (ls, le), qual

    def test_calls_inherits_imports(self, tmp_path):
        _, db = index_fixture("tsrepo", tmp_path)
        calls = {(c["src_q"], c["dst_q"]): c["data"] for c in edges_of(db, "calls")}
        assert calls[("src.service.Service.run", "src.util.helper")]["raw"] == "helper"
        assert calls[("src.service.Service.run", "src.service.Service.log")]["raw"] == "this.log"
        # NOTE: console.log resolves to Service.log by short-name heuristic
        # (documented W1 behavior: last-component name match; raw is preserved
        # in data for later phases to refine).
        assert calls[("src.service.Service.log", "src.service.Service.log")]["raw"] == "console.log"
        # new Service('x') -> the class node
        assert calls[("src.service.launch", "src.service.Service")]["raw"] == "Service"
        assert calls[("src.service.launch", "src.service.Service.run")]["raw"] == "svc.run"

        inh = edges_of(db, "inherits")
        assert (inh[0]["src_q"], inh[0]["dst_q"]) == ("src.service.Service", "src.service.Base")

        imp = {(c["src_q"], c["dst_q"]): c["data"] for c in edges_of(db, "imports")}
        assert imp[("src.service", "src.util")]["module"] == "./util"


# ------------------------------------------------------------------ store
class TestStoreAPI:
    def test_find_symbol_and_edge_traversal(self, tmp_path):
        _, db = index_fixture("pyrepo", tmp_path)
        with open_store(db) as s:
            # exact qualified match
            hits = s.find_symbol("pkg.core.Engine.run")
            assert len(hits) == 1 and hits[0]["name"] == "run"
            # short-name fallback
            hits = s.find_symbol("helper")
            assert any(h["qualified_name"] == "pkg.util.helper" for h in hits)
            # unknown name -> empty
            assert s.find_symbol("does_not_exist") == []

            run_id = s.find_symbol("pkg.core.Engine.run")[0]["id"]
            outs = s.out_edges(run_id, "calls")
            dsts = {e["dst_qualified"] for e in outs}
            assert "pkg.util.helper" in dsts and "pkg.core.Engine.log" in dsts

            helper_id = s.find_symbol("pkg.util.helper")[0]["id"]
            ins = s.in_edges(helper_id, "calls")
            srcs = {e["src_qualified"] for e in ins}
            # called by Engine.run and by parse
            assert srcs == {"pkg.core.Engine.run", "pkg.util.parse"}

    def test_defines_edges_cover_all_symbols(self, tmp_path):
        _, db = index_fixture("pyrepo", tmp_path)
        with open_store(db) as s:
            n_symbols = s._conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind != 'file'").fetchone()[0]
            n_defines = s._conn.execute(
                "SELECT COUNT(*) FROM edges WHERE kind = 'defines'").fetchone()[0]
            assert n_defines == n_symbols


# -------------------------------------------------------------------- cli
class TestLanguageFilter:
    def test_language_filter(self, tmp_path):
        result, _ = index_fixture("pyrepo", tmp_path, language="py")
        assert result.files_parsed == 3
        result, _ = index_fixture("pyrepo", tmp_path, language="ts")
        assert result.files_parsed == 0

    def test_invalid_path(self):
        with pytest.raises(ValueError):
            index_repo("/does/not/exist", quiet=True)
