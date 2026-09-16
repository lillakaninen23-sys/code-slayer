"""Planner qualification harness (Phase 8.2e).

## Purpose: evidence, never authority

This module answers exactly one question, repeatedly and deterministically:
*did this run of `Planner.plan()`/`WorkerAdapter.infer()` produce a genuine,
schema-valid, evidence-grounded, task-relevant structured tool call, in a
runtime environment verified capable of holding the whole request* — never
whether a model is "generally good," never whether one lucky success
occurred, and never a result contaminated by the runtime silently
truncating the request before the model ever saw it. It produces
`PlannerTrial`/`ToolTransportTrial` records and aggregate `CaseMetrics`; it
never writes durable state, never selects a production planner, and never
grants trust, permission, or policy authority to anything. Every function
here is pure with respect to Code Slayer's own control-plane database —
the only side effect any function in this module ever has is the one real
network call a `Planner`/`WorkerAdapter` implementation itself makes (and
`INVALID_ENVIRONMENT`/`ENVIRONMENT_UNVERIFIED`/`OUTPUT_BUDGET_EXHAUSTED`
trials make none, or contribute nothing new beyond the one call already
made).

## No prose recovery, structurally

Classification here never inspects `PlannerResponse.raw`/`.error`'s free
text for anything resembling a plan, and never re-derives a tool call from
`WorkerResponse.text`. The sole inputs to `classify_planner_response()` are
`PlannerResponse.outcome`/`.output`/`.failure_category`/`.finish_reason`,
plus the original task text for a deliberately weak, generic lexical-
relevance check (see "Qualification success semantics" below).

## This module never touches trust, policy, leases, or checkpoints

No import here reaches `workers.trust`, `workers.promotion`,
`policy.engine`, `lease.manager`, `repo.checkpoint`, `tools.executor`, or
`permissions.service`. Selecting a production planner remains an entirely
separate, explicit, human decision made outside this module.

The one exception (added for scope/policy classification below) is
`tools.file_tools` — pure, side-effect-free path-string validation
(`relative_path()`/`within()`) with no database, no lease, no policy
decision, and no filesystem access of its own. Reusing it here means a
plan-claimed path this module calls `POLICY_VIOLATION` is exactly, and
only, a path Code Slayer's own real `ToolExecutor` would also refuse for
real tool execution (`tools.file_tools.relative_path()` is the same
function every real capability call already runs first) — never a
second, independently-invented policy.

## Failure classification: the canonical semantics this taxonomy covers

`TrialOutcome`/`QualificationOutcome` predate, and satisfy, the later
"typed qualification failure classification" requirement stated in
canonical, role-agnostic language. This cross-reference is additive
documentation only — no prior decision below it is removed:

- `CAPABILITY_FAILURE` → `QualificationOutcome.FAIL_CAPABILITY`.
- `CORRECTABLE_TEST_FAILURE` → `TrialOutcome.PLAN_VALIDATION_REJECTED`
  (the only correctness/evidence check this role has: does every
  concrete repository claim survive `planning.evidence` verification).
- `SCOPE_VIOLATION` → `TrialOutcome.SCOPE_VIOLATION` /
  `QualificationOutcome.FAIL_SCOPE` (see below — now implemented).
- `POLICY_VIOLATION` → `TrialOutcome.POLICY_VIOLATION` /
  `QualificationOutcome.FAIL_POLICY` (see below — now implemented).
- `MALFORMED_OUTPUT` → `TrialOutcome.NON_TOOL_RESPONSE` (no genuine tool
  call at all) and `TrialOutcome.TOOL_SCHEMA_INVALID` (a tool call whose
  arguments failed the strict schema check) — two distinguishable
  sub-cases of the same broader semantic, kept separate because their
  structured feedback differs.
- `TRANSPORT_TIMEOUT` → `TrialOutcome.TRANSPORT_TIMEOUT` /
  `QualificationOutcome.FAIL_TRANSPORT_TIMEOUT` — never retried as a
  correction (nothing the model did wrong), never `FAIL_CAPABILITY`,
  and subject to `run_corrected_planner_case()`'s own conservative
  early-stop threshold (unchanged, see "Self-correction / retry" below).
- `RUNTIME_TOOL_FAILURE` → **not yet producible in this role**. This
  harness never executes a real, side-effecting tool call (the Planner
  role only ever produces a structured proposal); there is structurally
  nothing that could fail as "the tool itself broke" the way a Coder
  role's real `create_file`/`write_file` execution could. This semantic
  slot is reserved for a future Coder-role qualification harness that
  performs real tool execution — mirrored exactly on how
  `FAIL_POLICY`/`FAIL_SCOPE` were reserved, unproduced, before this
  revision, and remains a disclosed, intentional gap, not an oversight.
- `INFRASTRUCTURE_FAILURE` → `TrialOutcome.TRANSPORT_ERROR` /
  `QualificationOutcome.FAIL_RUNTIME` — any non-timeout transport/
  connection failure (`workers.protocol.WorkerAdapterError` that is not
  a timeout). Never retried as a correction, never `FAIL_CAPABILITY`.

## Scope/policy violation: what changed, and why (this revision)

**Previously**: `FAIL_POLICY`/`FAIL_SCOPE` existed as `QualificationOutcome`
members but were explicitly documented as "reserved, not produced today"
— planning had no policy/scope dimension to classify against.

**Now**: a genuine, schema-valid structured plan's own claimed
`affected_files` paths are checked, deterministically and without any
network call, against two independent, code-owned authorities:

1. **Policy** (`TrialOutcome.POLICY_VIOLATION`, always checked, never
   opt-in): any claimed path that Code Slayer's own real path-safety
   authority (`tools.file_tools.relative_path()`) would refuse for real
   tool execution — path traversal (`..`), an absolute path, a
   `.git`-internal path, embedded control characters, or a malformed/
   non-UTF-8 path. This is a hard, universal, task-independent property;
   it is checked identically regardless of whether the caller supplied
   an `allowed_scope`.
2. **Scope** (`TrialOutcome.SCOPE_VIOLATION`, opt-in via a caller-
   supplied `allowed_scope: tuple[str, ...] | None`): a claimed path
   that passed the policy check but is not `tools.file_tools.within()`
   any declared allowed-scope prefix for *this qualification task*.
   Never checked at all when `allowed_scope` is not supplied — existing
   callers that never pass it see byte-for-byte the same classification
   behavior as before this revision (backward compatible).

Policy is checked before scope: a path already rejected by the
universal, non-negotiable policy check is reported as `POLICY_
VIOLATION`, never additionally as `SCOPE_VIOLATION`, matching this
codebase's existing "a hard disqualifier outweighs a softer signal"
convention. Both are correctable (`_CORRECTABLE_OUTCOMES`) — the model
gets exactly the same bounded, same-model retry chance as any other
correctable failure — but correctability is about giving the *model* a
chance to comply, never about the *check* becoming weaker: the identical
policy/scope check runs again, at full strength, on every retry.

## `usage.prompt_tokens` describes what was evaluated, never proof of what survived (this revision)

A third review found the second hardening pass's own `EXACT` label
overclaimed: `usage.prompt_tokens < effective_context_tokens` proves only
that *whatever the runtime actually evaluated* fit — it says nothing about
whether the runtime silently truncated a larger original request down to
that smaller evaluated size before ever reporting a count. A request that
started at 9000 tokens, got truncated to 3900, and ran against a 4096
context would report `usage.prompt_tokens=3900 < 4096` — comfortably under
context, and *still wrong* about what was actually sent.

`TokenMeasurementSource` therefore separates two genuinely different
claims:

- `ESTIMATED` — the conservative `chars/3.0` heuristic. Never claimed
  exact.
- `ACTUAL_EVALUATED` — a real, runtime-reported `usage.prompt_tokens`
  count. A plain fact about what the runtime evaluated for *this* call —
  never, by itself, proof that the full untruncated request survived.
- `VERIFIED_FULL_INPUT` — `ACTUAL_EVALUATED` cross-checked against an
  independently-established `expected_untruncated_input_tokens` baseline
  (a `VerifiedExpectedInput`) for the *exact same* request fingerprint,
  where the two are numerically equal. Only this source may be read as
  "the full input was preserved."
- `UNKNOWN` — no usable count, or a cross-check that came back
  inconclusive/mismatched.

## Expected vs. actual, bound to a request fingerprint (this revision)

A caller (a qualification driver, having independently verified — e.g. by
the same cross-context-size comparison methodology used to diagnose
Class C — that a specific frozen request's true untruncated size is N
tokens) constructs a `VerifiedExpectedInput(request_fingerprint, expected_
tokens, method)`. `run_planner_trial()` accepts this as `verified_expected_
input`; after a real call, if the *current* request's own fingerprint
matches, it compares the real `usage.prompt_tokens` against `expected_
tokens`:

- `actual == expected` → `VERIFIED_FULL_INPUT`; the trial proceeds to
  ordinary content-based classification with that measurement recorded.
- `actual != expected` (in particular `actual < expected`, the truncation
  case) → the trial becomes `TrialOutcome.INVALID_ENVIRONMENT`
  (`detail` names `input_truncated` or `unexpected_growth_vs_verified_
  baseline`), never a capability failure.

**A `VerifiedExpectedInput` never applies to a different request.** The
fingerprint covers everything that affects the actually-rendered input —
task, repository context, prior correction feedback, and the fixed tool/
schema/instruction text (`_rendered_request_fingerprint()`, over the exact
string `planning.worker_planner._render_planning_prompt()` produces). A
retry attempt's added feedback changes this fingerprint, so a baseline
verified for attempt 1 silently does **not** apply to attempt 2 — the
comparison is skipped (not wrongly reused), and the trial falls back to
the weaker "did the reported count reach the effective context ceiling"
check instead (still real signal, just not the stronger preserved-input
guarantee). Retries are therefore always measured on their own terms,
never against a stale baseline.

## Pre-call preflight still only blocks a clearly hopeless request

`preflight_check()`'s `ESTIMATED`-based pre-call gate is unchanged in
spirit: it blocks only requests far beyond any plausible estimation error
(`_HOPELESS_OVERFLOW_MULTIPLIER`); an injected `TokenCounter` may resolve
a merely-marginal case pre-call, but only when it reports `VERIFIED_FULL_
INPUT` — an `ACTUAL_EVALUATED` or `UNKNOWN` report from a counter is never
enough to authoritatively unblock or block a marginal case, matching the
same "never let a lesser-confidence measurement decide the ambiguous
case" discipline as the post-call path.

## Verified `RuntimeContextProfile` required for scoreable qualification

Unchanged from the second hardening pass: `run_planner_case_with_
correction()`/`run_corrected_planner_case()` require a `context_profile`
for a scoreable result; omitting one (without the loudly-named `unsafe_
allow_unverified_environment=True` escape hatch) produces `Qualification
Outcome.ENVIRONMENT_UNVERIFIED` — zero attempts, zero network calls.

## Explicit output budget, and now output-budget exhaustion (this revision)

`RuntimeContextProfile.output_budget_enforcement_verified` still gates
whether `max_tokens` is actually sent (see `PlannerRequest.output_token_
budget`/`WorkerRequest.max_output_tokens`). This revision adds detecting
when it was *hit*: `WorkerResponse.finish_reason`/`PlannerResponse.
finish_reason`, threaded from the real provider response, report `"length"`
when generation was cut off by a token cap (ours, or the runtime's own
unconfigured default). A response cut short this way is structurally
unreliable evidence about the model's actual capability — truncated JSON
can look exactly like a schema violation, a non-tool response, or a
plausible-but-incomplete plan, none of which are real signal about the
model.

`classify_planner_response()` therefore checks `finish_reason == "length"`
**before** any other classification and returns `TrialOutcome.OUTPUT_
BUDGET_EXHAUSTED` — never `NON_TOOL_RESPONSE`/`TOOL_SCHEMA_INVALID`/
`PLAN_VALIDATION_REJECTED`/a silently-accepted `VALID_STRUCTURED_PLAN`.
This is deliberately its own outcome, distinct from both a model-
attributable failure and an environment/context problem: the model was
never actually given the chance to finish. `QualificationOutcome.OUTPUT_
BUDGET_EXHAUSTED` mirrors it at the attempt-chain level — not retried
(there is nothing the model did wrong to give feedback about; the budget
itself would need to change), not counted as `FAIL_CAPABILITY`, and
excluded from `aggregate_planner_trials()`'s capability-rate denominators
exactly like `INVALID_ENVIRONMENT`. `AttemptProvenance` records `finish_
reason`/`completion_tokens`/`requested_max_tokens` for every attempt,
regardless of outcome.

## Qualification success semantics

`VALID_STRUCTURED_PLAN` (`QUALIFICATION_PASS`) requires every one of:

- `TOOL_RESPONSE_PRESENT` — a genuine, authorized tool call was made.
- `SCHEMA_VALID` — its params passed `parse_planner_output()`'s strict
  check.
- `PLAN_STRUCTURALLY_VALID` — synonymous with `SCHEMA_VALID` today; no
  additional structural check exists yet (an explicit, disclosed gap).
- `EVIDENCE_VALID` — no concrete repository claim was rejected.
- `TASK_RELEVANT` — a deliberately weak, generic, deterministic lexical-
  overlap check (`_is_task_relevant()`). Not a semantic validator; two
  disclosed failure modes remain open (a genuinely relevant plan phrased
  differently can be wrongly rejected; an irrelevant plan reusing copied
  vocabulary can still pass this one gate) — never addressed with an
  embeddings/LLM-based validator, deliberately, to stay deterministic and
  code-owned.

A plan cut short by the output budget never reaches any of these gates at
all (`OUTPUT_BUDGET_EXHAUSTED` pre-empts classification entirely).

## Tool-choice enforcement is advisory only on this runtime

`tool_choice: "required"` is not verifiably enforced by Ollama 0.16.1's
OpenAI-compatible endpoint. `RuntimeContextProfile.tool_choice_enforcement`
(default `"ADVISORY_ONLY_UNVERIFIED"`) records this in provenance.

## Architecture gap: no per-request `num_ctx` control today

Ollama's native `/api/chat`/`/api/generate` endpoints honor `options.
num_ctx`; its OpenAI-compatible endpoint — the only one
`OpenAICompatibleAdapter` speaks — silently ignores it. The only currently
available lever is a distinct model tag whose `PARAMETER num_ctx` is
already baked in via `ollama create`, acceptable for qualification when
the tag uses identical underlying weights, the difference is documented,
effective context is independently verified, and provenance binds the
result to that exact profile/tag.

## Outcome taxonomy

`TrialOutcome`: `TRANSPORT_TIMEOUT`/`TRANSPORT_ERROR` (transport-layer,
never a compliance signal); `NON_TOOL_RESPONSE`; `TOOL_SCHEMA_INVALID`;
`POLICY_VIOLATION` (a claimed path Code Slayer's own real path-safety
authority would refuse for real execution, always checked);
`SCOPE_VIOLATION` (a claimed path outside a caller-declared task scope,
only checked when a scope is declared); `PLAN_VALIDATION_REJECTED`;
`TASK_NOT_RELEVANT`; `VALID_STRUCTURED_PLAN` (every gate passed);
`OUTPUT_BUDGET_EXHAUSTED` (cut short by a token cap, before
classification); `INVALID_ENVIRONMENT` (a verified environment could
not, or provably might not, hold the request — no verdict on the
model).

`QualificationOutcome` mirrors these at the attempt-chain level and adds
`ENVIRONMENT_UNVERIFIED` (no verified profile was ever supplied at all).

## Cold start vs warm

`run_planner_case()`/`run_tool_transport_case()` accept a `warm_up` flag
that runs one extra, unrecorded trial first, excluded from every rate.

## Self-correction / retry

`run_planner_case_with_correction()` gives one task instance up to
`max_correction_attempts` same-model retries, only for `NON_TOOL_RESPONSE`/
`TOOL_SCHEMA_INVALID`/`PLAN_VALIDATION_REJECTED`/`TASK_NOT_RELEVANT`/
`POLICY_VIOLATION`/`SCOPE_VIOLATION` — never `OUTPUT_BUDGET_EXHAUSTED`
(nothing to correct), never an unbounded loop, never a second model,
never a loosened bar. Preflight (and post-call verification) runs on
every attempt including retries. The original task/repository context/
tool instructions are never dropped from the `PlannerRequest` Python
object across retries — only `prior_attempt_feedback` (and, once,
`output_token_budget`) ever change. No larger reviewer/second model is
ever loaded for a correction retry: every attempt in one task
instance's chain — including every retry — goes through the exact same
`planner`/`WorkerAdapter` object the caller supplied once; this module
never constructs a second one, so whatever runtime residency ("keep the
model loaded") behavior the underlying inference runtime provides is
preserved unchanged across the whole chain.

**Early stop.** `run_corrected_planner_case()` stops issuing further
repetitions once `early_stop_after_consecutive_transport_failures`
(default 3) task instances in a row end in `FAIL_TRANSPORT_TIMEOUT`/
`FAIL_RUNTIME`. `INVALID_ENVIRONMENT`/`ENVIRONMENT_UNVERIFIED`/`OUTPUT_
BUDGET_EXHAUSTED` never feed this counter — a single timeout never
disqualifies anything; only a conservative, consecutive run of them
(runtime/profile-scoped, never written back as a capability verdict)
stops further repetitions early, purely to avoid wasting time working
through every remaining class against a runtime that has already shown
a clear, repeated pattern.

**`FAIL_POLICY`/`FAIL_SCOPE` are now producible** (this revision) — see
"Scope/policy violation" above for exactly when and why, and for the
explicit note on what prior decision this supersedes.

## Provenance

`build_attempt_provenance()` binds one attempt to the exact request/
runtime/profile that produced it, including the token measurement's own
source/method, the expected-vs-actual input comparison, and output-budget
exhaustion evidence. Raw repository text is never stored — only bounded
sha256 fingerprints.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from code_slayer.intelligence.models import Snapshot
from code_slayer.planning.evidence import validate_plan_against_intelligence
from code_slayer.planning.planner import (
    Planner,
    PlannerFailureCategory,
    PlannerOutcome,
    PlannerRequest,
    PlannerResponse,
    render_bounded_context,
)
from code_slayer.planning.worker_planner import TOOL_NAME, _render_planning_prompt
from code_slayer.tools import file_tools as files
from code_slayer.tools.models import ToolError
from code_slayer.workers.openai_compatible_adapter import _TOOL_SCHEMAS
from code_slayer.workers.protocol import (
    WorkerAdapter,
    WorkerAdapterError,
    WorkerRequest,
    WorkerResponseKind,
)
from code_slayer.workers.protocol_validation import validate_response

_DETAIL_MAX_LEN = 300  # bounded, safe evidence only -- never raw model prose


class TrialOutcome(StrEnum):
    """See the module docstring's outcome-taxonomy section."""

    TRANSPORT_TIMEOUT = "TRANSPORT_TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    NON_TOOL_RESPONSE = "NON_TOOL_RESPONSE"
    TOOL_SCHEMA_INVALID = "TOOL_SCHEMA_INVALID"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    PLAN_VALIDATION_REJECTED = "PLAN_VALIDATION_REJECTED"
    TASK_NOT_RELEVANT = "TASK_NOT_RELEVANT"
    VALID_STRUCTURED_PLAN = "VALID_STRUCTURED_PLAN"
    OUTPUT_BUDGET_EXHAUSTED = "OUTPUT_BUDGET_EXHAUSTED"
    INVALID_ENVIRONMENT = "INVALID_ENVIRONMENT"


