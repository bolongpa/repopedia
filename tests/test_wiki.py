"""Tests for repopedia.wiki (no network)."""

import filecmp
import re
from pathlib import Path

import pytest

from repopedia.index import index_repo
from repopedia.store import open_store
from repopedia.wiki import generate_wiki, staleness_warning

FIXTURES = Path(__file__).parent / "fixtures"
CITATION_RE = re.compile(r"[A-Za-z0-9_\-./]+\.(py|ts|tsx):\d+")


@pytest.fixture()
def pyrepo_db(tmp_path):
    db = tmp_path / "graph.db"
    index_repo(FIXTURES / "pyrepo", db_path=db, quiet=True)
    return db


@pytest.fixture()
def wiki_out(tmp_path, pyrepo_db):
    out = tmp_path / "wiki"
    result = generate_wiki(FIXTURES / "pyrepo", out, db_path=pyrepo_db, llm=None)
    return out, result


def _pages(out: Path) -> dict[str, str]:
    return {
        str(p.relative_to(out)): p.read_text(encoding="utf-8")
        for p in sorted(out.rglob("*.md"))
    }


class TestWikiPages:
    def test_expected_pages_exist(self, wiki_out):
        out, result = wiki_out
        assert (out / "index.md").exists()
        assert (out / "modules" / "pkg.md").exists()
        assert (out / "architecture.md").exists()
        assert set(result["pages"]) == {
            "index.md", "modules/pkg.md", "architecture.md"}

    def test_citations_present(self, wiki_out):
        out, _ = wiki_out
        pages = _pages(out)
        assert CITATION_RE.search(pages["modules/pkg.md"]), \
            "module page must cite file:line"
        assert CITATION_RE.search(pages["architecture.md"]), \
            "architecture page must cite file:line"

    def test_mermaid_fences_balanced(self, wiki_out):
        out, _ = wiki_out
        for rel, text in _pages(out).items():
            fences = text.count("```")
            assert fences % 2 == 0, f"unbalanced fences in {rel}"
        arch = _pages(out)["architecture.md"]
        assert arch.count("```mermaid") >= 2, \
            "architecture page should have dependency + inheritance diagrams"

    def test_module_dependency_diagram_has_edges(self, tmp_path):
        # two top-level modules with a real import between them
        repo = tmp_path / "twomod"
        (repo / "alpha").mkdir(parents=True)
        (repo / "beta").mkdir(parents=True)
        (repo / "alpha" / "x.py").write_text(
            "from beta.y import thing\n\n\ndef use():\n    return thing()\n")
        (repo / "beta" / "y.py").write_text("def thing():\n    return 1\n")
        db = tmp_path / "graph.db"
        index_repo(repo, db_path=db, quiet=True)
        out = tmp_path / "wiki"
        generate_wiki(repo, out, db_path=db, llm=None)
        arch = (out / "architecture.md").read_text(encoding="utf-8")
        assert "-->" in arch, "dependency graph should contain arrows"
        assert "alpha" in arch and "beta" in arch

    def test_deterministic_output(self, tmp_path, pyrepo_db):
        out1 = tmp_path / "w1"
        out2 = tmp_path / "w2"
        generate_wiki(FIXTURES / "pyrepo", out1, db_path=pyrepo_db, llm=None)
        generate_wiki(FIXTURES / "pyrepo", out2, db_path=pyrepo_db, llm=None)
        files1 = sorted(p.relative_to(out1) for p in out1.rglob("*.md"))
        files2 = sorted(p.relative_to(out2) for p in out2.rglob("*.md"))
        assert files1 == files2
        for rel in files1:
            assert filecmp.cmp(out1 / rel, out2 / rel, shallow=False), \
                f"non-deterministic output in {rel}"

    def test_stale_sha_warns(self, tmp_path, pyrepo_db):
        with open_store(pyrepo_db) as s:
            s.set_meta("head_sha", "deadbeef" * 5)
            warning = staleness_warning(FIXTURES / "pyrepo", s)
        assert warning is not None
        assert "repopedia update" in warning

    def test_non_git_repo_warns(self, tmp_path, pyrepo_db):
        with open_store(pyrepo_db) as s:
            warning = staleness_warning(tmp_path, s)
        assert warning is not None
        assert "not a git repository" in warning

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(ValueError, match="no graph database"):
            generate_wiki(FIXTURES / "pyrepo", tmp_path / "wiki",
                          db_path=tmp_path / "nope.db")
