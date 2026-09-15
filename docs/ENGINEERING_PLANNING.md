# Engineering Planning (Phase 8.2)

Transforms an original user engineering request plus authoritative
[Repository Intelligence](REPOSITORY_INTELLIGENCE.md) evidence into a
structured, durable, evidence-backed engineering plan.

**This is planning only.** No repository mutation, no model write
tools, no command execution, no checkpoint creation, no merge/
promotion, no `AUTO` trust. `planning.service.EngineeringPlanningService`
imports no `tools.executor.ToolExecutor`, `policy.engine.PolicyEngine`,
`lease.manager.LeaseManager`, `repo.checkpoint`, or `workers.trust`
promotion path — a plan this module produces is structurally incapable
of authorizing execution, not merely conventionally forbidden from it.
`READY` means a plan is internally consistent and evidence-backed; it
never means execution is authorized.

## Core invariant

A model-generated claim is not repository fact. The planner may propose
intent, design, implementation strategy, risks, and file changes, but
any concrete repository claim — "this file exists," "this command is
discovered," "this symbol exists" — must be validated against
authoritative [Repository Intelligence](REPOSITORY_INTELLIGENCE.md)
evidence (`planning.evidence.validate_plan_against_intelligence`) before
it can become part of a `READY` plan.

## Pipeline

```
original engineering request
      |
      v
persistent plan revision record (planning.service, control-plane database)
      |
      v
RepositoryIntelligenceService.inspect()   (Phase 8.1/8.1a — authoritative
      |                                    repository binding + evidence)
      v
Planner.plan()                            (planning.planner — bounded
      |                                    context in, structured claim out)
      v
planning.evidence.validate_plan_against_intelligence()
      |
      v
QuestionGate.evaluate()                    (workers.question_gate,
      |                                     reused completely unmodified)
      +-- ASK -> durable NEEDS_INPUT
      |
      +-- SUPPRESS -> durable READY
```

## Plan model

`planning.models.EngineeringPlanContent` is the complete,
evidence-validated planning content for one plan revision: `goal`,
`requirements`, `assumptions`, `affected_files`, `planned_changes`,
`dependencies`, `risks`, `verification_steps`, `discovered_commands`,
`authority_requirements`, `open_questions`, `evidence_refs`, and
`validation_issues`. It is persisted content-addressed
(`planning.provenance`, `source_kind="engineering_plan_content"`);
`store.migrations.0008_engineering_planning`'s `engineering_plans`
table holds only a small, mutable pointer row — repository binding
(`repo_id`, `worktree_id`, `head_sha`, `working_tree_dirty`,
`working_tree_fingerprint`, `intelligence_snapshot_id`), the request's
content hash, revision/predecessor linkage, `state`/`reason`, and every
content-hash pointer (planner input, planner's raw output, evidence
validation result, final validated content) — mirroring
`repository_intelligence_snapshots`/`runner_runs`'s own "thin row, large
content-addressed blob" convention.

Each `AffectedFile` carries `path`, `action` (`inspect`/`modify`/
`create`/`delete`), `reason`, and — set exclusively by evidence
validation, never by the planner — `exists_in_repository` plus
`evidence` (`EvidenceRef` citations back to the exact snapshot that
proved it). An existing-file claim never becomes authoritative merely
because the planner named it.

## Plan states

`DRAFT` | `NEEDS_INPUT` | `READY` | `SUPERSEDED`
(`planning.models.PlanState`) — a small state model, layered above and
never overloading `core.states.TaskState` or `runner.
local_worker_runner.RunStatus`.

