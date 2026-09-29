"""Shared data structures and helpers for the tree-sitter extractors.

Each language extractor parses one source file and returns a ``FileFacts``
object: the symbols defined in the file plus the raw (unresolved) call
sites, import statements, and inheritance clauses found in it. Resolution
against the whole repo (turning raw names into graph edges) happens in
``repopedia.index``, which sees every file.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Symbol kinds stored in the ``nodes`` table.
KIND_FILE = "file"
KIND_CLASS = "class"
KIND_FUNCTION = "function"
KIND_METHOD = "method"

# Edge kinds stored in the ``edges`` table.
EDGE_DEFINES = "defines"  # file -> symbol it defines
EDGE_IMPORTS = "imports"  # file -> file it imports (dst NULL when external/unresolvable)
EDGE_CALLS = "calls"  # function/method -> function/method/class it calls (dst NULL when unresolved)
EDGE_INHERITS = "inherits"  # class -> base class (dst NULL when base is outside the repo)


@dataclass
class Symbol:
    """A named symbol defined in a file."""

    kind: str  # class | function | method
    name: str  # short name, e.g. "run"
    qualified_name: str  # e.g. "pkg.mod.Service.run"
    line_start: int  # 1-based, inclusive
    line_end: int  # 1-based, inclusive


@dataclass
class CallSite:
    """A call expression found inside a symbol (or at module top level)."""

    enclosing_qualified: str  # qualified name of enclosing symbol, or "" for module level
    raw: str  # dotted name as written, e.g. "helper", "eng.run", "this.log"
    line: int  # 1-based line of the call


@dataclass
class ImportStmt:
    """An import statement."""

    module: str  # module as written, e.g. "pkg.util", "./util", "fs", ".mod" (leading dots = relative, python)
    level: int = 0  # python relative-import level (0 = absolute)
    line: int = 1


@dataclass
class InheritClause:
    """A base class listed in a class definition."""

    class_qualified: str  # qualified name of the subclass
    base: str  # base class name as written, e.g. "Base", "pkg.Base"
    line: int = 1


@dataclass
class FileFacts:
    """Everything extracted from one source file (before repo-wide resolution)."""

    language: str  # "python" | "typescript"
    module: str  # dotted module path, e.g. "pkg.mod"
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    imports: list[ImportStmt] = field(default_factory=list)
    inherits: list[InheritClause] = field(default_factory=list)
    has_error: bool = False


def node_text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def line_start(node) -> int:
    return node.start_point[0] + 1


def line_end(node) -> int:
    """1-based inclusive end line.

    tree-sitter end points are exclusive; when a node ends exactly at the
    start of a line (trailing newline), the previous line is the real end.
    """
    row, col = node.end_point[0], node.end_point[1]
    if col == 0:
        return row  # 0-based row of previous line == its 1-based number
    return row + 1


def child_by_type(node, *types):
    for child in node.children:
        if child.type in types:
            return child
    return None


def iter_named(node):
    """Depth-first iteration over named nodes (skips punctuation)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.is_named:
            yield n
        stack.extend(reversed(n.children))
