"""Deterministic, never-executed command discovery (Phase 8.1 §4).

Every `CommandCandidate` names exactly the repository file/section it
came from (`evidence_source`) and a `confidence` reflecting how directly
that evidence names an actual runnable command — a `Makefile` target or
a `package.json` script is `high` (the repository itself declared it
runnable under that exact name); a bare `tox.ini`/`noxfile.py` presence
is `medium` (a real command exists, but which exact invocation depends
on the tool's own environment resolution this module does not attempt to
replicate). Nothing in this module ever imports `subprocess` — discovery
only, never execution, and never a promise that a discovered command is
safe, correct, or currently working.
"""

from __future__ import annotations

import json
import re
import tomllib

from code_slayer.intelligence.models import CommandCandidate

_PURPOSE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("test", ("test",)),
    ("lint", ("lint",)),
    ("format", ("format", "fmt")),
    ("typecheck", ("typecheck", "type-check", "type_check", "mypy", "tsc")),
    ("build", ("build", "compile")),
)


def _purpose_for(name: str) -> str | None:
    lowered = name.lower()
    for purpose, keywords in _PURPOSE_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return purpose
    return None


def discover_commands(paths: set[str], read_text) -> tuple[CommandCandidate, ...]:
    commands: list[CommandCandidate] = []
    commands.extend(_node_scripts(paths, read_text))
    commands.extend(_python_tools(paths, read_text))
    commands.extend(_makefile_targets(paths, read_text))
    if "tox.ini" in paths:
        commands.append(CommandCandidate("tox", "test", "tox.ini", "medium"))
    if "noxfile.py" in paths:
        commands.append(CommandCandidate("nox", "test", "noxfile.py", "medium"))
    return tuple(commands)


def _node_scripts(paths: set[str], read_text) -> list[CommandCandidate]:
    if "package.json" not in paths:
        return []
    text = read_text("package.json")
    if text is None:
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return []
    results = []
    for name in sorted(scripts):
        if not isinstance(name, str):
            continue
        purpose = _purpose_for(name)
        if purpose is not None:
            results.append(CommandCandidate(
                f"npm run {name}", purpose, f"package.json:scripts.{name}", "high",
            ))
    return results


def _python_tools(paths: set[str], read_text) -> list[CommandCandidate]:
    if "pyproject.toml" not in paths:
        return []
    text = read_text("pyproject.toml")
    if text is None:
        return []
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    tool = data.get("tool") if isinstance(data, dict) else None
    tool = tool if isinstance(tool, dict) else {}
    results = []
    if "pytest" in tool:
        results.append(CommandCandidate("pytest", "test", "pyproject.toml:[tool.pytest]", "high"))
    ruff_tool = tool.get("ruff")
    if isinstance(ruff_tool, dict):
        results.append(CommandCandidate(
            "ruff check .", "lint", "pyproject.toml:[tool.ruff]", "high",
        ))
        if "format" in ruff_tool:
            results.append(CommandCandidate(
                "ruff format .", "format", "pyproject.toml:[tool.ruff.format]", "high",
            ))
    if "mypy" in tool:
        results.append(CommandCandidate(
            "mypy .", "typecheck", "pyproject.toml:[tool.mypy]", "high",
        ))
    if "black" in tool:
        results.append(CommandCandidate(
            "black .", "format", "pyproject.toml:[tool.black]", "high",
        ))
    return results


_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.-]+):(?!=)")


def _makefile_targets(paths: set[str], read_text) -> list[CommandCandidate]:
    makefile = next((p for p in ("Makefile", "makefile", "GNUmakefile") if p in paths), None)
    if makefile is None:
        return []
    text = read_text(makefile)
    if text is None:
        return []
    results = []
    for line in text.splitlines():
        match = _MAKE_TARGET.match(line)
        if not match or line.startswith("\t") or line.startswith("#"):
            continue
        target = match.group(1)
        if target.startswith("."):
            continue
        purpose = _purpose_for(target)
        if purpose is not None:
            results.append(CommandCandidate(
                f"make {target}", purpose, f"{makefile}:target:{target}", "high",
            ))
    return results