`DRAFT` covers both "not yet evaluated" and "the planner's own claim
contradicted authoritative repository fact" (`reason=
"evidence_validation_failed"`) — the latter can only be corrected by a
fresh `replan()`, never a silent auto-repair. `NEEDS_INPUT` means the
Question Gate found at least one unresolved blocking ambiguity. `READY`
means every required field is valid, every repository claim resolved,
no blocking open question remains, and the repository binding was
current *at the moment this was computed* — it does not mean execution
is authorized. `SUPERSEDED` means a later revision (`replan()`)
replaced this one; the row and everything it references remain
readable forever.

**`STALE` is deliberately not a stored state.** A `READY` row whose own
recorded `(head_sha, working_tree_fingerprint)` no longer matches the
repository's real, current identity is reported as `effective_state ==
"STALE"` by every read — computed fresh every time via
`RepositoryIntelligenceService.is_identity_current()` (a small, additive
Phase 8.2 method that reuses `intelligence.builder.probe_identity()`
exactly as `.status()`/`.query()` already do), never durably rewritten.
Durably transitioning to `STALE` would mean either rewriting rows
nobody asked about, or letting a row silently go stale without anything
ever updating it — both duplicate authority
`RepositoryIntelligenceService` already holds.

## Durability / revision history

Plans survive a process restart: `PlanningRepo`
(`store.planning_repo`) is a thin, transactional wrapper over
`engineering_plans`/`engineering_plan_human_resolutions`, mirroring
`RunnerRepo`'s own "identity fields locked, evolving fields mutable"
discipline. `replan()` always inserts a new row (`predecessor_plan_id`,
`revision = predecessor.revision + 1`) over the *same* original request
and transitions the predecessor to `SUPERSEDED` — it never rewrites or
deletes the predecessor's content. `resume()` never re-invokes the
planner; its structured output is durable state, read back
content-addressed, exactly like `runner.local_worker_runner.
LocalWorkerRunner.resume()` never re-invokes the Prompt Analyst merely
to continue.

## Planner boundary

`planning.planner.Planner` is a minimal `typing.Protocol`
(`plan(PlannerRequest) -> PlannerResponse`) — provider-neutral, no
provider name anywhere in this package. `PlannerRequest` carries only
bounded context: the original request, selected `ProjectEvidence`/
`CommandCandidate` evidence, and a bounded `ContextPack` — never the
whole repository. `PlannerStructuredOutput` reuses `workers.
prompt_analysis.Ambiguity`/`AmbiguityRiskClass` unchanged for planner-
raised ambiguity, so the exact same hardened `workers.question_gate.
QuestionGate` decides SUPPRESS/ASK over a planning ambiguity exactly as
it does over a prompt-analysis one.

`planning.worker_planner.WorkerAdapterPlanner` is the one production
bridge to the existing worker/model transport: it builds one
`workers.protocol.WorkerRequest` (`tool_requirement=REQUIRED`,
`allowed_tools=("emit_engineering_plan",)`), calls the same
`WorkerAdapter.infer()` every real/fake worker already implements, and
validates the response through the same `workers.protocol_validation.
validate_response()` every bounded worker turn already goes through —
including its reserved tool-call-transport-marker detection, which is
what stops a model from embedding a tool call inside plain text to
bypass this protocol. `planning.planner.parse_planner_output()` then
does strict, whole-shape schema validation of the resulting structured
payload — an unrecognized field, wrong type, or non-mapping/non-list
shape rejects the whole thing (`PlannerOutcome.MALFORMED`), never a
partial reconstruction or a heuristic parse of free text.

`planning.fake_planner.FakePlanner` is a deterministic, fully offline
test double mirroring `workers.fake_prompt_analyst.FakePromptAnalyst`.
Production planning with no `Planner` configured fails explicitly
(`APIError("planner_not_configured", ...)`, HTTP `503`) — it never
silently falls back to a fake.

## Evidence validation

`planning.evidence.validate_plan_against_intelligence(output, snapshot)`
checks every concrete claim in a `PlannerStructuredOutput` against an
already-built `intelligence.models.Snapshot`:

- **Affected files**: `create` against a path that already exists, or
  `inspect`/`modify`/`delete` against a path that does not exist, is a
  genuine defect — `blocking=True`, and the plan cannot reach `READY`
  without a fresh `replan()`. `create` against a genuinely absent path
  is a legitimate proposal, tagged `exists_in_repository=False` with a
  `"file_absent"` evidence reference (itself real evidence: the
  snapshot was checked and the path was confirmed absent).
- **Discovered commands**: only a `command` string that exactly matches
  an authoritative `CommandCandidate` survives, and it is replaced by
  that candidate's own `purpose`/`evidence_source` — never the
  planner's own (possibly fabricated) description. An unmatched command
  is dropped with an advisory issue — never blocking, and never
  executed by anything in this package.
- **Evidence claims** (`file_exists`/`symbol_exists`/
  `command_discovered`): checked directly against the snapshot's files/
  symbols/commands; an unsupported claim is dropped with an advisory
  issue, never blocking and never promoted.

## Question Gate integration

`planning.service` wraps a planner's own `Ambiguity` list in a
throwaway `workers.prompt_analysis.PromptAnalysis(original_prompt=
original_request, ambiguities=...)` purely as the adapter shape
`QuestionGate.evaluate()` already expects — no second ambiguity-
resolution engine, no broadened suppression authority. Durable user
resolutions are recorded exactly like `runner.local_worker_runner.
LocalWorkerRunner.record_user_resolution()`
(`engineering_plan_human_resolutions`, append-only, content-addressed
answer text) and re-evaluated on `resume()`.

## Training / evaluation provenance

`planning.provenance` persists five distinct, content-addressed
documents per planning attempt — the original request, the bounded
planner input, the planner's raw structured output, the evidence
validation result, and the final validated plan content — so a future
training/evaluation export can reconstruct exactly what was asked, what
context the planner received, what it said, what evidence validation
did with that, and what final plan resulted, without duplicating raw
repository content (facts are cited by path/snapshot_id reference,
never re-embedded as bytes). Training itself is out of scope for this
phase.

## HTTP API

See [`WEBUI_API.md`](WEBUI_API.md#engineering-planning-phase-82) for
`GET/POST /api/plans`, `GET /api/plans/{plan_id}`, `POST /api/plans/
{plan_id}/resume`, `POST /api/plans/{plan_id}/replan`, and `POST
/api/plans/{plan_id}/resolutions`.

## Known limitations

- `replan()` does not automatically carry forward an earlier revision's
  durable human resolutions — a fresh planner call may raise entirely
  different ambiguities anyway; a caller may still pass ephemeral
  `resolutions=` explicitly.
- No deterministic finalization gates or independent review yet (later
  Phase 8 slices) — this phase produces a plan, never a verified diff.
- No execution path exists that consumes a `READY` plan; that is a
  deliberately separate, later decision.
