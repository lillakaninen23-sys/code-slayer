# CSLR Verification Standard

**Status:** normative  
**Scope:** all CSLR development, operations, certification, routing, updates,
security decisions, status reporting, agent work, and human/operator claims.

## 1. Absolute principle: no inherent trust

No actor or artifact is inherently trusted merely because it made a claim.

This includes, without exception:

- the project owner/operator;
- a user;
- ChatGPT, Grok, Claude, Codex, or any other assistant;
- a CSLR worker, reviewer, planner, coder, security worker, or future Lead/Manager;
- documentation, handoff notes, memory, roadmap/status prose, or a previous chat;
- a commit message, branch name, tag, PR description, or release note;
- quoted or copied terminal output;
- a test report or benchmark summary;
- runtime configuration, model tags, model aliases, or environment variables;
- WebUI state that is not backed by canonical durable state;
- external services or APIs that merely report their own state.

A claim may be useful input. It is not evidence by itself.

The required flow for every **material** claim is:

```text
CLAIM
  ↓
INDEPENDENT VERIFICATION
  ↓
EVIDENCE
  ↓
POLICY / INVARIANT CHECK
  ↓
ACTION
```

If independent verification is not available, the correct status is:

```text
UNVERIFIED
```

Never silently replace missing evidence with assumption, confidence,
plausibility, familiarity, memory, or consensus.

## 2. What counts as material

A claim is material when getting it wrong could change a decision, mutate
state, weaken a boundary, waste significant work, hide a regression, or
misrepresent project status.

Material claims include, at minimum:

- what commit/branch/version is actually running;
- whether something is committed or pushed;
- whether local HEAD matches origin;
- whether tests/lint/type checks passed in the checkout being acted on;
- whether a database, state root, evidence directory, or worktree is isolated;
- whether a model/runtime is the expected one;
- model digest, runtime version, context/runtime identity, normalizer/protocol;
- whether a certificate exists, is current, and is bound to the same runtime;
- whether a worker is eligible for a production role;
- whether a permission, trust decision, lease, checkpoint, or authority exists;
- whether a side effect occurred or did not occur;
- whether an update is safe to apply;
- whether a file or checkout is clean;
- whether a security invariant holds;
- whether an external/local service is actually reachable and serving the expected thing.

Verification depth should be proportional to risk, but **material claims are
never promoted to VERIFIED solely from assertion**.

## 3. Evidence labels

CSLR documentation, WebUI, reports, and agent responses should use precise
status language. Preferred labels:

| Label | Meaning |
| --- | --- |
| `CLAIMED` | stated by an actor, not independently checked |
| `OBSERVED` | directly observed once, but not necessarily identity-bound or sufficient for trust |
| `CONFIG_BOUND` | comes from approved configuration; not independently live-measured |
| `LIVE_ATTESTED` | checked against the live target/runtime through the canonical attestation path |
| `VERIFIED` | independently checked with sufficient evidence for the decision being made |
| `UNVERIFIED` | evidence is missing or cannot currently be obtained |
| `MISMATCH` | independently observed state conflicts with the expected/approved state |
| `STALE` | evidence may once have been valid but is no longer current enough for the decision |

Do not use `VERIFIED` as a synonym for "someone reported PASS".

## 4. Verification must be independent of the claim where practical

The verifier should not simply ask the same actor to restate its claim.

Examples:

### Git / source state

Claim:

> "Certification Center is committed and pushed."

Required evidence may include:

- query the remote repository directly;
- resolve the remote branch SHA;
- inspect the commit and parent;
- inspect changed files/diff;
- after fetch, compare local HEAD with remote HEAD.

A commit message saying "add Certification Center" is not proof that all
required files were included.

### Tests

Claim:

> "1970 tests passed."

For a decision that depends on this result, run the relevant test command in
the exact checkout/environment being accepted, or obtain authoritative CI
evidence bound to that exact commit and configuration.

A pasted summary from an agent is `CLAIMED` until verified.

### Runtime/model identity

Claim/configuration:

> "This worker is qwen3-coder-ctx16k:30b."

Verify the live target according to the canonical runtime-attestation path,
including immutable digest where available. Model name/tag alone is
insufficient.

A changed digest is a new runtime identity for certification purposes unless
the governing policy explicitly proves otherwise.

### Certification

Claim:

> "Baseline Security passed."

Verify, through canonical durable state:

- certificate row exists where expected;
- outcome is valid;
- evidence reference/hash is valid;
- evidence rereads successfully;
- worker/runtime binding matches;
- required cases are present;
- canary actions were not executed where prohibited;
- no hard disqualifier invalidates eligibility;
- the certificate is current for the same runtime identity.

