"""Python extractor: tree-sitter parse -> FileFacts.

Covers: module/class/function/method definitions (incl. decorated and
async), import statements (absolute, aliased, relative), call expressions
(identifier and attribute forms), and class bases. Nested functions and
classes get dotted qualified names (``outer.inner``).
"""

from __future__ import annotations

from tree_sitter import Language, Parser

import tree_sitter_python

from .base import (
    CallSite,
    FileFacts,
    ImportStmt,
    InheritClause,
    Symbol,
    child_by_type,
    line_end,
    line_start,
    node_text,
)


class PythonExtractor:
    language_name = "python"

    def __init__(self) -> None:
        self._parser = Parser(Language(tree_sitter_python.language()))

    # -- public ---------------------------------------------------------
    def extract(self, source: bytes, module: str) -> FileFacts:
        tree = self._parser.parse(source)
        facts = FileFacts(language="python", module=module)
        facts.has_error = tree.root_node.has_error
        self._walk_block(tree.root_node, module, None, facts)
        return facts

    # -- structure ------------------------------------------------------
    def _unwrap(self, node):
        # decorated_definition -> the real definition inside
        if node.type == "decorated_definition":
            inner = child_by_type(node, "function_definition", "class_definition")
            return inner if inner is not None else node
        return node

    def _walk_block(self, node, scope_qual: str, enclosing: str | None, facts: FileFacts) -> None:
        """Walk a module/class/function body, registering definitions."""
        for child in node.children:
            node = self._unwrap(child)
            if node.type == "function_definition":
                self._register_function(node, scope_qual, "function", enclosing, facts)
            elif node.type == "class_definition":
                self._register_class(node, scope_qual, facts)
            elif node.type in ("import_statement", "import_from_statement"):
                self._register_import(node, facts)
            else:
                # module-level statements (e.g. inside `if __name__ == ...`):
                # capture calls, attributed to the file itself
                self._walk_calls(node, "", facts)

    def _register_function(self, node, scope_qual: str, kind: str, enclosing: str | None, facts: FileFacts) -> None:
        name_node = node.child_by_field_name("name")
        name = node_text(name_node)
        qual = f"{scope_qual}.{name}" if scope_qual else name
        facts.symbols.append(
            Symbol(kind=kind, name=name, qualified_name=qual,
                   line_start=line_start(node), line_end=line_end(node))
        )
        body = node.child_by_field_name("body")
        if body is not None:
            self._walk_scope(body, qual, facts)

    def _register_class(self, node, scope_qual: str, facts: FileFacts) -> None:
        name_node = node.child_by_field_name("name")
        name = node_text(name_node)
        qual = f"{scope_qual}.{name}" if scope_qual else name
        facts.symbols.append(
            Symbol(kind="class", name=name, qualified_name=qual,
                   line_start=line_start(node), line_end=line_end(node))
        )
        # bases: identifiers / dotted names in the argument list
        args = child_by_type(node, "argument_list")
        if args is not None:
            for base in args.named_children:
                if base.type in ("identifier", "dotted_name"):
                    facts.inherits.append(
                        InheritClause(class_qualified=qual, base=node_text(base),
                                      line=line_start(base))
                    )
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                sub = self._unwrap(child)
                if sub.type == "function_definition":
                    self._register_function(sub, qual, "method", qual, facts)
                elif sub.type == "class_definition":
                    self._register_class(sub, qual, facts)
                elif sub.type in ("import_statement", "import_from_statement"):
                    self._register_import(sub, facts)
                else:
                    self._walk_calls(sub, qual, facts)

    def _walk_scope(self, node, enclosing_qual: str, facts: FileFacts) -> None:
        """Walk a function body: nested defs become their own symbols, everything
        else is scanned for call sites attributed to ``enclosing_qual``."""
        for child in node.children:
            sub = self._unwrap(child)
            if sub.type == "function_definition":
                self._register_function(sub, enclosing_qual, "function", enclosing_qual, facts)
            elif sub.type == "class_definition":
                self._register_class(sub, enclosing_qual, facts)
            else:
                self._walk_calls(sub, enclosing_qual, facts)

    def _walk_calls(self, node, enclosing_qual: str, facts: FileFacts) -> None:
        if node.type == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type in ("identifier", "attribute"):
                facts.calls.append(
                    CallSite(enclosing_qualified=enclosing_qual,
                             raw=node_text(fn), line=line_start(node))
                )
        for child in node.children:
            # don't cross into nested scopes here; they are handled by _walk_scope
            if child.type in ("function_definition", "class_definition", "decorated_definition"):
                continue
            self._walk_calls(child, enclosing_qual, facts)

    # -- imports ----------------------------------------------------------
    def _register_import(self, node, facts: FileFacts) -> None:
        line = line_start(node)
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    facts.imports.append(ImportStmt(module=node_text(child), line=line))
                elif child.type == "aliased_import":
                    mod = child_by_type(child, "dotted_name")
                    if mod is not None:
                        facts.imports.append(ImportStmt(module=node_text(mod), line=line))
        else:  # import_from_statement: "from X import Y" — only X is the module
            level = 0
            mod_name = ""
            for child in node.children:
                if child.type == "import":
                    break  # keyword: everything after is imported names, not the module
                if child.type == "relative_import":
                    prefix = child_by_type(child, "import_prefix")
                    if prefix is not None:
                        level = prefix.text.count(b".")
                    dotted = child_by_type(child, "dotted_name")
                    if dotted is not None:
                        mod_name = node_text(dotted)
                elif child.type == "dotted_name":
                    mod_name = node_text(child)
            facts.imports.append(ImportStmt(module=mod_name, level=level, line=line))
