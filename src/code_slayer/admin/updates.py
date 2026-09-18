"""Fail-closed update check and optional fast-forward apply."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from code_slayer.admin.process import ProcessError, run_fixed

EXPECTED_REMOTES = frozenset({
    "https://github.com/lillakaninen23-sys/code-slayer.git",
    "git@github.com:lillakaninen23-sys/code-slayer.git",
})


@dataclass(frozen=True)
class UpdateCheck:
    status: str
    checkout: str
    current_commit: str | None
    origin_commit: str | None
    branch: str | None
    dirty: bool
    divergent: bool
    remote: str | None
    detail: str


def _git(checkout: Path, *args: str, runner=None) -> str:
    run = runner or run_fixed
    result = run(("git", "-C", str(checkout), *args), timeout=60.0)
    if result.returncode != 0:
        raise ProcessError("git_failed", result.stderr.strip() or args[0])
    return result.stdout.strip()


def check_for_update(checkout: Path, *, runner=None, fetch: bool = True) -> UpdateCheck:
    run = runner or run_fixed
    root = checkout.resolve()
    if not (root / ".git").exists() and not (root / ".git").is_file():
        return UpdateCheck(
            status="UNVERIFIED", checkout=str(root), current_commit=None,
            origin_commit=None, branch=None, dirty=True, divergent=True,
            remote=None, detail="not_a_git_checkout",
        )
    remote = _git(root, "remote", "get-url", "origin", runner=run)
    if remote not in EXPECTED_REMOTES:
        return UpdateCheck(
            status="MISMATCH", checkout=str(root), current_commit=None,
            origin_commit=None, branch=None, dirty=False, divergent=False,
            remote=remote, detail="unexpected_origin_remote",
        )
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD", runner=run)
    current = _git(root, "rev-parse", "HEAD", runner=run)
    porcelain = _git(root, "status", "--porcelain", runner=run)
    dirty = bool(porcelain)
    if fetch:
        _git(root, "fetch", "origin", runner=run)
    upstream = f"origin/{branch}" if branch != "HEAD" else "origin/HEAD"
    try:
        origin = _git(root, "rev-parse", upstream, runner=run)
    except ProcessError:
        return UpdateCheck(
            status="UNVERIFIED", checkout=str(root), current_commit=current,
            origin_commit=None, branch=branch, dirty=dirty, divergent=True,
            remote=remote, detail="origin_ref_missing",
        )
    ancestor = run(
        ("git", "-C", str(root), "merge-base", "--is-ancestor", current, origin),
    )
    fast_forward = ancestor.returncode == 0
    behind = current != origin
    divergent = behind and not fast_forward
    if dirty:
        status = "MISMATCH"
        detail = "dirty_working_tree"
    elif divergent:
        status = "MISMATCH"
        detail = "divergent_history"
    elif not behind:
        status = "VERIFIED"
        detail = "up_to_date"
    else:
        status = "VERIFIED"
        detail = "update_available"
    return UpdateCheck(
        status=status, checkout=str(root), current_commit=current,
        origin_commit=origin, branch=branch, dirty=dirty, divergent=divergent,
        remote=remote, detail=detail,
    )


def apply_update(checkout: Path, *, runner=None) -> UpdateCheck:
    """Fast-forward only. Refuses dirty or divergent checkouts.

    Does not treat git merge success as a completed deployment — the
    caller must restart the service and re-read the running commit.
    """
    run = runner or run_fixed
    pre = check_for_update(checkout, runner=run, fetch=True)
    if pre.dirty:
        raise ProcessError("update_refused_dirty")
    if pre.divergent:
        raise ProcessError("update_refused_divergent")
    if pre.status == "MISMATCH" and pre.detail == "unexpected_origin_remote":
        raise ProcessError("update_refused_unexpected_remote")
    if pre.detail == "up_to_date":
        return pre
    if pre.origin_commit is None or pre.current_commit is None:
        raise ProcessError("update_refused_unverified")
    merge = run(
        ("git", "-C", str(checkout), "merge", "--ff-only", pre.origin_commit),
        timeout=60.0,
    )
    if merge.returncode != 0:
        raise ProcessError("update_ff_only_failed", merge.stderr.strip())
    return check_for_update(checkout, runner=run, fetch=False)
