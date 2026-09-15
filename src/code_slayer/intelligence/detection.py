"""Deterministic project/language detection (Phase 8.1 §3).

Every `ProjectEvidence` this module returns is grounded in at least one
concrete file that was actually found in the current inventory —
`evidence_paths` always names it. A file *extension* existing somewhere
in the repository is never, by itself, evidence of a framework or
project kind; only these specific, well-known marker files are.
"""

from __future__ import annotations

import tomllib

from code_slayer.intelligence.models import ProjectEvidence

_MARKERS: tuple[tuple[str, str], ...] = (
    ("python", "pyproject.toml"),
    ("python", "setup.py"),
    ("python", "setup.cfg"),
    ("node", "package.json"),
    ("node", "package-lock.json"),
    ("node", "pnpm-lock.yaml"),
    ("node", "yarn.lock"),
    ("rust", "Cargo.toml"),
    ("go", "go.mod"),
    ("java", "pom.xml"),
    ("java", "build.gradle"),
    ("java", "build.gradle.kts"),
)


def detect_projects(paths: set[str], read_text) -> tuple[ProjectEvidence, ...]:
    """`paths` is the set of every inventoried file's repository-relative
    path (regardless of size/classification — a marker file is evidence
    merely by existing). `read_text(path) -> str | None` reads a small
    marker file's content when a detector needs it (e.g. a project name
    from `pyproject.toml`); `None` means unreadable/oversized/binary, in
    which case the detector still records the bare evidence path with
    no extracted facts, rather than failing the whole snapshot."""
    found: dict[str, list[str]] = {}
    for kind, name in _MARKERS:
        if name in paths:
            found.setdefault(kind, []).append(name)
    for suffix in (".sln", ".csproj"):
        matches = sorted(p for p in paths if p.endswith(suffix))
        if matches:
            found.setdefault("dotnet", []).extend(matches)

    projects = []
    for kind in sorted(found):
        evidence_paths = tuple(sorted(set(found[kind])))
        facts = _facts(kind, evidence_paths, read_text)
        projects.append(ProjectEvidence(kind, evidence_paths, facts))
    return tuple(projects)


def _facts(kind: str, evidence_paths: tuple[str, ...], read_text) -> dict:
    if kind == "python" and "pyproject.toml" in evidence_paths:
        text = read_text("pyproject.toml")
        if text is not None:
            try:
                data = tomllib.loads(text)
            except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                return {}
            project = data.get("project", {})
            facts = {}
            if isinstance(project.get("name"), str):
                facts["name"] = project["name"]
            if isinstance(project.get("requires-python"), str):
                facts["requires_python"] = project["requires-python"]
            if "tool" in data and isinstance(data["tool"], dict):
                facts["configured_tools"] = sorted(data["tool"])
            return facts
    if kind == "node" and "package.json" in evidence_paths:
        text = read_text("package.json")
        if text is not None:
            import json

            try:
                data = json.loads(text)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
            facts = {}
            if isinstance(data, dict):
                if isinstance(data.get("name"), str):
                    facts["name"] = data["name"]
                if isinstance(data.get("scripts"), dict):
                    facts["script_names"] = sorted(data["scripts"])
            return facts
    return {}
