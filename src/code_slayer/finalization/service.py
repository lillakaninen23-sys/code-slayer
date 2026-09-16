"""Finalizer orchestration: real `TaskStateMachine` transitions driven by
verified evidence, plus the `CHECKPOINTED -> COMPLETED` transition guard.

No parallel state machine: every transition here goes through
`core.state_machine.TaskStateMachine.transition_in_transaction()`, the
same production authority every other module uses, over the existing
`core.transitions` graph. `INVALID_ENVIRONMENT`/`BLOCKED` verdicts both
resolve to the existing `TaskState.BLOCKED` — never a new state — relying
on `core.transitions.validate_transition()`'s own existing resume-origin
mechanics (a task entering `BLOCKED` durably records its origin as
`current_phase`, and resuming must return there) for a correct
`resume_target`.

## Content identity: an actual Git tree id, not a hash of hashes

A `VERIFIED` decision is bound to a real Git tree id (`verified_tree_sha`),
computed by `repo.checkpoint_git.build_tree()` — the exact same function
`repo.checkpoint.CheckpointManager` itself calls to build a real
checkpoint's tree, from the exact same inputs (the baseline tree at
`metadata["head"]`, plus one blob per owned path, written via `repo.
checkpoint_git.hash_blob()` — also the same function `CheckpointManager`
uses). This is deliberately the strongest, smallest, already-existing
content identity available (audited against `repo/checkpoint.py`/`repo/
checkpoint_git.py` before writing this): Git's own tree object recursively
hashes every entry's exact blob content, path, and mode, so two tree ids
can only be equal if the full content is byte-for-byte identical — never
merely because the same path names, or a hash of (path, some-hash) pairs,
happen to coincide while the underlying bytes differ. Once a checkpoint
actually exists, its own tree id is already durably stored
(`store.checkpoint_repo.CheckpointVerified.tree_sha`) — the guard below
compares two already-known strings directly, recomputing nothing on the
checkpoint side and never duplicating `repo.checkpoint`'s own tree-
building logic a second time for that half of the comparison.

## Verification executes against an isolated tree, never the live worktree

Binding a `VERIFIED` decision's `verified_tree_sha` to whatever checkpoint
happens to match it (above) is not enough by itself: it proves the
checkpoint's content did not drift *after* verification, but says nothing
about whether the verification *commands themselves* ran against exactly
that content. A live worktree can contain content no tree id here will
ever represent — a modified-but-unowned tracked file, an untracked
scratch file, a `.gitignore`d generated artifact — and `pytest`/`ruff`/
`mypy` scan whatever is actually on disk, not merely the paths Code
Slayer owns. Running them in the live `repo_root` would let such content
silently influence a verification verdict while being completely absent
from `verified_tree_sha` and the eventual checkpoint.

`Finalizer._materialize_verification_tree()` closes this by construction,
not by detection: it builds the exact tree `edits` produces on top of
`base_tree_sha` (`repo.checkpoint_git.build_tree()` — the same function
`CheckpointManager` itself calls) and checks it out as real files under a
fresh, isolated temporary directory via Git's own `checkout-index`
(`repo.checkpoint_git.checkout_tree_to_directory()`) — never by Code
Slayer independently copying or reconstructing file bytes in Python, and
never by reading anything from the live working tree beyond what
`_baseline_facts()` already reads to validate each owned path. Every
verification command then runs with that isolated directory as `cwd`,
never `repo_root`. Ignored/untracked/unowned-modified live content is
therefore not merely disallowed — it does not exist in the directory
verification commands execute in at all, by the same construction that
already makes `verified_tree_sha` exact. This is the sole authority for
verification purity; a live-worktree-drift check is, at most, an
additional diagnostic, never a substitute for isolation.

The isolated tree represents repository content only. The *process*
environment verification commands run in (the Python interpreter,
installed packages, `PATH`, explicitly-configured runtime variables — see
`finalization.verification._environment()`) is unaffected by this and
remains visible, exactly as before: isolating *content* is not the same
as sandboxing the *runtime*, and this module does not attempt the latter.
A project whose verification genuinely requires an ignored, repository-
local, uncheckpointed artifact to be present is an environment/support
gap, not something this mechanism silently works around by leaking live
content back in.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.core import TaskState, TaskStateMachine, TransitionRequest
from code_slayer.core.state_machine import TransitionGuard
from code_slayer.core.transitions import InvalidTransition
from code_slayer.finalization.policy import evaluate_completion, evaluate_verification
from code_slayer.finalization.types import (
    FinalizationFacts,
    FinalizerDecision,
    FinalizerVerdict,
    ReviewEvidence,
)
from code_slayer.finalization.verification import (
    FinalizationError,
    run_verification_commands,
)
from code_slayer.lease.manager import LeaseHandle, LeaseManager
from code_slayer.repo import checkpoint_git as cg
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.inspection import inspect_repository
from code_slayer.store.baseline_repo import BaselineError, BaselineRepo
from code_slayer.store.checkpoint_repo import CheckpointRepo, parse_verified
from code_slayer.store.db import transaction
from code_slayer.store.task_repo import TaskRepo
from code_slayer.tools import file_tools as files

DEFAULT_MAX_REPAIR_ATTEMPTS = 3


class Finalizer:
    """Code-owned authority for deciding what happens after a mutating
    worker turn's verification evidence is in. Holds no opinion about
    *when* it is called (`runner.local_worker_runner` calls it once, after
    `IMPLEMENTING -> VERIFYING`; a future background dispatcher could call
    it again after a repair cycle returns to `VERIFYING` — same method,
    same evidence discipline, no new authority needed)."""

    def __init__(
        self, conn: sqlite3.Connection, *, blobs_dir: Path | str, tmp_dir: Path | str,
    ) -> None:
        self._conn = conn
        self._blobs_dir = Path(blobs_dir).resolve()
        self._tmp_dir = Path(tmp_dir).resolve()
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._tasks = TaskRepo(conn)
        self._audit = AuditWriter(conn)
        self._leases = LeaseManager(conn)
        self._machine = TaskStateMachine(conn)

    def decide_after_verification(
        self, task_id: str, lease: LeaseHandle, *,
        max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
        review: ReviewEvidence | None = None,
    ) -> FinalizerDecision:
        """Run the verification-command pipeline for a `VERIFYING` task
        and durably transition it to exactly one of `REPAIRING` (via
        `REPAIR_REQUIRED`), `BLOCKED` (via `BLOCKED` or
        `INVALID_ENVIRONMENT`), or -- via `VERIFIED` -- directly to
        `READY_FOR_CHECKPOINT` when no reviewer was consulted (`review_
        required = false` in this phase/version; no reviewer model exists
        yet, so this is always the path taken today), or to `REVIEWING`
        when a real, approved review verdict actually was supplied (see
        `finalization.types.ReviewEvidence`) -- audit/state history never
        pretends a review happened when none did.

        Raises `FinalizationError` (no transition attempted) if the lease
        is not current, or the task is not actually `VERIFYING` -- a
        caller error, never silently converted into a task-state
        transition under uncertain authority."""
        if not isinstance(lease, LeaseHandle):
            raise FinalizationError("malformed_lease_handle")
        task = self._tasks.get(task_id)
        if task.state != TaskState.VERIFYING.value:
            raise FinalizationError("wrong_task_state")
        if not self._leases.is_current(lease):
            raise FinalizationError("stale_fencing_token")

        verification: tuple = ()
        repair_attempts = self._repair_attempt_count(task_id)
        try:
            baseline_facts, repo_root, base_tree_sha, edits, owned_paths = self._baseline_facts(
                task,
            )
        except (
            BaselineError, ValueError, TypeError, KeyError, OSError,
            sqlite3.Error, AttributeError, cg.CheckpointGitError,
        ):
            decision = FinalizerDecision(
                FinalizerVerdict.BLOCKED, "invalid_context_or_baseline", TaskState.BLOCKED,
            )
            return self._apply(task_id, lease, decision, repair_attempts, verification, None)

        # The tree must be honestly buildable -- coherent, hash-verified
        # owned content, no protected conflict, no drift -- before
        # anything is materialized or executed at all; matches
        # `evaluate_verification()`'s own priority order, so a task that
        # would be denied on these grounds regardless never pays for
        # materialization or subprocess execution whose evidence would be
        # discarded anyway.
        tree_sha: str | None = None
        verifiable = (
            baseline_facts["identity_valid"] and baseline_facts["baseline_valid"]
            and baseline_facts["owned_paths_valid"] and not baseline_facts["protected_conflict"]
            and not baseline_facts["unresolved"]
        )
        if verifiable:
            try:
                isolated_dir, tree_sha, cleanup = self._materialize_verification_tree(
                    base_tree_sha, edits, repo_root,
                )
            except (cg.CheckpointGitError, OSError):
                # The exact content verification would run against cannot
                # even be materialized right now -- an environment
                # problem, not a code defect (approved amendment #A):
                # never fall back to running commands against the live
                # worktree instead.
                decision = FinalizerDecision(
                    FinalizerVerdict.INVALID_ENVIRONMENT,
                    "verification_tree_materialization_failed", TaskState.BLOCKED,
                )
                return self._apply(task_id, lease, decision, repair_attempts, verification, None)
            try:
                # Verification commands run with `cwd=isolated_dir` --
                # never `repo_root` -- so ignored/untracked/unowned-
                # modified live content cannot influence the result; see
                # the module docstring's "Verification executes against
                # an isolated tree" section.
                verification = run_verification_commands(
                    self._conn, task_id=task_id, verification_root=isolated_dir, lease=lease,
                    tree_sha=tree_sha,
                )
            finally:
                cleanup()

        facts = FinalizationFacts(
            task_id=task.task_id, repo_id=task.repo_id, worktree_id=task.worktree_id,
            state=TaskState(task.state), verification=verification, review=review,
            repair_attempts=repair_attempts, max_repair_attempts=max_repair_attempts,
            **baseline_facts,
        )
        decision = evaluate_verification(facts)
        # Bind a VERIFIED decision to exactly the content it was about
        # (approved amendment #2 / TOCTOU closure) -- the same tree id
        # verification commands actually just ran against, built the same
        # way `repo.checkpoint.CheckpointManager` itself will build
        # whatever checkpoint actually gets created from this task.
        # `tree_sha` is guaranteed set here: VERIFIED is unreachable
        # unless `verifiable` was true, which is exactly when it was
        # computed above.
        tree_evidence = (
            (tree_sha, owned_paths) if decision.verdict == FinalizerVerdict.VERIFIED else None
        )
        return self._apply(task_id, lease, decision, repair_attempts, verification, tree_evidence)

    def _materialize_verification_tree(
        self, base_tree_sha: str, edits: tuple[cg.TreeEdit, ...], repo_root: Path,
    ) -> tuple[Path, str, Callable[[], None]]:
        """Build the exact tree `edits` produces on top of `base_tree_sha`
        (the same machinery `repo.checkpoint.CheckpointManager` itself
        uses) and materialize it as real files under a fresh, isolated
        temporary directory via Git's own `checkout-index` (`repo.
        checkpoint_git.checkout_tree_to_directory`) -- never by Code
        Slayer independently copying or reconstructing file bytes in
        Python, and never touching the live repository working tree
        beyond the read-only object-database lookups `build_tree()`/
        `hash_blob()` already perform. Returns `(isolated_dir, tree_sha,
        cleanup)`; the caller must call `cleanup()` once verification
        commands have finished, on every path (success or failure)."""
        token = uuid.uuid4()
        index_path = self._tmp_dir / f"finalizer-verify-index-{token}"
        isolated_dir = self._tmp_dir / f"finalizer-verify-tree-{token}"
        isolated_dir.mkdir(parents=True)
        try:
            tree_sha = cg.build_tree(base_tree_sha, edits, cwd=repo_root, index_path=index_path)
            cg.checkout_tree_to_directory(index_path, isolated_dir, cwd=repo_root)
        except BaseException:
            shutil.rmtree(isolated_dir, ignore_errors=True)
            raise
        finally:
            index_path.unlink(missing_ok=True)

        def cleanup() -> None:
            shutil.rmtree(isolated_dir, ignore_errors=True)

        return isolated_dir, tree_sha, cleanup

    def _apply(
        self, task_id: str, lease: LeaseHandle, decision: FinalizerDecision,
        repair_attempts: int, verification: tuple,
        tree_evidence: tuple[str, tuple[tuple[str, str], ...]] | None,
    ) -> FinalizerDecision:
        with transaction(self._conn):
            if not self._leases.is_current(lease):
                raise FinalizationError("stale_fencing_token")
            current = self._tasks.get(task_id)
            self._machine.transition_in_transaction(
                task_id, request=TransitionRequest(
                    expected_state=TaskState(current.state), to_state=decision.target_state,
                    reason=f"finalizer:{decision.verdict.value}:{decision.reason_code}",
                ), actor_id="finalizer",
            )
            payload = {
                "verdict": decision.verdict.value, "reason_code": decision.reason_code,
                "target_state": decision.target_state.value,
                "repair_attempts": repair_attempts,
                "verification": [
                    {
                        "command": r.command, "purpose": r.purpose, "status": r.status,
                        "returncode": r.returncode, "operation_id": r.operation_id,
                    }
                    for r in verification
                ],
            }
            if tree_evidence is not None:
                # Provenance (approved amendment #2, item E): the
                # authoritative content identity (`verified_tree_sha`, a
                # real Git tree id) plus the human-readable owned-path
                # detail it was built from, so a later reader can see
                # directly which content a given checkpoint's own
                # matching tree id refers to, never only an opaque hash.
                tree_sha, owned_paths = tree_evidence
                payload["verified_tree_sha"] = tree_sha
                payload["verified_owned_paths"] = [list(p) for p in owned_paths]
            self._audit.append(
                task_id=task_id, event_type=EventType.FINALIZATION_DECIDED,
                actor_type="system", actor_id="finalizer", payload=payload,
            )
        return decision

    def _repair_attempt_count(self, task_id: str) -> int:
        return _count_transitions_to(self._conn, task_id, TaskState.REPAIRING)

    def _baseline_facts(
        self, task,
    ) -> tuple[dict, Path, str, tuple[cg.TreeEdit, ...], tuple[tuple[str, str], ...]]:
        if not BaselineRepo(self._conn).exists(task.task_id):
            raise FinalizationError("no_baseline_recorded")
        manifest = InspectionService(
            self._conn, blobs_dir=self._blobs_dir,
        ).read_manifest(task.task_id)
        metadata = manifest["inspection"]
        identity_valid = (task.repo_id, task.worktree_id) == (
            metadata["repo_id"], metadata["worktree_id"],
        )
        root = Path(metadata["repo_root"])
        live = inspect_repository(root, establish_identity=False)
        baseline_protected = BaselineRepo(self._conn).protected_paths(task.task_id)
        baseline_valid = (
            identity_valid and live.head == metadata["head"] and live.branch == metadata["branch"]
            and baseline_protected == manifest["protected_paths"]
        )
        owned_rows = self._conn.execute(
            "SELECT p.path, o.after_evidence FROM task_owned_paths p "
            "JOIN tool_operations o ON p.last_operation_id = o.operation_id "
            "WHERE p.task_id = ? AND p.deleted = 0 AND o.status = 'SUCCEEDED' "
            "ORDER BY p.path",
            (task.task_id,),
        ).fetchall()
        owned_paths_valid = True
        protected_conflict = False
        edits: list[cg.TreeEdit] = []
        owned_paths: list[tuple[str, str]] = []
        for row in owned_rows:
            path, expected_hash = row["path"], row["after_evidence"]
            if any(files.within(path, protected) for protected in baseline_protected):
                protected_conflict = True
                continue
            try:
                with files.parent_fd(root, path) as (parent, name):
                    info = files.inspect_leaf(parent, name)
                    if info is None:
                        owned_paths_valid = False
                        continue
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        data = files.read_bytes(fd)
                    finally:
                        os.close(fd)
            except Exception:
                owned_paths_valid = False
                continue
            if files.digest(data) != expected_hash:
                owned_paths_valid = False
                continue
            # Same function `repo.checkpoint.CheckpointManager` itself
            # uses to build a real checkpoint's tree edits -- reused, not
            # reimplemented, so the two sides can only ever agree by
            # actually computing the same content identity the same way.
            blob_sha = cg.hash_blob(data, cwd=root)
            edits.append(cg.TreeEdit(path, blob_sha))
            owned_paths.append((path, expected_hash))
        unresolved = self._conn.execute(
            "SELECT 1 FROM tool_operations WHERE worktree_id = ? "
            "AND status IN ('STARTED', 'UNKNOWN') LIMIT 1",
            (task.worktree_id,),
        ).fetchone() is not None
        facts = {
            "identity_valid": identity_valid, "baseline_valid": baseline_valid,
            "owned_paths_valid": owned_paths_valid, "protected_conflict": protected_conflict,
            "unresolved": unresolved,
        }
        base_tree_sha = cg.resolve_tree_sha(metadata["head"], cwd=root)
        return facts, root, base_tree_sha, tuple(edits), tuple(owned_paths)


def _count_transitions_to(conn: sqlite3.Connection, task_id: str, state: TaskState) -> int:
    rows = conn.execute(
        "SELECT payload_json FROM audit_events WHERE task_id = ? "
        "AND event_type = 'STATE_TRANSITION' ORDER BY seq",
        (task_id,),
    ).fetchall()
    count = 0
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if payload.get("to_state") == state.value:
            count += 1
    return count


def _latest_verified_finalization(conn: sqlite3.Connection, task_id: str) -> dict | None:
    """The most recent `FINALIZATION_DECIDED(verdict=VERIFIED)` audit
    payload for this task, or `None` if none exists, or a later
    `STATE_TRANSITION` into `IMPLEMENTING` has superseded it (a
    re-mutation without a fresh verification pass). Read-only, append-
    only-log evidence only -- never a worker's or model's own claim.
    Returns the full payload (not just a boolean) so the caller can
    compare its `verified_tree_sha` against a checkpoint's own durable
    evidence -- see `checkpointed_completion_guard`."""
    rows = conn.execute(
        "SELECT event_type, payload_json FROM audit_events WHERE task_id = ? "
        "AND event_type IN ('FINALIZATION_DECIDED', 'STATE_TRANSITION') ORDER BY seq DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if row["event_type"] == "STATE_TRANSITION" and payload.get("to_state") == (
            TaskState.IMPLEMENTING.value
        ):
            return None
        if row["event_type"] == "FINALIZATION_DECIDED" and payload.get("verdict") == (
            FinalizerVerdict.VERIFIED.value
        ):
            return payload
    return None


def checkpointed_completion_guard(conn: sqlite3.Connection) -> TransitionGuard:
    """Build the `TransitionGuard` that vetoes `CHECKPOINTED -> COMPLETED`
    unless durable evidence proves THREE things: a real `checkpoints` row
    exists; a prior, unsuperseded `VERIFIED` finalization record exists;
    and (closing the TOCTOU gap -- approved amendment #2) that record's
    own `verified_tree_sha` (a real Git tree id -- see the module
    docstring's "Content identity" section) is IDENTICAL to the
    checkpoint's OWN already-durable `store.checkpoint_repo.
    CheckpointVerified.tree_sha` -- a direct string comparison, nothing
    recomputed from live Git/filesystem state on the checkpoint side, and
    never duplicating `repo.checkpoint`'s own tree-building logic a
    second time. A checkpoint that represents different content than
    whatever was last verified (repo mutated after verification, before
    or during checkpointing) fails this comparison and is refused, with a
    distinct `verification_content_mismatch` reason.

    Bound to `conn` at construction, exactly like every other closure-
    based guard/decision helper in this codebase (e.g. `workers.trust.
    WorkerTrustManager._transition_in_transaction`'s fresh re-read under
    the caller's already-open write lock); a no-op for every other edge --
    critically including the untouched `bounded_read_only_turn` shortcut
    (`IMPLEMENTING -> COMPLETED`), which this guard must never veto: it
    only ever applies to a task actually in `CHECKPOINTED`."""

    def _guard(task, request) -> None:
        if request.to_state != TaskState.COMPLETED or task.state != TaskState.CHECKPOINTED.value:
            return
        checkpoint = CheckpointRepo(conn).latest(task.task_id)
        checkpoint_confirmed = checkpoint is not None
        prior_payload = _latest_verified_finalization(conn, task.task_id)
        prior_verification_confirmed = prior_payload is not None
        content_fingerprint_matches = False
        if checkpoint_confirmed and prior_verification_confirmed:
            verified = parse_verified(checkpoint)
            content_fingerprint_matches = (
                prior_payload.get("verified_tree_sha") == verified.tree_sha
            )
        unresolved = conn.execute(
            "SELECT 1 FROM tool_operations WHERE worktree_id = ? "
            "AND status IN ('STARTED', 'UNKNOWN') LIMIT 1",
            (task.worktree_id,),
        ).fetchone() is not None
        facts = FinalizationFacts(
            task_id=task.task_id, repo_id=task.repo_id, worktree_id=task.worktree_id,
            state=TaskState(task.state), identity_valid=True, baseline_valid=True,
            owned_paths_valid=True, protected_conflict=False, unresolved=unresolved,
            checkpoint_confirmed=checkpoint_confirmed,
            prior_verification_confirmed=prior_verification_confirmed,
            content_fingerprint_matches=content_fingerprint_matches,
        )
        decision = evaluate_completion(facts)
        if decision.verdict != FinalizerVerdict.FINAL:
            raise InvalidTransition(f"finalization_guard:{decision.reason_code}")

    return _guard
