"""Cloud-escalation authorization: the explicit, off-by-default gate a
cloud/remote worker's adapter transport must pass before it is ever
invoked (Phase 7.7e — `docs/ROADMAP.md`'s "Cloud escalation
(`disabled`/`manual`/`maintenance`) is implemented here as a policy
dimension, not bolted on later", `docs/CODE_SLAYER_VISION.md` §4 "Normal
runtime vs. cloud escalation").

## The gap this closes

`store.workers_repo.WorkersRepo`/`NetworkClass` (`local`/`cloud`) already
existed, durably, since Phase 1 — but nothing actually *consulted*
`network_class` before `workers.execution.execute_guarded_turn()` called
`adapter.infer()`. A registered `network_class="cloud"` worker could
reach real network transport exactly like a local one: no explicit
escalation decision, no audit trail distinguishing the two. This module
is that missing decision, wired into `execute_guarded_turn()` itself —
the one function every normal application path already goes through to
reach any adapter at all (see that module's own docstring, "Control
plane vs. execution plane").

## Design: explicit, scoped, ephemeral — never a global switch

`CloudEscalationAuthorization` is a plain, caller-constructed value —
never a durable "cloud enabled" row, never a config flag this module
reads on its own. An operator/application caller builds one explicitly,
naming the *exact* `(task_id, worker_id, role)` triple it authorizes, and
passes it into `LocalWorkerRunner.start()`/`resume()` for that one call.
`check_cloud_escalation()` requires an *exact* match against the turn
actually being executed before it is ever honored — an authorization
built for one run, worker, or role never covers a different one, and
nothing here persists an authorization for reuse on a later call (a
resumed run after a crash must be re-authorized explicitly, exactly like
the first attempt — see `runner.local_worker_runner`'s own "no in-memory
shortcut, no durable global switch" posture elsewhere in this codebase).

This is deliberately not sourced from, or influenced by, anything a
model or a `PromptAnalyst` produces: `WorkerResponse`/`PromptAnalysis`
have no field this module ever reads, and nothing here accepts a
`ResolutionEvidence` (a human `QuestionGate` resolution) as if it were
automatically a cloud-transport authorization — those are answers to
different questions asked for different reasons. A caller that wants
cloud escalation must construct a `CloudEscalationAuthorization`
explicitly, through this module's own type, every time.

## Fail-closed network_class handling

`network_class` is read from the SAME `store.workers_repo.WorkersRepo`
row every other Phase 7.2/7.3 trust decision already reads (never
inferred from a model name, a URL string, or free text) — `"local"`
proceeds with no escalation required at all; `"cloud"` requires a
matching authorization; an unknown worker, or a `network_class` value
that is neither `"local"` nor `"cloud"` (a future/malformed value this
module does not understand), is refused — never silently treated as
local.

## Not a filtering/DLP layer

Per this phase's scope, this module enforces only *whether* transport to
a cloud worker may proceed at all — never redacting, filtering, or
inspecting the prompt/context content itself. Denying by default already
satisfies `docs/CODE_SLAYER_VISION.md` §4's "no automatic upload, ever"
invariant for this phase: nothing crosses the transport boundary at all
unless explicitly authorized, so there is nothing yet to filter. Content
inspection/redaction, if ever needed, is a later, separate, explicitly
scoped addition — this module does not attempt to anticipate it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from code_slayer.store.workers_repo import NetworkClass, WorkersRepo


@dataclass(frozen=True)
class CloudEscalationAuthorization:
    """One explicit, narrowly-scoped authorization for exactly one
    bounded worker turn's cloud transport — constructed only by an
    application/operator caller, never by this codebase's own model-
    facing surfaces (`workers.protocol.WorkerAdapter`, `workers.
    prompt_analysis.PromptAnalyst`, `workers.question_gate.QuestionGate`).

    `task_id` is `runner.local_worker_runner.LocalWorkerRunner`'s own
    `run_id` (task and run share identity for a bounded turn — see that
    module's `_setup_execution_plane()`); a direct `execute_guarded_turn()`
    caller outside the runner supplies whatever `task_id` it is actually
    executing under. `check_cloud_escalation()` requires all three fields
    to match the turn actually being executed exactly, or the
    authorization is treated as not applying at all.
    """

    task_id: str
    worker_id: str
    role: str
    reason: str


@dataclass(frozen=True)
class CloudEscalationResult:
    """`ok=True` means transport may proceed (local, or a cloud worker
    with a matching authorization); `ok=False` means it must not — a
    stable, auditable `reason`, never a raised exception, matching this
    codebase's `Decision`/`TrustResult`/`LeaseResult` convention.
    `network_class`, when known, is carried through for the caller's own
    audit payload; `None` only when the worker itself could not be
    resolved at all."""

    ok: bool
    reason: str
    network_class: str | None = None


def check_cloud_escalation(
    conn: sqlite3.Connection, *, task_id: str, worker_id: str, role: str,
    authorization: CloudEscalationAuthorization | None,
) -> CloudEscalationResult:
    """Decide, from durable worker registration alone, whether transport
    to `worker_id` may proceed for this exact `(task_id, role)` turn.

    Pure decision only — no audit side effect and no state mutation;
    `workers.execution.execute_guarded_turn()` is what actually audits
    both outcomes (`EventType.CLOUD_ESCALATION_EVALUATED`) and, on
    denial, returns before ever calling `adapter.infer()`.
    """
    worker = WorkersRepo(conn).get(worker_id)
    if worker is None:
        return CloudEscalationResult(False, "unknown_worker")
    if worker.network_class == NetworkClass.LOCAL:
        return CloudEscalationResult(
            True, "local_network_class_no_escalation_required", network_class=worker.network_class,
        )
    if worker.network_class != NetworkClass.CLOUD:
        # A value this module does not recognize at all -- never guessed
        # as either local or cloud, never treated as implicitly safe.
        return CloudEscalationResult(
            False, "unknown_network_class", network_class=worker.network_class,
        )
    if authorization is None:
        return CloudEscalationResult(
            False, "no_cloud_escalation_authorization", network_class=worker.network_class,
        )
    if not isinstance(authorization, CloudEscalationAuthorization):
        return CloudEscalationResult(
            False, "malformed_cloud_escalation_authorization", network_class=worker.network_class,
        )
    if (
        authorization.task_id != task_id
        or authorization.worker_id != worker_id
        or authorization.role != role
    ):
        # An authorization for a different run, worker, or role can never
        # silently cover this one.
        return CloudEscalationResult(
            False, "cloud_escalation_authorization_scope_mismatch",
            network_class=worker.network_class,
        )
    return CloudEscalationResult(
        True, "cloud_escalation_authorized", network_class=worker.network_class,
    )
