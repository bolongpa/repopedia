"""Tests for repopedia.mcp_server (no network, no stdio transport).

The tool logic lives in RepopediaTools methods — tests call them
directly. LLM paths use FakeLLM from repopedia.llm.
"""

import json
from pathlib import Path

import pytest

from repopedia.index import index_repo
from repopedia.llm import FakeLLM
from repopedia.mcp_server import RepopediaTools, _TOOL_NAMES, build_server
from repopedia.store import open_store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def tools(tmp_path):
    db = tmp_path / "graph.db"
    index_repo(FIXTURES / "pyrepo", db_path=db, quiet=True)
    return RepopediaTools(db, llm=None)


@pytest.fixture()
def tools_with_llm(tmp_path):
    db = tmp_path / "graph.db"
    index_repo(FIXTURES / "pyrepo", db_path=db, quiet=True)
    return RepopediaTools(db, llm=FakeLLM(reply="fake synthesis"))


def _assert_json_safe(obj):
    json.dumps(obj)  # raises TypeError on non-serializable values


class TestTools:
    def test_find_symbol(self, tools):
        res = tools.find_symbol("helper")
        _assert_json_safe(res)
        qns = {s["qualified_name"] for s in res["symbols"]}
        assert qns == {"pkg.util.helper"}
        assert res["symbols"][0]["location"] == "pkg/util.py:1"

    def test_get_callers(self, tools):
        res = tools.get_callers("pkg.util.helper")
        _assert_json_safe(res)
        got = {c["qualified_name"] for c in res["callers"]}
        assert got == {"pkg.core.Engine.run", "pkg.util.parse"}
        assert all("line_start" in c for c in res["callers"])

    def test_get_callees(self, tools):
        res = tools.get_callees("pkg.core.Engine.run")
        _assert_json_safe(res)
        resolved = {c["qualified_name"] for c in res["callees"] if c.get("resolved")}
        assert resolved == {"pkg.util.helper", "pkg.core.Engine.log"}

    def test_blast_radius(self, tools):
        res = tools.blast_radius("pkg.util.helper", depth=2)
        _assert_json_safe(res)
        vias = {r["via"] for r in res["impacted"]}
        assert "self" in vias
        assert any(v.startswith("calls(") for v in vias)
        assert res["depth"] == 2

    def test_search_codebase(self, tools):
        res = tools.search_codebase("helper", top_k=5)
        _assert_json_safe(res)
        assert res["results"]
        assert all(r["via"] in ("lexical", "graph") for r in res["results"])
        assert all("line_start" in r for r in res["results"])

    def test_get_file_symbols(self, tools):
        res = tools.get_file_symbols("pkg/core.py")
        _assert_json_safe(res)
        qns = {s["qualified_name"] for s in res["symbols"]}
        assert {"pkg.core.Base", "pkg.core.Engine",
                "pkg.core.Engine.run", "pkg.core.launch"} <= qns

    def test_unknown_symbol_returns_error_dict(self, tools):
        res = tools.get_callers("no.such.symbol")
        _assert_json_safe(res)
        assert "error" in res

    def test_ask_codebase_without_llm(self, tools):
        res = tools.ask_codebase("where is helper defined")
        _assert_json_safe(res)
        assert res["answer"] is None
        assert "no LLM configured" in res["note"]
        assert res["evidence"], "evidence must be returned even without an LLM"

    def test_ask_codebase_with_fake_llm(self, tools_with_llm):
        res = tools_with_llm.ask_codebase("where is helper defined")
        _assert_json_safe(res)
        assert res["answer"] == "fake synthesis"
        assert res["evidence"]
        # the synthesis prompt must enforce cite-only-what-you're-given
        system, _user = tools_with_llm.llm.calls[0]
        assert "file:line" in system
        assert "Never invent" in system

    def test_build_server_registers_all_tools(self, tools):
        server = build_server(tools.db_path, llm=None)
        names = {t.name for t in server._tool_manager.list_tools()}
        assert names == set(_TOOL_NAMES)