Never manufacture production eligibility from a presentation-layer PASS.

### Isolation

Claim:

> "This uses validation state, not production."

Verify resolved paths/state identities and, where relevant, before/after
integrity evidence for production state. Similar-looking path strings are not
enough if symlinks, aliases, or alternate roots can collapse them.

## 5. Human approval is authority, not factual proof

The operator may authorize an action even when some facts remain uncertain,
if policy permits that uncertainty. Authorization and truth are different
concepts.

For example:

- "I approve running this command" may grant authority to run it.
- It does **not** prove that the command is safe, that the checkout is clean,
  or that the target is the expected runtime.

CSLR must preserve this distinction.

## 6. Memory, handoffs, and documentation

Memory and project sources are context, not automatic truth.

Use them to know **what to verify**, not as a substitute for verification.

When current live/repository state conflicts with memory, notes, roadmap prose,
or a previous chat:

1. preserve the conflicting claim as historical context;
2. verify the current state;
3. mark the old claim stale/superseded where appropriate;
4. do not rewrite history to make the conflict disappear.

Current-state documents should include freshness/supersession rules where
needed.

## 7. Fail closed for security- or authority-relevant uncertainty

For security, permissions, production eligibility, runtime identity,
certification, update integrity, and destructive/mutating actions:

- missing evidence → no implicit allow;
- ambiguous identity → no certification/promotion;
- mismatch → block and surface the mismatch;
- stale evidence → reverify;
- unverifiable destructive target → do not execute.

"Probably correct" is not a security state.

## 8. Reports from agents and tools

Every agent working on CSLR must distinguish:

1. what it was told;
2. what it directly observed;
3. what it independently verified;
4. what remains unverified.

Final reports must not collapse those categories.

If an agent lacks access to perform a required verification, it must say so
and provide the smallest deterministic check needed to obtain evidence.

Do not say:

> "Everything is committed and pushed."

when the agent only ran local `git status`.

Say:

> "Local working tree is clean. Remote push state is UNVERIFIED because I
> cannot query origin."

unless remote state was actually checked.

## 9. WebUI requirements

Operator-facing CSLR surfaces should prefer evidence-backed state and expose
provenance where useful.

The UI should not silently convert:

- worker self-report → VERIFIED;
- configured model tag → LIVE_ATTESTED;
- validation certificate → production eligibility;
- cached status → current status;
- client-provided value → server-owned identity.

Where useful, display provenance such as:

```text
CONFIG_BOUND
LIVE_ATTESTED
VERIFIED
UNVERIFIED
MISMATCH
```

The browser remains a control surface, not a trust authority.

## 10. Update and deployment verification

Before an update/promotion is accepted, verify at the appropriate level:

- expected source branch/ref;
- exact commit SHA;
- clean/known local state;
- remote relationship (fast-forward/divergence);
- applicable tests/checks;
- schema/migration compatibility;
- persistent config/state preservation;
- service health after restart;
- actual running version/commit after deployment.

An update is not complete merely because `git pull` returned success.

## 11. Coding-agent operating procedure

Before making changes:

1. inspect the actual current checkout/state;
2. identify claims inherited from the prompt/handoff;
3. independently verify all material prerequisites available to the agent;
4. explicitly mark unavailable verification as `UNVERIFIED`;
5. preserve unrelated user-owned files.

Before committing:

1. inspect the diff;
2. run the required checks;
3. confirm only intended files are staged;
4. verify the parent/base is the expected one.

After committing/pushing, when those actions are required:

1. verify the resulting commit SHA;
2. verify parent SHA;
3. verify working-tree status;
4. verify the remote branch points to the expected SHA;
5. report evidence, not only the intended action.

## 12. Contradictions

When two sources disagree, do not choose whichever source sounds more
authoritative.

Resolve the contradiction by checking the underlying state.

Examples:

- agent report vs GitHub → inspect GitHub;
- local config vs live Ollama → attest live runtime and report mismatch;
- roadmap vs repository implementation → inspect repository/tests and mark the
  roadmap stale if appropriate;
- user statement vs durable state → distinguish requested intent from observed
  state.

No person or model receives an exemption from this rule.

## 13. Canonical short form

For prompts, checklists, UI copy, and reviews, this standard may be referenced
by its short form:

> **NO INHERENT TRUST. CLAIM → VERIFY → EVIDENCE → ACTION.  
> If it cannot be independently verified, mark it UNVERIFIED.**

The short form does not replace the requirements above.
