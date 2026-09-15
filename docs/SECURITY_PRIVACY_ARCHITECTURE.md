# Security & Privacy Architecture

**Status:** authoritative governance specification (Governance Foundation,
slice G1). Normative for every future design and every future agent
change — see [`AGENTS.md`](../AGENTS.md).
**Relationship to other documents:** this document specifies *invariants*
CSLR's security/privacy posture must hold, now and as new subsystems
(network discovery, NAS/storage integration, AI-server integration, model
management, the updater) are designed. [`docs/PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md)
specifies the machine-enforced vocabulary those invariants are expressed
through. [`docs/CODE_SLAYER_VISION.md`](CODE_SLAYER_VISION.md) remains the
authoritative long-term product vision; this document is that vision's
security/privacy layer made explicit and normative. Where an invariant here
overlaps something the Vision already states (cloud escalation, tool/command
policy, audit), this document cross-references rather than restates it.

**Keywords:** MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY in this document
are to be read as normative requirement levels (informally, per RFC 2119):
MUST/MUST NOT are non-negotiable; SHOULD/SHOULD NOT may be deviated from
only with a documented, deliberate reason; MAY is a permitted option, never
an implied default.

**Current vs. future — read this before anything else in this file.** Every
requirement below is a permanent invariant the moment it is written down.
Whether the *runtime engine that enforces it exists yet* is a separate
question, marked explicitly at each requirement as either:

- **Current implementation** — a real, already-built mechanism (cite it).
- **Required future behavior** — an invariant that MUST hold once the
  relevant subsystem (network discovery, NAS integration, AI-server
  discovery, model downloads, the updater, a credentials vault) is built;
  no such subsystem exists yet, and building one without first satisfying
  its requirement here is itself the defect this document exists to
  prevent.

Never read a "MUST" below as a claim that today's code already enforces it,
unless it is explicitly marked **Current implementation**.

---

## 1. Local-first

CSLR MUST be local-first by default.

Local-first means:

- user project data (repository content, task state, audit history,
  durable evidence) remains local unless one specific, explicitly
  authorized operation requires otherwise.
- telemetry/analytics are OFF by default (§9).
- cloud AI is OFF by default (**Current implementation**: `cloud_escalation`
  defaults to `disabled`; see [`CODE_SLAYER_VISION.md`§4](CODE_SLAYER_VISION.md#4-normal-runtime-vs-cloud-escalation)
  and `workers.cloud_escalation`).
- external network transmission requires explicit authority — a specific,
  scoped permission (`docs/PERMISSIONS_MODEL.md`), never an ambient
  capability the process simply has.
- LAN discovery is not implicit authority for internet access (§3, §4).

**Local-first does not mean CSLR never uses networking.** It means every
network use is explicit, scoped, attributable (§6), and user-controlled —
never ambient, never assumed, never bundled silently into an unrelated
feature.

## 2. Consent before discovery

*Required future behavior* — no discovery subsystem (LAN scanning, NAS
discovery, AI-server discovery) exists yet; this section specifies what
its consent gate MUST look like once one is designed.

CSLR MUST NOT scan or discover devices or services on the local network
without explicit, informed user consent, obtained before the first such
scan.

Before first discovery, the user-facing flow MUST conceptually offer:

> "Allow CSLR to look for compatible devices on your local network?"
>
> **[Allow]** &nbsp; **[Not now]** &nbsp; **[What will CSLR do?]**

"What will CSLR do?" MUST plainly explain, in the same interaction, before
any scan runs:

- what CSLR will search for (e.g. specific service types/protocols);
- what metadata may be observed (e.g. hostnames, advertised service
  records) as a result;
- what, if anything, may be retained, and where;
- what discovery will explicitly **not** do (e.g. it does not itself read,
  write, authenticate to, or configure anything it finds — §3);
- whether any observed data leaves the local system;
- how the permission can be revoked (§8);
- where the technical/source detail behind the claim above can be
  inspected (§11 — progressive transparency).

Consent MUST be obtained through this explicit, in-context flow — never
satisfied by a general Terms of Service acceptance, an installer default,
or a setting buried where a routine user is unlikely to find it. See
[`docs/PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md#consent-ux-requirements)
for the full consent-UX rules.

## 3. Consent is not transitive

This is a core invariant, and it applies to every future integration
(NAS/storage, AI servers, model sources, cloud providers) without
exception:

```
discover   != connect
connect    != authenticate
authenticate != read
read       != write
write      != configure
configure  != execute
```

Permission for one of these MUST NOT be silently interpreted as permission
for another. Concretely (all **required future behavior**, since none of
these integrations exist yet):

- Discovering a NAS on the network does NOT permit reading it.
- Authenticating to a NAS does NOT permit writing to it.
- Discovering a local AI server (e.g. an Ollama-compatible endpoint) does
  NOT permit sending project code/context to it — that is a separate
  `ai.connect`/inference-scoped permission (`docs/PERMISSIONS_MODEL.md`).
- Connecting to an AI server does NOT permit downloading models through
  it.
- Allowing model downloads does NOT permit arbitrary internet access —
  a model download is scoped to its own declared source.

Every step in that chain is its own permission grant, with its own scope,
its own record of when and why it was granted, and its own independent
revocation (§8).

## 4. Least privilege

CSLR MUST run with the least privilege reasonably required for the
operation actually being performed.

- CSLR MUST NOT run permanently as `root`/an administrative account
  merely for convenience.
- Where elevated privilege is genuinely required for one operation
  (**required future behavior** — e.g. a future storage-mount operation),
  the elevation MUST be: explicit (the user can see it is happening and
  why), purpose-specific (scoped to exactly that operation, not a general
  "run as root from now on"), temporary where at all possible, and visible
  in the audit trail (§6).
- This extends the isolation posture already established for mutating
  coding work — see [`CODE_SLAYER_VISION.md`§38 OS-level containment](CODE_SLAYER_VISION.md#38-os-level-containment)
  (rootless execution, read-only host mounts by default, credentials
  removed unless explicitly required) — to CSLR's own operating
  privileges, not only a worker's sandbox.

## 5. Fail closed

**Current implementation** for the decision surfaces that exist today:
`policy.engine.PolicyEngine` denies on malformed/ambiguous input rather
than guessing (`docs/TOOLS_AND_POLICY.md`); `lease.manager.LeaseManager`
and `workers.question_gate.QuestionGate` follow the identical posture
(`docs/LEASES_AND_RECOVERY.md`; `CODE_SLAYER_VISION.md`§34).

If CSLR cannot prove authority for an action, it MUST do nothing.

This is a permanent posture, not one specific to today's policy engine:

- Unknown or ambiguous permission state MUST NOT become an implicit
  allow, in any future subsystem, for any reason including "the user
  probably meant yes" or "this is obviously safe."
- A missing, expired, malformed, or unverifiable permission record MUST
  be treated identically to an explicit denial.
- This generalizes directly to every future permission scope in
  [`docs/PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md) — a scope CSLR
  cannot durably prove was granted behaves exactly as if it was denied.

## 6. No hidden network behavior

*Required future behavior*, since no dedicated network-activity audit
surface exists yet — this specifies the architecture requirement that
MUST be satisfiable once one is built, not a promise that it exists.

Every network action CSLR takes MUST be attributable. Concretely, the
architecture MUST make it possible to answer, for any network action:

- what destination was contacted;
- why (which user request, permission grant, or scheduled operation
  caused it);
- under what permission scope (`docs/PERMISSIONS_MODEL.md`) it was
  authorized;
- which CSLR subsystem initiated it;
- whether, and what, data was sent;
- when it happened.

This document does **not** promise packet-level forensic logging, deep
packet inspection, or any specific implementation — those are
implementation choices for whichever future design actually builds the
network-activity audit surface. What is required *now*, as an
architecture constraint on that future design, is that the six questions
above are answerable from durable evidence, in the same spirit as the
already-implemented append-only audit log (`audit_events`, ADR 0004; see
[`CODE_SLAYER_VISION.md`§60](CODE_SLAYER_VISION.md#60-audit--replay)) —
a future network-activity record is that same discipline applied to
network actions specifically, never a separate, weaker audit story.

## 7. Secrets

Secrets (credentials, API keys, passwords, tokens, private keys) MUST be
purpose-scoped: a secret granted for one integration/operation MUST NOT
be usable, or reachable, by an unrelated one.

Secrets MUST NOT appear in:

- ordinary logs;
- ordinary audit summaries (`audit_events` payloads — the existing
  discipline of recording *that* a decision was made and *why*, never
  raw sensitive content, already established by
  [`CODE_SLAYER_VISION.md`§60](CODE_SLAYER_VISION.md#60-audit--replay),
  extends to secrets specifically);
- model prompts, unless strictly required for one explicitly authorized
  operation, and even then scoped to exactly what that operation needs —
  never a secret handed to a model "just in case";
- training datasets (**Current implementation**, already required:
  [`CODE_SLAYER_VISION.md`§18](CODE_SLAYER_VISION.md#18-training-data-pipeline)
  requires "no secret contamination" for training eligibility; this
  document extends the same requirement to every other export surface
  below);
- exported diagnostics (e.g. a future `cslr doctor` report);
- ordinary WebUI/API responses (**Current implementation** precedent:
  the existing WebUI API already never returns raw prompt/answer text,
  provider configuration, or credentials — see
  [`docs/WEBUI_API.md`](WEBUI_API.md)'s safety boundary section — this
  document generalizes that same posture to secrets specifically, for
  every future endpoint).

**A dedicated secret-storage design (a credentials vault) does not exist
yet and is explicitly out of scope for this slice.** This document
establishes the requirement a future vault design MUST satisfy; it does
not specify or implement one.

## 8. Revocation

Permissions MUST be revocable.

- Revoking a permission MUST affect future operations — an operation
  already in flight when a scope is revoked is not retroactively made
  invalid, but no *new* operation may rely on a revoked scope.
- A future version of CSLR MUST NOT reinterpret an old, already-granted
  permission as authorization for a broader capability than the user
  actually granted. If a permission's meaning needs to change, that is a
  new permission, not a reinterpretation of the old one.
- This is exactly why permission semantics need stable, versioned meaning
  — see [`docs/PERMISSIONS_MODEL.md`§"version"](PERMISSIONS_MODEL.md).

## 9. Telemetry and analytics

Telemetry/analytics MUST be OFF by default (§1). No future subsystem may
flip this default without an explicit, separate, informed consent flow
following the same rules as §2's discovery consent (plain explanation,
no dark patterns, easy to find, easy to revoke).

## 10. Model authority

This section makes explicit, for security/privacy purposes specifically,
an authority boundary [`CODE_SLAYER_VISION.md`§2 and §34](CODE_SLAYER_VISION.md#2-core-principle)
already establish in general:

- Models MUST NOT grant themselves permissions.
- Model output is not user consent.
- Model output is not repository fact unless validated through
  authoritative evidence (**Current implementation** precedent: Phase 8.2's
  evidence validation, `docs/ENGINEERING_PLANNING.md`, never lets a
  model's own existing-file/command claim become fact merely because it
  was asserted).
- A model MUST NEVER be able to broaden, by anything it outputs: network
  scope, storage scope, trust level, cloud access, filesystem authority,
  or execution authority. Every one of these remains gated by
  deterministic policy/permission checks the model's own output cannot
  touch — the same structural argument already used for planning
  (`planning.service`'s own module docstring: "nothing in this module's
  import graph is even capable of it") generalizes to every future
  subsystem.

## 11. Transparency

CSLR SHOULD support progressive transparency for security-sensitive
behavior:

```
simple explanation  ->  technical details  ->  exact implementation / source code
```

For a security-sensitive feature, the user should never be required to
accept only "trust us" — there should always be a next, more detailed
layer available to whoever wants it.

**Open-source/source inspectability is part of the trust model, but
source visibility alone is NOT sufficient security.** Auditable source
does not substitute for the invariants elsewhere in this document (fail
closed, least privilege, consent, revocation) actually holding at
runtime — it is a complement to them, not a replacement.

## 12. Supply chain (future requirements)

*Required future behavior* — no updater, release-signing, or SBOM
tooling exists yet (§12 duplicates nothing here; see
[`docs/OPERATIONS_UX.md`](OPERATIONS_UX.md#update-ux-target) for the
user-facing update flow this underpins). A future update/release
mechanism MUST provide:

- signed releases;
- verified update artifacts (the client verifies a signature/checksum
  before treating a downloaded artifact as trusted, not after);
- checksums/signatures for every distributed artifact, not only the
  primary binary;
- rollback-safe updates — a failed or bad update MUST have a working path
  back to the last known-good version;
- dependency/SBOM visibility — what CSLR depends on MUST be inspectable;
- reproducible builds as a **long-term goal** — explicitly not a current
  property. Do not claim reproducible builds exist today; they do not.

This extends [`CODE_SLAYER_VISION.md`§30 Supply chain security](CODE_SLAYER_VISION.md#30-supply-chain-security)
(which covers dependencies generally) to CSLR's own distribution and
update mechanism specifically. `curl | sh`-style installation is
explicitly not the long-term primary installation model — see
[`docs/OPERATIONS_UX.md`](OPERATIONS_UX.md#update-ux-target).

## 13. Boundaries this document intentionally does not cross

Consistent with the Vision's own [Guiding Constraints](CODE_SLAYER_VISION.md#guiding-constraints)
("nothing in this document is a specification to implement now"), this
document does **not**:

- specify a wire format, database schema, or API for any of the above;
- implement network discovery, NAS integration, AI-server discovery,
  model downloading, an updater, a Permission Engine runtime, or a
  credentials vault;
- claim any of those subsystems exist.

Its job is narrower and comes first: fix the rules those subsystems MUST
satisfy, so that when they are designed, "does this violate an
established invariant" has an unambiguous answer.

See [`docs/PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md) for the
machine-enforceable permission vocabulary these invariants are expressed
through, and [`docs/ROADMAP.md`](ROADMAP.md#security--privacy--operations-future-track)
for how the subsystems this document constrains are sequenced.
