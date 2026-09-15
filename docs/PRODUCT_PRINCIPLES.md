# Product Principles

**Status:** authoritative governance specification (Governance Foundation,
slice G1). Normative for product/UX decisions — see [`AGENTS.md`](../AGENTS.md).
**Relationship to other documents:** [`docs/CODE_SLAYER_VISION.md`](CODE_SLAYER_VISION.md)
is the long-term engineering-capability vision; this document is its
product/UX counterpart — what using CSLR should feel like, independent of
which engineering capability is currently implemented.
[`docs/OPERATIONS_UX.md`](OPERATIONS_UX.md) applies these principles to the
specific operational surface (CLI, install, update, doctor).

**Keywords:** MUST/MUST NOT/SHOULD/SHOULD NOT/MAY as in
[`SECURITY_PRIVACY_ARCHITECTURE.md`](SECURITY_PRIVACY_ARCHITECTURE.md).

---

## 1. Simple by default

A normal user SHOULD NOT need to understand any of the following to
perform routine CSLR operations:

- Python virtual environments;
- systemd;
- SQLite;
- `fstab`;
- SMB/NFS internals;
- Ollama (or any other local model runtime) API internals;
- repository/state database paths;
- migration numbers.

These remain real implementation details CSLR is built from — nothing in
this principle asks for them to disappear from the codebase. It asks that
a routine user's *path through the product* never requires knowing they
exist. Advanced mode (§2) remains available for anyone who wants direct
access to them.

## 2. Powerful underneath

Simplicity MUST NOT mean removal of control. CSLR is a tool for engineers
and technically capable users; hiding complexity from a routine flow is
not the same thing as removing the capability to reach it.

The product SHOULD offer progressive disclosure, in three layers:

```
simple  ->  advanced  ->  exact technical detail
```

A routine user stays in "simple." A power user can drop to "advanced." A
developer, auditor, or sufficiently curious user can always reach the
exact underlying detail — the same "progressive transparency" ladder
[`SECURITY_PRIVACY_ARCHITECTURE.md`§11](SECURITY_PRIVACY_ARCHITECTURE.md#11-transparency)
already requires for security-sensitive features, generalized here to
the whole product.

## 3. The browser is a control panel, not a lifeline

WebUI/browser lifetime MUST NOT own durable work. Closing the browser,
backgrounding a tab, losing network connectivity, or locking a phone
MUST NOT cancel server-owned work already durably accepted.

**Current implementation precedent:** Phase 8.2d's durable background
planning jobs (`docs/ENGINEERING_PLANNING.md#durable-background-jobs-phase-82d`).
`POST /api/plans` durably accepts a job and returns `202` immediately;
execution is owned by a server-side background executor, entirely
independent of the HTTP connection that requested it, and a disconnecting
client is never interpreted as cancellation.

This is the pattern every future long-running, browser-initiated
operation MUST follow — a model download, a NAS connection attempt, an
update installation, or anything else that can outlive a single HTTP
request.

## 4. No mystery behavior

A user SHOULD always be able to determine:

- what CSLR is currently doing;
- why (which request/permission/schedule caused it);
- what it can access;
- what it has actually accessed;
- which model/server is being used for a given operation;
- whether a given operation's traffic remains local or leaves the
  machine;
- what authority (permission scope, trust level) an operation is
  currently relying on.

This is the product-facing restatement of
[`SECURITY_PRIVACY_ARCHITECTURE.md`§6 "No hidden network behavior"](SECURITY_PRIVACY_ARCHITECTURE.md#6-no-hidden-network-behavior)
and of the existing audit/provenance discipline
(`CODE_SLAYER_VISION.md`§60) — applied here as a product expectation, not
only an architecture requirement.

## 5. Safe failure beats magic success

CSLR MUST prefer a clear, honest failure over silently bypassing an
established invariant to make an integration or a model appear to work.

**Current implementation precedent:** the structured tool-call protocol
(`workers.protocol_validation`) never parses free-form prose into a
trusted tool call, and never "recovers" a malformed structured output
into something usable — a model that fails to comply with the protocol
produces a recorded, categorized failure
(`planning.planner.PlannerFailureCategory`;
`docs/ENGINEERING_PLANNING.md#failure-provenance-phase-82b`), never a
best-effort guess. This same posture is required of every future
integration this document's sibling documents anticipate: a NAS that
can't authenticate cleanly, an AI server that returns garbage, or a
model download that fails a checksum MUST fail visibly and safely, never
silently degrade into an unverified, best-guess success.

## 6. User ownership

The user's repositories, infrastructure, credentials, models, and data
remain user-controlled at all times.

CSLR is an orchestrator/tool acting on the user's behalf, within
explicitly granted authority — it is never the owner of those resources.
This is the product-level restatement of
[`SECURITY_PRIVACY_ARCHITECTURE.md`](SECURITY_PRIVACY_ARCHITECTURE.md)'s
consent/revocation/fail-closed invariants: ownership stays with the user
because authority is always scoped, always revocable, and never assumed.
