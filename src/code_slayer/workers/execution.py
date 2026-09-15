"""Bounded, single-turn orchestration: a real `WorkerAdapter` inference,
through Code Slayer's actual production security/tool stack, for exactly
one qualified, read-only capability (Phase 7.5a/7.5b — `docs/ROADMAP.md
#local-worker-runtime`, `docs/CODE_SLAYER_VISION.md` §40-43, §58).

## What this module is

The first place a real, structurally-validated `WorkerToolCall` is ever
allowed to reach the real `PolicyEngine`/`ToolExecutor` — not a scratch
driver, not a fake result. It coordinates five already-existing,
independently-tested boundaries without weakening or duplicating any of
them:

- `WorkerAdapter` — provider transport/mapping (unmodified)
- `protocol_validation.validate_response()` — structural validity only
  (unmodified; still the sole authority on TEXT/TOOL_CALL/MALFORMED/
  UNAUTHORIZED_CAPABILITY, still never parses leaked textual protocol)
- `workers.trust.WorkerTrustManager` — whether this exact
  `(worker_id, role, capability)` scope has earned authority
  (unmodified)
- `policy.engine.PolicyEngine` (via `tools.executor.ToolExecutor`) —
  whether this specific requested operation is allowed in this task's
  concrete context (unmodified)
- `tools.executor.ToolExecutor` — actual controlled execution plus
  durable evidence/audit (unmodified)

This module only wires them together and adds the smallest additional
durable evidence needed to attribute a turn to the Phase 7 concepts
(`worker_id`/`role`/capability/trust) that none of the above already
know about.

## Scope: read_file only, one bounded turn

Only `read_file` is ever offered (`_ALLOWED_TOOLS`) — the sole
capability any worker can currently be `GUARDED` for (`workers.
conformance.PROMOTABLE_CAPABILITIES`). Extending this is a deliberate
future code change, never something a request or a model can do. At
most one tool call is ever executed per turn, and at most one
continuation inference follows it — never a recursive/autonomous loop;
a second tool request in the continuation is refused, not executed.

## The read_file content problem, and how this module solves it

Phase 7.5a's original approach here — re-opening the target repository
file a second time after `ToolExecutor.execute()` had already finished,
then hash-verifying that second read against `ToolResult.output_hash` —
was fail-safe (a mismatch was still caught) but left an unnecessary
TOCTOU/provenance boundary: the bytes the worker actually consumed came
from a *different* filesystem read than the one `ToolExecutor` itself
authorized and audited, open to a window between the two reads.

Phase 7.5b closes that boundary from the other side, inside
`ToolExecutor` itself (`tools/executor.py`'s `_finish()`/`_file_effect()`):
a `read_file` operation's exact bytes are now persisted into the same
content-addressed `ContentStore` every other piece of durable evidence
already lives in, classified `source_kind="tool_read_output"`, under
the exact digest `ToolResult.output_hash` already carries. The
`evidence_content()` helper below retrieves that already-persisted
blob by that hash — it never touches the repository a second time, and
it is not a second, independent trust decision: every actual
authorization (lease, policy, ownership, baseline, scope) already came
from the one real `ToolExecutor.execute()` call that produced this
evidence. `evidence_content()` still recomputes and compares the
retrieved bytes' own digest against the expected hash before ever
handing anything to the model — fail closed
(`evidence_verification_failed`) on a missing blob, a read error
against the blob store, or a digest mismatch. It deliberately does not
require the retrieved blob's `source_kind` to still read
`tool_read_output`: `ContentStore.put()`'s own dedup may legitimately
land this exact hash on a blob some other trusted evidence path already
stored first (e.g. a baseline-time `rules_snapshot` of a recognized
document like README.md whose content is byte-identical) — see
`ToolExecutor._finish()`'s own comment on this. What establishes trust
is that `expected_hash` came from the one real `ToolExecutor.execute()`
call, never the label on whichever row dedup happened to land on. No
filesystem fallback to the repository is ever attempted.

## Trust gate

`current_trust(worker_id, role, capability)` is checked only once a
structurally valid `WorkerToolCall` actually names a capability — never
before inference (an untrusted worker may still safely produce a plain
`TEXT` response), and never generalized beyond the exact capability
requested (`read_file` trust never authorizes `run_command`/
`create_file`/`write_file`/`apply_patch`/`checkpoint_create`, and
`capability=None` role-level trust is never consulted). `GUARDED` or
`AUTO` both authorize; `LOCKED` denies before `ToolExecutor` is ever
constructed.

## Automatic downgrade: narrow, deterministic, and why

A severe, unambiguous worker-side protocol violation — raw textual
tool-call syntax leaking into `TEXT` content
(`protocol_validation`'s `textual_tool_protocol_leakage` reason,
the exact 2026-09-14 failure class) — occurring during a turn where
this worker was actually being trusted to use `read_file` calls
`WorkerTrustManager.downgrade_to_locked()` for that exact
`(worker_id, role, "read_file")` scope, using the existing, unmodified
Phase 7.2 API (a no-op if the scope was not `GUARDED`/`AUTO` to begin
with — `downgrade_to_locked()`'s own re-check already handles that
safely). No other `MALFORMED` reason triggers this (an adapter-side
shape like `no_choices_in_response` says nothing about the worker's own
behavior), and `UNAUTHORIZED_CAPABILITY` (a well-formed request for a
tool that was simply never offered) does not either — that is normal,
expected containment, not evidence of a broken worker.

## Control plane vs. execution plane (Phase 7.7)

`trust_conn` (default: `conn`, unchanged single-connection behavior)
lets a caller supply the exact `(worker_id, role, capability)` trust
history from a *different* database than the one `ToolExecutor`/
`AuditWriter` write execution evidence into — `runner.
local_worker_runner.LocalWorkerRunner`'s own control-plane/execution-
plane separation: durable worker trust is control-plane evidence (Phase
7.2/7.3) that must survive independently of whichever disposable job
worktree a given turn's execution happens to use, so it is never copied
into that job worktree's own database. `current_trust()` and
`downgrade_to_locked()` both read/write `trust_conn` exclusively; every
other call in this module (`ToolExecutor`, `AuditWriter`) still uses
`conn`. Passing the same connection for both (the default) reproduces
Phase 7.5a/7.5b's original, still fully supported, single-database
behavior exactly.

## Durable evidence

No parallel audit system. The real execution's own evidence
(`TOOL_REQUESTED`/`POLICY_EVALUATED`/`OPERATION_STARTED`/
`OPERATION_FINISHED`, `tool_operations` row, content-addressed hashes)
comes entirely from the unmodified `ToolExecutor`. This module adds
exactly two new, minimal `EventType` members
(`WORKER_TOOL_CALL_EVALUATED`, `WORKER_TURN_FINISHED`) to attribute a
turn to the Phase 7 concepts `ToolExecutor`'s own audit trail has no
notion of — worker_id, role, and the protocol/trust decisions made
before ever reaching it — via the same `AuditWriter` every other module
in this codebase uses. No new event store, no schema change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.lease.manager import LeaseHandle
from code_slayer.policy.engine import Decision
from code_slayer.store.content_store import ContentStore
from code_slayer.tools import file_tools as files
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import ToolRequest, ToolResult
from code_slayer.workers.protocol import (
    ToolRequirement,
    WorkerAdapter,
    WorkerAdapterError,
    WorkerRequest,
    WorkerToolResult,
)
from code_slayer.workers.protocol_validation import ValidationOutcome, validate_response
from code_slayer.workers.trust import TrustLevel, WorkerTrustManager

# The complete, fixed set of capabilities a bounded worker turn can ever
# offer -- code-owned, never request data. `read_file` is the only
# capability any worker can currently be GUARDED for
# (`workers.conformance.PROMOTABLE_CAPABILITIES`); extending this set is
# a deliberate future code change alongside real GUARDED evidence for
# whatever is added, never something this slice does implicitly.
_ALLOWED_TOOLS: tuple[str, ...] = ("read_file",)

# A worker's output_summary is a short, caller-prepared summary (see
# `workers.protocol.WorkerToolResult`), never the full raw content
# unbounded -- matches the 1 MiB ceiling `tools.file_tools.MAX_FILE_BYTES`
# already enforces on the file itself, bounded further here to keep the
# continuation prompt itself bounded.
_MAX_SUMMARY_CHARS = 4000

_TRUSTED_LEVELS = frozenset({TrustLevel.GUARDED, TrustLevel.AUTO})

# The one MALFORMED reason severe and unambiguous enough to trigger an
# automatic GUARDED -> LOCKED downgrade -- see the module docstring's
# "Automatic downgrade" section for why no other reason qualifies.
_DOWNGRADE_TRIGGERING_REASON = "textual_tool_protocol_leakage"


@dataclass(frozen=True)
class TurnOutcome:
    """The complete, bounded result of one worker turn. Never raises for
    an ordinary denial/violation/limit -- `ok=False` with a stable
    `reason` covers every one of those; only a genuinely unexpected
    programming error propagates as a real exception."""

    ok: bool
    reason: str
    final_text: str | None = None
    executed: bool = False
    tool_result: ToolResult | None = None
    downgraded: bool = False


def _emit(conn: sqlite3.Connection, task_id: str, worker_id: str, event_type, payload) -> None:
    AuditWriter(conn).append(
        task_id=task_id, event_type=event_type, actor_type="worker",
        actor_id=worker_id, payload=payload,
    )


def evidence_content(
    conn: sqlite3.Connection, blobs_dir: Path | str, expected_hash: str | None,
) -> bytes | None:
    """Fetch the exact bytes the real `ToolExecutor.execute()` call
    already read and durably persisted for this operation -- addressed
    solely by the `output_hash` that same call already produced. Never
    reopens the repository file: this is a lookup against the same
    content-addressed `ContentStore` `ToolExecutor` itself just wrote
    the evidence into, not a second, independent trust decision.

    Public (Phase 7.7): `runner.local_worker_runner.LocalWorkerRunner`'s
    mid-turn crash-recovery path reuses this exact same lookup to
    reconstruct a prior turn's `WorkerToolResult` from durable evidence
    alone, never by re-deriving it a different way — one evidence
    boundary, one implementation.

    `None` on any failure -- no such blob, an unreadable/corrupted blob
    file, or a digest mismatch -- fail closed, never a guess, and never a
    filesystem fallback to the repository. Deliberately does not require
    the blob's `source_kind` to be `READ_EVIDENCE_KIND` specifically:
    `ToolExecutor`'s own dedup may legitimately have landed this exact
    hash on a blob some other trusted evidence path stored first (e.g. a
    baseline-time `rules_snapshot` of a recognized document like
    README.md whose content happens to be byte-identical) -- see
    `tools.executor.ToolExecutor._finish()`'s comment on this. What
    actually establishes trust here is that `expected_hash` itself came
    from the one real, already-authorized `ToolExecutor.execute()` call
    above, not this blob's label."""
    if not isinstance(expected_hash, str):
        return None
    store = ContentStore(conn, blobs_dir)
    try:
        content = store.read(expected_hash)
    except (KeyError, OSError):
        return None
    if files.digest(content) != expected_hash:
        return None
    return content


