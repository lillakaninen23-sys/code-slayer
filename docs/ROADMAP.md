# Code Slayer — Roadmap

**This roadmap describes capability stages, not calendar dates.** No stage below has a promised delivery date. Stages are ordered by what depends on what, not by how soon each will be built.

Read this alongside [`docs/CODE_SLAYER_VISION.md`](CODE_SLAYER_VISION.md) (the long-term "why" and "what") and the Foundation Plan (the authoritative near-term architecture). This document is the "in what order," and the order exists to prevent a layer from being built on a layer that isn't proven yet.

> **Do not read this as a promise that every item must be built before Code Slayer becomes useful.** Project Mode and Maintenance Mode (the two most immediately useful [operating modes](CODE_SLAYER_VISION.md#6-operating-modes)) become usable once **Core Agent** and **Local Worker Runtime** exist — everything from **Engineering Intelligence** onward makes Code Slayer *better*, not *functional for the first time*.

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
  Lab ────────────► Training
   │                     │
   └──────────┬──────────┘
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
[`docs/TOOLS_AND_POLICY.md`](TOOLS_AND_POLICY.md)). The worktree lease
(with fencing), checkpoint mechanism, and agent loop remain deferred.

The state-machine engine (task phases, legal transitions, guards), the tool abstraction with risk classification, the policy engine, the worktree lease (with fencing), the checkpoint mechanism (Git plumbing), and resume-from-crash. This is where a "task" becomes a real, resumable thing rather than a row that exists.

**Depends on:** Foundation.
**Delivers:** the ability to run one real task, end to end, safely, resumably — with a deterministic/fake worker, per the Foundation Plan's own v0.1 scope. Project Mode and Maintenance Mode become meaningfully possible once this stage plus Local Worker Runtime both exist.

---

## Local Worker Runtime

**Status: not started.**

The Model Registry, Provider Registry, Capability Registry, and role routing described in the vision's [Model-Agnostic Architecture](CODE_SLAYER_VISION.md#3-model-agnostic-architecture). The first real provider adapter targets a local model (Qwen, per ADR 0006) behind a generic local-endpoint interface. Cloud escalation (`disabled`/`manual`/`maintenance`) is implemented here as a policy dimension, not bolted on later.

**Depends on:** Core Agent (a worker needs a task/phase/tool loop to plug into).
**Delivers:** Code Slayer actually doing work with a real model, entirely locally, with cloud as an explicit, audited, off-by-default escape hatch.

---

## Engineering Intelligence

**Status: not started.**

Repository indexing, a context engine (so a worker gets the *relevant* slice of a codebase, not all of it or a guess), planning that produces a durable, reviewable plan, and independent review as a distinct role from implementation.

**Depends on:** Local Worker Runtime (planning and review are role-routed work, like any other phase).
**Delivers:** tasks that scale past "a few files" — real refactors, real multi-file features, plans a human can read and approve before implementation starts.

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

## Training

**Status: not started.**

The verified-dataset pipeline, dataset splits (`TRAIN`/`VALIDATION`/`FROZEN_EVAL`), language/domain specialists, training recipes, and the frozen-benchmark discipline described in the vision's [Training Data Pipeline](CODE_SLAYER_VISION.md#18-training-data-pipeline) through [Benchmark-Driven Routing](CODE_SLAYER_VISION.md#23-benchmark-driven-routing) sections.

**Depends on:** Lab (training data comes from verified Lab experience) and Knowledge (dataset curation reuses the same provenance/dedup discipline).
**Delivers:** the ability to turn accumulated, verified experience into a measurably better local model — evidence-gated at every step, never a raw dump of audit logs into a fine-tune.

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