# A genuine, authorized `emit_engineering_plan` tool call was made for
# every one of these six outcomes.
_GENUINE_TOOL_CALL_OUTCOMES = frozenset({
    TrialOutcome.TOOL_SCHEMA_INVALID,
    TrialOutcome.POLICY_VIOLATION,
    TrialOutcome.SCOPE_VIOLATION,
    TrialOutcome.PLAN_VALIDATION_REJECTED,
    TrialOutcome.TASK_NOT_RELEVANT,
    TrialOutcome.VALID_STRUCTURED_PLAN,
})
_SCHEMA_VALID_OUTCOMES = frozenset({
    TrialOutcome.POLICY_VIOLATION, TrialOutcome.SCOPE_VIOLATION,
    TrialOutcome.PLAN_VALIDATION_REJECTED, TrialOutcome.TASK_NOT_RELEVANT,
    TrialOutcome.VALID_STRUCTURED_PLAN,
})
# Outcomes that never represent a real, assessable qualification attempt --
# excluded from every rate's denominator in `aggregate_planner_trials`.
_NON_ASSESSABLE_OUTCOMES = frozenset({
    TrialOutcome.INVALID_ENVIRONMENT, TrialOutcome.OUTPUT_BUDGET_EXHAUSTED,
})


@dataclass(frozen=True)
class PlannerTrial:
    """One qualification trial's outcome. `detail` is bounded, code-
    derived evidence only -- never raw model text."""

    outcome: TrialOutcome
    latency_seconds: float
    detail: str | None = None
    plan_state_hint: str | None = None
    goal_fingerprint: str | None = None


