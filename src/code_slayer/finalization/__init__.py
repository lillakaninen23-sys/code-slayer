"""Code-owned job finalization (Deterministic Finalization phase).

No worker or reviewer model may declare a mutating job done by itself.
This package computes FINAL / REPAIR_REQUIRED / BLOCKED / INVALID_ENVIRONMENT
from verifiable runtime evidence (durable `tool_operations` rows produced by
actually executing repository-grounded, code-verified commands) and durable
provenance (checkpoints, audit history) — never from a worker's or model's
own self-report.

`INVALID_ENVIRONMENT` is not a new `TaskState`: it is a finalizer verdict and
audit reason code, expressed at the task-state level as the existing
`TaskState.BLOCKED` with a structured `reason_code` and the state machine's
own existing resume-origin mechanics (`core.transitions`) — never a parallel
state machine. See `docs/` for the approved design (Deterministic
Finalization audit).

Submodules:

- `types` — verdicts, evidence/result dataclasses, the (currently
  unimplemented) reviewer input contract (`ReviewEvidence`).
- `verification` — grounds candidate verification commands against the
  *live* repository (never against a planner's own asserted text) and
  executes only a fixed, code-owned, safe subset of them, recording durable
  `tool_operations` evidence for each.
- `policy` — pure, deterministic decision functions over verified evidence.
- `service` — orchestrates the above into real `TaskStateMachine`
  transitions, plus the `CHECKPOINTED -> COMPLETED` transition guard.
"""
