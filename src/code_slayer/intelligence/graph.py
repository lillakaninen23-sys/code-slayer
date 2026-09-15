"""Deterministic internal dependency/relation graph (Phase 8.1 §6).

Every edge here is resolved from an actual AST import statement (never a
filename-similarity guess) against the *actual set of files already in
this snapshot's own inventory* — an import of a package this repository
does not itself contain (e.g. `flask`, `pytest`) resolves to nothing and
is silently not an edge; this module has no notion of, and never
fetches, third-party package metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from code_slayer.intelligence.models import GraphEdge
from code_slayer.intelligence.symbols import ImportStatement

# A "src layout" (`src/<package>/...`) is common enough, and used by
# this very repository, to warrant one deliberate special case: every
# module is indexed both under its full repository-relative dotted path
# and, if it starts with `src.`, under the path with that one leading
# component stripped -- exactly what an absolute `import <package>...`
# statement actually names once installed/run from a `src/` layout.
_STRIPPABLE_PREFIXES = ("src.",)


@dataclass(frozen=True)
class _ModuleIndex:
    by_dotted: dict[str, str]  # dotted module/package name -> file path
    package_dirs: set[str]  # repository-relative directory paths that are Python packages


def _dotted(path: str) -> str:
    stem = path[:-3] if path.endswith(".py") else path
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    return stem.replace("/", ".")


def build_module_index(python_paths: set[str]) -> _ModuleIndex:
    by_dotted: dict[str, str] = {}
    package_dirs: set[str] = set()
    for path in sorted(python_paths):
        dotted = _dotted(path)
        candidates = {dotted}
        for prefix in _STRIPPABLE_PREFIXES:
            if dotted.startswith(prefix):
                candidates.add(dotted[len(prefix):])
        for candidate in candidates:
            by_dotted.setdefault(candidate, path)
        if path.endswith("/__init__.py"):
            package_dirs.add(str(PurePosixPath(path).parent))
    return _ModuleIndex(by_dotted, package_dirs)


def _dotted_from_parts(base: tuple[str, ...], module: str | None, name: str) -> str:
    segments = [*base]
    if module:
        segments.extend(module.split("."))
    segments.append(name)
    return ".".join(segments)


def build_edges(
    imports_by_path: dict[str, tuple[ImportStatement, ...]], python_paths: set[str],
) -> tuple[GraphEdge, ...]:
    index = build_module_index(python_paths)
    edges: list[GraphEdge] = []
    for importer, statements in sorted(imports_by_path.items()):
        for statement in statements:
            for target in _targets_for(importer, statement, index):
                if target != importer:
                    edges.append(GraphEdge(
                        importer, target, "imports", f"ast_import:{_describe(statement)}",
                    ))
    edges.extend(_test_naming_edges(python_paths))
    return tuple(sorted(set(edges), key=lambda e: (e.source, e.target, e.relation)))


def _describe(statement: ImportStatement) -> str:
    prefix = "." * statement.level
    if statement.names:
        return f"from {prefix}{statement.module or ''} import {','.join(statement.names)}"
    return f"import {statement.module}"


def _targets_for(
    importer: str, statement: ImportStatement, index: _ModuleIndex,
) -> list[str]:
    if statement.level == 0:
        if not statement.module:
            return []
        if statement.names:
            # `from pkg.sub import name` -- try the submodule `pkg.sub.name`
            # first (name is itself a module), then fall back to the
            # package `pkg.sub` (name is an attribute/class/function).
            results = []
            for name in statement.names:
                target = index.by_dotted.get(f"{statement.module}.{name}")
                results.append(target if target else index.by_dotted.get(statement.module))
            return [t for t in results if t]
        target = index.by_dotted.get(statement.module)
        return [target] if target else []
    # Relative import: resolve each name (or the bare module) against the
    # importer's own package directory, `level` steps up.
    parts = PurePosixPath(importer).parts[:-1]
    up = statement.level - 1
    if up > len(parts):
        return []
    base = parts[: len(parts) - up] if up else parts
    if statement.names:
        results = []
        for name in statement.names:
            dotted = _dotted_from_parts(base, statement.module, name)
            target = index.by_dotted.get(dotted)
            if not target and statement.module:
                target = index.by_dotted.get(_dotted_from_parts(base, None, statement.module))
            results.append(target)
        return [t for t in results if t]
    if statement.module:
        dotted = _dotted_from_parts(base, None, statement.module)
        target = index.by_dotted.get(dotted)
        return [target] if target else []
    return []


def _test_naming_edges(python_paths: set[str]) -> list[GraphEdge]:
    edges = []
    sources_by_stem: dict[str, list[str]] = {}
    for path in python_paths:
        stem = PurePosixPath(path).stem
        if not (stem.startswith("test_") or stem.endswith("_test")) and "tests" not in path:
            sources_by_stem.setdefault(stem, []).append(path)
    for path in python_paths:
        stem = PurePosixPath(path).stem
        if stem.startswith("test_"):
            candidate = stem[len("test_"):]
        elif stem.endswith("_test"):
            candidate = stem[: -len("_test")]
        else:
            continue
        for source in sources_by_stem.get(candidate, ()):
            edges.append(GraphEdge(path, source, "tests_by_name", f"name_convention:{candidate}"))
    return edges