def _bounded(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:_DETAIL_MAX_LEN]


def _structural_fingerprint(output) -> str:
    action_counts = sorted({f.action for f in output.affected_files})
    return (
        f"files={len(output.affected_files)}:actions={','.join(action_counts)}:"
        f"changes={len(output.planned_changes)}:ambiguities={len(output.ambiguities)}:"
        f"risks={len(output.risks)}:requirements={len(output.requirements)}"
    )


# -- task-relevance: a deliberately weak, generic, deterministic check ------

_ENGLISH_STOPWORDS = frozenset({
    "about", "above", "after", "again", "against", "their", "there",
    "these", "those", "which", "while", "would", "could", "should",
    "where", "being", "other", "under", "using", "based", "every",
    "before", "because", "without", "within", "still", "between",
})


def _significant_words(text: str) -> frozenset[str]:
    words = re.findall(r"[a-zA-Z]{5,}", text.lower())
    return frozenset(w for w in words if w not in _ENGLISH_STOPWORDS)


def _is_task_relevant(original_request: str, goal: str) -> bool:
    """See the module docstring's "Qualification success semantics"
    section for exactly what this does and does not prove."""
    task_words = _significant_words(original_request)
    if not task_words:
        return True  # nothing meaningful to compare against -- never penalize this
    goal_words = _significant_words(goal)
    return bool(task_words & goal_words)


def _policy_or_scope_violation(
    output, allowed_scope: tuple[str, ...] | None,
) -> tuple[TrialOutcome, str] | None:
    """Deterministic, network-free check of a genuine, schema-valid
    plan's own claimed `affected_files` paths. Policy (always checked)
    outranks scope (opt-in): a path already rejected on policy grounds
    is never also reported as a scope violation. Returns `None` when
    neither applies. See the module docstring's "Scope/policy violation"
    section."""
    policy_rejected = sorted({
        f.path for f in output.affected_files
        if not _is_policy_safe_path(f.path)
    })
    if policy_rejected:
        return TrialOutcome.POLICY_VIOLATION, _bounded(",".join(policy_rejected))
    if allowed_scope is not None:
        out_of_scope = sorted({
            f.path for f in output.affected_files
            if not any(files.within(f.path, scope) for scope in allowed_scope)
        })
        if out_of_scope:
            return TrialOutcome.SCOPE_VIOLATION, _bounded(",".join(out_of_scope))
    return None


def _is_policy_safe_path(path: str) -> bool:
    try:
        files.relative_path(path)
    except ToolError:
        return False
    return True


def classify_planner_response(
    response: PlannerResponse, snapshot: Snapshot | None = None,
    *, original_request: str | None = None, allowed_scope: tuple[str, ...] | None = None,
) -> tuple[TrialOutcome, str | None, str | None]:
    """Pure function: never calls a model, never mutates anything.
    `finish_reason == "length"` is checked first, before anything else --
    see the module docstring's "Output-budget exhaustion" section.

    `allowed_scope`, when supplied, is this qualification task's own
    declared set of allowed path prefixes -- never sent to the model,
    never derived from its output; purely this function's own ground
    truth for classifying what the model actually claimed. Omitting it
    (the default) skips scope checking entirely -- byte-for-byte the
    same behavior as before this parameter existed. The universal policy
    check below always runs regardless."""
    if not isinstance(response, PlannerResponse):
        raise TypeError("classify_planner_response requires a PlannerResponse")
    if snapshot is not None and not isinstance(snapshot, Snapshot):
        raise TypeError("snapshot must be an intelligence.models.Snapshot or None")

    if response.finish_reason == "length":
        return (
            TrialOutcome.OUTPUT_BUDGET_EXHAUSTED,
            "finish_reason=length: generation was cut off by a token cap before it could finish",
            None,
        )

    if response.outcome != PlannerOutcome.STRUCTURED or response.output is None:
        category = response.failure_category
        detail = _bounded(response.error)
        if category == PlannerFailureCategory.TRANSPORT_ERROR:
            if response.error and "timeout" in response.error:
                return TrialOutcome.TRANSPORT_TIMEOUT, detail, None
            return TrialOutcome.TRANSPORT_ERROR, detail, None
        if category == PlannerFailureCategory.SCHEMA_INVALID:
            return TrialOutcome.TOOL_SCHEMA_INVALID, detail, None
        return TrialOutcome.NON_TOOL_RESPONSE, detail, None

    violation = _policy_or_scope_violation(response.output, allowed_scope)
    if violation is not None:
        outcome, detail = violation
        return outcome, detail, None

    if snapshot is not None:
        validation = validate_plan_against_intelligence(response.output, snapshot)
        if validation.blocking:
            return (
                TrialOutcome.PLAN_VALIDATION_REJECTED,
                _bounded(";".join(validation.issues)), "DRAFT",
            )
        hint = "NEEDS_INPUT" if validation.content.open_questions else "READY"
    else:
        hint = None

    if original_request is not None and not _is_task_relevant(
        original_request, response.output.goal,
    ):
        return (
            TrialOutcome.TASK_NOT_RELEVANT,
            "goal shares no significant word with the original task", None,
        )

    return TrialOutcome.VALID_STRUCTURED_PLAN, None, hint


