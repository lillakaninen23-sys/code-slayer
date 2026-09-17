# WebUI application API — foundation 1

Architecture: WebUI → thin Flask application API → `LocalWorkerRunner` → existing
backend. Flask was added because no HTTP framework was present; Waitress supplies
the small threaded WSGI server, without a development reloader or debugger.
See [Flask's Waitress deployment guidance](https://flask.palletsprojects.com/en/stable/deploying/waitress/).
Application construction, runtime wiring, route validation and read projections
are separate modules under `src/code_slayer/api/`.

## Run locally

Inside the backend checkout, with its virtual environment active:

```bash
python3 -m pip install -e '.[dev]'
codeslayer serve --repo . --webui-dir ../path-to-codeslayer-webui-sidecar
```

Pass the actual sidecar checkout directory. Open `http://127.0.0.1:8765`.
`--webui-dir` is optional for API-only use. `--port` selects another port when an
old prototype server is already using 8765. Development edits are served directly;
no asset copying or frontend build is required. Both assets and `/api` use one
origin. Committed source contains no machine-specific paths. No Sites hosting or
remote deployment is involved in this local backend integration.

The CLI defaults to **127.0.0.1:8765**, no CORS, no debugger and no authentication.
Requests require a trusted Host; cross-origin browser requests are rejected.
Mutations require JSON and reject unknown fields. Body size is capped at 64 KiB.
For explicit LAN use, set `--host` and `--trusted-host` to the intended bind address
and host name. This exposes unauthenticated application actions to reachable peers;
use only an appropriately protected environment. Wildcard CORS is not supported.

The configured repository selects normal external state through `store.location`.
The existing `CODESLAYER_STATE_ROOT` environment setting can isolate development
state. These are server configuration, never HTTP input. Startup uses the runner's
normal identity/migration initialization; request reads open SQLite in read-only
mode. Every action owns and closes a fresh runner and connection on its request
thread. There is no separate task queue, duplicate task state, or in-memory lease.

## Runtime configuration (deliberately explicit)

Phase 7 provides the `PromptAnalyst` protocol and a **test-only** fake analyst;
it does not provide a production analyst implementation or a persisted adapter
configuration loader. This slice does not manufacture one or treat empty analysis
as blanket permission. The default server supports all durable read views and human
resolutions; start returns `503 analyst_not_configured` until configured. READY
runs show `configure_worker_adapter` when no adapter factory is supplied.

An installed, trusted host application can supply these dependencies:

```python
from code_slayer.api.service import RuntimeBindings

def bindings():
    return RuntimeBindings(
        analyst_factory=make_your_prompt_analyst,
        adapter_factory=make_adapter_for_registered_worker_and_role,
    )
```

Start with `codeslayer serve ... --runtime-factory your_runtime:bindings`, or call
`create_app(repo_path, bindings=RuntimeBindings(...), webui_dir=...)` directly.
Factories create request-local objects. An adapter factory takes `(worker_id, role)`
and returns the matching configured `WorkerAdapter`, or `None` when unavailable.
Use the existing `OpenAICompatibleAdapter(OpenAICompatibleConfig(...))` for supported
local endpoints. Host wiring must match the adapter's actual runtime/model/network
to the registered worker identity; it must not silently substitute another worker.
Registration, conformance, and promotion use existing backend application services,
not HTTP routes. No secrets or transport settings are returned to the UI.

A factory must not perform inference just by constructing an adapter. Transport
belongs inside `infer`, where the existing runner execution path checks cloud
approval. No HTTP action accepts or supplies `CloudEscalationAuthorization`, so
cloud workers receive the existing audited pre-transport denial when execution is
attempted. Question resolutions are wholly separate from cloud approval.

## JSON contract v1

`GET /api/health` returns `status`, `api_version: 1`, installed `source_version`,
current/known schema versions, configuration status, action configuration flags,
`capability_profiles: ["read_only"]`, and `cloud_authorization: "not_exposed"`.
The project's HEAD is not mislabelled as the installed service's source HEAD.

| Endpoint | Contract |
| --- | --- |
| `GET /api/project` | `repo_id`, `worktree_id`, display name, repository path, HEAD, branch, detached flag, control-plane availability, schema version |
| `GET /api/runs?limit=100&offset=0` | `{runs: [...], next_offset}`; newest first, maximum 100 per page |
| `GET /api/runs/{id}` | Summary plus task status, prompt/analysis hashes, structured questions, exact trust, execution IDs/isolation, operation ID, checkpoint refs, execution-state availability and next safe action |
| `POST /api/runs` | `{prompt, worker_id, role, capability_profile?: "read_only"}` → 201 RunResult and Location |
| `POST /api/runs/{id}/resume` | `{}` → RunResult through `LocalWorkerRunner.resume` |
| `POST /api/runs/{id}/resolutions` | `{ambiguity_id, answer, resolution_kind: "FACT" or "AUTHORIZATION"}` → current RunResult through `record_user_resolution` |
| `GET /api/workers` | `{workers: [...]}`; registration metadata, recorded availability, exact trust scopes |
| `GET /api/workers/{id}/trust` | Exact scopes, current level from WorkerTrustManager, last 100 events per scope, truncation flag, unrecorded scope level |
| `GET /api/workers/{id}/conformance` | Current suite version and newest 20 conformance runs with case results |
| `GET /api/runs/{id}/audit?limit=100` | Newest bounded events from control/execution databases, chronological presentation, association and plane labels |
| `GET /api/intelligence/status` | Whether the repository has been indexed, whether that snapshot is still current, snapshot id/HEAD/created-at, file count, detected project kinds |
| `POST /api/intelligence/refresh` | `{}` → forces a fresh, full, deterministic `RepositoryIntelligenceService.inspect()`; returns the same shape as `status` |
| `POST /api/intelligence/query` | `{text, limit?: 1-50}` → `{stale, candidates: [{path, score, reasons}]}`, ranked against the latest durable snapshot (never rebuilds) |
| `POST /api/intelligence/context-pack` | `{text, max_files?, max_bytes?, per_file_bytes?}` → bounded selected-file content, project/command evidence, `omitted`, `budget_exhausted`, `stale`; `409` if nothing has ever been indexed |

### Engineering planning (Phase 8.2 / 8.2d)

| Method / path | Returns |
| --- | --- |
| `GET /api/plans?limit=100&offset=0` | `{plans: [...]}`; a plan summary/detail per entry (see below), newest first |
| `GET /api/plans/{plan_id}` | Full plan detail: `state`, `effective_state` (`"STALE"` in place of `"READY"` when the repository has changed since binding), `reason`, revision/predecessor linkage, repository binding, `questions`, and full `content` |
| `POST /api/plans` | `{request}` → **202** `{job_id, plan_id, state: "QUEUED", status_url}` immediately; `503 planner_not_configured` if no server-side `Planner` is configured. Durable, server-owned background execution — see [`ENGINEERING_PLANNING.md`](ENGINEERING_PLANNING.md#durable-background-jobs-phase-82d); HTTP client disconnect never cancels it |
| `POST /api/plans/{plan_id}/resume` | `{}` → **200**, synchronous: re-evaluates the Question Gate against durable resolutions; never invokes the planner, so there is no long-running turn to background |
| `POST /api/plans/{plan_id}/replan` | `{}` → **202**, same durable-job contract as `POST /api/plans` |
| `POST /api/plans/{plan_id}/resolutions` | `{ambiguity_id, answer, resolution_kind: "FACT" or "AUTHORIZATION"}` → current plan detail through `record_user_resolution`; recording alone does not advance state — resume re-evaluates |
| `GET /api/planning-jobs?limit=100&offset=0` | `{jobs: [...]}`; each with `job_id`, `plan_id`, `kind` (`"create"`/`"replan"`), `state` (`QUEUED`/`RUNNING`/`SUCCEEDED`/`FAILED`), `attempt`, timestamps, `failure_category`/`failure_reason` |
| `GET /api/planning-jobs/{job_id}` | One job's full durable status — poll this after a `202` until `state` is `SUCCEEDED`/`FAILED`, then read the plan via `plan_id` |

A job's `state` is distinct from the plan's own `state`: `SUCCEEDED` means
the planner turn itself completed and produced genuine structured output —
the resulting plan may legitimately be `READY`, `NEEDS_INPUT`, or even
`DRAFT` (evidence validation rejected a claim). `FAILED` means the turn
itself never produced valid structured output; `failure_category` is one
of the small, safe, code-owned `PlannerFailureCategory` values — never raw
model text, which stays durable-internal-only.

A plan's `content` (when not `None`) carries `goal`, `requirements`, `assumptions`,
`affected_files` (each with `path`, `action`, `reason`, `exists_in_repository`,
`evidence`), `planned_changes`, `dependencies`, `risks`, `verification_steps`,
`discovered_commands`, `authority_requirements`, `open_questions`,
`evidence_refs`, and `validation_issues`. `exists_in_repository` and `evidence`
are set exclusively by evidence validation against authoritative Repository
Intelligence — never accepted from a model's own claim. Planning is read-only
with respect to the repository: no route here writes a repository file,
executes a discovered command, creates a checkpoint, or grants any trust —
see [`ENGINEERING_PLANNING.md`](ENGINEERING_PLANNING.md).

### Permissions (CSLR Governance Foundation, slice G2)

| Method / path | Returns |
| --- | --- |
| `GET /api/permissions` | `{definitions: [...]}` — every registered `PermissionDefinition`'s full explanation metadata (`permission_key`, `semantic_version`, `action`, `resource_type`, `sensitivity`, `user_title`, `user_summary`, `what_it_does`, `what_it_does_not_do`, `data_observed`, `data_retained`, `data_transmitted`, `revocable`, `technical_details`, `implementation_reference`, `user_selectable_scope`). Read-only, code-owned; safe to expose in full. |
| `GET /api/permissions/requests?limit=100&offset=0` | `{requests: [...]}`, newest first; each with `state` (`PENDING`/`ALLOWED`/`DENIED`, derived — never a stored status column) and its embedded `definition`. |
| `GET /api/permissions/requests/{request_id}` | One request's full detail. |
| `POST /api/permissions/requests/{request_id}/decision` | `{decision: "ALLOW" or "DENY"}` → current request detail. **No other field is accepted** — a client can never change `permission_key`/`semantic_version`/`resource`/`scope` for an existing request; those are already durably fixed server-side by `request_id`. |
| `GET /api/permissions/grants?limit=100&offset=0` | `{grants: [...]}`, newest first; each with `state` (`ACTIVE`/`REVOKED`/`EXPIRED`, derived). |
| `POST /api/permissions/grants/{grant_id}/revoke` | `{}` → current grant detail, `state: "REVOKED"`. Idempotent; revoking twice is a safe no-op, never a second revocation record. |

**There is deliberately no `POST /api/permissions/requests`.** A browser can
never mint its own permission request — only a trusted backend subsystem,
through `permissions.service.PermissionService.request()` (Python-level
only), creates one. A `POST` to that path returns `404`/`405`.
`authority_origin` on every grant is always `USER_EXPLICIT` — an explicit
human `ALLOW` decision is the only way a grant exists; nothing a model,
planner, or worker outputs can create or influence one. See
[`docs/PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md) for the full
specification and current-vs-future implementation status.

### Certification Center v1

The browser is a control surface over canonical backend APIs. It never
computes PASS/FAIL/HARD, never supplies `outcome`, `evidence_ref`,
`adapter`, fingerprint, digest, or a hard-disqualifier list, and never
talks to Ollama. Live Baseline Security certification writes only to
isolated validation state:

`{state_root}/validation-certification/{repo_id}/{worktree_id}/state.db`

never production `{state_root}/repos/{repo_id}/worktrees/{worktree_id}/state.db`.
Production eligibility is still computed only by
`evaluate_production_eligibility` against production state.

Expected runtime identity is server-owned (`RuntimeBindings.baseline_certification_targets`),
never HTTP input. Host wiring example: `runtime_qwen_coder:create_runtime`.

| Method / path | Returns |
| --- | --- |
| `GET /api/certification/workers` | `{environment: "VALIDATION", workers: [...]}` compact status: runtime `VERIFIED`/`MISMATCH`/`UNREACHABLE`/`UNKNOWN` from last durable preflight (never a live probe on GET), Baseline Security from **validation** certificates (`CERTIFIED`/`FAILED`/`NOT_CERTIFIED`), role certificates from production (`CERTIFIED`/`NOT_CERTIFIED`), production eligibility from `evaluate_production_eligibility` (`ELIGIBLE`/`BLOCKED` plus the evaluator's reason) |
| `GET /api/certification/workers/{id}` | Worker detail: identity with `CONFIG_BOUND` vs `LIVE_ATTESTED` sources (`effective_context_tokens` is config-bound and `measured_by_ollama: false`), last durable preflight, history, `ready_for_certification` |
| `POST /api/certification/workers/{id}/baseline/preflight` | `{ }` only. Probes Ollama version/tags; **never infers**. Writes a durable READY or INCOMPLETE run. Failed preflight cannot be started. Digest mismatch cannot be started |
| `POST /api/certification/workers/{id}/baseline/runs` | `{ }` only → **202** `{run_id, state: "QUEUED", ...}` and `Location`. Delegates to `certify_live_baseline_security`. One in-flight attempt; a second POST returns `409 certification_already_in_progress`. Closing the browser does not cancel the run. GET on this path does not start a run |
| `GET /api/certification/runs/{id}` | Durable run projection. Poll after 202. Progress is derived from run state. Incomplete runs have `has_certificate: false` |
| `GET /api/certification/runs/{id}/evidence` | Evidence reread through `read_baseline_security_evidence` from the validation ContentStore. `409` if missing/unverified |
| `GET /api/certification/workers/{id}/history` | `{runs, validation_certificates, production_certificates}` — a run is not a certificate |

Live Planner/Coder/Reviewer/Repairer/Security certification is not available
in v1 (`future_actions[].available: false`). PASS, FAIL, and HARD_DISQUALIFIED
all record a validation certificate; INCOMPLETE does not. None of these
routes grant trust, permissions, or production eligibility.

A summary contains run/task IDs, status, worker/role, creation/update timestamps,
question strings, reason code and execution worktree ID. Detailed questions contain
`ambiguity_id`, `question`, `risk_class` and `answer_recorded`. Answers preserve the
user's exact text; prompts are hashed by the existing runner without normalization.
Answer text and original prompt text are not returned. A recorded answer is not a
SUPPRESS decision: only resume re-evaluates QuestionGate.

A RunResult response contains `run_id`, `status`, `task_id`, `questions`, machine
`reason`, and `evidence_refs`. Large final-text blobs are omitted. `COMPLETED`,
`FAILED`, and `DENIED_TRUST` resume idempotently even without an adapter. RUNNING
means owned; the API never assumes a crash. INTERRUPTED_RESUMABLE is displayed as
requiring operator reconciliation. READY can be resumed when an adapter is wired;
blocked runs can be answered and resumed without an adapter to reach READY.

Statuses are runner outcomes, not HTTP errors. Errors always use:

```json
{"error":{"code":"not_found","message":"Resource not found.","retryable":false}}
```

Unknown resources: 404; malformed/unsupported fields: 400; non-JSON: 415; oversized
body: 413; stale question/run mismatch: 409; unavailable configuration/state: 503;
unexpected application failure: 500 with no stack trace or exception text. A lost
POST response is not replay permission: refresh durable run state before retrying.
There is no start-request deduplication token in v1; a second start creates a new run.

## Safety and audit boundary

Routes never create trusted ResolutionEvidence, write trust, promote workers,
create leases, choose fencing/session IDs, edit runner rows, invoke ToolExecutor,
execute shell, modify checkpoints or select arbitrary worktree/DB paths. This API
only offers the existing bounded read-only profile. It can display historical job
worktree runs and their separate execution evidence; it never merges them.
No AUTO or model mutation trust was added. The `/api/intelligence/*` routes
(Phase 8.1, [`REPOSITORY_INTELLIGENCE.md`](REPOSITORY_INTELLIGENCE.md)), the
`/api/plans*` routes (Phase 8.2, [`ENGINEERING_PLANNING.md`](ENGINEERING_PLANNING.md)),
and the `/api/permissions*` routes (Governance Foundation slice G2,
[`PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md)), and the
`/api/certification*` routes (Certification Center v1) are the
additions since WebUI Foundation 1 — deterministic, read-only
repository evidence, planning, permission consent/revocation, and a
control surface over isolated Baseline Security certification. Never a
filesystem/DB path from the client, never a new authority: a repository
fact still cannot become trusted `ResolutionEvidence` except through the
existing, unmodified application-owned authority path, a `READY` plan
authorizes no execution, no mutation, and no command,
`/api/permissions*` can only decide on or revoke a permission
request/grant a trusted backend subsystem already created — never mint
one, and never accept anything beyond `{decision}` / `{}` in a mutating
body — and `/api/certification*` never accepts an outcome, evidence
reference, adapter, digest, fingerprint, or hard-disqualifier list.
Certificates from this surface are written only to isolated validation
state; they do not grant trust, permissions, or production eligibility.

Audit payloads use an allowlist of short machine fields. Prompt/answer contents,
provider errors, raw parameters, large blobs, filesystem locations and lease tokens
are omitted. New runner provenance includes `run_id` to distinguish identical
prompts in separate runs. Historical events without it remain unchanged and are
labelled `shared_prompt_identity`; they cannot prove unique run attribution.
The timeline is a display projection, **not hash-chain verification**. Use the
existing `audit.verify.verify_chain` API for integrity checks. Truncated audit and
checkpoint lists are a first-slice limitation, not evidence that older records are
absent. Missing execution state is explicitly surfaced.

## Validation

```bash
python3 -m pytest -q
.venv/bin/ruff check .
git diff --check
# In the sidecar:
npm test
```

Tests use temporary Git repositories/control databases and deterministic fake worker
responses. They execute the real runner, gate, human-resolution recording, trust,
conformance, audit and completion boundaries. No live model inference is necessary.
