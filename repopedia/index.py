"""Repo indexer: walk a source tree, extract per-file facts, resolve
cross-file references, and persist the knowledge graph to SQLite.

Resolution is deliberately heuristic and documented as such:
- calls resolve by short name when the name is unambiguous in the repo;
  ``self.x``/``this.x`` match method ``x``; ``ClassName(...)`` resolves to
  the class node. Ambiguous or unknown names are kept as edges with
  ``dst=NULL`` and ``data={"raw": ...}`` so later phases can see them.
- imports resolve to repo files via normal module-resolution rules;
  stdlib / third-party / bare specifiers stay ``dst=NULL`` with
  ``data={"module": ...}``.
- inherits resolves when the base class name is unique in the repo.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from .extract import (
    EDGE_CALLS,
    EDGE_DEFINES,
    EDGE_IMPORTS,
    EDGE_INHERITS,
    KIND_FILE,
    FileFacts,
)
from .extract.python import PythonExtractor
from .extract.typescript import TypeScriptExtractor
from .store import Store

SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".venv", "venv", ".tox",
    "dist", "build", ".pytest_cache", ".repopedia",
}

EXTENSIONS = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
}


@dataclass
class IndexResult:
    files_parsed: int = 0
    symbols: int = 0
    edges: int = 0
    skipped: list[str] = field(default_factory=list)  # repo-relative paths with reason


def module_for(rel: str, language: str) -> str:
    """Dotted module path from a repo-relative file path."""
    p = Path(rel)
    if language == "python":
        parts = list(p.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)
    # typescript: strip extension, keep path separators as dots
    return ".".join(p.with_suffix("").parts)


_extractors: dict[str, object] | None = None


def _get_extractors() -> dict[str, object]:
    """Shared extractor instances (stateless across files)."""
    global _extractors
    if _extractors is None:
        _extractors = {
            "python": PythonExtractor(),
            "typescript": TypeScriptExtractor(),
            "tsx": TypeScriptExtractor(tsx=True),
        }
    return _extractors


def parse_file(root: Path, path: Path, result: IndexResult) -> tuple[str, str, "FileFacts"] | None:
    """Parse one file into (relpath, language, FileFacts).

    Records the reason in ``result.skipped`` and returns None for files
    that must not enter the graph. Shared by full index and incremental update.
    """
    rel = path.relative_to(root).as_posix()
    lang = EXTENSIONS.get(path.suffix)
    if lang is None:  # not a source file we handle
        return None
    try:
        source = path.read_bytes()
    except OSError as exc:
        result.skipped.append(f"{rel} (unreadable: {exc})")
        return None
    try:
        source.decode("utf-8")
    except UnicodeDecodeError:
        result.skipped.append(f"{rel} (not utf-8)")
        return None
    module = module_for(rel, "python" if lang == "python" else "typescript")
    try:
        facts = _get_extractors()[lang].extract(source, module)  # type: ignore[index]
    except Exception as exc:  # never let one file kill the run
        result.skipped.append(f"{rel} (parser crashed: {exc})")
        return None
    if facts.has_error:
        result.skipped.append(f"{rel} (syntax errors)")
        return None
    return rel, lang, facts


def _iter_source_files(root: Path, language: str):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        ext = path.suffix
        lang = EXTENSIONS.get(ext)
        if lang is None:
            continue
        if language == "py" and lang != "python":
            continue
        if language == "ts" and lang not in ("typescript", "tsx"):
            continue
        yield path, lang


def index_repo(root: str | Path, db_path: str | Path | None = None,
               language: str = "all", quiet: bool = False) -> IndexResult:
    """Index ``root`` into a SQLite knowledge graph. Returns summary stats."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    db_path = Path(db_path) if db_path else root / ".repopedia" / "graph.db"

    result = IndexResult()
    # phase 1: parse every file
    parsed: list[tuple[str, str, FileFacts]] = []  # (relpath, language, facts)
    for path, lang in _iter_source_files(root, language):
        item = parse_file(root, path, result)
        if item is not None:
            parsed.append(item)
            result.files_parsed += 1

    for skipped in result.skipped:
        warn(f"skipped {skipped}", quiet)

    store = Store(db_path)
    try:
        store.clear()
        _persist(store, root, parsed, result, quiet)
        store.commit()
    finally:
        store.close()
    return result


@dataclass
class LookupMaps:
    """Name-resolution indexes over the store's current nodes.

    Rebuilt from the store (``build_lookup_maps``) so incremental updates
    can re-resolve one file's edges against everything else.
    """
    file_ids: dict[str, int] = field(default_factory=dict)
    qual_to_id: dict[str, int] = field(default_factory=dict)
    short_to_ids: dict[str, list[int]] = field(default_factory=dict)
    class_ids: dict[str, list[int]] = field(default_factory=dict)


def build_lookup_maps(store: Store) -> LookupMaps:
    """Build resolution maps from the nodes already in the store."""
    maps = LookupMaps()
    for row in store.all_nodes():
        nid, kind, name, qual = row["id"], row["kind"], row["name"], row["qualified_name"]
        maps.qual_to_id[qual] = nid
        if kind == KIND_FILE:
            maps.file_ids[row["file"]] = nid
            continue
        maps.short_to_ids.setdefault(name, []).append(nid)
        if kind == "class":
            maps.class_ids.setdefault(name, []).append(nid)
    return maps


