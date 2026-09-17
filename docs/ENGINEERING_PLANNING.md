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
bypass this protocol. Native structured `tool_calls` always take
precedence. A leaked textual protocol is **fail-closed by default**
(`PlannerOutcome.MALFORMED`, `NON_TOOL_RESPONSE`); an operator may
explicitly enable a strict, deterministic Tool Protocol Compatibility
Layer (`workers.protocol_normalization` / `workers.
qwen_textual_tool_normalizer`) for a named `(normalizer_id,
normalizer_version)` pair, which accepts only one exact grammar and
then re-enters the same `validate_response()` +
`parse_planner_output()` path — never a fuzzy/LLM repair, never a
model-selected decoder, and never a silent change to runtime-profile
identity or certificates. Native vs. normalized behavior is recorded
distinctly in `planning.provenance.store_planner_output` via
`PlannerResponse.tool_call_transport`. For a `NORMALIZED` turn the
original provider/model textual payload is persisted as a distinct,
internal-only, non-exportable content-addressed blob (`source_kind=
"engineering_plan_original_transport"`), referenced from the planner-
output document by hash; canonical structured params remain in
`PlannerResponse.raw`. Neither is surfaced through `PlanRecord` or the
HTTP API, and neither influences permissions, trust, certification,
parser acceptance, or execution authority. `planning.planner.
parse_planner_output()` then does strict, whole-shape schema validation
of the resulting structured payload — an unrecognized field, wrong
type, or non-mapping/non-list shape rejects the whole thing
(`PlannerOutcome.MALFORMED`), never a partial reconstruction or a
heuristic parse of free text.

`planning.fake_planner.FakePlanner` is a deterministic, fully offline
test double mirroring `workers.fake_prompt_analyst.FakePromptAnalyst`.
Production planning with no `Planner` configured fails explicitly
(`APIError("planner_not_configured", ...)`, HTTP `503`) — it never
silently falls back to a fake.

## Planner-turn context budget (Phase 8.2b)

A live production planning turn against a real local model (Devstral,
over `OpenAICompatibleAdapter`) sent a durable planner input of
**151211 bytes** with no explicit bound at all — `planning.service` was
handing `RepositoryIntelligenceService.build_context_pack()` no
`max_files`/`max_bytes`/`per_file_bytes` arguments, so it silently fell
back to `intelligence.limits`'s own *indexing-pipeline* defaults
(sized for a general-purpose context consumer, not one planner turn's
prompt). Faced with that much prompt content, the model ignored the
required structured `emit_engineering_plan` tool call entirely and
emitted ~4 KB of free-form implementation prose instead, containing
unsupported/hallucinated repository claims — observed as
`invalid_transport_response:text_response`, twice.

`planning.limits` now defines explicit, centralized, deterministic
planner-turn bounds, deliberately separate from and tighter than
`intelligence.limits`'s own indexing defaults:

| Constant | Value |
| --- | --- |
| `PLANNER_MAX_FILES` | 8 |
| `PLANNER_MAX_TOTAL_FILE_BYTES` | 32768 |
| `PLANNER_MAX_PER_FILE_BYTES` | 8192 |

`planning.service._run_planning_attempt()` passes these explicitly to
`build_context_pack()` — never the Repository Intelligence query
defaults. Byte limits only, never token estimation: tokenization is
model/tokenizer-specific and non-deterministic across providers, so it
can never be an authoritative boundary; `intelligence.query.
build_context_pack()`'s own byte accounting (already deterministic and
tested) is reused unchanged, never duplicated.

Selection itself is unchanged from Phase 8.1: `intelligence.query.
rank()`'s existing weighted order (exact path/symbol matches highest,
then associated tests, then import neighbors, then generic project-
config relevance) decides which evidence is *offered* to the budget;
this phase only tightens how much of that already-ranked evidence one
planner turn is ever handed, never re-ranks it.

`planning.planner.render_bounded_context()` is the one, shared,
deterministic JSON-safe view of a `PlannerRequest` — used identically
by `WorkerAdapterPlanner` (what is actually sent to the model) and
`planning.provenance.store_planner_input()` (what is durably recorded),
so the two can never silently drift apart. It deliberately excludes
`context_pack.projects`/`.commands` (`intelligence.query.
build_context_pack()` copies `Snapshot.projects`/`.commands` onto every
`ContextPack` for unrelated callers' convenience; a planner turn already
receives those same facts exactly once via `PlannerRequest.
repo_context`/`.discovered_commands` — repeating them a second time
nested inside `context_pack` would be pure, unbounded-growth
duplication) and the full `context_pack.omitted` path list (low-ranked/
excluded files carry no positive evidence value; only their count is
retained).

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

## Failure provenance (Phase 8.2b)

`planning.planner.PlannerFailureCategory` is a stable, coarse,
code-owned taxonomy of why one planning turn did not produce
`PlannerOutcome.STRUCTURED`, set on `PlannerResponse.failure_category`
by `WorkerAdapterPlanner`:

- `TRANSPORT_ERROR` — the call to the model never produced a response
  at all (`workers.protocol.WorkerAdapterError`: network/timeout/HTTP-
  status/malformed-JSON failure at the transport layer).
