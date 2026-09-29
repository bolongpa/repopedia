"""repopedia CLI: index, query, update, and search a code knowledge graph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .incremental import update_repo
from .index import index_repo
from .query import (
    UnknownSymbol,
    blast_radius,
    callees,
    callers,
    file_symbols,
    inheritance_chain,
)
from .search import hybrid_search
from .store import open_store


def _db_for(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    return Path(args.repo) / ".repopedia" / "graph.db"


def _out(obj, as_json: bool) -> int:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        _pretty(obj)
    return 0


def _pretty(obj) -> None:
    if isinstance(obj, dict) and obj.get("_kind") == "update":
        r = obj["result"]
        if r["up_to_date"]:
            print("already up to date")
        elif r["full_reindex"]:
            print(f"full reindex: {r['symbols']} symbols, {r['edges']} edges")
        else:
            print(f"updated: +{r['files_added']} added, "
                  f"~{r['files_updated']} modified, -{r['files_deleted']} deleted")
            print(f"  symbols: {r['symbols']}, edges: {r['edges']}")
        if r["skipped"]:
            print("  skipped:")
            for s in r["skipped"]:
                print(f"    - {s}")
        return
    if isinstance(obj, dict) and obj.get("_kind") == "symbols":
        for s in obj["symbols"]:
            print(f"  {s['qualified_name']}  {s['file']}:{s['line_start']}")
        return
    for item in obj:
        loc = f"{item['file']}:{item.get('line_start', '?')}"
        name = item.get("qualified_name") or f"<unresolved: {item.get('raw')}>"
        extra = []
        if item.get("via"):
            extra.append(f"[{item['via']}]")
        if item.get("score") is not None:
            extra.append(f"score={item['score']}")
        print(f"  {name}  {loc}  {' '.join(extra)}".rstrip())


# -- commands ---------------------------------------------------------------
def cmd_index(args: argparse.Namespace) -> int:
    try:
        result = index_repo(args.path, db_path=args.db, language=args.language)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    db = Path(args.db) if args.db else Path(args.path) / ".repopedia" / "graph.db"
    print(f"indexed {args.path}")
    print(f"  db:      {db}")
    print(f"  files:   {result.files_parsed} parsed, {len(result.skipped)} skipped")
    print(f"  symbols: {result.symbols}")
    print(f"  edges:   {result.edges}")
    if result.skipped:
        print("  skipped files:")
        for s in result.skipped:
            print(f"    - {s}")
    return 0


def _run_query(args: argparse.Namespace, fn, *fn_args) -> int:
    try:
        with open_store(_db_for(args)) as store:
            res = fn(store, *fn_args)
    except UnknownSymbol as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return _out(res, args.json)


def cmd_query_callers(args: argparse.Namespace) -> int:
    return _run_query(args, callers, args.symbol)


def cmd_query_callees(args: argparse.Namespace) -> int:
    return _run_query(args, callees, args.symbol)


def cmd_query_inherits(args: argparse.Namespace) -> int:
    return _run_query(args, inheritance_chain, args.symbol)


def cmd_query_file(args: argparse.Namespace) -> int:
    with open_store(_db_for(args)) as store:
        syms = file_symbols(store, args.file)
    return _out({"_kind": "symbols", "symbols": syms}, args.json)


def cmd_blast_radius(args: argparse.Namespace) -> int:
    return _run_query(args, blast_radius, args.symbol, args.depth)


def cmd_update(args: argparse.Namespace) -> int:
    result = update_repo(args.path, db_path=args.db)
    return _out({"_kind": "update", "result": {
        "up_to_date": result.up_to_date,
        "full_reindex": result.full_reindex,
        "files_added": result.files_added,
        "files_deleted": result.files_deleted,
        "files_updated": result.files_updated,
        "symbols": result.symbols,
        "edges": result.edges,
        "new_sha": result.new_sha,
        "skipped": result.skipped,
    }}, args.json)


def cmd_search(args: argparse.Namespace) -> int:
    with open_store(_db_for(args)) as store:
        res = hybrid_search(store, " ".join(args.query), top_k=args.top_k)
    return _out(res, args.json)


# -- parser -----------------------------------------------------------------
def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=".",
                   help="repository root (default: current directory)")
    p.add_argument("--db", default=None,
                   help="SQLite path (default: <repo>/.repopedia/graph.db)")
    p.add_argument("--json", action="store_true",
                   help="machine-readable JSON output")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repopedia",
        description="Index a source repository into a queryable code knowledge graph.",
    )
    p.add_argument("--version", action="version", version=f"repopedia {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    idx = sub.add_parser("index", help="index a repository (full)")
    idx.add_argument("path", help="path to the repository root")
    idx.add_argument("--db", default=None,
                     help="output SQLite path (default: <repo>/.repopedia/graph.db)")
    idx.add_argument("--language", choices=["py", "ts", "all"], default="all",
                     help="limit to Python (.py) or TypeScript (.ts/.tsx) files")
    idx.set_defaults(func=cmd_index)

    q = sub.add_parser("query", help="query the graph")
    qsub = q.add_subparsers(dest="qcommand", required=True)
    for name, help_text, func in [
        ("callers", "who calls this symbol", cmd_query_callers),
        ("callees", "what this symbol calls", cmd_query_callees),
        ("inherits", "base classes of this class", cmd_query_inherits),
    ]:
        c = qsub.add_parser(name, help=help_text)
        c.add_argument("symbol", help="qualified name (e.g. pkg.mod.Service.run)")
        _add_common(c)
        c.set_defaults(func=func)
    cf = qsub.add_parser("file", help="symbols defined in a file")
    cf.add_argument("file", help="repo-relative file path")
    _add_common(cf)
    cf.set_defaults(func=cmd_query_file)

    br = sub.add_parser("blast-radius", help="what could break if this changes")
    br.add_argument("symbol", help="qualified name (e.g. pkg.mod.Service.run)")
    br.add_argument("--depth", type=int, default=3,
                    help="call-graph traversal depth (default: 3)")
    _add_common(br)
    br.set_defaults(func=cmd_blast_radius)

    up = sub.add_parser("update", help="incremental reindex from git history")
    up.add_argument("path", nargs="?", default=".",
                    help="path to the repository root (default: .)")
    up.add_argument("--db", default=None,
                    help="SQLite path (default: <repo>/.repopedia/graph.db)")
    up.add_argument("--json", action="store_true")
    up.set_defaults(func=cmd_update)

    se = sub.add_parser("search", help="hybrid lexical + graph search")
    se.add_argument("query", nargs="+", help="search terms")
    se.add_argument("--top-k", type=int, default=10)
    _add_common(se)
    se.set_defaults(func=cmd_search)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