def index_file_nodes(store: Store, root: Path, rel: str, lang: str,
                     facts: FileFacts, maps: LookupMaps, result: IndexResult) -> None:
    """Persist one parsed file's nodes (+ defines edges). Updates ``maps``."""
    language = "python" if lang == "python" else "typescript"
    fid = store.add_node(KIND_FILE, Path(rel).name, facts.module, rel, 1,
                         _line_count(root, rel), language)
    maps.file_ids[rel] = fid
    maps.qual_to_id[facts.module] = fid
    for sym in facts.symbols:
        nid = store.add_node(sym.kind, sym.name, sym.qualified_name, rel,
                             sym.line_start, sym.line_end, language)
        maps.qual_to_id[sym.qualified_name] = nid
        maps.short_to_ids.setdefault(sym.name, []).append(nid)
        if sym.kind == "class":
            maps.class_ids.setdefault(sym.name, []).append(nid)
        store.add_edge(fid, nid, EDGE_DEFINES)
        result.symbols += 1
        result.edges += 1


def resolve_file_edges(store: Store, root: Path, rel: str, lang: str,
                       facts: FileFacts, maps: LookupMaps, result: IndexResult) -> None:
    """Resolve and persist one file's imports/inherits/calls against ``maps``."""
    fid = maps.file_ids[rel]
    for imp in facts.imports:
        target = _resolve_import(root, rel, lang, imp.module, imp.level)
        data = {"module": imp.module}
        store.add_edge(fid, maps.file_ids.get(target) if target else None,
                       EDGE_IMPORTS, data)
        result.edges += 1
    for inh in facts.inherits:
        dst = _resolve_class(inh.base, maps.class_ids, maps.qual_to_id)
        store.add_edge(maps.qual_to_id[inh.class_qualified], dst, EDGE_INHERITS,
                       {"raw": inh.base})
        result.edges += 1
    for call in facts.calls:
        src = maps.qual_to_id.get(call.enclosing_qualified, fid)
        dst, candidates = _resolve_call(call.raw, maps.short_to_ids, maps.class_ids)
        data = {"raw": call.raw}
        if dst is None and candidates:
            data["candidates"] = candidates
        store.add_edge(src, dst, EDGE_CALLS, data)
        result.edges += 1


def _persist(store: Store, root: Path, parsed, result: IndexResult, quiet: bool) -> None:
    maps = LookupMaps()
    for rel, lang, facts in parsed:
        index_file_nodes(store, root, rel, lang, facts, maps, result)
    for rel, lang, facts in parsed:
        resolve_file_edges(store, root, rel, lang, facts, maps, result)


def _line_count(root: Path, rel: str) -> int:
    try:
        with open(root / rel, "rb") as f:
            return sum(1 for _ in f) or 1
    except OSError:
        return 1


# -- resolution ----------------------------------------------------------
def _resolve_call(raw: str, short_to_ids: dict, class_ids: dict) -> tuple[int | None, int]:
    """Heuristic name-based resolution. Returns (node_id or None, candidate count)."""
    target = raw.split(".")[-1]
    if not target or target in ("self", "this", "super"):
        return None, 0
    cls = class_ids.get(target, [])
    if len(cls) == 1:
        return cls[0], 1  # ClassName(...) instantiation
    cands = [i for i in short_to_ids.get(target, [])]
    # prefer non-class symbols for call targets when ambiguous with a class
    non_class = [i for i in cands if i not in cls]
    pool = non_class or cands
    if len(pool) == 1:
        return pool[0], 1
    return None, len(pool)


def _resolve_class(base: str, class_ids: dict, qual_to_id: dict) -> int | None:
    cands = class_ids.get(base.split(".")[-1], [])
    if len(cands) == 1:
        return cands[0]
    # try qualified suffix match, e.g. "pkg.Base"
    for qual, nid in qual_to_id.items():
        if qual == base or qual.endswith("." + base):
            cands.append(nid)
    uniq = list(dict.fromkeys(cands))
    return uniq[0] if len(uniq) == 1 else None


def _resolve_import(root: Path, rel: str, lang: str, module: str, level: int) -> str | None:
    """Resolve an imported module to a repo-relative file path, or None."""
    if lang == "python":
        return _resolve_py_import(root, rel, module, level)
    return _resolve_ts_import(root, rel, module)


def _resolve_py_import(root: Path, rel: str, module: str, level: int) -> str | None:
    parts = Path(rel).parent.parts
    if level:
        # relative: level=1 -> current package dir; level=2 -> its parent; ...
        # (for __init__.py the package is its own dir, which is also `parts`)
        base = list(parts[: len(parts) - level + 1]) if level <= len(parts) else []
        if module:
            base = base + module.split(".")
    else:
        if not module:
            return None
        base = module.split(".")
    for cand in (Path(*base).with_suffix(".py"), Path(*base, "__init__.py")):
        if (root / cand).is_file():
            return cand.as_posix()
    return None


def _resolve_ts_import(root: Path, rel: str, module: str) -> str | None:
    if not module.startswith("."):
        return None  # bare specifier: stdlib / third-party
    base = (Path(rel).parent / module)
    for cand in (base.with_suffix(".ts"), base.with_suffix(".tsx"),
                 base / "index.ts", base / "index.tsx"):
        # with_suffix on a path with no suffix just appends; normalize manually
        p = root / cand
        if p.is_file():
            return cand.as_posix()
    # module already had an extension, e.g. "./util.ts"
    if (root / base).is_file() and base.suffix in (".ts", ".tsx"):
        return base.as_posix()
    return None


def warn(msg: str, quiet: bool) -> None:
    if not quiet:
        print(f"warning: {msg}", file=sys.stderr)
