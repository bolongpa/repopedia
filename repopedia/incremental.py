"""Git-diff based incremental reindexing.

``repopedia update`` diffs the working tree against the commit the graph
was built from (recorded in the store's ``meta`` table as ``head_sha``)
and re-extracts only the changed source files, re-resolving their edges
against the full store. Unchanged files are never touched.

Repair pass: files that *pointed into* a changed file (callers, importers)
get their non-defines edges re-resolved too, so e.g. an ``imports`` edge
to a renamed file doesn't silently vanish. The one remaining gap is
*previously unresolvable* edges in files that point at nothing changed:
if file C calls ``foo()`` (unresolved) and an unrelated change adds
``def foo()``, C's edge stays ``dst=NULL`` until a full
``repopedia index``. Per-file correctness after update is exact.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .index import (
    EXTENSIONS,
    IndexResult,
    build_lookup_maps,
    index_file_nodes,
    index_repo,
    parse_file,
    resolve_file_edges,
    warn,
)
from .store import Store

META_SHA_KEY = "head_sha"


@dataclass
class UpdateResult:
    up_to_date: bool = False
    full_reindex: bool = False
    files_added: int = 0
    files_deleted: int = 0
    files_updated: int = 0
    symbols: int = 0
    edges: int = 0
    new_sha: str | None = None
    skipped: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def is_git_repo(repo: str | Path) -> bool:
    return _git(Path(repo), "rev-parse", "--git-dir") is not None


def get_head_sha(repo: str | Path) -> str | None:
    return _git(Path(repo), "rev-parse", "HEAD")


def changed_files(repo: Path, old_sha: str) -> list[tuple[str, str | None, str | None]]:
    """Parse ``git diff --name-status -z`` into (status, old_path, new_path).

    status is the single-letter code (A/M/D/R/T/...); renames carry both
    paths, other statuses carry only the new path.
    """
    out = _git(repo, "diff", "--name-status", "-z", old_sha, "HEAD")
    if out is None:
        return []
    parts = out.split("\0")
    changes: list[tuple[str, str | None, str | None]] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        if not status:
            i += 1
            continue
        code = status[0]
        if code == "R" and i + 2 < len(parts):
            changes.append(("R", parts[i + 1], parts[i + 2]))
            i += 3
        elif code == "D" and i + 1 < len(parts):
            changes.append(("D", parts[i + 1], None))
            i += 2
        elif i + 1 < len(parts):
            changes.append((code, None, parts[i + 1]))
            i += 2
        else:
            i += 1
    return changes


def _is_source(path: str) -> bool:
    return Path(path).suffix in EXTENSIONS


def update_repo(repo_path: str | Path, db_path: str | Path | None = None,
                quiet: bool = False) -> UpdateResult:
    """Incrementally update the graph from git history. Returns a summary."""
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    db = Path(db_path) if db_path else root / ".repopedia" / "graph.db"
    result = UpdateResult()

    if not is_git_repo(root):
        warn(f"{root} is not a git repo; falling back to full reindex", quiet)
        idx = index_repo(root, db_path=db, quiet=quiet)
        result.full_reindex = True
        result.symbols = idx.symbols
        result.edges = idx.edges
        return result

    new_sha = get_head_sha(root)
    result.new_sha = new_sha
    if _git(root, "status", "--porcelain"):
        # Diff is against HEAD (spec): uncommitted edits are invisible to
        # update. Say so loudly instead of silently reporting "up to date".
        warn("uncommitted changes present; update diffs against HEAD — "
             "commit first to include them", quiet)
    store = Store(db)
    try:
        old_sha = store.get_meta(META_SHA_KEY)

        if old_sha is not None and new_sha is not None and old_sha == new_sha:
            result.up_to_date = True
            return result

        if old_sha is None or new_sha is None:
            # No baseline (or unreadable HEAD): full reindex, then record sha.
            store.close()
            idx = index_repo(root, db_path=db, quiet=quiet)
            result.full_reindex = True
            result.symbols = idx.symbols
            result.edges = idx.edges
            if new_sha:
                with Store(db) as s2:
                    s2.set_meta(META_SHA_KEY, new_sha)
            return result

        added: list[str] = []
        deleted: list[str] = []
        modified: list[str] = []
        for code, old_p, new_p in changed_files(root, old_sha):
            if code == "D" and old_p:
                deleted.append(old_p)
            elif code == "R":
                if old_p:
                    deleted.append(old_p)
                if new_p:
                    added.append(new_p)
            elif new_p:
                (added if code == "A" else modified).append(new_p)

        src_delete = [p for p in dict.fromkeys(deleted) if _is_source(p)]
        src_modified = [p for p in dict.fromkeys(modified) if _is_source(p)]
        src_add = [p for p in dict.fromkeys(added + modified) if _is_source(p)]
        result.files_added = len([p for p in added if _is_source(p)])
        result.files_updated = len(src_modified)

        # 1. remove stale content first, so resolution sees a clean world.
        #    (M counts as delete-then-add: its old nodes must go.)
        #    Remember which unchanged files pointed into the changed ones —
        #    their edges need a repair pass in step 4.
        affected = store.inbound_source_files(src_delete + src_modified)
        for rel in src_delete:
            if store.delete_file(rel):
                result.files_deleted += 1
        for rel in [p for p in src_modified if p not in src_delete]:
            store.delete_file(rel)

        # 2. parse changed files
        idx_result = IndexResult()
        parsed = []
        for rel in src_add:
            item = parse_file(root, root / rel, idx_result)
            if item is not None:
                parsed.append(item)
        result.skipped = idx_result.skipped
        for skipped in idx_result.skipped:
            warn(f"skipped {skipped}", quiet)

        # 3. add nodes for all changed files, then resolve edges against the
        #    full store (a changed file may call another changed file).
        maps = build_lookup_maps(store)
        for rel, lang, facts in parsed:
            index_file_nodes(store, root, rel, lang, facts, maps, idx_result)
        for rel, lang, facts in parsed:
            resolve_file_edges(store, root, rel, lang, facts, maps, idx_result)

        # 4. repair: re-resolve references of unchanged files that pointed
        #    into changed files (their inbound edges died with the old nodes).
        for rel in affected:
            if rel in src_add:
                continue  # already handled above (e.g. rename target)
            store.delete_non_defines_edges(rel)
            item = parse_file(root, root / rel, idx_result)
            if item is None:
                continue
            _, lang, facts = item
            resolve_file_edges(store, root, rel, lang, facts, maps, idx_result)

        result.symbols = idx_result.symbols
        result.edges = idx_result.edges
        store.commit()

        store.set_meta(META_SHA_KEY, new_sha)
        return result
    finally:
        store.close()
