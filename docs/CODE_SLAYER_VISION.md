# Code Slayer — Vision

**Status:** authoritative long-term guidance. Not a spec for what exists today.
**Audience:** every worker that ever acts on this project — Claude, Codex, Astra, Qwen, future models, and Code Slayer's own future self.
**Relationship to other documents:** the *Foundation Plan* (Revision 2.1) is the authoritative near-term architecture for the durable core. The ADRs in `adr/` record specific decisions already made and implemented. This document is the horizon those decisions are aimed at — it does not override them, and where it describes something not yet built, that absence is deliberate, not an oversight (see [Guiding Constraints](#guiding-constraints)).

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

Roles are a vocabulary, not a fixed cast: `planner`, `coder`, `debugger`, `reviewer`, `repair`, `test engineer`, `security reviewer`, `architecture reviewer`, `deep reviewer`, `investigator`, `task generator`, and others as they prove useful. A single physical model may fill several roles; a role may be filled by different models over time as evidence changes.

**A future model must be replaceable without redesigning core state-machine logic.** If adopting a new model ever requires touching `core/`, the registry design has failed at its one job.

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

**Attempt budgets must be configurable, not buried as hardcoded assumptions** — a number a future engineer has to go find in code is a number that will quietly become wrong.

---

## 12. Evidence-driven correctness

A model saying "looks correct" is not proof. The long-term verification stack may include: unit tests, integration tests, hidden tests, property-based testing, differential testing, mutation testing, fuzzing, sanitizers, static analysis, type checking, performance benchmarks, runtime health checks, installation testing, reboot testing, VM testing, and independent adversarial review.

**Reviewer output is evidence, not absolute truth.** Objective, machine-verifiable evidence dominates model opinion — including a reviewer model's opinion. This is the same posture the Foundation Plan already takes toward its own audit hash chain: an integrity signal, not a claim of infallibility.

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

Training eligibility requires: the task was solved, required tests passed, hidden/independent verification passed where applicable, no unresolved findings, provenance is available, and no secret contamination. **Code Slayer does not train directly on raw audit logs** — audit is history; a dataset is a curated, filtered, verified product built from it.

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

**Newer does not automatically mean better.** Different models may win different roles — role assignment is per-role evidence, not a single global ranking.

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

A known-good rollback target is always preserved. This is not a convenience — it is the property that makes self-development safe to attempt at all.

---

## 25. Constitutional / protected acceptance gates

Code Slayer must not "improve" itself by weakening the tests that judge it. Some acceptance gates receive special protection, requiring stronger review or explicit owner policy to ever change: state/recovery invariants, security tests, Git safety tests, audit integrity, self-update tests, frozen benchmarks, and permission policies.

Concretely, today, this already means the Foundation Plan's INV-1 through INV-21 and the tests in `tests/` that enforce them are exactly this category of gate — a future self-development capability must treat them as protected from day one of its own design, not as a retrofit.

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

## Guiding constraints

These apply to every section above, without exception:

- **Build incrementally.** Nothing in this document is a specification to implement now.
- **Validate each layer before adding the next.** A layer built on an unproven layer below it is technical debt with extra steps.
- **Avoid architecture astronautics.** A registry, a scheduler, or an engine described here earns its implementation when a real, current need demands it — not before.
- **Future capabilities remain deferred until needed.** Deferred is not the same as forgotten; it means the trigger for building it hasn't happened yet.
- **Foundation invariants matter more than speculative features.** If a new capability in this document would ever require weakening a Foundation Plan invariant (INV-1 through INV-21) to build, the invariant wins and the capability's design is wrong.

See [`docs/ROADMAP.md`](ROADMAP.md) for how these capabilities are sequenced.
