"""Deterministic, parser-based source-structure extraction (Phase 8.1
§5 "Symbol / module intelligence").

Built as a small per-extension registry so a later language gets its own
narrow extractor function, never a change to this module's own
contract or to any caller. Phase 8.1 ships exactly one: Python, via the
standard library's own `ast` module — never a hand-rolled parser, never
a "universal compiler frontend."

A malformed/unparsable file must never invalidate the rest of a
snapshot: `extract(path, text)` raises exactly `SymbolExtractionError`
for anything that goes wrong with parsing *this one file*, so
`intelligence.builder.build_snapshot()` can catch it, record a
`SymbolError`, and continue — the same "fail locally, never destroy
the whole batch" posture already used throughout this codebase.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from code_slayer.intelligence.models import SymbolRecord


class SymbolExtractionError(RuntimeError):
    """This one file could not be parsed; nothing else is affected."""


@dataclass(frozen=True)
class ImportStatement:
    """One `import`/`from ... import ...` statement, exactly as written
    — resolution against the repository's own file inventory is
    `intelligence.graph`'s job, not this module's."""

    level: int  # 0 for an absolute `import`; >=1 for `from .[.[.]] import`
    module: str | None  # dotted module path, or None for a bare `from . import x`
    names: tuple[str, ...]  # imported names for a `from` statement; () for plain `import a.b.c`


@dataclass(frozen=True)
class ExtractionResult:
    symbols: tuple[SymbolRecord, ...]
    imports: tuple[ImportStatement, ...]


_ExtractorFn = Callable[[str, str], ExtractionResult]
_EXTRACTORS: dict[str, _ExtractorFn] = {}


def _register(*extensions: str) -> Callable[[_ExtractorFn], _ExtractorFn]:
    def decorator(fn: _ExtractorFn) -> _ExtractorFn:
        for ext in extensions:
            _EXTRACTORS[ext] = fn
        return fn
    return decorator


def supports(path: str) -> bool:
    return PurePosixPath(path).suffix in _EXTRACTORS


def extract(path: str, text: str) -> ExtractionResult | None:
    """`None` when no extractor is registered for this file's extension
    (not an error — most files in a repository are not source code this
    package understands yet). Raises `SymbolExtractionError` for a file
    of a supported extension that could not actually be parsed."""
    extractor = _EXTRACTORS.get(PurePosixPath(path).suffix)
    if extractor is None:
        return None
    try:
        return extractor(path, text)
    except SymbolExtractionError:
        raise
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise SymbolExtractionError(str(exc)) from exc


@_register(".py")
def _extract_python(path: str, text: str) -> ExtractionResult:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        raise SymbolExtractionError(str(exc)) from exc

    module_name = path[:-3].replace("/", ".") if path.endswith(".py") else path
    symbols: list[SymbolRecord] = [
        SymbolRecord("module", module_name, module_name, path, 1, len(text.splitlines()) or 1),
    ]
    imports: list[ImportStatement] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            self._record("class", node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self._record("method" if self._stack else "function", node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            self._record("method" if self._stack else "async_function", node)

        def _record(self, kind: str, node: ast.AST) -> None:
            qualname = ".".join([*self._stack, node.name])
            symbols.append(SymbolRecord(
                kind, node.name, f"{module_name}.{qualname}", path,
                node.lineno, getattr(node, "end_lineno", None),
            ))
            self._stack.append(node.name)
            self.generic_visit(node)
            self._stack.pop()

        def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
            for alias in node.names:
                imports.append(ImportStatement(0, alias.name, ()))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
            imports.append(ImportStatement(
                node.level, node.module, tuple(alias.name for alias in node.names),
            ))

    _Visitor().visit(tree)
    return ExtractionResult(tuple(symbols), tuple(imports))
