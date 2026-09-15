# Code Slayer — Roadmap

**This roadmap describes capability stages, not calendar dates.** No stage below has a promised delivery date. Stages are ordered by what depends on what, not by how soon each will be built.

Read this alongside [`docs/CODE_SLAYER_VISION.md`](CODE_SLAYER_VISION.md) (the long-term "why" and "what") and the Foundation Plan (the authoritative near-term architecture). This document is the "in what order," and the order exists to prevent a layer from being built on a layer that isn't proven yet.

> **Do not read this as a promise that every item must be built before Code Slayer becomes useful.** Project Mode and Maintenance Mode (the two most immediately useful [operating modes](CODE_SLAYER_VISION.md#6-operating-modes)) become usable once **Core Agent** and **Local Worker Runtime** exist — everything from **Engineering Intelligence** onward makes Code Slayer *better*, not *functional for the first time*.

> **Current accepted baseline:** Phase 7 / **Local Worker Runtime = VERIFIED / ACCEPTED**, including the persistent LocalWorkerRunner and Phase 7.7a–7.7e hardening. Phase 6 leases/fencing remain the execution authority. WebUI Foundation 1 adds the thin HTTP/application view described in [`WEBUI_API.md`](WEBUI_API.md). Phase 8 / Engineering Intelligence remains not started.

Every stage below also operates under principles [`docs/CODE_SLAYER_VISION.md`](CODE_SLAYER_VISION.md) already establishes as non-negotiable, restated here only by reference, never duplicated: **quality over throughput** (§2), the original user prompt stays authoritative over any derived analysis (§32, §59), **model consensus is never evidence** (§34), the Safety Runtime is deterministic and model-independent (§36), a model *earns* autonomy through conformance evidence rather than being granted it (§41, §42), the coding worker is never the sole judge of its own output (§47), Runtime Learning and model Training are separate systems (§55 vs. §18–23), and an operational incident becomes regression/evaluation evidence rather than being discarded (§58).

---

## How to read the dependency chain

```
Foundation
   │
   ▼
Core Agent
   │
   ▼
Local Worker Runtime ──────────────┐
   │                                │
   ▼                                ▼
Engineering Intelligence      WebUI (reads/drives the same
   │                          backend APIs as every stage
   ▼                          from Core Agent onward)
Environments
   │
   ▼
Knowledge ──────────────┐
   │                     │
   ▼                     ▼
  Lab              Runtime Learning
   │                     │
   └──────────┬──────────┘
              ▼
          Training
              │
              ▼
      Autonomous R&D
              │
              ▼
      Self-Development
              │
              ▼
       Systems / OS
```

`WebUI` is drawn off to the side deliberately: it can start as soon as `Local Worker Runtime` gives it something real to show, and grows in step with every stage after it — it is a *view*, not a rung on the capability ladder.

---

## Foundation

**Status: complete (Phase 1), tagged `codeslayer-v0.1-foundation`.**

Durable state, append-only audit, Git safety primitives (repo/worktree identity), and the operations journal. Everything above this stage assumes these invariants hold and never re-litigates them casually (see [Guiding Constraints](CODE_SLAYER_VISION.md#guiding-constraints)).

**Depends on:** nothing.
**Delivers:** the substrate every later stage persists state into.

---

## Core Agent

**Status: in progress.** Phase 2 supplies durable task-state semantics;
Phase 3 supplies repository inspection and immutable rule/baseline discovery;
Phase 4 supplies a closed tool-capability registry, risk-classified policy
decisions, and a controlled, journaled, crash-aware executor (see
[`docs/TOOLS_AND_POLICY.md`](TOOLS_AND_POLICY.md)); Phase 5 supplies the
durable Git checkpoint mechanism itself — commit/tree/ref plumbing, its own
narrow policy decision, and crash-safe recovery (see
[`docs/CHECKPOINTS.md`](CHECKPOINTS.md)); Phase 6 supplies the worktree
lease with fencing, integrated into every Phase 4/5 mutation path, plus a
generic unresolved-operation recovery framework (see
[`docs/LEASES_AND_RECOVERY.md`](LEASES_AND_RECOVERY.md)) — completed by two
follow-up hardening commits that close a lease-takeover quiescence race
(an `ACTIVE`-but-expired lease now durably passes through `QUIESCING`
before any new epoch is granted) and harden process-liveness identity
(exact `/proc`-derived process identity, no time tolerance, and a strict
`GONE`-requires-positive-evidence vs. `UNKNOWN` distinction). The agent
loop itself remains deferred.

The state-machine engine (task phases, legal transitions, guards), the tool abstraction with risk classification, the policy engine, the worktree lease (with fencing), the checkpoint mechanism (Git plumbing), and resume-from-crash. This is where a "task" becomes a real, resumable thing rather than a row that exists.

**Depends on:** Foundation.
**Delivers:** the ability to run one real task, end to end, safely, resumably — with a deterministic/fake worker, per the Foundation Plan's own v0.1 scope. Project Mode and Maintenance Mode become meaningfully possible once this stage plus Local Worker Runtime both exist.

---

## Local Worker Runtime

**Status: VERIFIED / ACCEPTED — Foundation Plan Phase 7.** The implemented LocalWorkerRunner composes these boundaries; the broader descriptions below remain the direction of the runtime, not a grant of unimplemented autonomy. Current real-worker execution is bounded and read-only; mutation trust, AUTO and primary-worktree promotion are not enabled.

1. **Safety Runtime baseline.** The deterministic, model-independent containment layer — path boundaries, the policy engine extended per [Tool / Command Safety](CODE_SLAYER_VISION.md#39-tool--command-safety), tool-protocol validation ([§40](CODE_SLAYER_VISION.md#40-tool-protocol-validation)) — exists *before* a real model is wired in, not retrofitted after. See [Safety Runtime](CODE_SLAYER_VISION.md#36-safety-runtime).
2. **Model adapter + conformance.** The Model Registry, Provider Registry, Capability Registry, and role routing described in [Model-Agnostic Architecture](CODE_SLAYER_VISION.md#3-model-agnostic-architecture). The first real provider adapter targets a local model (Qwen, per ADR 0006) behind a generic local-endpoint interface, and must pass [preflight/conformance](CODE_SLAYER_VISION.md#41-model-preflight--conformance) before it receives any real task. Cloud escalation (`disabled`/`manual`/`maintenance`) is implemented here as a policy dimension, not bolted on later.
3. **Trust starts `LOCKED`.** A newly adapted model begins at the most restricted [trust level](CODE_SLAYER_VISION.md#42-model-trust-levels) and only earns `GUARDED`, then broad `AUTO` autonomy, through demonstrated evidence — never granted upfront because a model looks capable in general.
4. **Isolated mutating jobs.** Every mutating job runs in its own disposable worktree/sandbox per [Isolated Job Execution](CODE_SLAYER_VISION.md#37-isolated-job-execution), so a `GUARDED`-level worker's failure stays disposable and never reaches the owner's primary working tree. The repository-isolation foundation for this (a Code-Slayer-owned, explicit-base-revision, disposable linked worktree; still no OS-level containment) is real today as Phase 7.5c — see [`docs/JOB_WORKTREES.md`](JOB_WORKTREES.md).
5. **Prompt / question intelligence.** The [Prompt Analyst](CODE_SLAYER_VISION.md#32-prompt-analyst) and [Question Gate](CODE_SLAYER_VISION.md#33-question-gate) sit in front of the worker from the start: the analyst's structured analysis never replaces the original prompt, and the gate suppresses a question only when authoritative evidence — repository, runtime, prior verified task evidence — already answers it.

**Accepted evidence:** real adapter conformance and exact `read_file` trust promotion, deterministic runner/QuestionGate/human-resolution integration, isolated job-worktree containment and denial, recovery/fencing hardening, and explicit cloud transport authorization. This acceptance does not claim successful real-model mutation or a production PromptAnalyst implementation.

**Accepted follow-up backlog:** two P2 findings from the independent Phase-7 acceptance review remain OPEN — safely deferable, not acceptance blockers, and out of scope for WebUI Foundation 1. Neither is fixed or removed by this slice:

1. **Concurrent same-handle job-worktree cleanup result handling.** Two concurrent `release_job_worktree()` calls against the *same* `JobWorktree` handle are not mutually serialized against each other at the Git-removal step: the caller that loses the race can receive an uncaught `FileNotFoundError` instead of a clean `CleanupResult`, once its sibling has already removed the worktree. No state corruption or data loss has been reproduced — the worktree is still removed exactly once, honoring every existing safety check — and this path is not reachable through any current production call site (nothing yet calls `release_job_worktree()` concurrently for one handle). Backlog only.
2. **No single coherent runner-driven mutating model job yet.** Real isolated `ToolExecutor` mutation plus a real `CheckpointManager` checkpoint is proven inside a managed job worktree, but not yet as one continuous `PromptAnalyst -> QuestionGate -> LocalWorkerRunner -> mutation-trusted model -> managed worktree` run: `workers.execution.execute_guarded_turn()`'s offered capability set is still `read_file`-only, and no worker holds mutation trust. Deliberate — trust is never granted merely to demonstrate the pipeline — and closes naturally once a future phase extends the worker-facing capability set for a worker that has actually earned mutation trust. Backlog only.

**Depends on:** Core Agent (a worker needs a task/phase/tool loop to plug into).
**Delivers:** Code Slayer actually doing work with a real model, entirely locally, with cloud as an explicit, audited, off-by-default escape hatch — and every step above holding before the next is attempted.

---

## Engineering Intelligence

**Status: not started.**

Repository indexing, a context engine (so a worker gets the *relevant* slice of a codebase, not all of it or a guess), planning that produces a durable, reviewable plan, deterministic finalization (tests/lint/typecheck/build/diff gates — see [Diff / Commit Gates](CODE_SLAYER_VISION.md#50-diff--commit-gates)), and [independent review](CODE_SLAYER_VISION.md#47-independent-review) as a distinct role from implementation. **The coding worker must not be the sole judge of its own output** — a reviewer's structured `PASS`/`NEEDS_FIX`/`FAIL`/`UNSAFE`/`INSUFFICIENT_EVIDENCE` outcome is only as trustworthy as the deterministic evidence handed to it alongside the diff, which is why finalization is deterministic *before* it is reviewed, not instead of being reviewed.

**Depends on:** Local Worker Runtime (planning and review are role-routed work, like any other phase, gated by the same trust/conformance evidence that stage establishes).
**Delivers:** tasks that scale past "a few files" — real refactors, real multi-file features, plans a human can read and approve before implementation starts, and a diff nothing merges without independent review having seen it.

---

## Environments

**Status: not started.**

Containers and VM management, plus runtime/install validation — the machinery behind the vision's [VM / Environment Testing](CODE_SLAYER_VISION.md#13-vm--environment-testing): build → fresh environment → install → launch → test → reboot → upgrade → uninstall, for whatever kind of software is being built.

**Depends on:** Engineering Intelligence (there needs to be something worth verifying this thoroughly before the machinery to verify it is worth building).
**Delivers:** verification that survives contact with a clean environment, not just "tests passed in the same directory the code was written in." A hard prerequisite for the [Systems / OS](#systems--os) stage.

---

## Knowledge

**Status: not started.**

The experience store, retrieval, Git/internet research (with provenance — see the vision's [Internet / Git Research](CODE_SLAYER_VISION.md#16-internet--git-research) and [License / Provenance](CODE_SLAYER_VISION.md#17-license--provenance)), and the Audit/Knowledge/Skill-Model separation described in the [Knowledge Engine](CODE_SLAYER_VISION.md#14-knowledge-engine) section.

**Depends on:** Engineering Intelligence (there must be real task experience worth curating before curation machinery is worth building) and Environments (verified outcomes are what makes an experience trustworthy enough to curate).
**Delivers:** Code Slayer that gets measurably better at *this* codebase and at recurring problems, instead of re-deriving the same context every task.

---

## Lab

**Status: not started.**

Curriculum management, task generation, the skill model, hidden tests, and mutation/fuzz testing — the machinery in the vision's [Lab / Training Mode](CODE_SLAYER_VISION.md#8-lab--training-mode) and [Curriculum and Task Generation](CODE_SLAYER_VISION.md#9-curriculum-and-task-generation) sections, run in the isolated, disposable environments this stage's name refers to.

**Depends on:** Knowledge (a curriculum needs a skill model, and a skill model needs curated evidence) and Environments (disposable, isolated execution).
**Delivers:** Code Slayer that can improve itself against a curriculum without an owner hand-authoring every exercise.

---

## Runtime Learning

**Status: not started.**

Adapts *how the system operates* — model routing, role suitability, trust level, risk thresholds, prompt templates, context packaging, tool guardrails, question suppression, reviewer escalation, task-pattern reuse — from durable job outcomes: accepted work, policy denials, malformed tool calls, reviewer findings, repair counts, rollbacks. See the vision's [Runtime Learning](CODE_SLAYER_VISION.md#55-runtime-learning) section for the full input list.

**Distinct from Training, deliberately (below):** Runtime Learning never changes a model's weights — only how Code Slayer routes, trusts, and gates the models it already has. It is the curated evidence trail an accepted job leaves on its way toward becoming training-eligible, not training itself; conflating the two is exactly what rule 9 of the accepted vision exists to prevent.

**Depends on:** Knowledge (the same curated-evidence, provenance-first discipline) and Local Worker Runtime (there must be real job outcomes — accepted, denied, rolled back — to learn from).
**Delivers:** routing and trust decisions grounded in this system's own demonstrated track record instead of a static configuration.

---

## Training

**Status: not started.**

The verified-dataset pipeline, dataset splits (`TRAIN`/`VALIDATION`/`FROZEN_EVAL`), language/domain specialists, training recipes, and the frozen-benchmark discipline described in the vision's [Training Data Pipeline](CODE_SLAYER_VISION.md#18-training-data-pipeline) through [Benchmark-Driven Routing](CODE_SLAYER_VISION.md#23-benchmark-driven-routing) sections, plus [Training Provenance](CODE_SLAYER_VISION.md#56-training-provenance) and [Training Evaluation](CODE_SLAYER_VISION.md#57-training-evaluation).

A training candidate's path here is never a direct hop from raw worker output — every step below has to hold:

```
accepted verified job
        │
        ▼
runtime learning evidence
        │
        ▼
training eligibility / curated candidate
        │
        ▼
dataset curation
        │
        ▼
training
        │
        ▼
held-out evaluation
        │
        ▼
possible model promotion
```

**A trained model is not promoted on training loss alone** — it must clear the frozen (`FROZEN_EVAL`) held-out evaluation before it replaces, or is routed alongside, an existing proven model, exactly as [New Model Adoption](CODE_SLAYER_VISION.md#22-new-model-adoption) already requires for any model, applied here to one Code Slayer trained itself.

**Depends on:** Lab (curriculum-generated verified experience, at scale) and Runtime Learning (the curated evidence trail real accepted jobs leave behind) — both routes converge here, and neither is a raw audit-log dump or raw worker output.
**Delivers:** the ability to turn accumulated, verified experience into a measurably better local model — evidence-gated at every step, never a raw dump of audit logs or unverified worker output into a fine-tune.

---

## WebUI

**Status: not started.**

The local control room described in the vision's [Future WebUI](CODE_SLAYER_VISION.md#5-future-webui) section — Projects, Tasks, Workers, Models, Diff, Tests, Checkpoints, Audit, and more, all reading and driving the *same* backend APIs as the CLI.

**Depends on:** Core Agent, at minimum, for there to be state worth showing; it grows incrementally alongside every stage above and below this line rather than being "finished" at any point.
**Delivers:** visibility and control that doesn't require reading raw audit rows or `codeslayer` CLI output to understand what's happening.

---

## Autonomous R&D

**Status: not started.**

Experiments, disposable worktrees, and benchmark-driven optimization applied to *projects Code Slayer is building*, not to Code Slayer itself yet — the difference between this stage and Self-Development.

**Depends on:** Training (an R&D loop needs the benchmark and evaluation discipline that stage establishes) and Lab (the same disposable-environment discipline, applied to real project work instead of curriculum exercises).
**Delivers:** the ability to try several real approaches to a hard problem, measure them, and keep the best one — without a human orchestrating each attempt.

---

## Self-Development

**Status: not started.**

Code Slayer diagnosing, testing, and improving Code Slayer itself, per the vision's [Self-Development / Self-Repair](CODE_SLAYER_VISION.md#24-self-development--self-repair) and [Constitutional / Protected Acceptance Gates](CODE_SLAYER_VISION.md#25-constitutional--protected-acceptance-gates) sections: trusted runtime → candidate worktree/version → tests → benchmark → canary → promote/rollback. **Never in-place self-modification of the active trusted instance.**

**Depends on:** Autonomous R&D (the experiment/benchmark loop, now pointed at Code Slayer's own codebase) and a settled, trustworthy set of protected acceptance gates from every stage before it — this stage is only as safe as the invariants it refuses to let itself weaken.
**Delivers:** Code Slayer that improves its own capability over time under supervision, with an always-available rollback target.

---

## Systems / OS

**Status: not started.**

Cross-compilation, QEMU-based boot/runtime testing, and eventually kernel and complete OS projects — the long-horizon end of the languages/toolchains scope in the vision.

**Depends on:** Environments (VM/QEMU verification is not optional at this level — it's the only way to know a kernel change didn't just corrupt the boot process) and Self-Development (the risk profile of systems-level autonomous work demands the supervisor/canary/rollback discipline that stage establishes).
**Delivers:** the top of the original scope statement — Code Slayer capable of engineering work at every level from a CLI script to an operating system.

---

## A note on sequencing discipline

Every "Depends on" above is a real dependency, not a suggestion: building a later stage before its dependency is solid produces a system that *looks* capable and fails unpredictably under the exact conditions the skipped stage exists to guard against (an unverified plan, an untested environment, an ungated self-modification). When in doubt about whether a stage is "ready to start," the test is not "do we want this capability" — it's "does every stage above it in this chain already hold up under real, adversarial use." If the answer is no, the next unit of work belongs in the stage that's still shaky, not the one that sounds more exciting.

**Safety before autonomy is the sharpest instance of this rule.** Real autonomous model execution must never outrun the Safety Runtime containing it: if Local Worker Runtime introduces a model adapter before every safety feature a given capability needs actually exists, that capability stays `LOCKED` for that model — not "temporarily trusted until the safety work catches up." Capability and containment are not allowed to trade off against each other; a model that looks impressive does not buy its way past a `LOCKED`/`GUARDED` gate it hasn't earned through conformance evidence.
