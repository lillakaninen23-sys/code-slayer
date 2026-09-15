"""Path safety and deterministic default exclusions (Phase 8.1 §2/§11).

Every candidate path this package ever reads comes from `repo.inspection.
inspect_repository()` — already Git-authoritative, already rejecting
`.`/`..`/non-UTF-8 components (`repo.inspection.safe_path()`). This
module adds two more things Git itself does not decide: (a) our own
default deny-list of dependency/cache directory *names* (defense in
depth for a repository whose `.gitignore` does not already exclude
them — most do, since these paths never appear in `inspect_repository()`
output at all when it is respected), and (b) the actual, final safety
check before any byte is ever read: the candidate path must resolve
(symlinks included) to somewhere still inside the repository root.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

# Directory *names*, matched as a whole path component anywhere in a
# candidate path — never a substring match. Every name here is an
# unambiguous, universally-recognized dependency/cache/build-output
# convention; anything more repo-specific is left to the repository's
# own `.gitignore` (already respected upstream by `inspect_repository()`
# excluding `ignored_paths` from the candidate set).
EXCLUDED_DIR_NAMES = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", "target", ".next", ".nuxt", "coverage",
    ".idea", ".vscode",
})


def has_excluded_component(relative_path: str) -> bool:
    parts = PurePosixPath(relative_path).parts
    return any(part in EXCLUDED_DIR_NAMES or part.endswith(".egg-info") for part in parts)


def resolve_within_repo(repo_root: Path, relative_path: str) -> Path | None:
    """The real, symlink-resolved absolute path for `relative_path`, or
    `None` if it does not exist or escapes `repo_root` once resolved —
    fail closed, never a best-effort guess, matching this codebase's
    established path-confinement posture (`tools.file_tools.
    relative_path`)."""
    candidate = (repo_root / relative_path)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return resolved
