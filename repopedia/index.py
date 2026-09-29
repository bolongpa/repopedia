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

    py_extractor = PythonExtractor()
    ts_extractor = TypeScriptExtractor()
    tsx_extractor = TypeScriptExtractor(tsx=True)

    result = IndexResult()
    # phase 1: parse every file
    parsed: list[tuple[str, str, FileFacts]] = []  # (relpath, language, facts)
    for path, lang in _iter_source_files(root, language):
        rel = path.relative_to(root).as_posix()
        try:
            source = path.read_bytes()
        except OSError as exc:
            result.skipped.append(f"{rel} (unreadable: {exc})")
            continue
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError:
            result.skipped.append(f"{rel} (not utf-8)")
            continue
        module = module_for(rel, "python" if lang == "python" else "typescript")
        try:
            if lang == "python":
                facts = py_extractor.extract(source, module)
            elif lang == "tsx":
                facts = tsx_extractor.extract(source, module)
            else:
                facts = ts_extractor.extract(source, module)
        except Exception as exc:  # never let one file kill the run
            result.skipped.append(f"{rel} (parser crashed: {exc})")
            continue
        if facts.has_error:
            result.skipped.append(f"{rel} (syntax errors)")
            continue
        parsed.append((rel, lang, facts))
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


def _persist(store: Store, root: Path, parsed, result: IndexResult, quiet: bool) -> None:
    # -- nodes ---------------------------------------------------------
    file_ids: dict[str, int] = {}
    qual_to_id: dict[str, int] = {}
    short_to_ids: dict[str, list[int]] = {}
    class_ids: dict[str, list[int]] = {}

    def reg_short(name: str, nid: int, is_class: bool) -> None:
        short_to_ids.setdefault(name, []).append(nid)
        if is_class:
            class_ids.setdefault(name, []).append(nid)

    for rel, lang, facts in parsed:
        language = "python" if lang == "python" else "typescript"
        fid = store.add_node(KIND_FILE, Path(rel).name, facts.module, rel, 1,
                             _line_count(root, rel), language)
        file_ids[rel] = fid
        qual_to_id[facts.module] = fid
        for sym in facts.symbols:
            nid = store.add_node(sym.kind, sym.name, sym.qualified_name, rel,
                                 sym.line_start, sym.line_end, language)
            qual_to_id[sym.qualified_name] = nid
            reg_short(sym.name, nid, sym.kind == "class")
            store.add_edge(fid, nid, EDGE_DEFINES)
            result.symbols += 1
            result.edges += 1

    # -- edges ----------------------------------------------------------
    for rel, lang, facts in parsed:
        fid = file_ids[rel]
        for imp in facts.imports:
            target = _resolve_import(root, rel, lang, imp.module, imp.level)
            data = {"module": imp.module}
            store.add_edge(fid, file_ids.get(target) if target else None,
                           EDGE_IMPORTS, data)
            result.edges += 1
        for inh in facts.inherits:
            dst = _resolve_class(inh.base, class_ids, qual_to_id)
            store.add_edge(qual_to_id[inh.class_qualified], dst, EDGE_INHERITS,
                           {"raw": inh.base})
            result.edges += 1
        for call in facts.calls:
            src = qual_to_id.get(call.enclosing_qualified, fid)
            dst, candidates = _resolve_call(call.raw, short_to_ids, class_ids)
            data = {"raw": call.raw}
            if dst is None and candidates:
                data["candidates"] = candidates
            store.add_edge(src, dst, EDGE_CALLS, data)
            result.edges += 1


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
