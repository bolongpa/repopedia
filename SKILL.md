---
name: repopedia
description: Use when working with a codebase's structure — finding who calls a function, what breaks if code changes (blast radius), looking up symbols, or generating code documentation. For Python/TypeScript repos indexed into a repopedia knowledge graph.
license: MIT
metadata:
  version: "0.2.0"
  author: Bolong Pan
  homepage: https://github.com/bolongpa/repopedia
---

# repopedia

repopedia builds a **code knowledge graph** from a source repo (tree-sitter AST → SQLite): symbols, calls, imports, inheritance. Query it structurally instead of guessing from text search. Every answer cites `file:line`. MIT-licensed, local-first, no Docker, no server.

## When to use repopedia vs alternatives

| Question | Best tool |
|---|---|
| "Who calls `Engine.run`?" / "What breaks if I change this?" | **repopedia** (`get_callers`, `blast_radius`) — needs the call graph, text search can't do this |
| "Where is the retry logic?" (keyword-ish) | **repopedia** (`search_codebase`: BM25 + one graph hop) or ripgrep for exact strings |
| "What does this function do?" (semantics) | vector RAG / LLM reading the file — repopedia gives structure, not meaning |
| Multi-hop: "who mentors the engineer leading Project Phoenix?" | **repopedia** — joins across files via graph traversal |

Rule of thumb: **structure questions → repopedia; meaning questions → LLM; exact strings → ripgrep.**

## Quickstart

```bash
pip install repopedia
repopedia index /path/to/repo        # builds .repopedia/graph.db
repopedia blast-radius pkg.mod.func  # what breaks if this changes?
```

## MCP tools (via `repopedia mcp --repo /path/to/repo`, stdio)

| Tool | One-line |
|---|---|
| `find_symbol` | symbols by qualified/short name, each citing `file:line` |
| `get_callers` / `get_callees` | one `calls` hop each way (unresolved sites marked, not hidden) |
| `blast_radius` | transitive reverse calls + importing files; answers "what breaks if I change this?" |
| `search_codebase` | BM25 + one graph hop, ranked, `via: lexical\|graph` |
| `get_file_symbols` | everything a file defines |
| `ask_codebase` | retrieval + grounded synthesis; without an LLM configured it returns the evidence block and says so |

`ask_codebase` synthesizes prose only when `REPOPEDIA_LLM_BASE_URL` / `REPOPEDIA_LLM_API_KEY` / `REPOPEDIA_LLM_MODEL` are set (any OpenAI-compatible endpoint). The model is instructed to cite only symbols from the provided evidence.

## CLI

```bash
repopedia index <repo> [--db PATH] [--language py|ts|all]
repopedia query callers|callees|inherits|file <target>
repopedia blast-radius <qualified.name> [--depth 3] [--json]
repopedia search "retry handler" [--top-k 10]
repopedia update <repo>        # git-diff incremental reindex
repopedia wiki <repo> --out docs/  # markdown wiki generated FROM the graph
repopedia mcp --repo <repo>    # MCP server (stdio)
```

## Honest limitations

- **Python + TypeScript only** (`.py`, `.ts`, `.tsx`). More languages are on the roadmap.
- **No embeddings.** `search_codebase` is BM25 + graph traversal, deliberately — code identifiers aren't natural language, and structure beats vectors for "where" questions.
- **Heuristic resolution.** Calls resolve by short name when unambiguous; ambiguous/unknown references are kept as `dst=NULL` with the raw text preserved — shown, never hidden.
- **LLM is optional everywhere.** Without one, you get the graph, the tools, and deterministic structural wikis — no faked synthesis.
- **Stale graphs warn loudly.** `repopedia wiki` compares the graph's recorded commit against git HEAD and embeds the warning in the output.
