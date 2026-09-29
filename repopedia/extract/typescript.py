"""TypeScript extractor: tree-sitter parse -> FileFacts.

Covers: class/function/method declarations (incl. exported and async),
import statements (relative module paths), call expressions (identifier and
member-expression forms, e.g. ``this.log``), ``new`` expressions, and
``extends`` heritage clauses. ``.tsx`` files use the TSX grammar.

Known W1 limits (documented, not silent): arrow functions assigned to
variables and object-literal methods are not extracted as symbols; generic
type arguments are ignored.
"""

from __future__ import annotations

from tree_sitter import Language, Parser

import tree_sitter_typescript

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


class TypeScriptExtractor:
    language_name = "typescript"

    def __init__(self, tsx: bool = False) -> None:
        fn = tree_sitter_typescript.language_tsx() if tsx else tree_sitter_typescript.language_typescript()
        self._parser = Parser(Language(fn))

    # -- public ---------------------------------------------------------
    def extract(self, source: bytes, module: str) -> FileFacts:
        tree = self._parser.parse(source)
        facts = FileFacts(language="typescript", module=module)
        facts.has_error = tree.root_node.has_error
        self._walk_statements(tree.root_node, module, None, facts)
        return facts

    # -- structure ------------------------------------------------------
    def _unwrap(self, node):
        # export_statement -> the real declaration inside
        if node.type == "export_statement":
            inner = child_by_type(node, "class_declaration", "function_declaration")
            return inner if inner is not None else node
        return node

    def _walk_statements(self, node, scope_qual: str, enclosing: str | None, facts: FileFacts) -> None:
        for child in node.children:
            node_ = self._unwrap(child)
            if node_.type == "function_declaration":
                self._register_function(node_, scope_qual, "function", facts)
            elif node_.type == "class_declaration":
                self._register_class(node_, scope_qual, facts)
            elif node_.type == "import_statement":
                self._register_import(node_, facts)
            else:
                self._walk_calls(node_, enclosing or "", scope_qual, facts)

    def _register_function(self, node, scope_qual: str, kind: str, facts: FileFacts) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:  # anonymous default-exported function; skip
            return
        name = node_text(name_node)
        qual = f"{scope_qual}.{name}" if scope_qual else name
        facts.symbols.append(
            Symbol(kind=kind, name=name, qualified_name=qual,
                   line_start=line_start(node), line_end=line_end(node))
        )
        body = node.child_by_field_name("body")
        if body is not None:
            self._walk_calls(body, qual, qual, facts)

    def _register_class(self, node, scope_qual: str, facts: FileFacts) -> None:
        name_node = child_by_type(node, "type_identifier")
        if name_node is None:
            return
        name = node_text(name_node)
        qual = f"{scope_qual}.{name}" if scope_qual else name
        facts.symbols.append(
            Symbol(kind="class", name=name, qualified_name=qual,
                   line_start=line_start(node), line_end=line_end(node))
        )
        # extends clause -> base class
        heritage = child_by_type(node, "class_heritage")
        if heritage is not None:
            for clause in heritage.children:
                if clause.type == "extends_clause":
                    base = child_by_type(clause, "type_identifier", "identifier")
                    if base is not None:
                        facts.inherits.append(
                            InheritClause(class_qualified=qual, base=node_text(base),
                                          line=line_start(clause))
                        )
        body = child_by_type(node, "class_body")
        if body is not None:
            for child in body.children:
                if child.type == "method_definition":
                    self._register_method(child, qual, facts)
                else:
                    self._walk_calls(child, qual, qual, facts)

    def _register_method(self, node, class_qual: str, facts: FileFacts) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = node_text(name_node)
        qual = f"{class_qual}.{name}"
        facts.symbols.append(
            Symbol(kind="method", name=name, qualified_name=qual,
                   line_start=line_start(node), line_end=line_end(node))
        )
        body = node.child_by_field_name("body")
        if body is not None:
            self._walk_calls(body, qual, qual, facts)

    def _walk_calls(self, node, enclosing_qual: str, scope_qual: str, facts: FileFacts) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type in ("identifier", "member_expression"):
                facts.calls.append(
                    CallSite(enclosing_qualified=enclosing_qual,
                             raw=node_text(fn), line=line_start(node))
                )
        elif node.type == "new_expression":
            ctor = node.child_by_field_name("constructor")
            if ctor is not None and ctor.type == "identifier":
                facts.calls.append(
                    CallSite(enclosing_qualified=enclosing_qual,
                             raw=node_text(ctor), line=line_start(node))
                )
        elif node.type == "function_declaration":
            # nested function declaration: its own symbol, own scope
            self._register_function(node, scope_qual, "function", facts)
            return
        elif node.type == "class_declaration":
            self._register_class(node, scope_qual, facts)
            return
        for child in node.children:
            self._walk_calls(child, enclosing_qual, scope_qual, facts)

    # -- imports ----------------------------------------------------------
    def _register_import(self, node, facts: FileFacts) -> None:
        # import ... from '<module>'; the module is the string literal child
        for child in node.children:
            if child.type == "string":
                frag = child_by_type(child, "string_fragment")
                mod = node_text(frag) if frag is not None else node_text(child).strip("'\"")
                facts.imports.append(ImportStmt(module=mod, line=line_start(node)))
                return
