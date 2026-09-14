"""Durable Git checkpoint creation and crash recovery (Phase 5).

`CheckpointManager.create()` takes only the repository content a task is
recorded as owning (Phase 4's `task_owned_paths`), revalidates it against
the task's immutable baseline (Phase 3) and current repository reality,
and — only if that revalidation and an explicit policy decision both
allow it — creates one immutable Git commit representing exactly that
content, under a dedicated ref namespace the user's own branch, HEAD, and
index are never touched by (`repo.checkpoint_git`). The mutation is
journaled exactly like a Phase 4 tool call (`tool_operations`,
`STARTED` durably committed before any Git side effect), and the durable
`checkpoints` row plus the `READY_FOR_CHECKPOINT -> CHECKPOINTED`
transition are written together, atomically, only once the commit is
known to exist.

Crash recovery is narrow and evidence-based, not a general reconciler: the
one fact this module ever uses to resolve an unresolved `checkpoint_create`
operation is whether its specific, uniquely-named ref
(`refs/codeslayer/checkpoints/<task_id>/<seq>`) now exists — a ref Code
Slayer exclusively writes, so its presence or absence is unambiguous,
durable evidence of whether the commit was actually created. A caught
Python exception during Git plumbing is always a *deterministic* failure
(the subprocess ran to completion with a known result); genuine
uncertainty is reserved for a real process crash, resolved only later, on
a subsequent call, from that same ref evidence — never guessed.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.core import TaskState, TaskStateMachine, TransitionRequest
from code_slayer.lease.manager import LeaseHandle, LeaseManager
from code_slayer.policy.engine import (
    CheckpointPolicyInput,
    Decision,
    PolicyResult,
    evaluate_checkpoint,
)
from code_slayer.repo import checkpoint_git as cg
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.inspection import inspect_repository
from code_slayer.store.baseline_repo import BaselineError, BaselineRepo
from code_slayer.store.checkpoint_repo import (
    CheckpointError,
    CheckpointRepo,
    CheckpointVerified,
    parse_verified,
)
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import (
    OperationStatus,
    ToolOperationsRepo,
    compute_request_hash,
)
from code_slayer.tools import file_tools as files
from code_slayer.tools.registry import CAPABILITIES

TOOL_NAME = "checkpoint_create"


@dataclass(frozen=True)
class CheckpointResult:
    decision: str
    reason: str
    checkpoint_id: str | None = None
    commit_sha: str | None = None
    tree_sha: str | None = None
    seq: int | None = None
    operation_status: str | None = None


@dataclass(frozen=True)
class _Context:
    root: Path
    base_head_sha: str | None
    base_tree_sha: str
    parent_commit_sha: str | None
    parent_checkpoint_id: str | None
    seq: int
    git_ref: str
    branch: str | None
    edits: tuple[cg.TreeEdit, ...]
    owned_paths: tuple[tuple[str, str], ...]  # (path, sha256 content hash)
    request_hash: str


def _ref_for(task_id: str, seq: int) -> str:
    return f"refs/codeslayer/checkpoints/{task_id}/{seq}"


def _seq_from_ref(ref: str) -> int:
    return int(ref.rsplit("/", 1)[-1])


class CheckpointManager:
    def __init__(
        self, conn: sqlite3.Connection, *, blobs_dir: Path | str, tmp_dir: Path | str,
        lease: LeaseHandle | None = None,
    ) -> None:
        # `lease` is optional here: `reconcile()` never consults it (Git-
        # ref evidence and the state machine's own expected-state check
        # already make a stale finalize safe — see `create()`'s docstring)
        # so a purely-recovery caller (generic recovery, §13) need not
        # hold one. `create()` requires a real `LeaseHandle` and denies
        # closed if none was given.
        if lease is not None and not isinstance(lease, LeaseHandle):
            raise CheckpointError("malformed_lease_handle")
        self._conn = conn
        self._blobs_dir = Path(blobs_dir).resolve()
        self._tmp_dir = Path(tmp_dir).resolve()
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._tasks = TaskRepo(conn)
        self._operations = ToolOperationsRepo(conn)
        self._checkpoints = CheckpointRepo(conn)
        self._machine = TaskStateMachine(conn)
        self._audit = AuditWriter(conn)
        self._lease = lease
        self._leases = LeaseManager(conn)

    # -- public API ---------------------------------------------------

    def create(self, task_id: str) -> CheckpointResult:
        """Create a checkpoint, first resolving any unfinished prior attempt.

        Starting a *new* checkpoint attempt requires a currently-valid
        lease. Recovering/finalizing a prior attempt does not: once a
        commit exists, checkpoint truth is Git-ref evidence, not lease
        state (`docs/LEASES_AND_RECOVERY.md`) — and the state machine's
        own expected-state check already refuses a stale finalize that
        would conflict with whatever a newer session has since done.

        This method's *automatic* reconciliation attempt is itself gated
        on the caller holding a currently-valid lease: reconciliation
        assumes the pending operation's own originating session is truly
        gone, which is not something an arbitrary or stale caller should
        get to assert as a side effect of merely calling `create()`. An
        owner/operator who explicitly wants to attempt reconciliation
        regardless — e.g. from generic recovery — has `reconcile()` for
        exactly that (`docs/LEASES_AND_RECOVERY.md`'s known limitations).
        """
        task = self._tasks.get(task_id)
        lease_ok = (
            self._lease is not None
            and task.worktree_id == self._lease.worktree_id
            and self._leases.is_current(self._lease)
        )
        pending = self._find_pending_operation(task_id)
        if pending is not None and lease_ok:
            resolved = self._safe_reconcile(task, pending)
            if resolved is not None:
                # A prior attempt is now confirmed COMPLETE; do not also
                # attempt a new one in the same call.
                return resolved
            task = self._tasks.get(task_id)  # pending was FAILED; state unchanged

        operation_id = str(uuid.uuid4())
        try:
            facts, context = self._facts(task)
            decision = self._decision(facts)
        except (CheckpointError, BaselineError, ValueError, TypeError, KeyError,
                OSError, sqlite3.Error, AttributeError) as exc:
            reason = str(exc) if isinstance(exc, CheckpointError) else "invalid_context_or_request"
            decision = PolicyResult(Decision.DENY, reason)
            facts = None
            context = None
        if not lease_ok:
            # Fencing supersedes whatever the checkpoint-specific facts/
            # policy concluded, matching ToolExecutor's ordering (task
            # state -> lease validity -> policy).
            decision = PolicyResult(Decision.DENY, "stale_fencing_token")

        summary = {
            "operation_id": operation_id, "tool": TOOL_NAME,
            "request_hash": context.request_hash if context else None,
        }
        with transaction(self._conn):
            self._event(task_id, EventType.TOOL_REQUESTED, summary)
            self._event(task_id, EventType.POLICY_EVALUATED, {
                **summary, "decision": decision.decision, "reason": decision.reason,
            })
            if not lease_ok:
                self._event(task_id, EventType.FENCE_STALE_REJECTED, {
                    **summary, "worktree_id": task.worktree_id,
                    "claimed_generation": self._lease.generation if self._lease else None,
                })
            if facts is not None and not facts.baseline_valid:
                self._event(task_id, EventType.EXTERNAL_MODIFICATION_DETECTED, {
                    **summary, "kind": "baseline_drift",
                })
            if facts is not None and not facts.owned_paths_valid:
                self._event(task_id, EventType.EXTERNAL_MODIFICATION_DETECTED, {
                    **summary, "kind": "owned_path_changed",
                })
            if decision.decision != Decision.ALLOW:
                self._event(task_id, EventType.POLICY_DENIED, {
                    **summary, "decision": decision.decision, "reason": decision.reason,
                })
                return CheckpointResult(decision.decision, decision.reason)
            self._operations.start_in_transaction(
                task_id=task_id, worktree_id=task.worktree_id,
                worker_id=self._lease.worker_id, worker_session_id=self._lease.worker_session_id,
                lease_generation=self._lease.generation,
                tool_name=TOOL_NAME, risk_class=CAPABILITIES[TOOL_NAME].risk.value,
                request_hash=context.request_hash, target_resource=context.git_ref,
                before_evidence=context.parent_commit_sha or "ROOT", operation_id=operation_id,
            )
            self._event(task_id, EventType.OPERATION_STARTED, {
                **summary, "resource": context.git_ref,
                "before_evidence": context.parent_commit_sha or "ROOT", "status": "STARTED",
            })
            self._event(task_id, EventType.CHECKPOINT_VALIDATED, {
                **summary, "task_id": task_id, "base_head_sha": context.base_head_sha,
                "seq": context.seq, "owned_path_count": len(context.owned_paths),
            })
        # STARTED is committed before any Git object or ref is created.
        try:
            index_path = self._tmp_dir / f"checkpoint-index-{operation_id}"
            tree_sha = cg.build_tree(
                context.base_tree_sha, context.edits, cwd=context.root, index_path=index_path,
            )
            parents = (context.parent_commit_sha,) if context.parent_commit_sha else ()
            commit_sha = cg.commit_tree(
                tree_sha, parents,
                f"Code Slayer checkpoint\n\ntask: {task_id}\nseq: {context.seq}\n",
                cwd=context.root,
            )
            # Fencing revalidation at the deepest practical boundary: the
            # tree/commit objects above are inert until a ref names them,
            # so this is the last possible moment to refuse making the
            # checkpoint externally visible under authority that may have
            # since been superseded — never rely solely on the validation
            # performed before `build_tree()` started.
            if not self._leases.is_current(self._lease):
                raise cg.CheckpointGitError("stale_fencing_token")
            cg.create_ref(context.git_ref, commit_sha, cwd=context.root)
        except Exception as exc:
            # A caught exception here means the failing subprocess already
            # ran to completion with a known result: nothing this attempt
            # did created the dedicated ref (create_ref itself either
            # never ran or raised on a non-zero return), so no ambiguity —
            # this is a deterministic failure, not an UNKNOWN outcome.
            reason = str(exc) if isinstance(exc, cg.CheckpointGitError) else "checkpoint_git_failed"
            with transaction(self._conn):
                self._operations.finish_in_transaction(
                    operation_id, status=OperationStatus.FAILED, result={"reason": reason},
                )
                self._event(task_id, EventType.OPERATION_FINISHED, {
                    "operation_id": operation_id, "status": OperationStatus.FAILED,
                    "reason": reason,
                })
            return CheckpointResult(Decision.ALLOW, reason, operation_status=OperationStatus.FAILED)
        finally:
            index_path.unlink(missing_ok=True)

        try:
            return self._finalize(
                task_id=task_id, operation_id=operation_id, commit_sha=commit_sha,
                tree_sha=tree_sha, git_ref=context.git_ref, seq=context.seq,
                parent_checkpoint_id=context.parent_checkpoint_id,
                parent_commit_sha=context.parent_commit_sha, base_head_sha=context.base_head_sha,
                branch=context.branch, owned_paths=context.owned_paths,
                request_hash=context.request_hash,
            )
        except Exception:
            # The commit itself is confirmed to exist (we hold its sha);
            # only the durable bookkeeping (finish/checkpoints row/state
            # transition, all one atomic transaction) failed to commit —
            # for any reason, including a state-machine guard rejecting
            # the transition. The tool_operations row is still STARTED —
            # genuinely uncertain from here, resolved by a later
            # reconcile()/create() via the same ref-exists evidence, never
            # guessed at as SUCCEEDED or FAILED right now.
            return CheckpointResult(
                Decision.ALLOW, "finalization_incomplete", commit_sha=commit_sha,
                tree_sha=tree_sha, seq=context.seq, operation_status=OperationStatus.UNKNOWN,
            )

    def reconcile(self, task_id: str) -> CheckpointResult | None:
        """Explicitly resolve any unfinished `checkpoint_create` operation
        for this task. Returns `None` if there was nothing to resolve (or
        nothing could safely be determined right now)."""
        task = self._tasks.get(task_id)
        pending = self._find_pending_operation(task_id)
        if pending is None:
            return None
        return self._safe_reconcile(task, pending)

    # -- internals ------------------------------------------------------

    def _safe_reconcile(self, task, pending_row) -> CheckpointResult | None:
        try:
            return self._reconcile(task, pending_row)
        except Exception:
            # Reconciliation itself could not even run right now (e.g. the
            # baseline manifest is unreadable) — leave the pending
            # operation exactly as it was rather than raising past this
            # method. A caller retrying `create()` independently hits the
            # same condition through `_facts()` and denies closed.
            return None

    def _event(self, task_id, event_type, payload) -> None:
        self._audit.append(
            task_id=task_id, event_type=event_type, actor_type="system",
            actor_id="checkpoint-manager", payload=payload,
        )

    def _decision(self, facts) -> PolicyResult:
        try:
            result = evaluate_checkpoint(facts)
        except Exception:
            return PolicyResult(Decision.DENY, "malformed_policy")
        if (not isinstance(result, PolicyResult) or not isinstance(result.decision, Decision)
                or not isinstance(result.reason, str) or not result.reason):
            return PolicyResult(Decision.DENY, "malformed_policy")
        return result

    def _find_pending_operation(self, task_id: str):
        row = self._conn.execute(
            "SELECT * FROM tool_operations WHERE task_id = ? AND tool_name = ? "
            "AND status IN ('STARTED', 'UNKNOWN') ORDER BY started_at DESC LIMIT 1",
            (task_id, TOOL_NAME),
        ).fetchone()
        return row

    def _reconcile(self, task, pending_row) -> CheckpointResult | None:
        operation_id = pending_row["operation_id"]
        git_ref = pending_row["target_resource"]
        manifest = InspectionService(
            self._conn, blobs_dir=self._blobs_dir,
        ).read_manifest(task.task_id)
        root = Path(manifest["inspection"]["repo_root"])
        # Ground truth is checked before opening any transaction — a git
        # subprocess call must never run with the SQLite write lock held.
        try:
            resolved_commit = cg.resolve_ref(git_ref, cwd=root)
            ref_check_failed = False
        except Exception:
            resolved_commit = None
            ref_check_failed = True

        with transaction(self._conn):
            self._event(task.task_id, EventType.RECONCILIATION_STARTED, {
                "operation_id": operation_id, "target_resource": git_ref,
            })
            if ref_check_failed:
                # Cannot even determine ground truth right now — genuinely
                # uncertain, not guessed. Leave the row unresolved (STARTED
                # is marked UNKNOWN; UNKNOWN stays UNKNOWN) for a later retry.
                self._event(task.task_id, EventType.RECONCILIATION_FINDING, {
                    "operation_id": operation_id, "finding": "ref_check_failed",
                    "resolved_status": OperationStatus.UNKNOWN,
                })
                if pending_row["status"] != OperationStatus.UNKNOWN:
                    self._conn.execute(
                        "UPDATE tool_operations SET status = ? "
                        "WHERE operation_id = ? AND status = ?",
                        (OperationStatus.UNKNOWN, operation_id, OperationStatus.STARTED),
                    )
                return None
            if resolved_commit is None:
                self._operations.finish_in_transaction(
                    operation_id, status=OperationStatus.FAILED,
                    result={"reason": "checkpoint_ref_absent_after_crash"},
                )
                self._event(task.task_id, EventType.OPERATION_FINISHED, {
                    "operation_id": operation_id, "status": OperationStatus.FAILED,
                    "reason": "checkpoint_ref_absent_after_crash",
                })
                self._event(task.task_id, EventType.RECONCILIATION_FINDING, {
                    "operation_id": operation_id, "finding": "ref_absent",
                    "resolved_status": OperationStatus.FAILED,
                })
                return None
            self._event(task.task_id, EventType.RECONCILIATION_FINDING, {
                "operation_id": operation_id, "finding": "ref_exists",
                "resolved_status": OperationStatus.SUCCEEDED, "commit_sha": resolved_commit,
            })
        try:
            tree_sha, parents = cg.read_commit(resolved_commit, cwd=root)
        except cg.CheckpointGitError:
            # The ref exists but the commit it names cannot be read back —
            # evidence is broken, not merely absent. Do not finalize a
            # checkpoint we cannot verify; require explicit handling.
            return None
        seq = _seq_from_ref(git_ref)
        owned_paths = self._current_owned_paths(task.task_id)
        return self._finalize(
            task_id=task.task_id, operation_id=operation_id, commit_sha=resolved_commit,
            tree_sha=tree_sha, git_ref=git_ref, seq=seq,
            parent_checkpoint_id=self._parent_checkpoint_id(task.task_id, seq),
            parent_commit_sha=parents[0] if parents else None,
            base_head_sha=manifest["inspection"]["head"],
            branch=manifest["inspection"]["branch"], owned_paths=owned_paths,
            request_hash=pending_row["request_hash"],
        )

    def _parent_checkpoint_id(self, task_id: str, seq: int) -> str | None:
        if seq == 0:
            return None
        rows = self._checkpoints.list_for_task(task_id)
        matching = [c for c in rows if c.seq == seq - 1]
        return matching[0].checkpoint_id if matching else None

    def _current_owned_paths(self, task_id: str) -> tuple[tuple[str, str], ...]:
        rows = self._conn.execute(
            "SELECT p.path, o.after_evidence FROM task_owned_paths p "
            "JOIN tool_operations o ON p.last_operation_id = o.operation_id "
            "WHERE p.task_id = ? AND p.deleted = 0 AND o.status = 'SUCCEEDED' "
            "ORDER BY p.path",
            (task_id,),
        ).fetchall()
        return tuple((row["path"], row["after_evidence"]) for row in rows)

    def _finalize(
        self, *, task_id, operation_id, commit_sha, tree_sha, git_ref, seq,
        parent_checkpoint_id, parent_commit_sha, base_head_sha, branch,
        owned_paths, request_hash,
    ) -> CheckpointResult:
        """Atomically resolve the journal entry to SUCCEEDED and record the
        durable checkpoint + state transition — called once the commit is
        already known (freshly created, or recovered by `_reconcile`)."""
        checkpoint_id = str(uuid.uuid4())
        with transaction(self._conn):
            self._operations.finish_in_transaction(
                operation_id, status=OperationStatus.SUCCEEDED, after_evidence=commit_sha,
                result={"reason": "completed", "commit_sha": commit_sha, "tree_sha": tree_sha},
            )
            self._event(task_id, EventType.OPERATION_FINISHED, {
                "operation_id": operation_id, "status": OperationStatus.SUCCEEDED,
                "after_evidence": commit_sha, "reason": "completed",
            })
            verified = CheckpointVerified(
                commit_sha=commit_sha, tree_sha=tree_sha, parent_commit_sha=parent_commit_sha,
                base_head_sha=base_head_sha, request_hash=request_hash, owned_paths=owned_paths,
            )
            self._checkpoints.record_in_transaction(
                checkpoint_id=checkpoint_id, task_id=task_id, seq=seq,
                parent_checkpoint=parent_checkpoint_id, created_at=utcnow_iso(),
                phase=TaskState.READY_FOR_CHECKPOINT.value, git_ref=git_ref, git_branch=branch,
                verified=verified, changed_paths=tuple(p for p, _ in owned_paths),
            )
            self._machine.transition_in_transaction(
                task_id, request=TransitionRequest(
                    TaskState.READY_FOR_CHECKPOINT, TaskState.CHECKPOINTED,
                    "durable checkpoint recorded",
                ),
            )
            self._event(task_id, EventType.CHECKPOINT_CREATED, {
                "checkpoint_id": checkpoint_id, "task_id": task_id, "seq": seq,
                "git_ref": git_ref, "commit_sha": commit_sha, "tree_sha": tree_sha,
                "parent_commit_sha": parent_commit_sha, "operation_id": operation_id,
            })
        return CheckpointResult(
            Decision.ALLOW, "completed", checkpoint_id, commit_sha, tree_sha, seq,
            OperationStatus.SUCCEEDED,
        )

    def _facts(self, task) -> tuple[CheckpointPolicyInput, _Context]:
        if not BaselineRepo(self._conn).exists(task.task_id):
            raise CheckpointError("no_baseline_recorded")
        manifest = InspectionService(
            self._conn, blobs_dir=self._blobs_dir,
        ).read_manifest(task.task_id)
        metadata = manifest["inspection"]
        identity_valid = (task.repo_id, task.worktree_id) == (
            metadata["repo_id"], metadata["worktree_id"],
        )
        roots = [Path(metadata[key]) for key in ("repo_root", "git_dir", "git_common_dir")]
        locations = [self._blobs_dir, self._tmp_dir, *(Path(r["file"]).resolve() for r in
                     self._conn.execute("PRAGMA database_list") if r["file"])]
        if any(location.is_relative_to(root) for root in roots for location in locations):
            raise CheckpointError("internal_storage_location")
        if Path(metadata["repo_root"]).resolve() != Path(metadata["repo_root"]):
            raise CheckpointError("root_changed")
        root = Path(metadata["repo_root"])

        # A live HEAD/branch re-check (drift since baseline), not a fresh
        # full re-inspection: a new *unrelated* untracked file appearing
        # since baseline is normal, expected, and must not block a
        # checkpoint of content Code Slayer actually owns — it is simply
        # excluded from the tree, never treated as "drift." Only HEAD/
        # branch moving, or the two durable copies of the ORIGINAL
        # protected-path set disagreeing with each other (a consistency
        # check, mirroring Phase 4's own `baseline_protection_mismatch`),
        # count as baseline drift here.
        live = inspect_repository(root, establish_identity=False)
        baseline_protected = BaselineRepo(self._conn).protected_paths(task.task_id)
        baseline_valid = (
            identity_valid
            and live.head == metadata["head"]
            and live.branch == metadata["branch"]
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
        edits: list[cg.TreeEdit] = []
        owned_paths: list[tuple[str, str]] = []
        protected_conflict = False
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
                # Missing, symlinked, hardlinked-into-a-conflict, oversized,
                # or otherwise unreadable: treat as external drift, not a
                # framework failure — the checkpoint decision denies on
                # `owned_paths_valid=False` rather than raising past here.
                owned_paths_valid = False
                continue
            actual_hash = files.digest(data)
            if actual_hash != expected_hash:
                owned_paths_valid = False
                continue
            blob_sha = cg.hash_blob(data, cwd=root)
            edits.append(cg.TreeEdit(path, blob_sha))
            owned_paths.append((path, expected_hash))

        unresolved = self._conn.execute(
            "SELECT 1 FROM tool_operations WHERE worktree_id = ? "
            "AND status IN ('STARTED', 'UNKNOWN') LIMIT 1",
            (task.worktree_id,),
        ).fetchone() is not None

        facts = CheckpointPolicyInput(
            task.task_id, task.repo_id, task.worktree_id, TaskState(task.state), False,
            identity_valid, baseline_valid, owned_paths_valid, protected_conflict, unresolved,
        )

        seq = self._checkpoints.next_seq(task.task_id)
        latest = self._checkpoints.latest(task.task_id)
        if latest is None:
            parent_commit_sha = metadata["head"]
            parent_checkpoint_id = None
        else:
            parent_commit_sha = parse_verified(latest).commit_sha
            parent_checkpoint_id = latest.checkpoint_id
        base_tree_sha = cg.resolve_tree_sha(metadata["head"], cwd=root)
        git_ref = _ref_for(task.task_id, seq)
        request_hash = compute_request_hash(TOOL_NAME, {
            "task_id": task.task_id, "repo_id": task.repo_id, "worktree_id": task.worktree_id,
            "seq": seq, "base_head": metadata["head"], "owned_paths": owned_paths,
        })
        context = _Context(
            root=root, base_head_sha=metadata["head"], base_tree_sha=base_tree_sha,
            parent_commit_sha=parent_commit_sha, parent_checkpoint_id=parent_checkpoint_id,
            seq=seq, git_ref=git_ref, branch=metadata["branch"], edits=tuple(edits),
            owned_paths=tuple(owned_paths), request_hash=request_hash,
        )
        return facts, context