- `NON_TOOL_RESPONSE` — a response arrived, but it was not a valid,
  authorized `emit_engineering_plan` tool call (plain `TEXT` — exactly
  the observed live failure — textual tool-call-transport-syntax
  leakage, a tool call naming something else, or an unauthorized/
  malformed tool call).
- `SCHEMA_INVALID` — the model *did* make a genuine, authorized
  `emit_engineering_plan` tool call, but its own `params` failed
  `parse_planner_output()`'s strict schema check.

`planning.service` persists only this coarse category into the plan's
durable, HTTP-visible `reason` field (`"malformed_planner_output:
transport_error"` / `"...non_tool_response"` / `"...schema_invalid"`)
— never `PlannerResponse.error`/`.raw` (the adapter's own raw text or
tool-call params). The full detail remains durable internally only,
in the content-addressed `planner_output_blob`
(`planning.provenance.store_planner_output()`, `source_kind=
"engineering_plan_planner_output"`) — inspectable by a caller with
direct `ContentStore` access, never surfaced through `planning.service.
PlanRecord` or the HTTP API by default. A successful `NORMALIZED` turn
additionally stores the original textual transport as a separate
internal blob (`source_kind="engineering_plan_original_transport"`);
that blob is also non-exportable and is never copied onto `PlanRecord`.

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

## Durable background jobs (Phase 8.2d)

`POST /api/plans`/`POST /api/plans/{plan_id}/replan` no longer hold the
HTTP connection for the whole planner turn — HTTP client lifetime has
**zero authority** over whether a planning job continues. Each durably
creates an `engineering_plans` `DRAFT` row (synchronously, no
inference) plus a `planning_jobs` row (migration `0009`) and returns
`202 Accepted` immediately with `{job_id, plan_id, state: "QUEUED",
status_url}`. `planning.executor.PlanningJobExecutor` — one long-lived,
process-owned background dispatcher, started once at server startup
(`api.service.ApplicationService.__init__`), never per request — claims
and executes it later. A disconnecting/backgrounded/suspended browser
changes nothing about whether or when the job runs; only an explicit,
future application-owned cancellation mechanism could ever cancel one
(none exists yet — client disconnect is never inferred as
cancellation).

**Job state is a separate concern from plan state**
(`planning.models.JobState`: `QUEUED`/`RUNNING`/`SUCCEEDED`/`FAILED`,
vs. `PlanState`): a job is `SUCCEEDED` whenever the planner turn itself
produced genuine structured output — regardless of whether the
resulting plan reached `READY`, `NEEDS_INPUT`, or even `DRAFT` because
evidence validation rejected one of its claims. A job is `FAILED` only
when the turn itself never produced valid structured output (a
transport failure, a non-tool response, or a schema-invalid tool call —
the same `PlannerFailureCategory` from Phase 8.2b, now the job's own
`failure_category`) or an internal error interrupted execution.

**Ownership/claim**: a job's `owner_pid`/`owner_pid_started_at`/
`owner_generation` mirror `lease.manager`'s own fencing-token pattern at
a much smaller scope — see `store.migrations.0009_planning_jobs`'s
module comment and `EngineeringPlanningService.claim_job()` for the
full safety argument (concurrent dispatchers, crash-then-restart,
stale-owner refusal, terminal-job immutability), built on the same
generic `lease.liveness.check_process_liveness()` Phase 6 already
established — never a new liveness mechanism.

`resume()` performs no model inference at all (it only re-evaluates the
already-hardened Question Gate against durable resolutions), so
`POST /api/plans/{plan_id}/resume` remains synchronous, `200 OK` — there
is no long-running turn to move into a background job.

**Server lifetime**: the executor's dispatcher thread is a daemon
thread with no explicit shutdown hook — a hard process kill (or a
graceful one) simply stops it, which is exactly the crash case its own
recovery logic already handles correctly on the next start. This works
unmodified under the existing systemd-hosted `codeslayer serve` process
model; no second ad-hoc background-process architecture was introduced.

## HTTP API

See [`WEBUI_API.md`](WEBUI_API.md#engineering-planning-phase-82) for
`GET/POST /api/plans`, `GET /api/plans/{plan_id}`, `POST /api/plans/
{plan_id}/resume`, `POST /api/plans/{plan_id}/replan`, `POST
/api/plans/{plan_id}/resolutions`, `GET /api/planning-jobs`, and
`GET /api/planning-jobs/{job_id}`.

## Known limitations

- `replan()` does not automatically carry forward an earlier revision's
  durable human resolutions — a fresh planner call may raise entirely
  different ambiguities anyway; a caller may still pass ephemeral
  `resolutions=` explicitly.
- No deterministic finalization gates or independent review yet (later
  Phase 8 slices) — this phase produces a plan, never a verified diff.
- No execution path exists that consumes a `READY` plan; that is a
  deliberately separate, later decision.
- No explicit, application-owned job-cancellation mechanism exists yet
  — a `QUEUED`/`RUNNING` job always runs to completion once accepted.
- The background executor defaults to exactly one concurrent worker.
  Testing under real concurrency (`max_workers=2`) surfaced an
  intermittent `git` index-lock race when two planning turns invoke
  Repository Intelligence against the *same* working tree at the same
  moment — evidence that safe parallelism here is not yet trivial, so
  the conservative default is kept rather than raised speculatively.
