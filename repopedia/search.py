"""Hybrid lexical + graph search. No vectors in MVP — deliberate.

Why: code identifiers are not natural language. ``"where is retry
handled"`` is answered better by matching the symbol ``retry`` /
``handle_retry`` and then *walking the graph* (who calls it, what file
it's in) than by embedding the query into a vector space trained on
prose. Structure beats embeddings for "where" questions; vectors are a
stretch goal, not the foundation.
"""

from __future__ import annotations

import math
import re

from .store import Store

_EDGE_EXPAND_KINDS = ("calls", "inherits", "imports")


def tokenize(text: str) -> list[str]:
    """Split identifiers into lowercase tokens.

    Handles ``snake_case``, ``kebab-case``, ``dotted.paths`` and
    ``camelCase``/``PascalCase`` (including acronym runs like HTMLParser).
    """
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    return [p.lower() for p in re.split(r"[^a-zA-Z0-9]+", text) if p]


class BM25:
    """~40 lines, zero dependencies. k1/b are the standard defaults."""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.docs = docs
        self.n = len(docs)
        self.avgdl = sum(len(d) for d in docs) / self.n if self.n else 0.0
        self.df: dict[str, int] = {}
        for doc in docs:
            for tok in set(doc):
                self.df[tok] = self.df.get(tok, 0) + 1

    def scores(self, query: list[str]) -> list[float]:
        out = [0.0] * self.n
        for tok in query:
            df = self.df.get(tok)
            if not df:
                continue
            idf = math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))
            for i, doc in enumerate(self.docs):
                tf = doc.count(tok)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * len(doc) / self.avgdl)
                out[i] += idf * tf * (self.k1 + 1) / denom
        return out


def _doc_tokens(node: dict) -> list[str]:
    return tokenize(node["qualified_name"]) + tokenize(node["file"]) + [node["kind"]]


def _neighbors(store: Store, node_id: int) -> list[dict]:
    """Resolved one-hop neighbors along structural edges."""
    out = []
    seen = set()
    for edge in store.out_edges(node_id):
        if edge["kind"] not in _EDGE_EXPAND_KINDS or edge["dst"] is None:
            continue
        if edge["dst"] not in seen:
            seen.add(edge["dst"])
            node = store.get_node(edge["dst"])
            if node:
                out.append(node)
    for edge in store.in_edges(node_id):
        if edge["kind"] not in _EDGE_EXPAND_KINDS:
            continue
        if edge["src"] not in seen:
            seen.add(edge["src"])
            node = store.get_node(edge["src"])
            if node:
                out.append(node)
    return out


def hybrid_search(store: Store, query: str, top_k: int = 10) -> list[dict]:
    """BM25 over symbols, then one graph hop to pull in related symbols.

    Returns ranked ``[{qualified_name, kind, name, file, line_start,
    score, via}]`` where ``via`` is ``"lexical"`` or ``"graph"``.
    Graph hits inherit half their seed's score.
    """
    nodes = store.all_nodes()
    if not nodes:
        return []
    docs = [_doc_tokens(n) for n in nodes]
    scores = BM25(docs).scores(tokenize(query))
    ranked = sorted(zip(nodes, scores), key=lambda pair: -pair[1])
    seeds = [(n, s) for n, s in ranked if s > 0][: max(top_k * 2, 5)]

    merged: dict[int, tuple[dict, float, str]] = {}
    for node, score in seeds:
        merged[node["id"]] = (node, round(score, 3), "lexical")
    for node, score in seeds:
        for nb in _neighbors(store, node["id"]):
            if nb["id"] not in merged:
                merged[nb["id"]] = (nb, round(score * 0.5, 3), "graph")

    final = sorted(merged.values(), key=lambda item: -item[1])[:top_k]
    return [
        {
            "qualified_name": node["qualified_name"],
            "kind": node["kind"],
            "name": node["name"],
            "file": node["file"],
            "line_start": node["line_start"],
            "score": score,
            "via": via,
        }
        for node, score, via in final
    ]
