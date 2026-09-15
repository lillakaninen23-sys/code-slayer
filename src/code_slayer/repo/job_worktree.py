"""Isolated, disposable job worktrees (Phase 7.5c — `docs/ROADMAP.md`'s
Local Worker Runtime stage, step 4 "Isolated mutating jobs";
`docs/CODE_SLAYER_VISION.md` §37 "Isolated job execution").

## What this module is

The foundation a mutating job needs before any real worker may ever earn
or exercise mutating trust: a way to create a Code-Slayer-owned,
disposable Git linked worktree — pinned to an explicit, frozen base
revision, never the user's primary working tree — that a task can then
run entirely inside, using the *unmodified* Phase 1-6 stack (`repo.
identity`, `tools.executor.ToolExecutor`, `policy.engine.PolicyEngine`,
`lease.manager.LeaseManager`, `repo.checkpoint.CheckpointManager`) exactly
as it already runs against any other repository. This module invents no
new identity system, no new policy, and no new lease mechanism — it only
creates the isolated *place* those already-real components then operate
against.

## Critical safety boundary — read this before relying on isolation

**A Git linked worktree is a repository-state isolation boundary, not an
OS-level sandbox.** It reliably keeps one worktree's `HEAD`/index/working
tree separate from another's — that is the guarantee this module and
`repo.identity` build on. It does **not** by itself prevent a process
running with the job worktree as `cwd` from writing `/tmp`, `$HOME`,
`/etc`, or any other absolute host path a process could otherwise reach.

This module never grants a worker arbitrary shell, arbitrary subprocess,
arbitrary filesystem access, or arbitrary network access. The *only* way
mutation can occur inside a job worktree, in this phase or any later one
that builds on it, is through Code Slayer's existing closed capability
set (`tools.registry.CAPABILITIES`) as authorized per-call by
`policy.engine.PolicyEngine` and enforced by `tools.executor.ToolExecutor`
— with `repo_root` simply pointing at the job worktree instead of the
primary one. Every existing confinement primitive (`tools.file_tools.
relative_path`/`parent_fd`, no absolute paths, no traversal) is
unmodified and unconditionally still applies. OS-level containment (a
dedicated unprivileged user, namespaces, seccomp, read-only host mounts —
`docs/CODE_SLAYER_VISION.md` §38) remains a stated *future* goal, not
something this phase claims or provides.

## Location: Code-Slayer-owned, never model-supplied

A job worktree's filesystem path is always generated here
(`state_root()/job-worktrees/<repo_id>/<job_id>`, `job_id` a fresh
`uuid4`) — never accepted as a parameter from a caller, and therefore
never something a request, a prompt, or a model output can influence.
This mirrors `store.location`'s own existing external-state principle
(`adr/0002-external-durable-state.md`): the path lives outside the
primary repository's working tree entirely, so a destructive operation
against the primary tree can never reach it, and a job worktree can never
be mistaken for, or nested inside, the tree it was created from.

## Base revision: explicit, frozen, detached

`create_job_worktree()` resolves whatever revision it is given (or the
primary worktree's current `HEAD`, if none) to a concrete commit sha
*before* creating anything, via `job_worktree_git.resolve_commit()`, and
creates the linked worktree `--detach`ed at that exact sha (`job_worktree_
git.add_worktree()`). No branch is created or moved — not the user's, not
a new one of Code Slayer's own — so nothing can later silently change
what commit the job is actually based on merely because some branch's
tip moved.

## Repository identity: shared repo_id, distinct worktree_id

A job worktree is resolved via the *same* `repo.identity.resolve()` every
other worktree uses: it shares `repo_id` with its primary repository (the
same `--local` git config value, visible from any linked worktree) and
receives its own fresh `worktree_id` (a new `codeslayer-id` file under
its own private `.git/worktrees/<name>/`). This is not a new identity
system — it is the existing one, simply resolved against a second,
Code-Slayer-created working tree of the same repository. Because
`store.location`'s durable-state paths are keyed by `(repo_id,
worktree_id)`, the job worktree automatically receives its own
`state.db`/`blobs/`/`tmp/` the moment its identity is resolved — no
separate mechanism is needed to keep job state from leaking into, or
reading, the primary worktree's own state.

## Durable lifecycle evidence: no schema migration

A job worktree's association with its task_id, source repo identity, and
worktree_id is recoverable entirely from existing, already-durable state
once a task exists: `tasks.repo_root`/`repo_id`/`worktree_id` (Phase 1),
`worker_leases` (Phase 6), and `checkpoints` (Phase 5). The one fact none
of those already carry — the frozen base commit a job worktree itself was
created from — is recorded in the small JSON sidecar
(`JOB_WORKTREE_METADATA_FILENAME`) this module writes into the job's own
state directory, keyed by exactly the `(repo_id, worktree_id)` pair
`store.location` already uses for every other piece of that worktree's
durable state. No new table or migration is needed, so none was added.

The one genuine gap that pre-dates any task existing is the narrow window
between `git worktree add` succeeding and this module's own state
directory/database existing yet. `create_job_worktree()` closes it with a
small JSON sidecar (`JOB_WORKTREE_METADATA_FILENAME`) written into the
job's own state directory immediately after identity is resolved, before
opening or migrating its database — see `JobWorktreeSetupIncompleteError`'s
docstring for what happens if even that cannot complete: the linked
worktree itself is never silently deleted, because Git's own worktree
registry (`.git/worktrees/`, `git worktree list`) is already independent,
durable ground truth that it exists, no matter what Code Slayer's own
bookkeeping managed to record.

## Cleanup: conservative, refusal-first, and race-closed (Phase 7.7b)

`release_job_worktree()` never deletes a job worktree unless it can
positively confirm, from durable evidence, that doing so loses nothing:
no active/quiescing lease, no unresolved (`STARTED`/`UNKNOWN`) tool
operation, no non-terminal task, no task-owned path that was never
covered by at least one durable checkpoint (Code-Slayer-owned
bookkeeping) — **and**, independently, that the ACTUAL on-disk state
exactly matches the latest checkpoint's tree (or the pinned
`base_revision`, if none exists yet), via real Git plumbing, never a
filesystem diff this module invents:

- `job_worktree_git.staged_changes()` — the job worktree's own REAL
  index (`git diff --cached --name-only HEAD`), not a private temporary
  one. A tracked file that was modified, staged, and then had its
  working-tree bytes restored is invisible to any working-tree-only
  comparison; the staged blob a later `git commit` would actually use is
  not clean, and cleanup must see that.
- `job_worktree_git.worktree_status()` — working tree vs. the checkpoint
  tree, via a private temporary index, `--ignored` included. Being
  ignored by `.gitignore` describes what a *commit* should skip, not
  what is disposable: an ignored build artifact, cache file, or log that
  physically exists is real content a `git worktree remove` would
  destroy just the same as an untracked one, so it is never excluded
  from this check.

Both are independent fail-safes from the bookkeeping checks above and
from each other — see `release_job_worktree()`'s own docstring for the
case each one catches that the others do not.

The decision to remove is also serialized against new ownership being
granted for the same worktree while cleanup is deciding: cleanup first
records a durable claim (`job_worktree_cleanup_claims`, Phase 7.7b's one
new migration) inside a committed write transaction that has *just*
re-verified the bookkeeping checks above, and `store.task_repo.TaskRepo.
create()` / `lease.manager.LeaseManager`'s fresh-epoch grant both refuse
to establish new ownership of a worktree a claim names — closing the
window between "cleanup decided this was safe" and "the worktree is
actually gone" that a single in-process check, unserialized against
concurrent task/lease creation, could not. If `git worktree remove`
itself then fails, the claim is deliberately left standing (still
blocking new ownership) rather than either pretending success or losing
track of the attempt; a later `release_job_worktree()` call safely
resumes or retries. See `release_job_worktree()`'s own docstring for the
full two-phase (claim, then verify-and-remove) protocol.

Any refusal condition — or simply not yet being able to determine one
safely (e.g. the job's own database was never successfully created, or a
Git cleanliness check itself could not run) — is a refusal, not a
best-effort guess. Even a successful cleanup only removes the disposable
Git working tree itself (`job_worktree_git.remove_worktree()`); the
job's own `state.db`/`blobs/` audit trail is deliberately left in place
as a historical record, never deleted by this module.

## No promotion into the primary worktree

Nothing here fast-forwards, merges, cherry-picks, or updates any branch
in the primary worktree. A job worktree's checkpoints live under
`refs/codeslayer/checkpoints/<task_id>/<seq>` exactly as
`repo.checkpoint.CheckpointManager` already creates them for any other
worktree — visible from the primary worktree too, since Git's object/ref
storage is shared by every linked worktree of one repository, but never
applied to the primary worktree's own `HEAD`, index, or working tree.
Promotion into a user's real branch is explicitly out of scope for this
phase (`docs/ROADMAP.md`'s Local Worker Runtime stage) and belongs after
stronger finalization/review infrastructure exists.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.repo import identity
from code_slayer.repo import job_worktree_git as jwg
from code_slayer.store import db as db_module
from code_slayer.store import location
from code_slayer.store.checkpoint_repo import CheckpointRepo
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.lease_repo import LeaseRepo, LeaseStatus
from code_slayer.store.task_repo import TaskRepo

JOB_WORKTREE_METADATA_FILENAME = "job_worktree.json"
_METADATA_FORMAT_VERSION = 1


class JobWorktreeError(RuntimeError):
    """A job-worktree lifecycle operation could not be completed safely."""


class JobWorktreeSetupIncompleteError(JobWorktreeError):
    """`git worktree add` succeeded, but setting up Code Slayer's own
    tracking for it (identity/state directory/database/audit event) did
    not. The linked worktree at `path` was deliberately **not** removed —
    this module never guesses that discarding a partially-set-up job is
    safe, and Git's own worktree registry (`git worktree list` against
    the primary repository) already durably proves it exists regardless
    of what Code Slayer's own bookkeeping captured.

    Recovery: `repo.identity.resolve(path)` is idempotent and safe to
    retry; once it succeeds, `create_job_worktree()`'s remaining setup
    can simply be re-attempted by resolving the same identity again, or
    the worktree can be removed manually with
    `job_worktree_git.remove_worktree()` once a human has confirmed
    nothing about it needs to be recovered.
    """

    def __init__(self, path: Path, cause: BaseException) -> None:
        self.path = path
        self.cause = cause
        super().__init__(
            f"job worktree at {path} was created but its setup did not complete "
            f"({cause!r}); it was deliberately NOT removed"
        )


@dataclass(frozen=True)
class JobWorktree:
    """Everything needed to operate a task against one isolated job
    worktree, and to later find/clean it up. Mirrors the fields
    `repo.identity.RepoIdentity` already carries, plus what this module
    adds: the frozen base revision and the derived Code-Slayer state
    paths (`store.location`)."""

    repo_id: str
    worktree_id: str
    path: Path
    primary_repo_root: Path
    base_revision: str
    git_dir: Path
    git_common_dir: Path
    state_dir: Path
    db_path: Path
    blobs_dir: Path
    tmp_dir: Path
    created_at: str


@dataclass(frozen=True)
class CleanupResult:
    """The outcome of one `release_job_worktree()` attempt. Never raises
    for an ordinary, expected refusal — `ok=False` with a stable `reason`
    covers every one of those, matching the rest of this codebase's
    `Decision`/`PolicyResult`/`LeaseResult`/`CheckpointResult` convention;
    only a genuinely unexpected programming error propagates as a real
    exception."""

    ok: bool
    reason: str


def _event(conn: sqlite3.Connection, event_type: EventType, payload: dict) -> None:
    AuditWriter(conn).append(
        task_id=None, event_type=event_type, actor_type="system",
        actor_id="job-worktree-manager", payload=payload,
    )


def create_job_worktree(
    primary_path: Path | str, *, base_revision: str | None = None,
    state_root_override: str | Path | None = None,
) -> JobWorktree:
    """Create one isolated, disposable job worktree of the repository
    containing `primary_path`, pinned to `base_revision` (or that
    repository's current `HEAD` if omitted), and return everything needed
    to operate a task against it.

    The job worktree's filesystem path is always generated by this
    function — never accepted as a parameter — under
    `state_root()/job-worktrees/<repo_id>/<job_id>`, a Code-Slayer-owned
    location outside the primary repository's own working tree. Detached,
    never a branch (see this module's docstring, "Base revision").

    Raises `job_worktree_git.JobWorktreeGitError` if `git worktree add`
    itself fails (nothing is created). Raises
    `JobWorktreeSetupIncompleteError` if the worktree *was* created but
    this function's own bookkeeping (identity/state dir/database/audit)
    could not complete — the worktree is deliberately left in place; see
    that exception's docstring for recovery.
    """
    primary = identity.resolve(primary_path)
    resolved_revision = jwg.resolve_commit(
        base_revision if base_revision is not None else "HEAD", cwd=primary.repo_root,
    )

    job_id = uuid.uuid4().hex
    job_path = (
        location.state_root(override=state_root_override) / "job-worktrees"
        / primary.repo_id / job_id
    ).resolve()
    if job_path.is_relative_to(primary.repo_root):
        # Defense in depth: state_root() never resolves inside a repo in
        # practice, but a job worktree must never end up nested inside
        # the tree it was created from regardless of configuration.
        raise JobWorktreeError("computed job worktree path is inside the primary repository")
    job_path.parent.mkdir(parents=True, exist_ok=True)

    jwg.add_worktree(job_path, resolved_revision, cwd=primary.repo_root)

    try:
        job_identity = identity.resolve(job_path)
        if job_identity.repo_id != primary.repo_id:
            # Unreachable in practice for a real linked worktree of the
            # same repository -- fail closed rather than trust a broken
            # invariant if it ever is.
            raise JobWorktreeError("job worktree resolved to an unexpected repo_id")

        created_at = utcnow_iso()
        state_dir = location.ensure_dirs(
            job_identity.repo_id, job_identity.worktree_id, override=state_root_override,
        )
        metadata = {
            "format_version": _METADATA_FORMAT_VERSION,
            "repo_id": job_identity.repo_id,
            "worktree_id": job_identity.worktree_id,
            "path": str(job_path),
            "primary_repo_root": str(primary.repo_root),
            "base_revision": resolved_revision,
            "git_dir": str(job_identity.git_dir),
            "git_common_dir": str(job_identity.git_common_dir),
            "created_at": created_at,
        }
        (state_dir / JOB_WORKTREE_METADATA_FILENAME).write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )

        conn = db_module.connect(
            location.db_path(
                job_identity.repo_id, job_identity.worktree_id, override=state_root_override,
            ),
        )
        try:
            db_module.migrate(conn)
            with transaction(conn):
                _event(conn, EventType.JOB_WORKTREE_CREATED, {
                    "repo_id": job_identity.repo_id, "worktree_id": job_identity.worktree_id,
                    "path": str(job_path), "primary_repo_root": str(primary.repo_root),
                    "base_revision": resolved_revision,
                })
        finally:
            conn.close()
    except BaseException as exc:
        raise JobWorktreeSetupIncompleteError(job_path, exc) from exc

    return JobWorktree(
        repo_id=job_identity.repo_id, worktree_id=job_identity.worktree_id, path=job_path,
        primary_repo_root=primary.repo_root, base_revision=resolved_revision,
        git_dir=job_identity.git_dir, git_common_dir=job_identity.git_common_dir,
        state_dir=state_dir, db_path=location.db_path(
            job_identity.repo_id, job_identity.worktree_id, override=state_root_override,
        ),
        blobs_dir=location.blobs_dir(
            job_identity.repo_id, job_identity.worktree_id, override=state_root_override,
        ),
        tmp_dir=location.tmp_dir(
            job_identity.repo_id, job_identity.worktree_id, override=state_root_override,
        ),
        created_at=created_at,
    )


def read_job_worktree_metadata(state_dir: Path | str) -> dict | None:
    """Read back the JSON sidecar `create_job_worktree()` wrote, or
    `None` if this state directory does not carry one (never raises for
    that ordinary case — a caller checking "is this a job worktree's
    state dir at all" should not need a try/except)."""
    metadata_path = Path(state_dir) / JOB_WORKTREE_METADATA_FILENAME
    if not metadata_path.exists():
        return None
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def _has_uncheckpointed_ownership(conn: sqlite3.Connection, worktree_id: str) -> bool:
    """True if any task that has ever run against `worktree_id` currently
    owns a path (`task_owned_paths`, `deleted = 0`) not covered by that
    task's own latest checkpoint's `changed_files_json` -- which
    `repo.checkpoint.CheckpointManager` always records as the *complete*
    owned-path set at checkpoint time (`ToolExecutor._facts()`'s
    `task_owned_paths` query it is built from is never incremental), so
    "covered by the latest checkpoint" is exactly "no checkpoint-worthy
    content has changed since". A task with owned paths but no checkpoint
    at all counts as uncheckpointed."""
    checkpoints = CheckpointRepo(conn)
    task_ids = [row["task_id"] for row in conn.execute(
        "SELECT DISTINCT p.task_id FROM task_owned_paths p JOIN tasks t ON p.task_id = t.task_id "
        "WHERE t.worktree_id = ? AND p.deleted = 0",
        (worktree_id,),
    )]
    for task_id in task_ids:
        owned = {row["path"] for row in conn.execute(
            "SELECT path FROM task_owned_paths WHERE task_id = ? AND deleted = 0", (task_id,),
        )}
        latest = checkpoints.latest(task_id)
        if latest is None:
            return True
        if not owned.issubset(set(json.loads(latest.changed_files_json))):
            return True
    return False


def _latest_checkpoint_tree(conn: sqlite3.Connection, worktree_id: str) -> str | None:
    """The `tree_sha` of the most recently created durable checkpoint
    across every task that has ever run against `worktree_id`, or `None`
    if none exists yet. Read via a plain SQL join rather than
    `CheckpointRepo` (which is scoped to one task_id at a time) because a
    worktree may have had more than one terminal task over its lifetime."""
    row = conn.execute(
        "SELECT c.verified_json FROM checkpoints c JOIN tasks t ON c.task_id = t.task_id "
        "WHERE t.worktree_id = ? AND c.status = 'COMPLETE' ORDER BY c.created_at DESC LIMIT 1",
        (worktree_id,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["verified_json"])["tree_sha"]


def _bookkeeping_safety_check(conn: sqlite3.Connection, worktree_id: str) -> str | None:
    """The Code-Slayer bookkeeping half of the cleanup safety decision: a
    stable refusal reason, or `None` if this half raises no objection.
    Always called fresh, inside the same write transaction that then
    (re-)records the durable cleanup claim (`_claim_for_cleanup()`), so
    the two can never be observed to disagree — the fix for the cleanup
    check/delete race (Phase 7.7b)."""
    lease = LeaseRepo(conn).get(worktree_id)
    if lease is not None and lease.status in (LeaseStatus.ACTIVE, LeaseStatus.QUIESCING):
        return "active_lease"
    unresolved = conn.execute(
        "SELECT 1 FROM tool_operations WHERE worktree_id = ? "
        "AND status IN ('STARTED', 'UNKNOWN') LIMIT 1",
        (worktree_id,),
    ).fetchone()
    if unresolved is not None:
        return "unresolved_operations"
    if TaskRepo(conn).get_active_for_worktree(worktree_id) is not None:
        return "active_task"
    if _has_uncheckpointed_ownership(conn, worktree_id):
        return "uncheckpointed_changes"
    return None


def _get_cleanup_claim(conn: sqlite3.Connection, worktree_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM job_worktree_cleanup_claims WHERE worktree_id = ?", (worktree_id,),
    ).fetchone()


def _claim_for_cleanup(conn: sqlite3.Connection, handle: JobWorktree) -> CleanupResult:
    """Phase 1 of cleanup's two-phase protocol: durably record, in one
    committed `BEGIN IMMEDIATE` transaction, that this worktree has just
    been freshly proven safe (its bookkeeping half, at least) to remove.

    This is the serialization primitive that closes the check/delete
    race: `store.task_repo.TaskRepo.create()` and `lease.manager.
    LeaseManager`'s fresh-epoch grant both check `job_worktree_cleanup_
    claims` inside their *own* write transaction before establishing new
    ownership of a worktree, and SQLite's own write-lock discipline
    (`store.db.transaction()`, `BEGIN IMMEDIATE`) totally orders this
    transaction against theirs — whichever commits first is the one the
    other observes. No filesystem lock is invented; the existing durable
    transaction discipline already provides everything this needs.

    Idempotent: resumes an existing claim on `handle.worktree_id` (left
    behind by an earlier, interrupted `release_job_worktree()` call)
    rather than creating a second one — a worktree_id can only ever have
    one claim row. If the bookkeeping check now fails despite an existing
    claim (state that should be impossible while a claim stands, since it
    is exactly what the claim prevents — but never trusted blindly), the
    stale claim is removed rather than left standing over a worktree
    cleanup no longer believes is safe.
    """
    with transaction(conn):
        existing = _get_cleanup_claim(conn, handle.worktree_id)
        refusal = _bookkeeping_safety_check(conn, handle.worktree_id)
        if refusal is not None:
            if existing is not None:
                conn.execute(
                    "DELETE FROM job_worktree_cleanup_claims WHERE worktree_id = ?",
                    (handle.worktree_id,),
                )
                _event(conn, EventType.JOB_WORKTREE_CLEANUP_ABORTED, {
                    "repo_id": handle.repo_id, "worktree_id": handle.worktree_id,
                    "reason": refusal,
                })
            return CleanupResult(False, refusal)
        if existing is None:
            token = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO job_worktree_cleanup_claims "
                "(worktree_id, claim_token, claimed_at, status) VALUES (?, ?, ?, 'CLAIMED')",
                (handle.worktree_id, token, utcnow_iso()),
            )
            _event(conn, EventType.JOB_WORKTREE_CLEANUP_CLAIMED, {
                "repo_id": handle.repo_id, "worktree_id": handle.worktree_id,
                "claim_token": token,
            })
    return CleanupResult(True, "claimed")


def _abort_claim(conn: sqlite3.Connection, handle: JobWorktree, *, reason: str) -> None:
    """Release the durable cleanup claim without removing anything —
    cleanup decided, after all, not to proceed (a filesystem-level
    safety check failed). Restores ordinary task/lease creation
    eligibility for this worktree."""
    with transaction(conn):
        conn.execute(
            "DELETE FROM job_worktree_cleanup_claims WHERE worktree_id = ?",
            (handle.worktree_id,),
        )
        _event(conn, EventType.JOB_WORKTREE_CLEANUP_ABORTED, {
            "repo_id": handle.repo_id, "worktree_id": handle.worktree_id, "reason": reason,
        })


def _finalize_removal(conn: sqlite3.Connection, handle: JobWorktree) -> CleanupResult:
    """Phase 2's successful terminal step: the Git worktree is confirmed
    gone, so the claim is released and the ordinary `JOB_WORKTREE_
    RELEASED` event is recorded — exactly the event a pre-Phase-7.7b
    cleanup already recorded on success, so existing audit consumers see
    no new event shape for the success path."""
    with transaction(conn):
        conn.execute(
            "DELETE FROM job_worktree_cleanup_claims WHERE worktree_id = ?",
            (handle.worktree_id,),
        )
        _event(conn, EventType.JOB_WORKTREE_RELEASED, {
            "repo_id": handle.repo_id, "worktree_id": handle.worktree_id,
            "path": str(handle.path), "reason": "conservative_cleanup",
        })
    return CleanupResult(True, "removed")


def release_job_worktree(handle: JobWorktree) -> CleanupResult:
    """Remove the disposable Git working tree at `handle.path` — never
    `handle.state_dir` itself (the `state.db`/`blobs/` audit trail is
    kept as a historical record) — but only once durable evidence
    positively confirms it is safe, and only by way of a fail-closed,
    two-phase protocol (Phase 7.7b) rather than a single in-process check
    that could go stale before removal actually happens:

    **Phase 1 — claim** (`_claim_for_cleanup()`): inside one committed
    write transaction, freshly re-check every bookkeeping fact (no active
    or quiescing lease; no unresolved `STARTED`/`UNKNOWN` tool operation;
    no non-terminal task; no task-owned path uncovered by a durable
    checkpoint — `_bookkeeping_safety_check()`) and, only if all pass,
    durably record a cleanup claim on `handle.worktree_id`. From that
    commit onward, `store.task_repo.TaskRepo.create()` and `lease.
    manager.LeaseManager` both refuse to grant this worktree to anyone
    new — closing the window between "cleanup decided this was safe" and
    "the worktree is actually gone" that an unserialized check could not.

    **Phase 2 — verify and remove**: with new ownership now provably
    impossible, check that the ACTUAL, on-disk working tree exactly
    matches the most recent durable checkpoint's tree (or, if none exists
    yet, `handle.base_revision`) two independent ways — real Git plumbing
    only, never this module's own filesystem diff: `job_worktree_git.
    staged_changes()` (the job worktree's own real index — catches a
    tracked change that was staged and then had its working-tree bytes
    restored, invisible to a working-tree-only comparison) and
    `job_worktree_git.worktree_status()` (working tree vs. the checkpoint
    tree, `--ignored` included — an ignored-but-present file is real
    content a removal would destroy and is never treated as disposable
    here). Either finding anything releases the claim without removing
    the worktree (`_abort_claim()`) and refuses. Only once both are clean
    does `job_worktree_git.remove_worktree()` actually run.

    **Failure during removal**: if `remove_worktree()` itself fails, the
    claim is deliberately left in place (still blocking new ownership)
    and a `JOB_WORKTREE_CLEANUP_REMOVAL_FAILED` event is recorded; no
    `JOB_WORKTREE_RELEASED` event is ever written for an incomplete
    removal. A later `release_job_worktree()` call for the same handle
    safely resumes: it recognizes the standing claim, asks Git's own
    worktree registry (`job_worktree_git.list_worktrees()`) whether the
    removal actually completed before a crash prevented recording it,
    and either finalizes (if so) or retries the removal (if not) —
    without re-doing the filesystem checks, which the claim has already
    made incapable of changing out from under it.

    Any refusal (or simply being unable to open the job's own database at
    all, e.g. because setup never completed) is `CleanupResult(ok=False,
    reason=...)`, never a best-effort deletion. See `docs/JOB_WORKTREES.md`
    for the full picture, including why the bookkeeping and Git-plumbing
    checks are independent fail-safes for each other rather than either
    one subsuming the other.
    """
    if not handle.db_path.exists():
        return CleanupResult(False, "state_incomplete_cannot_verify_safety")

    conn = db_module.connect(handle.db_path)
    try:
        claimed = _claim_for_cleanup(conn, handle)
        if not claimed.ok:
            return claimed

        claim_row = _get_cleanup_claim(conn, handle.worktree_id)
        resuming_removal = claim_row is not None and claim_row["status"] == "REMOVING"

        if resuming_removal:
            # A previous attempt got at least as far as marking removal
            # in progress before this call returned. Ask Git's own
            # registry what actually happened rather than guessing:
            # `remove_worktree()` may have succeeded and only the
            # in-process recording of that fact was lost to a crash.
            if str(handle.path) not in jwg.list_worktrees(cwd=handle.primary_repo_root):
                return _finalize_removal(conn, handle)
        else:
            try:
                staged = jwg.staged_changes(handle.path)
            except jwg.JobWorktreeGitError:
                _abort_claim(conn, handle, reason="git_staged_status_unavailable")
                return CleanupResult(False, "git_staged_status_unavailable")
            if staged:
                _abort_claim(conn, handle, reason="git_worktree_staged_changes")
                return CleanupResult(False, "git_worktree_staged_changes")

            target = _latest_checkpoint_tree(conn, handle.worktree_id) or handle.base_revision
            handle.tmp_dir.mkdir(parents=True, exist_ok=True)
            index_path = handle.tmp_dir / f"cleanup-status-{uuid.uuid4().hex}"
            try:
                dirty = jwg.worktree_status(handle.path, against=target, index_path=index_path)
            except jwg.JobWorktreeGitError:
                _abort_claim(conn, handle, reason="git_worktree_status_unavailable")
                return CleanupResult(False, "git_worktree_status_unavailable")
            finally:
                index_path.unlink(missing_ok=True)
            if dirty:
                _abort_claim(conn, handle, reason="git_worktree_dirty")
                return CleanupResult(False, "git_worktree_dirty")

            with transaction(conn):
                conn.execute(
                    "UPDATE job_worktree_cleanup_claims SET status = 'REMOVING' "
                    "WHERE worktree_id = ?",
                    (handle.worktree_id,),
                )

        try:
            jwg.remove_worktree(handle.path, cwd=handle.primary_repo_root, force=True)
        except jwg.JobWorktreeGitError as exc:
            with transaction(conn):
                _event(conn, EventType.JOB_WORKTREE_CLEANUP_REMOVAL_FAILED, {
                    "repo_id": handle.repo_id, "worktree_id": handle.worktree_id,
                    "path": str(handle.path), "error": str(exc),
                })
            return CleanupResult(False, "git_worktree_remove_failed")

        return _finalize_removal(conn, handle)
    finally:
        conn.close()