def execute_guarded_turn(
    conn: sqlite3.Connection, adapter: WorkerAdapter, *, task_id: str, worker_id: str,
    role: str, original_prompt: str, lease: LeaseHandle, blobs_dir: Path | str,
    trust_conn: sqlite3.Connection | None = None,
) -> TurnOutcome:
    """Run one bounded turn: inference -> validation -> trust gate ->
    (at most one) real `ToolExecutor.execute()` -> verified content ->
    one continuation inference -> a final validated non-tool response.

    `trust_conn` (default `conn`) is consulted for every trust read/
    write in this turn — see the module docstring's "Control plane vs.
    execution plane" section; `conn` remains the execution-plane
    connection `ToolExecutor`/`AuditWriter` use regardless.

    Never executes a second tool request, never recurses, never parses
    or recovers leaked textual tool-call syntax -- that responsibility
    stays entirely with the already-existing, unmodified
    `protocol_validation.validate_response()`."""
    trust = WorkerTrustManager(trust_conn if trust_conn is not None else conn)

    def _downgrade(capability: str, reason: str) -> bool:
        return trust.downgrade_to_locked(
            worker_id=worker_id, role=role, capability=capability, reason=reason,
        ).ok

    def _finish(outcome: str, turn_outcome: TurnOutcome, **extra) -> TurnOutcome:
        _emit(conn, task_id, worker_id, EventType.WORKER_TURN_FINISHED, {
            "worker_id": worker_id, "role": role, "outcome": outcome,
            "executed": turn_outcome.executed,
            "operation_id": turn_outcome.tool_result.operation_id
            if turn_outcome.tool_result else None,
            **extra,
        })
        return turn_outcome

    request = WorkerRequest(
        task_id=task_id, role=role, original_prompt=original_prompt,
        allowed_tools=_ALLOWED_TOOLS, tool_requirement=ToolRequirement.OPTIONAL,
    )
    try:
        response = adapter.infer(request)
    except WorkerAdapterError as exc:
        return _finish("transport_failure", TurnOutcome(False, f"transport_failure:{exc}"))

    result = validate_response(request, response)
    _emit(conn, task_id, worker_id, EventType.WORKER_TOOL_CALL_EVALUATED, {
        "worker_id": worker_id, "role": role, "turn": "initial",
        "protocol_outcome": result.outcome.value, "reason": result.reason,
        "capability": result.tool_call.tool if result.tool_call is not None else None,
    })

    if result.outcome == ValidationOutcome.VALID_TEXT:
        return _finish(
            "final_text_no_tool_used", TurnOutcome(True, "final_text_no_tool_used",
                                                     final_text=result.text),
        )

    if result.outcome == ValidationOutcome.MALFORMED:
        downgraded = (
            result.reason == _DOWNGRADE_TRIGGERING_REASON
            and _downgrade("read_file", "malformed_tool_call_protocol_during_execution")
        )
        return _finish(
            "protocol_violation",
            TurnOutcome(False, f"protocol_violation:{result.reason}", downgraded=downgraded),
            reason=result.reason, downgraded=downgraded,
        )

    if result.outcome == ValidationOutcome.UNAUTHORIZED_CAPABILITY:
        return _finish("capability_not_offered", TurnOutcome(False, "capability_not_offered"))

    # VALID_TOOL_CALL -- validate_response already guarantees
    # tool_call.tool is a member of allowed_tools, i.e. exactly "read_file".
    tool_call = result.tool_call
    trust_level = trust.current_trust(worker_id, role, tool_call.tool)
    if trust_level not in _TRUSTED_LEVELS:
        return _finish(
            "trust_denied", TurnOutcome(False, f"trust_denied:{trust_level.value}"),
            capability=tool_call.tool, trust_level=trust_level.value,
        )

    tool_request = ToolRequest(tool=tool_call.tool, path=tool_call.params.get("path"))
    executor = ToolExecutor(conn, blobs_dir=blobs_dir, lease=lease)
    tool_result = executor.execute(task_id, tool_request)

    if tool_result.decision != Decision.ALLOW or tool_result.status != "SUCCEEDED":
        return _finish(
            "policy_or_execution_denied",
            TurnOutcome(False, f"execution_denied:{tool_result.reason}", tool_result=tool_result),
            capability=tool_call.tool, decision=tool_result.decision, status=tool_result.status,
            reason=tool_result.reason,
        )

    content = evidence_content(conn, blobs_dir, tool_result.output_hash)
    if content is None:
        return _finish(
            "evidence_verification_failed",
            TurnOutcome(
                False, "evidence_verification_failed", executed=True, tool_result=tool_result,
            ),
            capability=tool_call.tool,
        )

    summary = content.decode("utf-8", errors="replace")
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS] + "\n...(truncated)"
    prior = WorkerToolResult(tool=tool_call.tool, output_summary=summary)

    request2 = WorkerRequest(
        task_id=task_id, role=role, original_prompt=original_prompt,
        allowed_tools=_ALLOWED_TOOLS, prior_tool_result=prior,
        tool_requirement=ToolRequirement.OPTIONAL,
    )
    try:
        response2 = adapter.infer(request2)
    except WorkerAdapterError as exc:
        return _finish(
            "continuation_transport_failure",
            TurnOutcome(
                False, f"continuation_transport_failure:{exc}",
                executed=True, tool_result=tool_result,
            ),
        )

    result2 = validate_response(request2, response2)
    _emit(conn, task_id, worker_id, EventType.WORKER_TOOL_CALL_EVALUATED, {
        "worker_id": worker_id, "role": role, "turn": "continuation",
        "protocol_outcome": result2.outcome.value, "reason": result2.reason,
    })

    if result2.outcome == ValidationOutcome.VALID_TEXT:
        return _finish(
            "final_text",
            TurnOutcome(
                True, "final_text", final_text=result2.text, executed=True,
                tool_result=tool_result,
            ),
        )

    if result2.outcome == ValidationOutcome.VALID_TOOL_CALL:
        # Bounded single-turn model: a second tool request is never
        # executed, recursively or otherwise.
        return _finish(
            "bounded_turn_limit_reached",
            TurnOutcome(
                False, "bounded_turn_limit_reached", executed=True, tool_result=tool_result,
            ),
            second_tool_requested=result2.tool_call.tool,
        )

    if result2.outcome == ValidationOutcome.MALFORMED:
        downgraded = (
            result2.reason == _DOWNGRADE_TRIGGERING_REASON
            and _downgrade("read_file", "malformed_tool_call_protocol_during_execution")
        )
        return _finish(
            "continuation_protocol_violation",
            TurnOutcome(
                False, f"continuation_protocol_violation:{result2.reason}",
                executed=True, tool_result=tool_result, downgraded=downgraded,
            ),
            reason=result2.reason, downgraded=downgraded,
        )

    # UNAUTHORIZED_CAPABILITY on the continuation -- same allowed_tools,
    # so this should not normally occur, but is handled the same
    # fail-closed way regardless.
    return _finish(
        "continuation_capability_not_offered",
        TurnOutcome(
            False, "continuation_capability_not_offered", executed=True, tool_result=tool_result,
        ),
    )
