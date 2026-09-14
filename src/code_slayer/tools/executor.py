"""Policy -> durable intent -> controlled effect -> atomic result/ownership/audit."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.core.states import TaskState
from code_slayer.lease.liveness import process_start_time
from code_slayer.lease.manager import LeaseHandle, LeaseManager
from code_slayer.policy.engine import Decision, PolicyEngine, PolicyInput, PolicyResult
from code_slayer.repo.baseline import InspectionService
from code_slayer.store.baseline_repo import BaselineError, BaselineRepo
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import ToolOperationsRepo, compute_request_hash
from code_slayer.tools import file_tools as files
from code_slayer.tools.command_tools import CommandRunner, validate_command
from code_slayer.tools.models import PatchHunk, RiskClass, ToolError, ToolRequest, ToolResult
from code_slayer.tools.registry import CAPABILITIES

# The `ContentStore.put(source_kind=...)` classification for a read_file
# operation's exact bytes (§Phase 7.5b) -- never `"command_output"`,
# which is reserved for a real subprocess's captured stdout/stderr. A
# caller that already holds this operation's `ToolResult.output_hash`
# (`workers.execution._evidence_content()`, today) retrieves the
# identical bytes this classification names, never a second read of the
# repository file itself.
READ_EVIDENCE_KIND = "tool_read_output"


class ToolExecutor:
    def __init__(
        self, conn: sqlite3.Connection, *, blobs_dir: Path | str, lease: LeaseHandle,
    ) -> None:
        if not isinstance(lease, LeaseHandle):
            raise ToolError("malformed_lease_handle")
        self._conn = conn
        self._blobs_dir = Path(blobs_dir).resolve()
        self._tasks = TaskRepo(conn)
        self._operations = ToolOperationsRepo(conn)
        self._audit = AuditWriter(conn)
        self._policy = PolicyEngine()
        self._lease = lease
        self._leases = LeaseManager(conn)

    def execute(self, task_id: str, request: ToolRequest) -> ToolResult:
        operation_id = str(uuid.uuid4())
        with transaction(self._conn):
            task = self._tasks.get(task_id)
            # Ownership/lease validity is checked before policy (task state
            # -> lease validity -> policy -> journaling -> effect ->
            # evidence -> finalization): a stale caller is refused
            # regardless of what policy would otherwise have allowed.
            lease_ok = (
                task.worktree_id == self._lease.worktree_id and self._leases.is_current(self._lease)
            )
            try:
                params = self._validate_request(request)
                facts, manifest, owned_hash = self._facts(task, request)
                decision = self._decision(facts)
                request_hash = compute_request_hash(request.tool, {
                    "task_id": task_id, "repo_id": task.repo_id,
                    "worktree_id": task.worktree_id, **params,
                })
            except (ToolError, BaselineError, ValueError, TypeError, KeyError,
                    OSError, sqlite3.Error, AttributeError) as exc:
                # Any unrecognized shape of task config, baseline manifest,
                # or request must deny rather than raise past this method:
                # an uncaught exception here is not a controlled decision.
                reason = str(exc) if isinstance(exc, ToolError) else "invalid_context_or_request"
                decision = PolicyResult(Decision.DENY, reason)
                facts = None
                request_hash = None
            if not lease_ok:
                # Fencing supersedes whatever policy/validation concluded —
                # a valid lease is not permission, but an invalid one is an
                # unconditional refusal.
                decision = PolicyResult(Decision.DENY, "stale_fencing_token")
            tool = request.tool if isinstance(request, ToolRequest) else "unknown"
            tool = tool if isinstance(tool, str) and tool in CAPABILITIES else "unknown"
            summary = {"operation_id": operation_id, "tool": tool, "request_hash": request_hash}
            self._event(task_id, EventType.TOOL_REQUESTED, summary)
            self._event(task_id, EventType.POLICY_EVALUATED, {
                **summary, "decision": decision.decision, "reason": decision.reason,
                "risk": facts.risk if facts else None,
            })
            if not lease_ok:
                self._event(task_id, EventType.FENCE_STALE_REJECTED, {
                    **summary, "worktree_id": task.worktree_id,
                    "claimed_generation": self._lease.generation,
                })
            if decision.decision != Decision.ALLOW:
                requires_approval = decision.decision == Decision.REQUIRE_APPROVAL
                denial_event = (
                    EventType.POLICY_APPROVAL_REQUIRED if requires_approval
                    else EventType.POLICY_DENIED
                )
                self._event(task_id, denial_event, {
                    **summary, "decision": decision.decision, "reason": decision.reason,
                })
                return ToolResult(operation_id, decision.decision, decision.reason)
            capability = CAPABILITIES[request.tool]
            before = "ABSENT" if request.tool == "create_file" else owned_hash
            self._operations.start_in_transaction(
                task_id=task_id, worktree_id=task.worktree_id,
                worker_id=self._lease.worker_id, worker_session_id=self._lease.worker_session_id,
                lease_generation=self._lease.generation,
                tool_name=request.tool, risk_class=facts.risk.value,
                request_hash=request_hash, target_resource=facts.resource,
                before_evidence=before, operation_id=operation_id,
            )
            self._event(task_id, EventType.OPERATION_STARTED, {
                **summary, "risk": facts.risk, "resource": facts.resource,
                "before_evidence": before, "status": "STARTED",
            })
        # STARTED is committed before any subprocess or target mutation.
        effect = {"mutated": False, "command_uncertain": False}
        runner = CommandRunner(
            Path(manifest["inspection"]["repo_root"]),
            timeout=request.command.timeout if request.command else 3.0,
            on_spawn=lambda pid: self._operations.record_child_pid(
                operation_id, pid, process_start_time(pid),
            ),
        )
        try:
            # verify_identity's own git subprocesses are read-only plumbing:
            # each either completes with a definite, checked result or
            # raises a deterministic ToolError (identity_mismatch, etc.) —
            # never left uncertain — so they do not toggle command_uncertain.
            runner.verify_identity(
                manifest["inspection"], mutation=capability.mutation, resource=facts.resource,
            )
            if request.tool == "run_command":
                # From here until a complete CommandOutput is obtained, the
                # child's outcome cannot be proven if this raises (checkpoint
                # item #2): an interrupted capture must not be reported as a
                # certain FAILED.
                effect["command_uncertain"] = True
                output = runner.run(request.command)
                effect["command_uncertain"] = False
                status = "SUCCEEDED" if (
                    output.returncode == 0 and not output.truncated and not output.timed_out
                ) else "FAILED"
                reason = "command_timeout" if output.timed_out else (
                    "output_limit" if output.truncated else "command_completed"
                )
                with transaction(self._conn):
                    # Revalidate authority immediately before durable
                    # finalization: a takeover during the command's run
                    # must not let a now-stale caller record its result.
                    if not self._leases.is_current(self._lease):
                        raise ToolError("stale_fencing_token")
                    return self._finish(
                        task_id, operation_id, status, reason, output.stdout, output.stderr,
                        returncode=output.returncode, truncated=output.truncated,
                    )
            # A short SQLite writer transaction serializes the final policy
            # recheck, file effect and result against other managed writers.
            with transaction(self._conn):
                # Lease validity is rechecked before the facts/policy
                # recheck, mirroring the initial ordering (lease -> policy):
                # task workflow state does not change merely because a
                # lease was taken over, so this is precisely the check
                # nothing in `_facts()` would otherwise catch a takeover
                # against during a long-running IMPLEMENTING operation.
                if not self._leases.is_current(self._lease):
                    raise ToolError("stale_fencing_token")
                current = self._tasks.get(task_id)
                checked, _, latest_hash = self._facts(current, request, exclude=operation_id)
                if self._decision(checked).decision != Decision.ALLOW or latest_hash != owned_hash:
                    raise ToolError("preconditions_changed")
                # `content` is only ever real file bytes for the read_file,
                # non-mutating case (empty for every mutating tool); those
                # bytes are not command output and must never be persisted
                # into the evidence store mislabeled as such (§15) — they
                # are handed to `_finish()` as `read_content` below, which
                # persists them, correctly classified, under the exact
                # `after` digest already computed here, so a caller can
                # later retrieve this exact, already-authorized read
                # without ever reopening the repository file a second time.
                content, after = self._file_effect(
                    Path(manifest["inspection"]["repo_root"]), request, owned_hash, effect,
                )
                if capability.mutation:
                    now = utcnow_iso()
                    if request.tool == "create_file":
                        self._conn.execute(
                            "INSERT INTO task_owned_paths "
                            "(task_id, path, first_owned_at, last_operation_id) "
                            "VALUES (?, ?, ?, ?)",
                            (task_id, request.path, now, operation_id),
                        )
                    else:
                        self._conn.execute(
                            "UPDATE task_owned_paths SET last_operation_id = ? "
                            "WHERE task_id = ? AND path = ?",
                            (operation_id, task_id, request.path),
                        )
                return self._finish(
                    task_id, operation_id, "SUCCEEDED", "completed", b"", b"", after=after,
                    output_hash=after if not capability.mutation else None,
                    read_content=content if not capability.mutation else None,
                )
        except Exception as exc:
            # A failed observation or DB result commit after mutation is
            # uncertain. Never claim FAILED means 'no file change' here.
            # Likewise, an interrupted run_command capture (command_uncertain)
            # cannot be proven FAILED (checkpoint item #2).
            status = "UNKNOWN" if (effect["mutated"] or effect["command_uncertain"]) else "FAILED"
            reason = str(exc) if isinstance(exc, ToolError) else "execution_or_capture_failed"
            with transaction(self._conn):
                return self._finish(task_id, operation_id, status, reason, b"", b"")

    def _event(self, task_id, event_type, payload):
        self._audit.append(
            task_id=task_id, event_type=event_type, actor_type="system",
            actor_id="controlled-tools", payload=payload,
        )

    def _decision(self, facts) -> PolicyResult:
        try:
            result = self._policy.evaluate(facts)
        except Exception:
            return PolicyResult(Decision.DENY, "malformed_policy")
        if (not isinstance(result, PolicyResult) or not isinstance(result.decision, Decision)
                or not isinstance(result.reason, str) or not result.reason):
            return PolicyResult(Decision.DENY, "malformed_policy")
        return result

    def _facts(self, task, request, *, exclude=None):
        loaded_config = json.loads(task.config_json)
        if not isinstance(loaded_config, dict):
            # A JSON list/number/string/bool at the top level has no
            # `.get()`; malformed config must deny, never raise past here.
            raise ToolError("missing_or_malformed_policy")
        config = loaded_config.get("tool_policy")
        if not isinstance(config, dict) or set(config) != {"scope"}:
            raise ToolError("missing_or_malformed_policy")
        if type(config["scope"]) is not list or not config["scope"]:
            raise ToolError("missing_or_malformed_policy")
        scopes = tuple(files.relative_path(p, allow_root=True) for p in config["scope"])
        inspector = InspectionService(self._conn, blobs_dir=self._blobs_dir)
        manifest = inspector.read_manifest(task.task_id)
        metadata = manifest["inspection"]
        if (task.repo_id, task.worktree_id) != (metadata["repo_id"], metadata["worktree_id"]):
            raise ToolError("identity_mismatch")
        roots = [Path(metadata[key]) for key in ("repo_root", "git_dir", "git_common_dir")]
        locations = [self._blobs_dir, *(Path(r["file"]).resolve() for r in self._conn.execute(
            "PRAGMA database_list"
        ) if r["file"])]
        if any(location.is_relative_to(root) for root in roots for location in locations):
            raise ToolError("internal_storage_location")
        if Path(metadata["repo_root"]).resolve() != Path(metadata["repo_root"]):
            raise ToolError("root_changed")
        protection = BaselineRepo(self._conn).protected_paths(task.task_id)
        if protection != manifest["protected_paths"]:
            raise ToolError("baseline_protection_mismatch")
        resource = "." if request.tool == "run_command" else request.path
        for link in metadata["gitlinks"]:
            if files.within(resource, link["path"]):
                raise ToolError("gitlink_boundary")
        exists = False
        if request.tool != "run_command":
            with files.parent_fd(roots[0], resource) as (parent, name):
                info = files.inspect_leaf(parent, name)
                exists = info is not None
                if info and CAPABILITIES[request.tool].mutation and info.st_nlink != 1:
                    raise ToolError("hardlink_mutation_denied")
        row = self._conn.execute(
            "SELECT p.last_operation_id, o.after_evidence, o.status, o.task_id, o.target_resource "
            "FROM task_owned_paths p LEFT JOIN tool_operations o "
            "ON p.last_operation_id = o.operation_id "
            "WHERE p.task_id = ? AND p.path = ? AND p.deleted = 0", (task.task_id, resource),
        ).fetchone()
        if row and (row["status"] != "SUCCEEDED" or row["task_id"] != task.task_id
                    or row["target_resource"] != resource or not row["after_evidence"]):
            raise ToolError("invalid_ownership_evidence")
        owned_hash = row["after_evidence"] if row else None
        pre_existing = exists or any(e["path"] == resource for e in metadata["index_entries"])
        capability = CAPABILITIES[request.tool]
        risk = capability.risk
        # create_file is never eligible for WRITE_EXISTING/approval: an
        # existing or already-owned path is a wrong tool choice the policy
        # engine denies outright (and O_CREAT|O_EXCL would refuse it at the
        # filesystem layer regardless). Only write_file/apply_patch against
        # a path this task does not already own become WRITE_EXISTING.
        if capability.mutation and request.tool != "create_file" and row is None:
            risk = RiskClass.WRITE_EXISTING
        unresolved = self._conn.execute(
            "SELECT 1 FROM tool_operations WHERE worktree_id = ? "
            "AND status IN ('STARTED', 'UNKNOWN') AND operation_id != ? LIMIT 1",
            (task.worktree_id, exclude or ""),
        ).fetchone() is not None
        return PolicyInput(
            task.task_id, task.repo_id, task.worktree_id, TaskState(task.state), request.tool,
            resource, risk, request.network, True,
            any(files.within(resource, scope) for scope in scopes),
            any(files.within(resource, path) for path in protection),
            row is not None, pre_existing, unresolved,
        ), manifest, owned_hash

    @staticmethod
    def _validate_request(request):
        if not isinstance(request, ToolRequest) or not isinstance(request.tool, str):
            raise ToolError("malformed_request")
        if request.tool not in CAPABILITIES:
            raise ToolError("unknown_tool")
        if type(request.network) is not bool:
            raise ToolError("malformed_request")
        if request.tool == "run_command":
            if request.path is not None or request.content is not None or request.hunks:
                raise ToolError("malformed_request")
            validate_command(request.command)
        else:
            files.relative_path(request.path)
            if request.command is not None:
                raise ToolError("malformed_request")
        if request.tool in ("create_file", "write_file"):
            if type(request.content) is not bytes or len(request.content) > files.MAX_FILE_BYTES:
                raise ToolError("invalid_content")
        elif request.content is not None:
            raise ToolError("unexpected_content")
        if request.tool in ("write_file", "apply_patch"):
            if (not isinstance(request.expected_hash, str) or len(request.expected_hash) != 64
                    or any(c not in "0123456789abcdef" for c in request.expected_hash)):
                raise ToolError("expected_content_hash_required")
        elif request.expected_hash is not None:
            raise ToolError("unexpected_content_hash")
        if type(request.hunks) is not tuple or len(request.hunks) > 128:
            raise ToolError("invalid_patch")
        if (request.tool == "apply_patch") != bool(request.hunks):
            raise ToolError("invalid_patch")
        for hunk in request.hunks:
            if (not isinstance(hunk, PatchHunk) or type(hunk.offset) is not int or hunk.offset < 0
                    or type(hunk.before) is not bytes or type(hunk.after) is not bytes):
                raise ToolError("invalid_patch")
        if sum(len(h.before) + len(h.after) for h in request.hunks) > files.MAX_FILE_BYTES:
            raise ToolError("invalid_patch")
        return {
            "path": request.path, "network": request.network,
            "expected_hash": request.expected_hash,
            "content_hash": files.digest(request.content) if request.content is not None else None,
            "content_size": len(request.content) if request.content is not None else None,
            "hunks": [{"offset": h.offset, "before_hash": files.digest(h.before),
                       "after_hash": files.digest(h.after)} for h in request.hunks],
            "command": asdict(request.command) if request.command else None,
        }

    @staticmethod
    def _file_effect(root, request, owned_hash, effect):
        with files.parent_fd(root, request.path) as (parent, name):
            creating = request.tool == "create_file"
            writing = CAPABILITIES[request.tool].mutation
            info = files.inspect_leaf(parent, name)
            if creating and info is not None:
                raise ToolError("path_already_exists")
            flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_RDWR if writing else os.O_RDONLY)
            if creating:
                flags |= os.O_CREAT | os.O_EXCL
            fd = os.open(name, flags, 0o600, dir_fd=parent)
            try:
                effect["mutated"] = creating
                opened = os.fstat(fd)
                current_info = files.inspect_leaf(parent, name)
                if current_info is None or (opened.st_dev, opened.st_ino) != (
                    current_info.st_dev, current_info.st_ino,
                ):
                    raise ToolError("resource_changed")
                if writing and opened.st_nlink != 1:
                    raise ToolError("hardlink_mutation_denied")
                before = files.read_bytes(fd)
                if not writing:
                    return before, files.digest(before)
                if not creating and (files.digest(before) != owned_hash
                                     or request.expected_hash != owned_hash):
                    raise ToolError("owned_content_changed")
                content = files.patch_bytes(before, request.hunks) if (
                    request.tool == "apply_patch"
                ) else request.content
                effect["mutated"] = True
                files.write_bytes(fd, content)
                after = files.digest(files.read_bytes(fd))
                observed = files.inspect_leaf(parent, name)
                if (observed is None or observed.st_ino != opened.st_ino
                        or observed.st_nlink != 1 or after != files.digest(content)):
                    raise ToolError("resource_changed")
                os.fsync(parent)
                return b"", after
            finally:
                os.close(fd)

    def _finish(self, task_id, operation_id, status, reason, stdout, stderr, *, after=None,
                returncode=None, truncated=False, output_hash=None, read_content=None):
        store = ContentStore(self._conn, self._blobs_dir)

        def _store(data: bytes) -> str | None:
            if not data:
                return None
            blob = store.put(
                data, media_type="application/octet-stream", source_kind="command_output",
                exportable=False, truncated=truncated,
            )
            if blob.source_kind != "command_output" or blob.exportable:
                # Deduplication must never let this call silently accept
                # bytes stored earlier under a different, conflicting
                # classification (§15) — fail rather than mislabel evidence.
                raise ToolError("evidence_classification_conflict")
            return blob.content_hash

        if read_content is not None:
            # The exact bytes ToolExecutor itself just read for a read_file
            # operation (§Phase 7.5b) — persisted under its own already-
            # computed `output_hash` digest, so a caller can retrieve this
            # identical, already-authorized read without ever reopening
            # the repository file a second time. Unlike `_store()` above,
            # empty content is still persisted: an empty file is a
            # legitimate read result a caller must still be able to fetch.
            #
            # Unlike `_store()`'s `command_output` classification above,
            # dedup landing on a pre-existing blob under a *different*
            # source_kind (e.g. `rules_snapshot`, from a baseline-time
            # snapshot of a recognized document like README.md whose
            # content is byte-identical) is not a conflict to fail closed
            # on here: content-addressing already guarantees identical
            # bytes for an identical hash, this call never relabels or
            # mutates that existing row, and the file being read is real
            # repository content that a baseline/rules snapshot may
            # entirely legitimately have captured first. What must never
            # happen is this call disagreeing with the digest
            # `_file_effect()` itself already computed for these exact
            # bytes.
            blob = store.put(
                read_content, media_type="application/octet-stream",
                source_kind=READ_EVIDENCE_KIND, exportable=False,
            )
            if blob.content_hash != output_hash:
                # Unreachable in practice -- `output_hash` is `after`,
                # already `digest(read_content)` -- but fail closed rather
                # than trust a broken invariant if that ever changes.
                raise ToolError("evidence_classification_conflict")

        hashes = [output_hash if output_hash is not None else _store(stdout), _store(stderr)]
        metadata = {"reason": reason, "output_hash": hashes[0], "stderr_hash": hashes[1],
                    "returncode": returncode, "truncated": truncated,
                    "reconcile_required": status == "UNKNOWN"}
        self._operations.finish_in_transaction(
            operation_id, status=status, after_evidence=after, result=metadata,
        )
        self._event(task_id, EventType.OPERATION_FINISHED, {
            "operation_id": operation_id, "status": status, "after_evidence": after, **metadata,
        })
        return ToolResult(
            operation_id, Decision.ALLOW, reason, status, *hashes, returncode, truncated,
        )
