"""MCP server exposing the repopedia code knowledge graph to AI agents.

Run: ``repopedia mcp --repo /path/to/repo`` (stdio transport).

Every tool returns plain JSON-serializable dicts/lists and every
symbol cites ``file:line``. The tool logic lives in :class:`RepopediaTools`
(methods take no MCP types), so tests call them directly without a
transport. ``build_server`` wires them into a FastMCP server.

``ask_codebase`` is the only tool that touches an LLM, and only when one
is configured (``REPOPEDIA_LLM_BASE_URL`` / ``REPOPEDIA_LLM_API_KEY`` /
``REPOPEDIA_LLM_MODEL``). Without it, the tool returns the structured
evidence block and says so honestly — no invented synthesis.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import query as Q
from .llm import llm_from_env
from .search import hybrid_search
from .store import open_store

_ASK_SYSTEM = """\
You are a codebase analyst. Answer the question using ONLY the evidence below.

Rules:
- Every factual claim about the code MUST cite the evidence as file:line \
(e.g. `pkg/core.py:12`). Cite the location given in the evidence.
- Only cite symbols and locations that appear in the evidence. \
Never invent file paths, symbol names, or line numbers.
- If the evidence does not contain enough information to answer, say so \
explicitly instead of guessing.
- Keep the answer short and structured.
"""


class RepopediaTools:
    """Graph tools bound to one database. All methods are JSON-safe."""

    def __init__(self, db_path: str | Path, llm=None) -> None:
        self.db_path = Path(db_path)
        # None -> resolve from env at call time is overkill; resolve once.
        self.llm = llm if llm is not None else llm_from_env()

    def _open(self):
        return open_store(self.db_path)

    @staticmethod
    def _err(exc: Exception) -> dict:
        return {"error": str(exc)}

    def find_symbol(self, name: str) -> dict:
        """Find symbols by qualified or short name. Every hit cites file:line."""
        with self._open() as s:
            hits = s.find_symbol(name)
        return {
            "query": name,
            "symbols": [
                {
                    "kind": h["kind"],
                    "name": h["name"],
                    "qualified_name": h["qualified_name"],
                    "location": f"{h['file']}:{h['line_start']}",
                }
                for h in sorted(hits, key=lambda h: h["qualified_name"])
            ],
        }

    def get_callers(self, name: str) -> dict:
        """Direct callers of a symbol (one reverse `calls` hop)."""
        try:
            with self._open() as s:
                res = Q.callers(s, name)
        except Q.UnknownSymbol as exc:
            return self._err(exc)
        return {"symbol": name, "callers": res}

    def get_callees(self, name: str) -> dict:
        """Direct callees of a symbol. Unresolved sites are marked resolved:false."""
        try:
            with self._open() as s:
                res = Q.callees(s, name)
        except Q.UnknownSymbol as exc:
            return self._err(exc)
        return {"symbol": name, "callees": res}

    def blast_radius(self, name: str, depth: int = 3) -> dict:
        """What could break if this symbol changes: transitive reverse calls
        up to `depth`, plus files importing the symbol's file."""
        try:
            with self._open() as s:
                res = Q.blast_radius(s, name, depth=depth)
        except Q.UnknownSymbol as exc:
            return self._err(exc)
        return {"symbol": name, "depth": depth, "impacted": res}

    def search_codebase(self, query: str, top_k: int = 10) -> dict:
        """Hybrid BM25 + one-hop graph search over symbols. No vectors."""
        with self._open() as s:
            res = hybrid_search(s, query, top_k=top_k)
        return {"query": query, "results": res}

    def get_file_symbols(self, file: str) -> dict:
        """Symbols defined in a file (repo-relative path)."""
        with self._open() as s:
            res = Q.file_symbols(s, file)
        return {"file": file, "symbols": res}

    def ask_codebase(self, question: str, top_k: int = 8) -> dict:
        """Answer a question about the codebase.

        Retrieval is hybrid_search (BM25 + graph). When an LLM is
        configured, a short synthesized answer is composed where every
        claim cites file:line from the evidence — the model is instructed
        to cite only what it was given. Without an LLM, the structured
        evidence block is returned alone, honestly labeled.
        """
        with self._open() as s:
            hits = hybrid_search(s, question, top_k=top_k)
        evidence = [
            {
                "qualified_name": h["qualified_name"],
                "kind": h["kind"],
                "location": f"{h['file']}:{h['line_start']}",
                "via": h["via"],
                "score": h["score"],
            }
            for h in hits
        ]
        if self.llm is None:
            return {
                "question": question,
                "answer": None,
                "note": ("no LLM configured (set REPOPEDIA_LLM_BASE_URL, "
                         "REPOPEDIA_LLM_API_KEY, REPOPEDIA_LLM_MODEL); "
                         "returning structured evidence only"),
                "evidence": evidence,
            }
        user = (
            f"Question: {question}\n\nEvidence (JSON):\n"
            + "\n".join(
                f"- {e['qualified_name']} [{e['kind']}] at {e['location']} "
                f"(via={e['via']}, score={e['score']})"
                for e in evidence
            )
        )
        answer = self.llm.complete(_ASK_SYSTEM, user)
        return {
            "question": question,
            "answer": answer,
            "model": self.llm.model,
            "evidence": evidence,
        }


_TOOL_NAMES = [
    "find_symbol",
    "get_callers",
    "get_callees",
    "blast_radius",
    "search_codebase",
    "get_file_symbols",
    "ask_codebase",
]


def build_server(db_path: str | Path, llm=None) -> FastMCP:
    """Create the FastMCP server with repopedia tools bound to ``db_path``."""
    tools = RepopediaTools(db_path, llm=llm)
    server = FastMCP("repopedia")
    for name in _TOOL_NAMES:
        server.tool()(getattr(tools, name))
    return server


def serve(db_path: str | Path, llm=None) -> None:
    """Run the MCP server over stdio (blocking)."""
    build_server(db_path, llm=llm).run()
