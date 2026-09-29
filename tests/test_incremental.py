"""Tests for repopedia.incremental (no network; git via temp repos)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from repopedia.incremental import (
    changed_files,
    get_head_sha,
    is_git_repo,
    update_repo,
)
from repopedia.index import index_repo
from repopedia.store import open_store

git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, env={**os.environ, **_GIT_ENV})


@pytest.fixture()
def gitrepo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "b.py").write_text("from a import alpha\n\n\ndef beta():\n    return alpha()\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


@git
class TestIncremental:
    def test_first_update_is_full_reindex(self, gitrepo, tmp_path):
        db = tmp_path / "g.db"
        res = update_repo(gitrepo, db_path=db, quiet=True)
        assert res.full_reindex and not res.up_to_date
        with open_store(db) as s:
            assert s.get_meta("head_sha") == get_head_sha(gitrepo)
            assert s.find_symbol("a.alpha")

    def test_no_changes_is_up_to_date(self, gitrepo, tmp_path):
        db = tmp_path / "g.db"
        index_repo(gitrepo, db_path=db, quiet=True)
        r1 = update_repo(gitrepo, db_path=db, quiet=True)
        assert r1.full_reindex  # no baseline yet
        r2 = update_repo(gitrepo, db_path=db, quiet=True)
        assert r2.up_to_date and not r2.full_reindex

    def test_modify_file_adds_symbol_and_edge(self, gitrepo, tmp_path):
        db = tmp_path / "g.db"
        index_repo(gitrepo, db_path=db, quiet=True)
        update_repo(gitrepo, db_path=db, quiet=True)  # baseline

        (gitrepo / "a.py").write_text(
            "def alpha():\n    return 1\n\n\ndef gamma():\n    return alpha()\n")
        _git(gitrepo, "add", ".")
        _git(gitrepo, "commit", "-qm", "add gamma")

        res = update_repo(gitrepo, db_path=db, quiet=True)
        assert not res.up_to_date and not res.full_reindex
        assert res.files_updated == 1
        with open_store(db) as s:
            assert s.get_meta("head_sha") == get_head_sha(gitrepo)
            assert s.find_symbol("a.gamma"), "new symbol missing after update"
            # gamma -> alpha call edge exists
            gamma = s.find_symbol("a.gamma")[0]
            dsts = {e["dst_qualified"] for e in s.out_edges(gamma["id"], "calls")}
            assert "a.alpha" in dsts
            # old content intact: b.beta still calls a.alpha
            beta = s.find_symbol("b.beta")[0]
            assert "a.alpha" in {e["dst_qualified"]
                                 for e in s.out_edges(beta["id"], "calls")}

    def test_delete_file_removes_its_nodes(self, gitrepo, tmp_path):
        db = tmp_path / "g.db"
        index_repo(gitrepo, db_path=db, quiet=True)
        update_repo(gitrepo, db_path=db, quiet=True)

        (gitrepo / "b.py").unlink()
        _git(gitrepo, "add", ".")
        _git(gitrepo, "commit", "-qm", "drop b")

        res = update_repo(gitrepo, db_path=db, quiet=True)
        assert res.files_deleted == 1
        with open_store(db) as s:
            assert not s.find_symbol("b.beta")
            assert not s.file_node("b.py")
            assert s.find_symbol("a.alpha")  # untouched file intact

    def test_non_git_dir_falls_back_to_full(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "x.py").write_text("def x():\n    pass\n")
        db = tmp_path / "g.db"
        assert not is_git_repo(plain)
        res = update_repo(plain, db_path=db, quiet=True)
        assert res.full_reindex
        with open_store(db) as s:
            assert s.find_symbol("x.x")

    def test_rename_is_delete_plus_add(self, gitrepo, tmp_path):
        db = tmp_path / "g.db"
        index_repo(gitrepo, db_path=db, quiet=True)
        update_repo(gitrepo, db_path=db, quiet=True)  # baseline
        base_sha = get_head_sha(gitrepo)

        _git(gitrepo, "mv", "a.py", "a2.py")
        _git(gitrepo, "commit", "-qm", "rename a.py -> a2.py")

        # raw diff parsing sees the rename
        kinds = {(c[0], c[2]) for c in changed_files(gitrepo, base_sha)}
        assert ("R", "a2.py") in kinds

        res = update_repo(gitrepo, db_path=db, quiet=True)
        assert res.files_added == 1 and res.files_deleted == 1
        with open_store(db) as s:
            assert not s.file_node("a.py"), "old path should be gone"
            assert s.find_symbol("a2.alpha"), "renamed file should be indexed"
            # b.beta's call to alpha still resolves (now to a2.alpha)
            beta = s.find_symbol("b.beta")[0]
            dsts = {e["dst_qualified"] for e in s.out_edges(beta["id"], "calls")}
            assert "a2.alpha" in dsts
