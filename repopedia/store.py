"""SQLite storage for the repopedia knowledge graph.

Schema (stable; Weeks 2-3 build on it — do not change column semantics):

    nodes(id, kind, name, qualified_name, file, line_start, line_end, language)
      kind: file | class | function | method
      qualified_name: dotted path, e.g. "pkg.mod.Service.run"
      file: repo-relative path; line_* are 1-based inclusive

    edges(src, dst, kind, data)
      src/dst: node ids. dst is NULL for unresolvable references
               (external imports, unresolved calls, out-of-repo bases).
      kind: defines | imports | calls | inherits
      data: JSON object with kind-specific extras, always including the
            raw written form:
              imports -> {"module": "pkg.util"} (+ "alias" when present)
              calls   -> {"raw": "helper"} (+ "candidates": N when ambiguous)
              inherits -> {"raw": "Base"}

Indexes are placed on the columns Week 2 queries: qualified_name lookups,
(kind) filters, file-scoped scans, and edge traversals by (src, kind) and
(dst, kind) — the latter powers reverse call-graph (blast radius) queries.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  qualified_name TEXT NOT NULL,
  file TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  language TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges(
  src INTEGER NOT NULL REFERENCES nodes(id),
  dst INTEGER,  -- NULL when the reference could not be resolved in-repo
  kind TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_nodes_qualified ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_edges_src_kind ON edges(src, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst_kind ON edges(dst, kind);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
"""


class Store:
    """Thin wrapper around the graph SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()

    # -- context manager ------------------------------------------------
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def commit(self) -> None:
        self._conn.commit()

    # -- writes -----------------------------------------------------------
    def add_node(self, kind: str, name: str, qualified_name: str, file: str,
                 line_start: int, line_end: int, language: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO nodes(kind,name,qualified_name,file,line_start,line_end,language)"
            " VALUES (?,?,?,?,?,?,?)",
            (kind, name, qualified_name, file, line_start, line_end, language),
        )
        return cur.lastrowid

    def add_edge(self, src: int, dst: int | None, kind: str, data: dict | None = None) -> None:
        self._conn.execute(
            "INSERT INTO edges(src,dst,kind,data) VALUES (?,?,?,?)",
            (src, dst, kind, json.dumps(data or {}, ensure_ascii=False)),
        )

    def clear(self) -> None:
        """Delete all graph content (used by full re-index)."""
        self._conn.execute("DELETE FROM edges")
        self._conn.execute("DELETE FROM nodes")
        self._conn.commit()

    # -- reads ------------------------------------------------------------
    def get_node(self, node_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return dict(row) if row else None

    def find_symbol(self, name: str) -> list[dict]:
        """Find symbols by qualified name, falling back to short-name match.

        Exact qualified-name match first; otherwise any symbol whose
        qualified name ends with ".<name>" or whose short name equals <name>.
        """
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE qualified_name=? AND kind != 'file'", (name,)
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE kind != 'file' AND (name=? OR qualified_name LIKE ?)",
            (name, f"%.{name}"),
        ).fetchall()
        return [dict(r) for r in rows]

    def out_edges(self, node_id: int, kind: str | None = None) -> list[dict]:
        q = ("SELECT e.src, e.dst, e.kind, e.data, n.qualified_name AS dst_qualified,"
             " n.file AS dst_file FROM edges e LEFT JOIN nodes n ON n.id = e.dst"
             " WHERE e.src=?")
        params: list = [node_id]
        if kind:
            q += " AND e.kind=?"
            params.append(kind)
        return [self._edge_dict(r) for r in self._conn.execute(q, params).fetchall()]

    def in_edges(self, node_id: int, kind: str | None = None) -> list[dict]:
        q = ("SELECT e.src, e.dst, e.kind, e.data, n.qualified_name AS src_qualified,"
             " n.file AS src_file FROM edges e JOIN nodes n ON n.id = e.src"
             " WHERE e.dst=?")
        params: list = [node_id]
        if kind:
            q += " AND e.kind=?"
            params.append(kind)
        return [self._edge_dict(r) for r in self._conn.execute(q, params).fetchall()]

    def nodes_in_file(self, file: str) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM nodes WHERE file=? ORDER BY line_start", (file,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        n = self._conn.execute("SELECT kind, COUNT(*) c FROM nodes GROUP BY kind").fetchall()
        e = self._conn.execute("SELECT kind, COUNT(*) c FROM edges GROUP BY kind").fetchall()
        return {"nodes": {r[0]: r[1] for r in n}, "edges": {r[0]: r[1] for r in e}}

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _edge_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["data"] = json.loads(d["data"]) if d["data"] else {}
        return d


@contextmanager
def open_store(path: str | Path):
    """Context-managed Store: ``with open_store(p) as s: ...``."""
    store = Store(path)
    try:
        yield store
    finally:
        store.close()
