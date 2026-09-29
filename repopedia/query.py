"""High-level graph queries for repopedia.

Every function takes an open :class:`~repopedia.store.Store` and returns
plain JSON-serializable dicts/lists — Week 3's MCP server calls these
directly, so no custom objects may leak out.

Conventions:
- ``qualified_name`` identifies a symbol (e.g. ``"pkg.mod.Service.run"``).
  Short names are accepted when unambiguous.
- Every result entry carries ``file`` and ``line_start`` so callers can
  cite ``file:line``.
- ``UnknownSymbol`` is raised when a name matches nothing (or is
  ambiguous); the CLI and MCP layers turn it into a user-facing error.
"""

from __future__ import annotations

from .store import Store


class UnknownSymbol(Exception):
    """Raised when a symbol name matches nothing or is ambiguous."""


def _resolve(store: Store, qualified_name: str) -> dict:
    matches = store.find_symbol(qualified_name)
    exact = [m for m in matches if m["qualified_name"] == qualified_name]
    if exact:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise UnknownSymbol(f"no symbol matches {qualified_name!r}")
    raise UnknownSymbol(
        f"{qualified_name!r} is ambiguous: "
        + ", ".join(sorted(m["qualified_name"] for m in matches))
    )


def _view(node: dict) -> dict:
    return {
        "kind": node["kind"],
        "name": node["name"],
        "qualified_name": node["qualified_name"],
        "file": node["file"],
        "line_start": node["line_start"],
        "line_end": node["line_end"],
    }


def callers(store: Store, qualified_name: str) -> list[dict]:
    """Direct callers of a symbol (one reverse ``calls`` hop)."""
    node = _resolve(store, qualified_name)
    out = []
    for edge in store.in_edges(node["id"], "calls"):
        src = store.get_node(edge["src"])
        out.append({**_view(src), "via": "calls"})
    out.sort(key=lambda d: d["qualified_name"])
    return out


def callees(store: Store, qualified_name: str) -> list[dict]:
    """Direct callees of a symbol (one forward ``calls`` hop).

    Unresolved call sites are included with ``resolved: false`` and the
    raw written name, so callers can see what the indexer couldn't match.
    """
    node = _resolve(store, qualified_name)
    out = []
    for edge in store.out_edges(node["id"], "calls"):
        if edge["dst"] is None:
            out.append({
                "qualified_name": None,
                "raw": edge["data"].get("raw"),
                "resolved": False,
                "via": "calls",
            })
        else:
            dst = store.get_node(edge["dst"])
            out.append({**_view(dst), "resolved": True, "via": "calls"})
    out.sort(key=lambda d: d["qualified_name"] or d.get("raw") or "")
    return out


def blast_radius(store: Store, qualified_name: str, depth: int = 3) -> list[dict]:
    """What could break if this symbol changes?

    Transitive closure over reverse ``calls`` edges up to ``depth``,
    plus every file that directly ``imports`` the symbol's file
    (import-driven impact). Each entry carries ``via`` (``"self"``,
    ``"calls(n)"`` or ``"imports"``) and ``depth``. Deduplicated
    (shallowest depth wins), ordered by depth then name.

    Known limitation: unresolved call sites (``dst=NULL``) can't be
    traversed — they're invisible to the closure. A full reindex is the
    fix when new symbols appear.
    """
    start = _resolve(store, qualified_name)
    results = [{**_view(start), "via": "self", "depth": 0}]
    seen: dict[str, int] = {start["qualified_name"]: 0}

    # -- reverse call-graph BFS -------------------------------------
    frontier = [(start["id"], 0)]
    while frontier:
        nid, d = frontier.pop(0)
        if d >= depth:
            continue
        for edge in store.in_edges(nid, "calls"):
            src = store.get_node(edge["src"])
            qn = src["qualified_name"]
            if qn not in seen or seen[qn] > d + 1:
                seen[qn] = d + 1
                results.append({**_view(src), "via": f"calls({d + 1})", "depth": d + 1})
                frontier.append((src["id"], d + 1))

    # -- import-driven impact: files importing the symbol's file ------
    fnode = store.file_node(start["file"])
    if fnode is not None:
        for edge in store.in_edges(fnode["id"], "imports"):
            imp = store.get_node(edge["src"])
            qn = imp["qualified_name"]
            if qn not in seen:
                seen[qn] = 1
                results.append({**_view(imp), "via": "imports", "depth": 1})

    results.sort(key=lambda r: (r["depth"], r["qualified_name"]))
    return results


def inheritance_chain(store: Store, qualified_name: str) -> list[dict]:
    """Base classes of a class, nearest first (BFS over ``inherits``)."""
    start = _resolve(store, qualified_name)
    if start["kind"] != "class":
        raise UnknownSymbol(f"{qualified_name!r} is a {start['kind']}, not a class")
    chain: list[dict] = []
    seen: set[int] = set()
    frontier = [start["id"]]
    while frontier:
        nid = frontier.pop(0)
        for edge in store.out_edges(nid, "inherits"):
            if edge["dst"] is None:
                chain.append({
                    "qualified_name": None,
                    "raw": edge["data"].get("raw"),
                    "resolved": False,
                })
                continue
            if edge["dst"] in seen:
                continue
            seen.add(edge["dst"])
            base = store.get_node(edge["dst"])
            chain.append({**_view(base), "resolved": True})
            frontier.append(edge["dst"])
    return chain


def file_symbols(store: Store, file: str) -> list[dict]:
    """Symbols defined in a file (excludes the file node itself)."""
    return [
        _view(n) for n in store.nodes_in_file(file) if n["kind"] != "file"
    ]
