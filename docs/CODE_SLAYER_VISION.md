# Code Slayer — Vision

**Status:** authoritative long-term guidance. Not a spec for what exists today.
**Audience:** every worker that ever acts on this project — Claude, Codex, Astra, Qwen, future models, and Code Slayer's own future self.
**Relationship to other documents:** the *Foundation Plan* (Revision 2.1) is the authoritative near-term architecture for the durable core. The ADRs in `adr/` record specific decisions already made and implemented. This document is the horizon those decisions are aimed at — it does not override them, and where it describes something not yet built, that absence is deliberate, not an oversight (see [Guiding Constraints](#guiding-constraints)). [`docs/SECURITY_PRIVACY_ARCHITECTURE.md`](SECURITY_PRIVACY_ARCHITECTURE.md), [`docs/PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md), [`docs/PRODUCT_PRINCIPLES.md`](PRODUCT_PRINCIPLES.md), and [`docs/OPERATIONS_UX.md`](OPERATIONS_UX.md) are this vision's governance-layer counterparts (§62) — normative specifications for security/privacy, permissions, product UX, and the operational surface, respectively.

---

## 1. Core identity

Code Slayer is **not**:

- a coding chatbot
- a Claude Code clone
- a Codex clone
- a wrapper around one LLM
- a Python-only agent

Code Slayer **is** intended to become a **local-first autonomous software engineering platform** — a system whose job is to design, build, modify, test, debug, maintain, and eventually operate on increasingly complex software systems, across the full range of what "software" means:

CLI applications · desktop applications · web applications · backend/API systems · mobile applications · databases · AI systems · infrastructure · distributed systems · embedded software · C/C++ systems software · compilers/runtimes · drivers · kernels · and, eventually, complete operating systems.

**Self-repair is a capability, not the primary product.** The ability to diagnose and fix its own bugs is one instance of a general engineering capability, not what Code Slayer is *for*.

---

## 2. Core principle

This is the sentence every other design decision in this document answers to:

> **MODELS PROPOSE.**
> **TOOLS EXECUTE.**
> **EVIDENCE DECIDES.**
> **STATE REMEMBERS.**
> **POLICY CONTROLS.**

A model's output is never itself an action, never itself a fact, and never itself a completed task. It is a proposal that a tool executes under policy, whose outcome is recorded as evidence, in state that survives the model, the process, and the machine.

A second principle governs what "progress" means for the system as a whole:

> Code Slayer should maximize **verified new capability and useful knowledge per unit of compute/time** — not lines of code, number of attempts, or database size.

More code is not more capability. More attempts is not more progress. A bigger database is not more knowledge. Every later section of this document is, in one way or another, an elaboration of that sentence.

A third principle names the trade-off explicitly, because it is easy to erode by accident:

> **QUALITY > THROUGHPUT.**

Code Slayer is not designed to maximize how many actions an AI can perform. It is designed to maximize how much **verified, recoverable, high-quality** software-engineering work can be safely delegated. Concretely, this means preferring:

- fewer high-confidence actions over many speculative ones
- verification over speed
- correctness over apparent productivity
- traceability over opaque autonomy
- recoverability over aggressive continuation
- deterministic evidence over model confidence

**Do not optimize token count, task count, tool-call count, completion speed, or apparent activity at the expense of correctness or safety.** A worker that produces many edits, many tool calls, or fast output is not better unless the resulting work is verified — §61 returns to this as the document's closing word.

---

## 3. Model-agnostic architecture

No model family, model size, provider, or endpoint is architecturally permanent.

**Qwen is the intended initial local production model family — never hardcoded into core orchestration.** The same is true of every future model. Core orchestration logic (the state machine, the policy engine, the lease/checkpoint mechanics) must never contain a conditional keyed on a provider or model name (this is already an implemented invariant — INV-10 in the Foundation Plan, and ADR 0006).

The long-term architecture that keeps this true as the system grows:

- **Model Registry** — what models exist, their capabilities, cost, and status.
- **Provider Registry** — how to actually reach a model (local endpoint, cloud API, embedded runtime).
- **Capability Registry** — what a model/provider pair is actually good at, backed by evidence (§13), not marketing.
- **Role routing** — which role a task phase needs, independent of which model fills it.
- **Benchmark-driven routing** — which model fills that role right now, decided by measurement (§23), re-decided as measurements change.

Roles are a vocabulary, not a fixed cast: `planner`, `coder`, `debugger`, `reviewer`, `repair`, `test engineer`, `security reviewer`, `architecture reviewer`, `deep reviewer`, `investigator`, `task generator`, and others as they prove useful — including `prompt analyst`, `question gate`, and `training/eval worker` (§32, §33, §44). A single physical model may fill several roles; a role may be filled by different models over time as evidence changes.

**A future model must be replaceable without redesigning core state-machine logic.** If adopting a new model ever requires touching `core/`, the registry design has failed at its one job.

This registry design is what a model is *allowed to reach* in principle. Whether a given model/provider/runtime is currently *trusted* to actually exercise a role autonomously is a separate, evidence-gated question — see Model preflight/conformance (§41), Model trust levels (§42), and the Model compatibility matrix (§43).

---

## 4. Normal runtime vs. cloud escalation

Normal production runtime is **local-first**:

```
Code Slayer → local models → local tools/environments
```

Claude, Astra, and any other cloud model are **optional maintenance/escalation workers**, never a required dependency for normal operation. This is already a decided, implemented policy shape (ADR 0006):

```
cloud_escalation = disabled   # default
cloud_escalation = manual
cloud_escalation = maintenance
```

Cloud workers must never become required for normal operation, at any point in this system's growth — this is a permanent architectural constraint, not a Phase 1 convenience.

Every cloud handoff must be **bounded, audited, and filtered**. No automatic upload, ever, of:

- whole repositories
- `.env` files
- tokens or credentials
- SSH keys
- any owner-configured secret path

This is a permanent invariant, not a policy default that a future convenience feature is allowed to loosen quietly.

---

## 5. Future WebUI

Code Slayer should eventually provide a local WebUI — an **engineering control room**, not a chat interface.

**The WebUI must use the same backend/core APIs as the CLI.** There is exactly one business-logic layer; the CLI and the WebUI are two renderings of it. A feature that exists in one and not the other because of separate logic paths is a bug in the architecture, not a missing feature.

Potential major surfaces, once built:

Projects · Tasks · Workers · Models · Architecture · Diff · Tests · Builds · Environments · Checkpoints · Artifacts · Knowledge · Skills · Training · Benchmarks · Audit · Settings.

This list describes shape, not a commitment to build all of it, or in this order — see the [Roadmap](ROADMAP.md).

---

## 6. Operating modes

These are long-term product modes, not separate products — they all sit on top of the same durable core (task state, audit, leases, checkpoints), differing in what they're pointed at and what "done" means.

- **Project Mode** — build and modify real software projects.
- **Maintenance Mode** — debug, repair, upgrade, and maintain existing software.
- **Lab / Training Mode** — autonomously solve coding and engineering challenges in isolated environments (§8).
- **Research Mode** — acquire and verify new knowledge from documentation, Git history, repositories, and the internet (§16).
- **Self-Development Mode** — diagnose, test, and improve Code Slayer itself through isolated candidate versions, never in place (§24).

---

## 7. Languages and toolchains

Code Slayer's core must not assume a particular programming language. Target languages and toolchains may eventually include Python, C, C++, C#, Rust, Go, Java, JavaScript, TypeScript, HTML/CSS and frontend frameworks, assembly, Zig, CUDA, embedded toolchains, and languages that don't exist yet.

Language-specific behavior belongs in:

- a **Toolchain Registry**
- **project adapters**
- an **Environment Registry**
- **model capabilities**

— never in giant language-specific conditionals inside core. The same discipline that keeps providers out of `core/` keeps languages out of it too.

---

## 8. Lab / Training Mode

Lab Mode is a first-class long-term capability: unattended coding sessions in which Code Slayer

1. receives or generates a challenge
2. analyzes the problem
3. writes an implementation
4. builds/runs/tests it
5. investigates any failure
6. repairs it
7. independently reviews the result
8. collects objective evidence
9. records verified lessons
10. updates its skill profile
11. chooses the next useful challenge

Training must occur in **protected, disposable environments**. Code Slayer must **never give unrestricted host access to arbitrary generated or downloaded code.** Possible isolation layers: sandbox, rootless container, VM, QEMU, and future emulator/hardware runners — chosen per task, not a single universal sandbox that outgrows its purpose.

---

## 9. Curriculum and task generation

The coding worker should not be solely responsible for generating and grading its own exercises — that is exactly the setup that lets a model reward itself for the wrong thing.

```
Skill Model → Curriculum Manager → Task Generator → Coder → Deterministic Judge → Independent Reviewer
```

Task generation may use a general model different from the coding specialist being trained. The generator should weigh: weak skills, recently trained skills, mastery, repeated task fingerprints, failure patterns, desired difficulty, and retention testing. **Avoid repeatedly training already-mastered skills** — that spends compute without buying capability, which violates the core principle in §2.

---

## 10. Skill model

Code Slayer should maintain a structured estimate of its own capability, by category — language, framework, debugging, concurrency, memory safety, architecture, frontend, databases, networking, testing, systems programming, and others as they become measurable.

**Mastery must be based primarily on evidence, not model self-confidence.** Adaptive training prioritizes weaknesses; mastered areas still receive occasional regression/retention checks, because capability that isn't re-verified is a claim, not a fact.

---

## 11. Loop / no-progress detection

Code Slayer must not endlessly repeat the same repair strategy. Track: failure signatures, patch similarity, repeated hypotheses, test deltas, benchmark deltas, and the number of no-progress attempts.

Repeated no-progress should trigger, in order: stop the current strategy → re-evaluate assumptions → gather new evidence → form a new hypothesis → consider a different worker/model → optionally escalate.

**Attempt budgets must be configurable, not buried as hardcoded assumptions** — a number a future engineer has to go find in code is a number that will quietly become wrong. §45 and §46 generalize this pattern (watchdogs and action budgets) beyond just repair loops.

---

## 12. Evidence-driven correctness

A model saying "looks correct" is not proof. The long-term verification stack may include: unit tests, integration tests, hidden tests, property-based testing, differential testing, mutation testing, fuzzing, sanitizers, static analysis, type checking, performance benchmarks, runtime health checks, installation testing, reboot testing, VM testing, and independent adversarial review.

**Reviewer output is evidence, not absolute truth.** Objective, machine-verifiable evidence dominates model opinion — including a reviewer model's opinion. This is the same posture the Foundation Plan already takes toward its own audit hash chain: an integrity signal, not a claim of infallibility. **Model consensus is not evidence either** — two models agreeing does not make something true; §34 defines the full authority hierarchy this generalizes to, and §47/§48 define how independent review is actually structured and staffed.

---

## 13. VM / environment testing

Code Slayer should eventually verify built software in clean environments — sandbox, container, VM, QEMU, emulator, and eventually physical test hosts. Examples of what "verified" should mean per domain:

- **Applications:** build → fresh VM → install → launch → test → reboot → upgrade → uninstall.
- **Web systems:** deploy backend/frontend → browser automation → persistence tests → accessibility/visual tests.
- **Systems software:** build → QEMU/VM → boot → serial logs → integration tests → crash detection.

This layer is a prerequisite for the [Systems / OS](ROADMAP.md#systems--os) stage of the roadmap — kernel and OS work cannot be evidence-driven without it.

---

## 14. Knowledge engine

Three things that must never be treated as the same thing:

- **Audit** — the complete history of what happened. Append-only, already implemented (`audit_events`, ADR 0004). Nothing is ever removed from it and nothing is ever curated out of it.
- **Knowledge** — curated, verified, useful lessons distilled *from* audit and experience. Small relative to audit, and actively maintained.
- **Skill Model** — what Code Slayer appears to be good or bad at, derived from evidence (§10), not a restatement of either of the above.

Knowledge entries carry provenance and a lifecycle state: `ACTIVE`, `SUPERSEDED`, `REVOKED`, `UNCERTAIN`, `STALE`, `ARCHIVED`. New information should not create duplicate knowledge — use semantic deduplication and consolidation instead of appending near-duplicates forever.

The shape this implies at retrieval time:

```
large archive  →  curated knowledge  →  relevant retrieval  →  minimal model context
```

Storage may become large. Working knowledge should remain compact. Task context should be smaller still. A design that lets any of those three grow in lockstep with the others has broken this principle.

Audit's role as durable, replayable ground truth — already partially real today via `audit_events` — is extended by §60 (Audit / replay) into a long-term capability to answer *what happened, using what evidence, under whose authority* for any past decision.

---

## 15. Learning from failures

Code Slayer should learn from successful fixes, failed attempts, regressions, incorrect hypotheses, reviewer findings, crash recovery, and historical Git fixes. A useful verified experience record may contain: problem, evidence, hypotheses, failed approaches, root cause, final fix, regression test, and validation evidence.

**Failed approaches are useful training signal but must never become "correct knowledge."** A failure that gets promoted to a "lesson" by mistake is worse than no lesson at all.

---

## 16. Internet / Git research

Code Slayer may eventually learn from official documentation, GitHub/Git repositories, issues, pull requests, commit history, release notes, RFCs, standards, security advisories, package documentation, and mailing lists.

**Internet information is evidence, not truth.** Every piece of it is stored with provenance: source, URL/reference, repository, commit/version, retrieval date, license where relevant, trust level, and applicability/version constraints.

**Unknown third-party code must never simply be downloaded and executed on the host.** Sandboxed inspection/execution only — the same isolation discipline as §8, applied to code Code Slayer did not write.

---

## 17. License / provenance

Code Slayer must not become a giant store of blindly copied third-party code. Track origin and licensing for anything retained. Prefer learning **principles, patterns, and verified behaviors** over copying large external code bodies. Training datasets preserve provenance where practical (§18).

---

## 18. Training data pipeline

Only high-quality, verified experience is eligible for fine-tuning:

```
raw experience → sanitization → deduplication → curation → verified dataset → training
```

Training eligibility requires: the task was solved, required tests passed, hidden/independent verification passed where applicable, no unresolved findings, provenance is available, and no secret contamination. **Code Slayer does not train directly on raw audit logs** — audit is history; a dataset is a curated, filtered, verified product built from it. §56 details what provenance a training example must carry; §58 describes how a contained operational incident, once resolved, becomes exactly this kind of eligible example.

---

## 19. Dataset splits

To prevent benchmark contamination, maintain a clear, explicit separation: `TRAIN`, `VALIDATION`, `FROZEN_EVAL`. **Frozen evaluation data must never silently enter training.** Dataset lineage is explicit and traceable end to end.

---

## 20. Language / domain specialists

Code Slayer may eventually create specialist fine-tunes or adapters — a Python specialist, a C specialist, a web specialist, a systems specialist, and so on. **Avoid exploding into hundreds of tiny adapters without measurable benefit.** A specialist exists because a benchmark proved it helps (§23), not because it was easy to make one. Cross-language and general engineering knowledge remains available independently of any specialist.

---

## 21. Training recipes

Keep four things distinct and independently versioned:

| Concept | Example |
|---|---|
| Dataset version | `python-v17` |
| Training recipe | `python-specialist-v4` |
| Base model | `model-X` |
| Resulting artifact | `model-X-python-v2` |

A future base model should be trainable using retained datasets and compatible recipes. **Do not assume raw LoRA weights transfer between unrelated architectures** — they generally don't. The reusable, durable asset across a base-model change is primarily the **verified dataset, the recipe, the evaluation suite, and the benchmark history** — not the trained weights themselves.

---

## 22. New model adoption

When a new coding model appears, the process is evidence-first, not hype-first:

1. register the model
2. inspect its capabilities/architecture
3. run frozen baseline benchmarks against it
4. optionally train specialists using existing datasets
5. benchmark the resulting candidate
6. compare against existing production workers
7. assign roles based on evidence
8. promote only where objectively better

**Newer does not automatically mean better.** Different models may win different roles — role assignment is per-role evidence, not a single global ranking. This same "earn it, don't assume it" posture governs a newly trained model's promotion too (§57) and a downgraded model/runtime's path back to `AUTO` (§42).

---

## 23. Benchmark-driven routing

Model routing should eventually weigh: language, task type, historical success, reasoning strength, speed, VRAM, latency, context capacity, tool capability, and current hardware availability.

**Do not route merely because a model is labeled "specialist." Measure it.** A label is a claim; a benchmark is evidence — see §2.

---

## 24. Self-development / self-repair

Code Slayer should eventually be capable of identifying bugs in itself, reproducing them, creating regression tests, implementing candidate fixes, testing candidates, benchmarking candidates, performing adversarial review, canary-running candidates, and promoting or rolling back.

**Code Slayer must never self-modify the active trusted instance in place.** The long-term architecture requires a minimal, stable supervisor/control layer, conceptually:

```
trusted runtime → candidate worktree/version → tests → benchmark → canary → promote/rollback
```

A known-good rollback target is always preserved. This is not a convenience — it is the property that makes self-development safe to attempt at all. §53 and §54 (canary/escape detection, circuit breaker) are the same "contain, never trust blindly" posture applied one level down, to individual model/provider/runtime behavior during ordinary operation rather than to Code Slayer's own candidate versions.

---

## 25. Constitutional / protected acceptance gates

Code Slayer must not "improve" itself by weakening the tests that judge it. Some acceptance gates receive special protection, requiring stronger review or explicit owner policy to ever change: state/recovery invariants, security tests, Git safety tests, audit integrity, self-update tests, frozen benchmarks, and permission policies.

Concretely, today, this already means the Foundation Plan's INV-1 through INV-21 and the tests in `tests/` that enforce them are exactly this category of gate — a future self-development capability must treat them as protected from day one of its own design, not as a retrofit. §51 extends the same "more protection, not necessarily more denial" posture to categories of *file*, not just categories of *test*.

---

## 26. Requirements as source of truth

Correct code that solves the wrong problem is still wrong. Projects need durable representations of: requirements, acceptance criteria, constraints, invariants, and explicitly out-of-scope behavior. Workers operate against these durable artifacts rather than relying only on conversation memory — the same "state remembers" principle from §2, applied to what a task is actually asking for.

---

## 27. Knowledge growth control

Do not optimize for maximum database growth. New knowledge is retained only when it adds value: a new root cause, a new failure mode, a new verified technique, something that supersedes old guidance, a meaningful performance improvement, or important version-specific information. Repeated evidence should increase confidence in existing knowledge rather than create a duplicate entry. Old knowledge becomes `STALE` or `SUPERSEDED` rather than accumulating alongside what replaced it.

---

## 28. Knowledge versioning / decay

A lesson may only be valid for a specific language version, compiler version, framework version, OS/kernel version, API version, or hardware generation. Knowledge retrieval must eventually account for applicability against the current context. **Obsolete truths must never be allowed to dominate current decisions** simply because they were recorded with confidence once.

---

## 29. Resource scheduler

Long-term Code Slayer may need to manage GPU VRAM, system RAM, CPU, disk, model loading, training jobs, VMs, builds, fuzzing, and benchmarks as shared, contended resources. The scheduler should eventually allow useful parallel work without resource thrashing — for example, GPU running coder inference, CPU running tests, a VM running an integration test, and a fine-tuning job queued for later GPU availability, all at once, deliberately, not by accident.

---

## 30. Supply chain security

Dependencies are code too. Future verification should consider dependency provenance, lockfiles, package versions, install scripts, container images, downloaded binaries, and compiler/toolchain provenance. **Adding or installing a dependency is a security-relevant operation**, not a routine one — it should be treated with the same seriousness as any other tool call under policy (Foundation Plan §17).

---

## 31. Backup / disaster recovery

Years of Code Slayer's accumulated knowledge must not live on one SSD without a recovery path. Long-term design must support snapshots, backups, export/import, integrity verification, migration, and restoration. At minimum, this protects: audit, knowledge, datasets, benchmarks, training recipes, the model registry, and important checkpoints. This is a natural extension of ADR 0002's existing principle (durable state lives outside the target repo) to the much larger body of state later stages will accumulate.

---

The sections below extend the vision above into an explicit safety, orchestration, and learning architecture — how the principle in §2 ("MODELS PROPOSE. TOOLS EXECUTE. EVIDENCE DECIDES. STATE REMEMBERS. POLICY CONTROLS.") actually gets enforced once real autonomous workers exist. None of §32–§61 is implemented. Where an existing implemented mechanism (the deterministic policy engine of Phase 4, the durable checkpoint/lease machinery of Phase 5/6, the append-only audit log of Phase 1) is what a later stage is expected to build on, that is called out explicitly, by name, rather than implied.

---

## 32. Prompt Analyst

Before a Coding Worker begins, a Prompt Analyst role receives the **complete** original user prompt and produces structured supplemental analysis: goals, explicit requirements, constraints, already-answered questions, likely ambiguities, risk points, required capabilities, expected verification, and likely scope boundaries.

**Critical invariant: the analysis never replaces, truncates, summarizes-away, or overrides the original prompt.** The Coding Worker must still receive the full original prompt, not a paraphrase of it — this is the same "state remembers, nothing gets silently rewritten" discipline §2 already commits to, applied to task intent itself (see also §59, Immutable task intent). The worker may disagree with the analyst when repository or runtime evidence supports a better interpretation than the analyst's — the analyst is advisory (§34), never a gate the worker's own evidence cannot override.

---

## 33. Question Gate

When a Coding Worker wants to ask the user a question, the request is routed through a Question Gate first, which checks it against the complete original prompt, the Prompt Analyst's output, repository facts, runtime facts, durable task state, and prior verified evidence.

The gate has exactly two outcomes:

- **`SUPPRESS`** — the answer is already explicit, is safely inferable from authoritative evidence (§34), or the decision is low-risk and a conservative choice is available (§35).
- **`ASK`** — real ambiguity remains, the answer materially affects correctness or safety, or a destructive/irreversible decision depends on it.

If the outcome is `ASK`, the question is rewritten into the smallest precise question necessary before it reaches the user. **The Question Gate must not invent requirements** — it filters and sharpens a real question; it never manufactures one to justify asking.

---

## 34. Authority model

Not every source of information a task touches carries the same weight, and the system should always be able to say *why* it believes something. Authoritative sources — sources a decision can actually be justified by — are, in order:

1. the original user prompt
2. deterministic policy
3. actual repository state
4. runtime state
5. tests / lint / typecheck / build / schema facts
6. durable audit/provenance

Advisory sources — useful, but never load-bearing on their own — are the Prompt Analyst (§32), Coding Worker reasoning, Reviewer reasoning, Question Gate reasoning (§33), and model consensus.

**LLM consensus is not evidence.** Two models agreeing does not make something true — see §12, which already applies this to a single reviewer model's opinion; this is the general form. For any important decision, the system should be able to name which prompt rule, repository fact, runtime fact, policy rule, or verification result supports it — a decision that can only be justified by "the model(s) said so" has not actually been justified.

---

## 35. Risk-based autonomy

Risk-based autonomy is the default long-term operating model: Code Slayer should remain highly autonomous, and routine, well-scoped, reversible work should proceed automatically, without stopping to ask.

Before asking the user anything, Code Slayer should first try to resolve the answer from authoritative evidence (§34): the complete original prompt, repository contents, Git history, project instructions, tests, schema/state, runtime facts, durable task state, and prior verified task evidence.

The user should be asked only when **meaningful uncertainty remains, and** at least one of the following also holds: the answer materially affects correctness; the action may damage data, state, history, or security; the action is destructive, irreversible, or externally consequential; or the architectural choice is genuinely ambiguous and materially important. For every other case of unresolved, low-risk ambiguity, Code Slayer should choose the conservative/reversible option, record the decision and its evidence (§34, §60), and continue automatically — an unresolved-but-low-stakes question is not a reason to stop.

---

## 36. Safety Runtime

A dedicated Safety Runtime, independent of any individual model provider, should remain authoritative even when a worker model behaves badly. **Safety must not depend on a model "being careful" — model failure is expected, and containing it is the Safety Runtime's job, not a hoped-for side effect of a good prompt.**

Over time this should include: hard path boundaries (§37), a deterministic policy engine (already real today, in narrower form, as Phase 4's policy engine — see `docs/TOOLS_AND_POLICY.md`), tool protocol validation (§40), scope enforcement, network policy, secrets policy, destructive-command protection (§39), Git protection, watchdogs (§45), resource/action budgets (§46), diff/test gates (§50), checkpoints/recovery (already real today, in narrower form, as Phase 5/6 — see `docs/CHECKPOINTS.md` and `docs/LEASES_AND_RECOVERY.md`), model trust levels (§42), and audit/provenance (already real today as `audit_events`, ADR 0004 — see §60).

---

## 37. Isolated job execution

The long-term target: every mutating coding job runs inside an isolated, disposable Code-Slayer worktree/sandbox, never directly in the owner's primary working tree. This buys: worker failure becomes disposable, unrelated host files stay unreachable, the primary repository stays protected, rollback stays cheap, and final promotion/merge happens only after verification (§50).

Hard path policy: writes are allowed only inside the job's assigned workspace; explicitly approved temp paths may additionally be allowed; arbitrary host paths are denied; and **an invented path never becomes writable merely because a model requested it.**

---

## 38. OS-level containment

Defense in depth around the policy architecture above, as long-term goals rather than a single universal sandbox: a dedicated unprivileged execution user, rootless execution where practical, read-only host mounts by default, a minimal environment, credentials removed unless explicitly required, CPU/RAM/time/process-count/disk limits, network default-deny, future namespace/containerization support, and seccomp/AppArmor or an equivalent where practical.

---

## 39. Tool / command safety

Deterministic policy (extending Phase 4's already-implemented ALLOW/DENY/REQUIRE_APPROVAL engine — see `docs/TOOLS_AND_POLICY.md`) should eventually cover at least: filesystem writes; writes outside the repo/worktree; `chmod`/`chown`; privilege changes; `rm -rf`/mass deletion; Git hard reset; `git clean`; history rewrite; push/force-push; migration/schema destruction; package/system installation; service manipulation; secrets/credential access; external side effects; and arbitrary network access.

**Prefer command/argv-aware policy over simplistic string blacklists** — a blacklist is a claim about a command's text; argv-aware policy is a fact about what the command actually does. The subprocess working directory should be forced to the assigned worktree (§37), never trusted from a request.

---

## 40. Tool protocol validation

A model/provider integration must produce valid, structured tool calls. Malformed raw text emitted where a structured tool invocation was expected — stray `<function=...>` markup, an unterminated `</tool_call>` fragment, or similar — is an **integration failure**, not a request the system tries to interpret.

Expected handling: stop the affected execution path, preserve state, record evidence, quarantine or downgrade the model/runtime (§42, §54), never interpret the malformed text as permission for anything, and allow safe rerouting to another worker. §58 documents a real, representative incident of exactly this failure mode and the response it should produce.

---

## 41. Model preflight / conformance

A model should not receive real autonomous coding work merely because text inference works. Every model/provider/runtime adapter should have preflight/conformance tests before it is trusted with anything real: basic inference, structured output, valid tool calling, reading a file, consuming a tool result, obeying a read-only request, a safe sandboxed edit, context health, timeout behavior, recovery behavior, and invalid-tool-call handling.

Compatibility evidence from these tests should be durable (§43) — a one-time smoke test is not a substitute for a persistent, queryable record of what an adapter has actually demonstrated.

---

## 42. Model trust levels

Three trust levels, evidence-gated in both directions:

- **`LOCKED`** — new, unknown, recently failing, incompatible, or with a malformed-tool-call history (§40). Capabilities heavily restricted.
- **`GUARDED`** — basic reliability demonstrated (§41); isolated coding allowed (§37); higher-risk operations still gated.
- **`AUTO`** — the relevant role/task class has demonstrated reliable behavior; broad autonomous operation allowed, still within the deterministic Safety Runtime's limits (§36) — trust never substitutes for policy.

Trust is scoped narrowly: model-specific, provider-specific, runtime-specific, version-specific, role-specific, and capability-specific. **A model may be trusted for analysis but not for mutation/tool execution.** Serious failures automatically downgrade trust; promotion back up always requires evidence, never elapsed time alone — the same posture §22 already takes toward "newer does not automatically mean better."

---

## 43. Model compatibility matrix

A durable record of observed compatibility, per model/provider/runtime/version, across dimensions such as: inference, long-context behavior, structured output, tool calling, file reading, file editing, shell interaction, Git interaction, read-only-constraint adherence, autonomous coding, review, repair, and timeout/recovery behavior.

**Routing (§44) should use this evidence, not brand reputation.** A model family's general reputation is not a substitute for this model/provider/runtime/version's own demonstrated record.

---

## 44. Model routing by role

Extending §3's role routing: the scheduler/orchestrator routes work by role and demonstrated suitability (§43), never by which model happens to be loaded. Roles include (§3's list, plus the roles this document adds): `prompt analyst` (§32), `planner`, `coder`, `reviewer`, `repair worker`, `deep reviewer`, `question gate` (§33), and `training/eval worker` (§57).

**A model may be best-suited for one role and prohibited from another** — a strong planner is not automatically a trusted mutator, and trust is granted per role/capability (§42), never inherited across roles just because the underlying model is the same.

---

## 45. Watchdog / anomaly detection

Beyond the repair-loop detection §11 already describes, long-term watchdogs should also watch for: excessive silence/hang, repeated identical tool calls, repeated no-op actions, tool loops, excessive retries, invented paths, unexplained external paths, scope drift, unrelated components/files, repeated denied operations, abnormal runtime, malformed tool calls (§40), suspicious permission changes, and an unexpectedly large diff.

**Watchdog action should normally be pause/contain, checkpoint, preserve evidence, and reassess/reroute — not blindly destroy work.** A worker that has gone wrong is a worker to contain and investigate, not one whose in-progress work is automatically discarded; that destroys the evidence needed to fix the underlying cause.

---

## 46. Action budgets

Bounded expectations, generalizing §11's attempt-budget concept to every kind of repeated action: tool calls, retries, repair loops, reviewer loops, runtime, model requests, changed files, and changed bytes.

**Budgets are not targets to maximize** — using less of a budget is not worse, and a worker racing to "use up" its budget has misunderstood what the budget is for (§2's quality-over-throughput principle again). Exceeding a budget should trigger evaluation/escalation, not an automatic hard stop that discards progress — the same contain-and-reassess posture as §45.

---

## 47. Independent review

The worker that writes code must not be the only judge of its own output. Two layers apply: deterministic verification (tests, lint, typecheck, build — already real today as part of Phase 4's finalization discipline) and, where appropriate, an independent reviewer role.

The reviewer receives the complete original prompt, the Prompt Analyst's output as supplemental context (never a replacement — §32), the repository baseline, the final diff, relevant source, test/lint/typecheck/build results, policy decisions, provenance, runtime findings, and the worker's own claims. Structured reviewer outcomes: `PASS`, `NEEDS_FIX`, `FAIL`, `UNSAFE`, `INSUFFICIENT_EVIDENCE`. **The reviewer normally reports findings rather than silently rewriting the code** — a repair, if one is needed, returns through an explicit repair loop, staying auditable and attributable rather than becoming an unattributed edit inside a review pass.

---

## 48. Heterogeneous review

Where practical, prefer an independent model family/provider for review rather than the family that produced the work — a Qwen worker reviewed by a Claude/Codex-class reviewer, a Claude worker reviewed by a Codex/Qwen-class reviewer, and so on. This reduces *correlated* failure (the same blind spot in both the author and the judge) but does not eliminate failure altogether — model diversity is supplemental, never a substitute for deterministic evidence (§34), which remains authoritative regardless of how many models agree.

For high-risk work (§51), a second independent reviewer may be used. Routine jobs should not incur unnecessary review cost — this, too, is §2's quality-over-throughput principle: review spent where risk justifies it, not spent everywhere out of habit.

---

## 49. Work-like orchestration

The long-term orchestrator is a persistent, multi-stage engineering system, not a single model call:

```
USER PROMPT
    ↓
Prompt Analyst (§32)
    ↓
Planner / Router
    ↓
Coding Worker
    ↓
Safety Runtime (§36)
    ↓
Deterministic Verification
    ↓
Independent Reviewer (§47)
    ↓
Repair loop if needed
    ↓
Acceptance / Checkpoint
    ↓
Training Candidate (§56)
```

**The orchestrator owns durable task state — no model chat history is source-of-truth.** This is §2's "STATE REMEMBERS" principle at system scale: the orchestrator, not any model's context window, is what a crash, restart, or worker swap resumes from. No agent loop or orchestrator exists yet; this describes the shape the Core Agent phase's task/tool loop (see `docs/ROADMAP.md#core-agent`) grows into once a real worker exists to drive it.

---

## 50. Diff / commit gates

Before accepting a final result, verify at least where applicable: path/scope correctness, unexpected-file detection, a staged-file allowlist, the final diff, focused tests, regression tests, lint, typecheck, build, `git diff --check`, migration/schema policy, protected-file changes (§51), provenance completeness, and reviewer outcome (§47).

**Local runtime artifacts must never accidentally enter commits.** This generalizes a discipline this repository's own development has already practiced informally in every phase to date — verifying test/lint/diff-check results and deliberately excluding local, untracked configuration before every commit — into durable, enforced machinery rather than something a worker has to remember to do correctly every time.

---

## 51. Protected / high-risk files

Changes to certain file categories should automatically raise risk: CI/CD, deployment, authentication, permissions, secrets handling, lockfiles/dependencies, migrations/schema, Git configuration, security policy, and update/install mechanisms.

**This does not mean automatically denying such work** — it means routing it through stronger verification (§47, §48) than routine changes receive. Supply-chain-relevant changes (§30) are a specific instance of this general category, not a separate mechanism.

---

## 52. Checkpoint / recovery (extended failure classes)

The durable checkpoint/lease architecture already implemented in Phase 5/6 — `docs/CHECKPOINTS.md` and `docs/LEASES_AND_RECOVERY.md` — is the mechanism this vision's broader recovery goals build on, not a separate future system. A worker crash, hang, provider outage, context failure, malformed tool call (§40), policy violation, or reviewer rejection (§47) must not require starting the task from scratch, and must not corrupt the main repository. **Recovery should be deterministic where possible** — an outcome derivable from durable evidence (§34), not reconstructed by asking a model to remember what it was doing.

---

## 53. Canary / escape detection

*Not implemented; a future defense-in-depth concept.* Code Slayer may place forbidden canary paths/resources inside a sandbox specifically to detect scope-escape behavior. Any attempt by a worker to touch such a resource is strong evidence that the model/runtime should be stopped and downgraded (§42) — the canary's value is entirely in never legitimately being touched, so any touch is unambiguous signal.

---

## 54. Circuit breaker

Repeated serious protocol or policy failures from a model, provider, or runtime should be able to trip a circuit breaker: disable `AUTO` for it, downgrade it to `LOCKED` (§42), stop assigning it mutating work, and require new conformance evidence (§41) before re-enabling — never a manual "just try it again" reset without new evidence.

---

## 55. Runtime learning

Runtime Learning is deliberately separate from Training (§18–§23): it adapts *how the system operates*, not *the weights of a model*. It may use durable job outcomes to improve model routing (§44), role suitability, trust level (§42), risk thresholds (§35), prompt templates, context packaging, tool guardrails (§39), question suppression (§33), reviewer escalation (§48), and task-pattern reuse — drawing on successful jobs, failed jobs, policy denials, user approvals/denials, malformed tool calls (§40), scope drift (§45), test failures, reviewer findings (§47), repair counts, rollback events, and final acceptance.

**Runtime adaptation must be auditable, reversible, and evidence-based** — the same three properties §34's authority model already demands of any important decision, applied here to decisions the system makes about *itself*.

---

## 56. Training provenance

Every training example (§18) should carry provenance: the original task, relevant context, the producing model, provider/runtime/version, role, tool history, deterministic verification results, reviewer result (§47), any user correction/approval, and the final accepted output.

**Explicit human corrections must remain distinguishable from inferred lessons** — a fix a person deliberately made and a pattern the system merely inferred from outcomes are different strengths of evidence, and conflating them would let an inferred, unverified pattern masquerade as a deliberate correction.

---

## 57. Training evaluation

A training run is not successful merely because training loss decreases. Trained versions must be evaluated on held-out regression/eval sets (§19's `FROZEN_EVAL` split) for: coding correctness, instruction adherence, tool protocol (§40), read-only compliance, scope control, hallucinated paths, destructive behavior, question quality (§33), repair quality, safety policy adherence (§36), recovery (§52), and regression rate.

**A newly trained model does not automatically replace an existing proven one — it must earn promotion**, exactly as §22 already requires for adopting any new model, applied here to a model Code Slayer produced itself.

---

## 58. Incidents become regression tests

Operational failures should become durable test/eval fixtures, not just war stories. The expected long-term pipeline:

```
malformed/unsafe behavior
      ↓
protocol/policy detection (§40)
      ↓
execution contained (§36, §45)
      ↓
model/runtime downgraded (§42, §54)
      ↓
task safely rerouted/recovered (§52)
      ↓
incident becomes regression evidence (§56)
```

**A representative example, for concreteness — not as criticism of one specific model family:** on 2026-09-14, a local coding model (via OpenCode/Qwen) was asked for a simple read-only repository-status task, but instead created an unrelated temporary file, attempted unrelated work on an unrelated `productController.ts`, invented `/home/user/app`-style paths that were never part of the actual repository, attempted permission-changing behavior, and later emitted raw `<function=...></tool_call>`-style text rather than a valid structured tool call. Every element of that sequence is a failure mode this document already names elsewhere — invented paths (§37), scope drift (§45), a permission-changing operation deterministic policy should gate (§39), and a malformed tool call (§40) — which is exactly the point: this is a *representative* failure shape a local model can produce, not a one-off, and the containment pipeline above is designed to catch each element of it independently, not rely on catching the whole sequence at once.

---

## 59. Immutable task intent

A task should have an immutable manifest/hash: the complete original user prompt plus normalized task metadata, with a durable identity so that later summaries, model handoffs, or reviewer prompts cannot silently mutate what the task actually asked for. Derived analysis (§32) may evolve as evidence accumulates; **original task intent must remain recoverable and authoritative** underneath it — the durable counterpart to §26's "requirements as source of truth," anchored to an unmutated original artifact rather than a durable *representation* of one.

---

## 60. Audit / replay

Building on the append-only audit log already implemented today (`audit_events`, ADR 0004 — see §14), the long-term target is that important model messages, tool calls, policy decisions, verification results, and task transitions are replayable/auditable enough to answer: what happened; what model did it; what evidence did it use; why was it allowed; why was it accepted; and how can the decision be reproduced. This is §34's authority model made queryable after the fact, not just enforced at decision time. **This does not imply logging raw secrets** — provenance records what was used and why, not the sensitive content itself.

---

## 61. Quality philosophy

Code Slayer should not "reward activity." A worker that produces many edits, many tool calls, or fast output is not better unless the resulting work is verified — this document opened with that claim in §2 as `QUALITY > THROUGHPUT`; this closing section is its concrete shape. The ideal autonomous worker:

- makes the smallest necessary change
- stays in scope
- uses evidence
- verifies its assumptions
- asks only when necessary (§33, §35)
- leaves durable provenance (§34, §60)
- recovers cleanly from failure (§52)
- improves from validated feedback (§55, §57)

Every mechanism in §32–§60 exists in service of that list — none of it is safety for its own sake, and none of it is process for its own sake.

---

## 62. Governance foundation: naming, local-first posture, transparency

**CSLR** is Code Slayer's official short name/brand (Governance Foundation,
slice G1) — "Code Slayer — Autonomous Engineering System" is the formal
descriptor, and `cslr` is the target user-facing future CLI name (see
[`docs/OPERATIONS_UX.md`](OPERATIONS_UX.md); the current, real CLI remains
`codeslayer` and is not renamed by this section). Existing technical
identifiers — the `codeslayer`/`code_slayer` packages, repositories, and
service names — are unaffected; CSLR is the product's name, not a rename
of its code.

Three product-level commitments, elaborated in full in their own
authoritative documents, are added here because they apply to every
section above and below, not to any one subsystem:

- **Local-first, explicit-authority networking.** User project data stays
  local unless one specific, explicitly authorized operation requires
  otherwise; telemetry and cloud AI are off by default; discovering
  something on the local network is never itself authority to connect
  to, read, write, or otherwise act on it. See
  [`docs/SECURITY_PRIVACY_ARCHITECTURE.md`](SECURITY_PRIVACY_ARCHITECTURE.md)
  and [`docs/PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md) for the full
  invariants and the permission vocabulary this implies — a strict
  superset of, and consistent with, the cloud-escalation posture already
  established in §4.
- **Simple by default, powerful underneath.** A routine user should never
  need to understand this project's own implementation details (a
  virtual environment, systemd, SQLite, migration numbers, a model
  runtime's API) to perform a routine operation — see
  [`docs/PRODUCT_PRINCIPLES.md`](PRODUCT_PRINCIPLES.md) and
  [`docs/OPERATIONS_UX.md`](OPERATIONS_UX.md).
- **Progressive transparency.** For anything security-sensitive, a user
  should be able to go from a simple explanation, to technical detail, to
  the exact implementation/source — never required to accept "trust us"
  alone. Source visibility is part of the trust model but is not, by
  itself, sufficient security — see
  [`docs/SECURITY_PRIVACY_ARCHITECTURE.md`§11](SECURITY_PRIVACY_ARCHITECTURE.md#11-transparency).

These documents are normative specifications, not additional vision prose
— see [`AGENTS.md`](../AGENTS.md) for the rule that every future agent
change must preserve their invariants.

---

## Guiding constraints

These apply to every section above, without exception:

- **Build incrementally.** Nothing in this document is a specification to implement now.
- **Validate each layer before adding the next.** A layer built on an unproven layer below it is technical debt with extra steps.
- **Avoid architecture astronautics.** A registry, a scheduler, or an engine described here earns its implementation when a real, current need demands it — not before.
- **Future capabilities remain deferred until needed.** Deferred is not the same as forgotten; it means the trigger for building it hasn't happened yet.
- **Foundation invariants matter more than speculative features.** If a new capability in this document would ever require weakening a Foundation Plan invariant (INV-1 through INV-21) to build, the invariant wins and the capability's design is wrong.
- **The safety and learning architecture in §32–§61 extends the existing durable core; it never weakens it.** The durable state model, the audit model, the policy model, the lease/fencing model, the checkpoint model, provider isolation, and fail-closed behavior are exactly what §32–§61 is built on top of — a Safety Runtime (§36), a trust model (§42), or a learning loop (§55) whose design would require loosening any of them has, like the invariant case above, gotten its own design wrong.

See [`docs/ROADMAP.md`](ROADMAP.md) for how these capabilities are sequenced.
