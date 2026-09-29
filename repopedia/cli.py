"""repopedia CLI: index a source repo into a SQLite knowledge graph."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .index import index_repo


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repopedia",
        description="Index a source repository into a queryable code knowledge graph.",
    )
    p.add_argument("--version", action="version", version=f"repopedia {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    idx = sub.add_parser("index", help="index a repository")
    idx.add_argument("path", help="path to the repository root")
    idx.add_argument("--db", default=None,
                     help="output SQLite path (default: <repo>/.repopedia/graph.db)")
    idx.add_argument("--language", choices=["py", "ts", "all"], default="all",
                     help="limit to Python (.py) or TypeScript (.ts/.tsx) files")
    idx.set_defaults(func=cmd_index)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