# -- token measurement: estimated / actual-evaluated / verified / unknown ----

DEFAULT_OUTPUT_TOKEN_BUDGET = 4096
DEFAULT_SAFETY_MARGIN_TOKENS = 1024
# Deliberately lower than typical English/JSON prose's real ratio (~4
# chars/token) so this estimate over-counts, never under-counts.
_CHARS_PER_TOKEN_CONSERVATIVE = 3.0
# How far beyond the effective context an ESTIMATED requirement must be
# before pre-call blocking is justified even without exact confirmation.
_HOPELESS_OVERFLOW_MULTIPLIER = 2.0
_TOOL_SCHEMA_JSON_LENGTH = len(json.dumps(_TOOL_SCHEMAS[TOOL_NAME]))


class TokenMeasurementSource(StrEnum):
    """See the module docstring's own section on this. `ESTIMATED` is
    never claimed exact; `ACTUAL_EVALUATED` (a real runtime `usage.
    prompt_tokens`) is never, by itself, proof the full untruncated
    request survived -- only `VERIFIED_FULL_INPUT` (cross-checked against
    an independently-established expected baseline for the identical
    request fingerprint) may be read that way."""

    ESTIMATED = "ESTIMATED"
    ACTUAL_EVALUATED = "ACTUAL_EVALUATED"
    VERIFIED_FULL_INPUT = "VERIFIED_FULL_INPUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TokenMeasurement:
    """One token count with its own confidence provenance.
    `token_count` is `None` only when `source == UNKNOWN`.
    `request_fingerprint` binds this measurement to the exact rendered
    request it was taken for -- never reused for a different one."""

    source: TokenMeasurementSource
    token_count: int | None
    request_fingerprint: str
    method: str


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _rendered_request_fingerprint(request: PlannerRequest) -> str:
    """Covers everything that affects the actually-rendered input: task,
    repository context, and the fixed tool/schema/instruction text (all
    baked into `_render_planning_prompt()`'s own output) *plus* prior
    correction feedback -- which `_render_planning_prompt()` itself does
    NOT include, since `planning.worker_planner.WorkerAdapterPlanner`
    sends it as a genuinely separate follow-up message
    (`WorkerRequest.prior_tool_result`), not folded into the main
    prompt string. Both are combined here so a retry's added feedback
    always changes this fingerprint, exactly as it should -- omitting it
    would let a stale `VerifiedExpectedInput` silently appear to apply to
    a request that has, in fact, grown since it was verified."""
    prompt = _render_planning_prompt(request)
    return _fingerprint(prompt + (request.prior_attempt_feedback or ""))


class TokenCounter(Protocol):
    """Injectable pre-call measurement provider. Only a `VERIFIED_FULL_
    INPUT`-sourced report may resolve a marginal pre-call ambiguity
    authoritatively (see `preflight_check`); `ACTUAL_EVALUATED`/`UNKNOWN`
    reports are treated the same as not having one."""

    def measure(self, request: PlannerRequest) -> TokenMeasurement: ...


def estimate_prompt_tokens(text: str) -> int:
    """A conservative, deterministic, stdlib-only ESTIMATE -- NOT a real
    tokenizer count."""
    if not isinstance(text, str):
        raise TypeError("estimate_prompt_tokens requires a str")
    return math.ceil(len(text) / _CHARS_PER_TOKEN_CONSERVATIVE)


def estimate_request_tokens(request: PlannerRequest) -> int:
    """The estimated total input token cost of `request` as it will
    actually be rendered -- the main prompt, the fixed tool-schema cost,
    and, when `prior_attempt_feedback` is set, its own follow-up
    message."""
    if not isinstance(request, PlannerRequest):
        raise TypeError("estimate_request_tokens requires a PlannerRequest")
    prompt = _render_planning_prompt(request)
    total_chars = len(prompt) + _TOOL_SCHEMA_JSON_LENGTH
    if request.prior_attempt_feedback is not None:
        total_chars += len(request.prior_attempt_feedback)
    return math.ceil(total_chars / _CHARS_PER_TOKEN_CONSERVATIVE)


def estimate_request_token_measurement(request: PlannerRequest) -> TokenMeasurement:
    """`estimate_request_tokens()` wrapped with its own confidence
    provenance -- always `ESTIMATED`."""
    return TokenMeasurement(
        source=TokenMeasurementSource.ESTIMATED, token_count=estimate_request_tokens(request),
        request_fingerprint=_rendered_request_fingerprint(request),
        method=f"char_heuristic:chars_per_token={_CHARS_PER_TOKEN_CONSERVATIVE}",
    )


def actual_evaluated_measurement_from_usage(request: PlannerRequest, usage) -> TokenMeasurement:
    """Always `ACTUAL_EVALUATED` -- a plain fact about what the runtime
    reported evaluating for this exact call, never a claim about whether
    the full untruncated request survived. See `VerifiedExpectedInput`/
    `verify_full_input_preservation()` for the stronger claim."""
    return TokenMeasurement(
        source=TokenMeasurementSource.ACTUAL_EVALUATED, token_count=usage.prompt_tokens,
        request_fingerprint=_rendered_request_fingerprint(request),
        method="runtime_usage_prompt_tokens",
    )


@dataclass(frozen=True)
class VerifiedExpectedInput:
    """A caller-verified, out-of-band-established fact: for the request
    whose rendered prompt hashes to `request_fingerprint`, the true
    (untruncated) input token count is `expected_tokens`. Never
    introspected/computed by this module -- exactly like `RuntimeContext
    Profile.effective_context_tokens`, this must come from independent
    verification (e.g. the same cross-context-size comparison methodology
    used to diagnose the real Class C truncation: measuring the identical
    request against a context configuration independently confirmed large
    enough that truncation could not have occurred there)."""

    request_fingerprint: str
    expected_tokens: int
    method: str


@dataclass(frozen=True)
class FullInputPreservationResult:
    verified: bool
    reason: str
    measurement: TokenMeasurement


def verify_full_input_preservation(
    request: PlannerRequest, actual_evaluated_tokens: int, expected: VerifiedExpectedInput,
) -> FullInputPreservationResult:
    """`verified=True` only when the *current* request's own fingerprint
    matches `expected.request_fingerprint` exactly AND `actual_evaluated_
    tokens == expected.expected_tokens`. A fingerprint mismatch (e.g. a
    retry that added feedback) means `expected` simply does not apply to
    this request -- never silently reused."""
    current_fp = _rendered_request_fingerprint(request)
    if current_fp != expected.request_fingerprint:
        return FullInputPreservationResult(
            verified=False, reason="request_fingerprint_mismatch_expected_measurement_stale",
            measurement=TokenMeasurement(
                source=TokenMeasurementSource.ACTUAL_EVALUATED, token_count=actual_evaluated_tokens,
                request_fingerprint=current_fp, method="runtime_usage_prompt_tokens",
            ),
        )
    if actual_evaluated_tokens < expected.expected_tokens:
        return FullInputPreservationResult(
            verified=False, reason="input_truncated",
            measurement=TokenMeasurement(
                source=TokenMeasurementSource.ACTUAL_EVALUATED, token_count=actual_evaluated_tokens,
                request_fingerprint=current_fp, method="runtime_usage_prompt_tokens",
            ),
        )
    if actual_evaluated_tokens > expected.expected_tokens:
        return FullInputPreservationResult(
            verified=False, reason="unexpected_growth_vs_verified_baseline",
            measurement=TokenMeasurement(
                source=TokenMeasurementSource.UNKNOWN, token_count=None,
                request_fingerprint=current_fp,
                method=(
                    f"runtime_usage_prompt_tokens={actual_evaluated_tokens}_exceeds_"
                    f"expected={expected.expected_tokens}_for_verified_fingerprint"
                ),
            ),
        )
    return FullInputPreservationResult(
        verified=True, reason="full_input_preservation_verified",
        measurement=TokenMeasurement(
            source=TokenMeasurementSource.VERIFIED_FULL_INPUT, token_count=actual_evaluated_tokens,
            request_fingerprint=current_fp, method=f"verified_expected:{expected.method}",
        ),
    )


@dataclass(frozen=True)
class RuntimeContextProfile:
    """A caller-*verified* runtime context configuration -- never
    introspected or assumed by this module itself."""

    model_tag: str
    effective_context_tokens: int
    output_token_budget: int = DEFAULT_OUTPUT_TOKEN_BUDGET
    safety_margin_tokens: int = DEFAULT_SAFETY_MARGIN_TOKENS
    model_digest: str | None = None
    endpoint: str | None = None
    runtime_version: str | None = None
    tool_choice_enforcement: str = "ADVISORY_ONLY_UNVERIFIED"
    output_budget_enforcement_verified: bool = False
    notes: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model_tag, str) or not self.model_tag:
            raise ValueError("model_tag must be a non-empty string")
        for name in ("effective_context_tokens", "output_token_budget", "safety_margin_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def required_context_tokens(self, measured_input_tokens: int) -> int:
        return measured_input_tokens + self.output_token_budget + self.safety_margin_tokens

    def fits(self, measured_input_tokens: int) -> bool:
        return self.required_context_tokens(measured_input_tokens) <= self.effective_context_tokens


@dataclass(frozen=True)
class PreflightResult:
    fits: bool
    measurement: TokenMeasurement
    required_context_tokens: int
    effective_context_tokens: int


def preflight_check(
    request: PlannerRequest, profile: RuntimeContextProfile, *,
    exact_counter: TokenCounter | None = None,
) -> PreflightResult:
    """Pure, no network call unless `exact_counter` is supplied and
    invoked. An `ESTIMATED` overflow blocks pre-call only when clearly
    hopeless, or when an injected `exact_counter` reports `VERIFIED_FULL_
    INPUT` and confirms overflow; a merely marginal `ESTIMATED` overflow
    (no counter, or a counter reporting anything less confident than
    `VERIFIED_FULL_INPUT`) reports `fits=True` and is left to
    `run_planner_trial()`'s post-call verification."""
    if not isinstance(profile, RuntimeContextProfile):
        raise TypeError("preflight_check requires a RuntimeContextProfile")
    estimate = estimate_request_token_measurement(request)
    required_with_estimate = profile.required_context_tokens(estimate.token_count)
    if required_with_estimate <= profile.effective_context_tokens:
        return PreflightResult(
            fits=True, measurement=estimate, required_context_tokens=required_with_estimate,
            effective_context_tokens=profile.effective_context_tokens,
        )
    if exact_counter is not None:
        exact = exact_counter.measure(request)
        if (
            exact.source == TokenMeasurementSource.VERIFIED_FULL_INPUT
            and exact.token_count is not None
        ):
            required_with_exact = profile.required_context_tokens(exact.token_count)
            return PreflightResult(
                fits=required_with_exact <= profile.effective_context_tokens,
                measurement=exact, required_context_tokens=required_with_exact,
                effective_context_tokens=profile.effective_context_tokens,
            )
        # A counter that could not report VERIFIED_FULL_INPUT never
        # authoritatively resolves this -- fall through to the same
        # hopeless-check-or-proceed logic as if none had been supplied.
    if required_with_estimate > profile.effective_context_tokens * _HOPELESS_OVERFLOW_MULTIPLIER:
        return PreflightResult(
            fits=False, measurement=estimate, required_context_tokens=required_with_estimate,
            effective_context_tokens=profile.effective_context_tokens,
        )
    return PreflightResult(
        fits=True, measurement=estimate, required_context_tokens=required_with_estimate,
        effective_context_tokens=profile.effective_context_tokens,
    )


def _run_planner_trial_with_response(
    planner: Planner, request: PlannerRequest, *, snapshot: Snapshot | None = None,
    context_profile: RuntimeContextProfile | None = None,
    exact_counter: TokenCounter | None = None,
    verified_expected_input: VerifiedExpectedInput | None = None,
    check_task_relevance: bool = True,
    allowed_scope: tuple[str, ...] | None = None,
) -> tuple[PlannerTrial, PlannerResponse | None]:
    """The real implementation, returning the raw `PlannerResponse`
    alongside the derived `PlannerTrial` -- `response` is `None` only
    when a pre-call preflight failure meant no call was ever made.
    `PlannerTrial` itself deliberately never carries a raw response (see
    its own docstring); `run_planner_case_with_correction()` uses this
    internal form so `build_attempt_provenance()` can read `usage`/
    `finish_reason` without a second call. `run_planner_trial()` (public)
    is a thin wrapper discarding the response.

    When `context_profile` is supplied: a clearly-hopeless pre-call
    preflight failure skips the call entirely (`INVALID_ENVIRONMENT`,
    latency 0.0). Otherwise the call is made, and:

    1. `finish_reason == "length"` (checked in `classify_planner_
       response`) always yields `OUTPUT_BUDGET_EXHAUSTED`, before
       anything else.
    2. When the response carries real `usage` and `verified_expected_
       input`'s fingerprint matches the *current* request, `actual !=
       expected` yields `INVALID_ENVIRONMENT` (`input_truncated`/
       `unexpected_growth_vs_verified_baseline`) -- the strong,
       fingerprint-bound check.
    3. Otherwise, when the response carries real `usage` and its
       `prompt_tokens` is at or above the effective context, that is
       still `INVALID_ENVIRONMENT` -- a weaker fallback signal (no
       verified baseline applied), never claimed as proof of full input
       preservation.
    4. Otherwise, ordinary content-based classification."""
    if context_profile is not None:
        preflight = preflight_check(request, context_profile, exact_counter=exact_counter)
        if not preflight.fits:
            return PlannerTrial(
                outcome=TrialOutcome.INVALID_ENVIRONMENT, latency_seconds=0.0,
                detail=(
                    f"required_context_tokens={preflight.required_context_tokens} "
                    f"exceeds effective_context_tokens={preflight.effective_context_tokens} "
                    f"(measurement_source={preflight.measurement.source.value}, "
                    f"token_count={preflight.measurement.token_count})"
                ),
            ), None
    started = time.monotonic()
    response = planner.plan(request)
    latency = time.monotonic() - started
    if not isinstance(response, PlannerResponse):
        response = PlannerResponse(
            PlannerOutcome.MALFORMED, error="planner_returned_non_planner_response",
            failure_category=PlannerFailureCategory.NON_TOOL_RESPONSE,
        )

    if context_profile is not None and response.usage is not None:
        actual = response.usage.prompt_tokens
        if verified_expected_input is not None:
            preservation = verify_full_input_preservation(request, actual, verified_expected_input)
            if preservation.reason != "request_fingerprint_mismatch_expected_measurement_stale":
                if not preservation.verified:
                    return PlannerTrial(
                        outcome=TrialOutcome.INVALID_ENVIRONMENT, latency_seconds=latency,
                        detail=(
                            f"{preservation.reason}: actual_evaluated_input_tokens={actual} "
                            f"expected_untruncated_input_tokens={verified_expected_input.expected_tokens}"
                        ),
                    ), response
                # verified: fall through to ordinary classification below.
            elif actual >= context_profile.effective_context_tokens:
                # No verified baseline applies to this (different)
                # fingerprint -- fall back to the weaker ceiling check.
                return PlannerTrial(
                    outcome=TrialOutcome.INVALID_ENVIRONMENT, latency_seconds=latency,
                    detail=(
                        f"observed usage.prompt_tokens={actual} >= "
                        f"effective_context_tokens={context_profile.effective_context_tokens}: "
                        "truncation cannot be excluded (no verified expected-input baseline "
                        "applies to this request fingerprint)"
                    ),
                ), response
        elif actual >= context_profile.effective_context_tokens:
            return PlannerTrial(
                outcome=TrialOutcome.INVALID_ENVIRONMENT, latency_seconds=latency,
                detail=(
                    f"observed usage.prompt_tokens={actual} >= "
                    f"effective_context_tokens={context_profile.effective_context_tokens}: "
                    "truncation cannot be excluded for this attempt"
                ),
            ), response

    outcome, detail, hint = classify_planner_response(
        response, snapshot,
        original_request=request.original_request if check_task_relevance else None,
        allowed_scope=allowed_scope,
    )
    fingerprint = _structural_fingerprint(response.output) if response.output is not None else None
    return PlannerTrial(
        outcome=outcome, latency_seconds=latency, detail=detail,
        plan_state_hint=hint, goal_fingerprint=fingerprint,
    ), response


def run_planner_trial(
    planner: Planner, request: PlannerRequest, *, snapshot: Snapshot | None = None,
    context_profile: RuntimeContextProfile | None = None,
    exact_counter: TokenCounter | None = None,
    verified_expected_input: VerifiedExpectedInput | None = None,
    check_task_relevance: bool = True,
    allowed_scope: tuple[str, ...] | None = None,
) -> PlannerTrial:
    """Exactly one call to `planner.plan(request)` at most, timed,
    classified. See `_run_planner_trial_with_response()` for the full
    behavior; this public wrapper discards the raw response."""
    trial, _response = _run_planner_trial_with_response(
        planner, request, snapshot=snapshot, context_profile=context_profile,
        exact_counter=exact_counter, verified_expected_input=verified_expected_input,
        check_task_relevance=check_task_relevance, allowed_scope=allowed_scope,
    )
    return trial


def run_planner_case(
    planner: Planner, request: PlannerRequest, *, repetitions: int,
    snapshot: Snapshot | None = None, warm_up: bool = False,
    context_profile: RuntimeContextProfile | None = None,
) -> tuple[tuple[PlannerTrial, ...], float | None]:
    """Run `repetitions` identical trials (same `request` every time)."""
    if not isinstance(repetitions, int) or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    cold_start_seconds = None
    if warm_up:
        cold_start_seconds = run_planner_trial(
            planner, request, snapshot=snapshot, context_profile=context_profile,
        ).latency_seconds
    trials = tuple(
        run_planner_trial(planner, request, snapshot=snapshot, context_profile=context_profile)
        for _ in range(repetitions)
    )
    return trials, cold_start_seconds


class ToolTransportOutcome(StrEnum):
    """The minimal-tool-control taxonomy for Class A."""

    TRANSPORT_TIMEOUT = "TRANSPORT_TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    NON_TOOL_RESPONSE = "NON_TOOL_RESPONSE"
    MALFORMED_TOOL_CALL = "MALFORMED_TOOL_CALL"
    GENUINE_TOOL_CALL = "GENUINE_TOOL_CALL"


@dataclass(frozen=True)
class ToolTransportTrial:
    outcome: ToolTransportOutcome
    latency_seconds: float
    detail: str | None = None


def run_tool_transport_trial(adapter: WorkerAdapter, request: WorkerRequest) -> ToolTransportTrial:
    started = time.monotonic()
    try:
        response = adapter.infer(request)
    except WorkerAdapterError as exc:
        latency = time.monotonic() - started
        reason = str(exc)
        outcome = (
            ToolTransportOutcome.TRANSPORT_TIMEOUT if "timeout" in reason
            else ToolTransportOutcome.TRANSPORT_ERROR
        )
        return ToolTransportTrial(outcome=outcome, latency_seconds=latency, detail=_bounded(reason))
    latency = time.monotonic() - started
    validation = validate_response(request, response)
    if validation.executable:
        return ToolTransportTrial(
            outcome=ToolTransportOutcome.GENUINE_TOOL_CALL, latency_seconds=latency,
        )
    if response.kind == WorkerResponseKind.TEXT:
        return ToolTransportTrial(
            outcome=ToolTransportOutcome.NON_TOOL_RESPONSE, latency_seconds=latency,
            detail=_bounded(validation.reason),
        )
    return ToolTransportTrial(
        outcome=ToolTransportOutcome.MALFORMED_TOOL_CALL, latency_seconds=latency,
        detail=_bounded(validation.reason),
    )


def run_tool_transport_case(
    adapter: WorkerAdapter, request: WorkerRequest, *, repetitions: int, warm_up: bool = False,
) -> tuple[tuple[ToolTransportTrial, ...], float | None]:
    if not isinstance(repetitions, int) or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    cold_start_seconds = None
    if warm_up:
        cold_start_seconds = run_tool_transport_trial(adapter, request).latency_seconds
    trials = tuple(run_tool_transport_trial(adapter, request) for _ in range(repetitions))
    return trials, cold_start_seconds


# -- aggregation --------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        raise ValueError("cannot take a percentile of an empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = fraction * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


@dataclass(frozen=True)
class CaseMetrics:
    """Aggregate evidence for one (candidate, case) pair. `INVALID_
    ENVIRONMENT`/`OUTPUT_BUDGET_EXHAUSTED` trials are counted in
    `repetitions`/`non_assessable_count` but excluded from every other
    rate's denominator (`assessable_repetitions`)."""

    case: str
    candidate: str
    repetitions: int
    assessable_repetitions: int
    non_assessable_count: int
    non_assessable_rate: float
    outcome_counts: dict
    genuine_tool_call_rate: float | None
    schema_valid_rate_of_all: float | None
    schema_valid_rate_of_genuine: float | None
    fully_valid_rate: float | None
    non_tool_response_rate: float | None
    transport_failure_rate: float | None
    median_latency_seconds: float | None
    p95_latency_seconds: float | None
    cold_start_seconds: float | None = None


def aggregate_planner_trials(
    case: str, candidate: str, trials: Sequence[PlannerTrial], *,
    cold_start_seconds: float | None = None,
) -> CaseMetrics:
    if not trials:
        raise ValueError("cannot aggregate an empty trial sequence")
    total = len(trials)
    counts: dict = {member.value: 0 for member in TrialOutcome}
    for trial in trials:
        counts[trial.outcome.value] += 1
    non_assessable = sum(counts[o.value] for o in _NON_ASSESSABLE_OUTCOMES)
    assessable = total - non_assessable
    genuine = sum(counts[o.value] for o in _GENUINE_TOOL_CALL_OUTCOMES)
    schema_valid = sum(counts[o.value] for o in _SCHEMA_VALID_OUTCOMES)
    fully_valid = counts[TrialOutcome.VALID_STRUCTURED_PLAN.value]
    non_tool = counts[TrialOutcome.NON_TOOL_RESPONSE.value]
    transport_failed = (
        counts[TrialOutcome.TRANSPORT_TIMEOUT.value] + counts[TrialOutcome.TRANSPORT_ERROR.value]
    )
    assessable_trials = [t for t in trials if t.outcome not in _NON_ASSESSABLE_OUTCOMES]
    latencies = sorted(t.latency_seconds for t in assessable_trials)

    def _rate(numerator: int) -> float | None:
        return (numerator / assessable) if assessable else None

    return CaseMetrics(
        case=case, candidate=candidate, repetitions=total,
        assessable_repetitions=assessable,
        non_assessable_count=non_assessable,
        non_assessable_rate=non_assessable / total,
        outcome_counts=counts,
        genuine_tool_call_rate=_rate(genuine),
        schema_valid_rate_of_all=_rate(schema_valid),
        schema_valid_rate_of_genuine=(schema_valid / genuine) if genuine else None,
        fully_valid_rate=_rate(fully_valid),
        non_tool_response_rate=_rate(non_tool),
        transport_failure_rate=_rate(transport_failed),
        median_latency_seconds=statistics.median(latencies) if latencies else None,
        p95_latency_seconds=_percentile(latencies, 0.95) if len(latencies) >= 2 else None,
        cold_start_seconds=cold_start_seconds,
    )


def observe_determinism(trials: Sequence[PlannerTrial]) -> dict:
    outcomes = {t.outcome for t in trials}
    fingerprints = {t.goal_fingerprint for t in trials if t.goal_fingerprint is not None}
    state_hints = {t.plan_state_hint for t in trials if t.plan_state_hint is not None}
    return {
        "protocol_form_changed": len(outcomes) > 1,
        "distinct_outcomes": sorted(o.value for o in outcomes),
        "structure_changed": len(fingerprints) > 1,
        "distinct_fingerprints": sorted(fingerprints),
        "plan_state_changed": len(state_hints) > 1,
        "distinct_plan_state_hints": sorted(state_hints),
    }


def payload_fingerprint(payload: dict) -> str:
    tools = payload.get("tools") or []
    tool_names = sorted(
        t.get("function", {}).get("name", "") for t in tools if isinstance(t, dict)
    )
    return hashlib.sha256(
        (
            f"model={payload.get('model')};tool_choice={payload.get('tool_choice')};"
            f"temperature={payload.get('temperature')};stream={payload.get('stream')};"
            f"tools={','.join(tool_names)}"
        ).encode()
    ).hexdigest()[:16]


# -- self-correction / retry --------------------------------------------------

DEFAULT_MAX_CORRECTION_ATTEMPTS = 2
DEFAULT_EARLY_STOP_AFTER_CONSECUTIVE_TRANSPORT_FAILURES = 3

_CORRECTABLE_OUTCOMES = frozenset({
    TrialOutcome.NON_TOOL_RESPONSE,
    TrialOutcome.TOOL_SCHEMA_INVALID,
    TrialOutcome.POLICY_VIOLATION,
    TrialOutcome.SCOPE_VIOLATION,
    TrialOutcome.PLAN_VALIDATION_REJECTED,
    TrialOutcome.TASK_NOT_RELEVANT,
})


class QualificationOutcome(StrEnum):
    """The final result of one task instance's whole attempt chain.
    `FAIL_POLICY`/`FAIL_SCOPE` are produced when a plan's own claimed
    paths fail the deterministic policy/scope check (see the module
    docstring's "Scope/policy violation" section) and the correction
    budget is exhausted while that is still the failure. `INVALID_
    ENVIRONMENT` means a verified profile could not hold the request (or
    truncation could not be excluded) at some attempt; `ENVIRONMENT_
    UNVERIFIED` means no profile was ever supplied; `OUTPUT_BUDGET_
    EXHAUSTED` means the response was cut short by a token cap before it
    could be assessed at all. None of these is ever a verdict on the
    model."""

    PASS_FIRST_TRY = "PASS_FIRST_TRY"
    PASS_AFTER_FEEDBACK = "PASS_AFTER_FEEDBACK"
    FAIL_CAPABILITY = "FAIL_CAPABILITY"
    FAIL_POLICY = "FAIL_POLICY"
    FAIL_SCOPE = "FAIL_SCOPE"
    FAIL_RUNTIME = "FAIL_RUNTIME"
    FAIL_TRANSPORT_TIMEOUT = "FAIL_TRANSPORT_TIMEOUT"
    OUTPUT_BUDGET_EXHAUSTED = "OUTPUT_BUDGET_EXHAUSTED"
    INVALID_ENVIRONMENT = "INVALID_ENVIRONMENT"
    ENVIRONMENT_UNVERIFIED = "ENVIRONMENT_UNVERIFIED"


# When the correction budget is exhausted on a still-failing trial, the
# final `QualificationOutcome` is normally `FAIL_CAPABILITY` -- except
# for these two, whose own dedicated final outcome must stay
# distinguishable from a genuine capability failure even after every
# retry chance is spent (see the module docstring's "Scope/policy
# violation" section). Every other correctable `TrialOutcome` not listed
# here still exhausts to `FAIL_CAPABILITY`, unchanged.
_EXHAUSTED_FINAL_OUTCOME: dict[TrialOutcome, QualificationOutcome] = {
    TrialOutcome.SCOPE_VIOLATION: QualificationOutcome.FAIL_SCOPE,
    TrialOutcome.POLICY_VIOLATION: QualificationOutcome.FAIL_POLICY,
}


@dataclass(frozen=True)
class CorrectionFeedback:
    qualification_class: str
    attempt_number: int
    failure_category: str
    expected_behaviour: str
    observed_behaviour: str
    required_correction: str

    def render(self) -> str:
        return (
            f"Attempt {self.attempt_number} failed.\n\n"
            f"Failure category: {self.failure_category}\n\n"
            f"Expected:\n{self.expected_behaviour}\n\n"
            f"Observed:\n{self.observed_behaviour}\n\n"
            f"Required correction:\n{self.required_correction}\n\n"
            "This is a correction attempt for the SAME qualification task."
        )


def _build_feedback(
    qualification_class: str, attempt_number: int, trial: PlannerTrial,
) -> CorrectionFeedback | None:
    if trial.outcome not in _CORRECTABLE_OUTCOMES:
        return None
    if trial.outcome == TrialOutcome.POLICY_VIOLATION:
        paths = trial.detail or "one or more claimed paths"
        return CorrectionFeedback(
            qualification_class=qualification_class, attempt_number=attempt_number,
            failure_category=trial.outcome.value,
            expected_behaviour=(
                "Every affected-file path must be a safe, plain relative path inside the "
                "repository -- never a path outside it, never a '.git' path, never containing "
                "'..'."
            ),
            observed_behaviour=f"The following claimed path(s) are not allowed: {paths}",
            required_correction=(
                "Remove or correct the path(s) named above. Solve the original task using only "
                "safe, in-repository paths; do not change unrelated parts of the plan."
            ),
        )
    if trial.outcome == TrialOutcome.SCOPE_VIOLATION:
        paths = trial.detail or "one or more claimed paths"
        return CorrectionFeedback(
            qualification_class=qualification_class, attempt_number=attempt_number,
            failure_category=trial.outcome.value,
            expected_behaviour="Only the paths explicitly allowed for this task may be affected.",
            observed_behaviour=(
                f"The following claimed path(s) are outside the allowed scope: {paths}"
            ),
            required_correction=(
                "Revert the out-of-scope change(s) named above and solve the original task "
                "without affecting any path outside the allowed scope."
            ),
        )
    if trial.outcome == TrialOutcome.NON_TOOL_RESPONSE:
        return CorrectionFeedback(
            qualification_class=qualification_class, attempt_number=attempt_number,
            failure_category=trial.outcome.value,
            expected_behaviour=(
                f"Call the '{TOOL_NAME}' tool exactly once with your complete structured plan."
            ),
            observed_behaviour="No valid structured tool call was received.",
            required_correction=(
                f"Respond only with a single tool call to '{TOOL_NAME}'. Do not respond with "
                "plain text, prose, or an explanation -- respond only with the tool call."
            ),
        )
    if trial.outcome == TrialOutcome.TOOL_SCHEMA_INVALID:
        return CorrectionFeedback(
            qualification_class=qualification_class, attempt_number=attempt_number,
            failure_category=trial.outcome.value,
            expected_behaviour=(
                f"A '{TOOL_NAME}' tool call whose arguments exactly match its declared schema."
            ),
            observed_behaviour=(
                "A tool call was made, but its arguments did not match the required schema."
            ),
            required_correction=(
                "Re-check every field name and type against the tool's declared schema. "
                "'goal' is required; every other field, if present, must match its declared type."
            ),
        )
    if trial.outcome == TrialOutcome.TASK_NOT_RELEVANT:
        return CorrectionFeedback(
            qualification_class=qualification_class, attempt_number=attempt_number,
            failure_category=trial.outcome.value,
            expected_behaviour="A goal that directly addresses the ORIGINAL_REQUEST given.",
            observed_behaviour="Your goal appears unrelated to the original task you were given.",
            required_correction=(
                "Revise your plan so its goal and changes directly address the ORIGINAL_REQUEST "
                "given, using only the REPOSITORY_CONTEXT already provided."
            ),
        )
    issues = trial.detail or "one or more claims were rejected"
    return CorrectionFeedback(
        qualification_class=qualification_class, attempt_number=attempt_number,
        failure_category=trial.outcome.value,
        expected_behaviour="Every concrete repository claim matches real repository evidence.",
        observed_behaviour=f"The following claims were rejected against real evidence: {issues}",
        required_correction=(
            "Correct only the rejected claims above using the same REPOSITORY_CONTEXT already "
            "provided; do not invent new facts and do not change unrelated parts of the plan."
        ),
    )


@dataclass(frozen=True)
class AttemptProvenance:
    """A safe, hashable binding from one qualification attempt to the
    exact request/runtime/profile that produced it -- never raw
    repository text, only bounded sha256 fingerprints."""

    qualification_class: str
    model_tag: str
    model_digest: str | None
    request_fingerprint: str
    task_fingerprint: str
    repo_context_fingerprint: str
    schema_fingerprint: str
    effective_context_tokens: int
    required_context_tokens: int
    token_measurement_source: str
    token_measurement_method: str
    measured_input_tokens: int | None
    expected_untruncated_input_tokens: int | None
    actual_evaluated_input_tokens: int | None
    full_input_preservation_verified: bool
    output_token_budget: int
    safety_margin_tokens: int
    output_budget_enforced: bool
    requested_max_tokens: int | None
    completion_tokens: int | None
    finish_reason: str | None
    attempt_number: int
    feedback_fingerprint: str | None
    endpoint: str | None
    runtime_version: str | None
    tool_choice_enforcement: str
    outcome: str
    environment_valid: bool


def build_attempt_provenance(
    *, qualification_class: str, request: PlannerRequest, profile: RuntimeContextProfile,
    attempt_number: int, trial: PlannerTrial,
    response: PlannerResponse | None = None,
    verified_expected_input: VerifiedExpectedInput | None = None,
) -> AttemptProvenance:
    """Pure; never mutates `request`/`trial`, never makes a network call.
    `response`, when supplied (the real response this attempt produced),
    provides `usage`/`finish_reason` evidence; without it, only the
    pre-call `ESTIMATED` measurement is recorded."""
    prompt = _render_planning_prompt(request)
    expected_tokens: int | None = None
    actual_tokens: int | None = None
    full_input_verified = False
    if response is not None and response.usage is not None:
        actual_tokens = response.usage.prompt_tokens
        if verified_expected_input is not None:
            preservation = verify_full_input_preservation(
                request, actual_tokens, verified_expected_input,
            )
            measurement = preservation.measurement
            full_input_verified = preservation.verified
            if preservation.reason != "request_fingerprint_mismatch_expected_measurement_stale":
                expected_tokens = verified_expected_input.expected_tokens
        else:
            measurement = actual_evaluated_measurement_from_usage(request, response.usage)
    else:
        measurement = estimate_request_token_measurement(request)
    required = profile.required_context_tokens(measurement.token_count or 0)
    return AttemptProvenance(
        qualification_class=qualification_class, model_tag=profile.model_tag,
        model_digest=profile.model_digest,
        request_fingerprint=_fingerprint(prompt),
        task_fingerprint=_fingerprint(request.original_request),
        repo_context_fingerprint=_fingerprint(
            json.dumps(render_bounded_context(request), sort_keys=True),
        ),
        schema_fingerprint=_fingerprint(json.dumps(_TOOL_SCHEMAS[TOOL_NAME], sort_keys=True)),
        effective_context_tokens=profile.effective_context_tokens,
        required_context_tokens=required,
        token_measurement_source=measurement.source.value,
        token_measurement_method=measurement.method,
        measured_input_tokens=measurement.token_count,
        expected_untruncated_input_tokens=expected_tokens,
        actual_evaluated_input_tokens=actual_tokens,
        full_input_preservation_verified=full_input_verified,
        output_token_budget=profile.output_token_budget,
        safety_margin_tokens=profile.safety_margin_tokens,
        output_budget_enforced=(
            profile.output_budget_enforcement_verified and request.output_token_budget is not None
        ),
        requested_max_tokens=request.output_token_budget,
        completion_tokens=(
            response.usage.completion_tokens if response is not None and response.usage else None
        ),
        finish_reason=response.finish_reason if response is not None else None,
        attempt_number=attempt_number,
        feedback_fingerprint=(
            _fingerprint(request.prior_attempt_feedback)
            if request.prior_attempt_feedback is not None else None
        ),
        endpoint=profile.endpoint, runtime_version=profile.runtime_version,
        tool_choice_enforcement=profile.tool_choice_enforcement,
        outcome=trial.outcome.value,
        environment_valid=trial.outcome != TrialOutcome.INVALID_ENVIRONMENT,
    )


@dataclass(frozen=True)
class QualificationAttemptResult:
    """The full attempt chain for one task instance. `provenance` is
    empty unless a `context_profile` was supplied."""

    qualification_class: str
    outcome: QualificationOutcome
    attempts: tuple[PlannerTrial, ...]
    feedback: tuple[CorrectionFeedback, ...]
    provenance: tuple[AttemptProvenance, ...] = field(default_factory=tuple)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def final_trial(self) -> PlannerTrial | None:
        return self.attempts[-1] if self.attempts else None


def run_planner_case_with_correction(
    planner: Planner, request: PlannerRequest, *, qualification_class: str,
    snapshot: Snapshot | None = None,
    max_correction_attempts: int = DEFAULT_MAX_CORRECTION_ATTEMPTS,
    context_profile: RuntimeContextProfile | None = None,
    exact_counter: TokenCounter | None = None,
    verified_expected_input: VerifiedExpectedInput | None = None,
    unsafe_allow_unverified_environment: bool = False,
    allowed_scope: tuple[str, ...] | None = None,
) -> QualificationAttemptResult:
    """One task instance, with up to `max_correction_attempts` same-model
    retries -- only when correctable.

    **Requires a verified `context_profile` for a scoreable result** (see
    the module docstring) unless `unsafe_allow_unverified_environment=
    True` is explicitly passed.

    `verified_expected_input`, when supplied, only ever applies to the
    exact request fingerprint it was verified for -- typically attempt
    1's pristine request. A retry (whose added feedback changes the
    fingerprint) is always measured fresh; the strong preserved-input
    guarantee simply does not carry over, and the weaker ceiling-based
    check applies instead (see `run_planner_trial`).

    `allowed_scope`, when supplied, is this qualification task's own
    declared set of allowed path prefixes, checked identically on every
    attempt including retries (see `classify_planner_response`'s own
    docstring) -- never relaxed on a retry, and never sent to the model
    itself."""
    if not isinstance(max_correction_attempts, int) or max_correction_attempts < 0:
        raise ValueError("max_correction_attempts must be a non-negative integer")
    if context_profile is None and not unsafe_allow_unverified_environment:
        return QualificationAttemptResult(
            qualification_class=qualification_class,
            outcome=QualificationOutcome.ENVIRONMENT_UNVERIFIED,
            attempts=(), feedback=(), provenance=(),
        )
    attempts: list[PlannerTrial] = []
    feedback_chain: list[CorrectionFeedback] = []
    provenance_chain: list[AttemptProvenance] = []
    current_request = request
    if (
        context_profile is not None and context_profile.output_budget_enforcement_verified
        and current_request.output_token_budget is None
    ):
        current_request = replace(
            current_request, output_token_budget=context_profile.output_token_budget,
        )
    attempt_number = 1
    while True:
        trial, response = _run_planner_trial_with_response(
            planner, current_request, snapshot=snapshot, context_profile=context_profile,
            exact_counter=exact_counter, verified_expected_input=verified_expected_input,
            allowed_scope=allowed_scope,
        )
        attempts.append(trial)
        if context_profile is not None:
            provenance_chain.append(build_attempt_provenance(
                qualification_class=qualification_class, request=current_request,
                profile=context_profile, attempt_number=attempt_number, trial=trial,
                response=response, verified_expected_input=verified_expected_input,
            ))
        if trial.outcome == TrialOutcome.INVALID_ENVIRONMENT:
            outcome = QualificationOutcome.INVALID_ENVIRONMENT
            break
        if trial.outcome == TrialOutcome.OUTPUT_BUDGET_EXHAUSTED:
            outcome = QualificationOutcome.OUTPUT_BUDGET_EXHAUSTED
            break
        if trial.outcome == TrialOutcome.VALID_STRUCTURED_PLAN:
            outcome = (
                QualificationOutcome.PASS_FIRST_TRY if attempt_number == 1
                else QualificationOutcome.PASS_AFTER_FEEDBACK
            )
            break
        if trial.outcome == TrialOutcome.TRANSPORT_TIMEOUT:
            outcome = QualificationOutcome.FAIL_TRANSPORT_TIMEOUT
            break
        if trial.outcome == TrialOutcome.TRANSPORT_ERROR:
            outcome = QualificationOutcome.FAIL_RUNTIME
            break
        if attempt_number > max_correction_attempts:
            outcome = _EXHAUSTED_FINAL_OUTCOME.get(
                trial.outcome, QualificationOutcome.FAIL_CAPABILITY,
            )
            break
        feedback = _build_feedback(qualification_class, attempt_number, trial)
        assert feedback is not None  # trial.outcome is in _CORRECTABLE_OUTCOMES here
        feedback_chain.append(feedback)
        current_request = replace(current_request, prior_attempt_feedback=feedback.render())
        attempt_number += 1
    return QualificationAttemptResult(
        qualification_class=qualification_class, outcome=outcome,
        attempts=tuple(attempts), feedback=tuple(feedback_chain),
        provenance=tuple(provenance_chain),
    )


def run_corrected_planner_case(
    planner: Planner, request: PlannerRequest, *, qualification_class: str, repetitions: int,
    snapshot: Snapshot | None = None,
    max_correction_attempts: int = DEFAULT_MAX_CORRECTION_ATTEMPTS,
    early_stop_after_consecutive_transport_failures: int = (
        DEFAULT_EARLY_STOP_AFTER_CONSECUTIVE_TRANSPORT_FAILURES
    ),
    context_profile: RuntimeContextProfile | None = None,
    exact_counter: TokenCounter | None = None,
    verified_expected_input: VerifiedExpectedInput | None = None,
    unsafe_allow_unverified_environment: bool = False,
    allowed_scope: tuple[str, ...] | None = None,
) -> tuple[tuple[QualificationAttemptResult, ...], bool]:
    """`repetitions` independent task instances. Without a verified
    `context_profile` (and no explicit `unsafe_allow_unverified_
    environment=True`), every repetition is cheaply `ENVIRONMENT_
    UNVERIFIED` with zero network calls."""
    if not isinstance(repetitions, int) or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    if (
        not isinstance(early_stop_after_consecutive_transport_failures, int)
        or early_stop_after_consecutive_transport_failures < 1
    ):
        raise ValueError(
            "early_stop_after_consecutive_transport_failures must be a positive integer",
        )
    transport_outcomes = frozenset({
        QualificationOutcome.FAIL_TRANSPORT_TIMEOUT, QualificationOutcome.FAIL_RUNTIME,
    })
    results: list[QualificationAttemptResult] = []
    consecutive_transport_failures = 0
    early_stopped = False
    for _ in range(repetitions):
        result = run_planner_case_with_correction(
            planner, request, qualification_class=qualification_class, snapshot=snapshot,
            max_correction_attempts=max_correction_attempts, context_profile=context_profile,
            exact_counter=exact_counter, verified_expected_input=verified_expected_input,
            unsafe_allow_unverified_environment=unsafe_allow_unverified_environment,
            allowed_scope=allowed_scope,
        )
        results.append(result)
        if result.outcome in transport_outcomes:
            consecutive_transport_failures += 1
        else:
            consecutive_transport_failures = 0
        if consecutive_transport_failures >= early_stop_after_consecutive_transport_failures:
            early_stopped = True
            break
    return tuple(results), early_stopped
