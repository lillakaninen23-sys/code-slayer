# Operations UX

**Status:** authoritative governance specification (Governance Foundation,
slice G1) — a **target specification for a future operational surface**.
**No `cslr` CLI, `cslr doctor`, updater, or storage/AI-server/model
management commands exist yet.** The current, real CLI is `codeslayer`
(`src/code_slayer/cli/main.py`: `codeslayer inspect`, `codeslayer serve`)
and is **not renamed by this document** — see
[Current vs. future](#0-current-vs-future).
**Relationship to other documents:** [`docs/PRODUCT_PRINCIPLES.md`](PRODUCT_PRINCIPLES.md)
states *why* (simple by default, powerful underneath); this document states
the concrete *target* CLI/operational surface those principles point to.
[`docs/PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md) and
[`docs/SECURITY_PRIVACY_ARCHITECTURE.md`](SECURITY_PRIVACY_ARCHITECTURE.md)
govern what any of the commands below are actually allowed to do —
discovery commands in particular MUST pass through the consent/permission
system described there before they do anything (§4).

**Keywords:** MUST/MUST NOT/SHOULD/SHOULD NOT/MAY as in
[`SECURITY_PRIVACY_ARCHITECTURE.md`](SECURITY_PRIVACY_ARCHITECTURE.md).

---

## 0. Current vs. future

Every command name and subsystem in this document is a **required future
target**, not a current feature. Nothing here renames, deprecates, or
wraps the existing `codeslayer` CLI in this slice. When a future slice
actually builds `cslr`, it MUST satisfy the requirements below; until
then, this document is a specification with no corresponding
implementation, and must not be cited as if it were one.

## 1. Target CLI identity

The primary user-facing CLI SHOULD eventually be:

```
cslr
```

Illustrative future surface (names, not a committed final syntax):

```
cslr --help
cslr help

cslr start
cslr stop
cslr status
cslr open

cslr install
cslr update

cslr doctor

cslr models
cslr ai
cslr storage

cslr backup
cslr restore

cslr logs
```

## 2. Child-simple operations

Routine installation, update, and repair SHOULD NOT require a user to
manually run:

- `systemctl` (or an equivalent service-manager invocation);
- `sqlite3` against a CSLR state database;
- editing `/etc/fstab`;
- `source .venv/bin/activate`;
- setting `PYTHONPATH`;
- `curl` against CSLR's own internal APIs;
- a database migration command;
- editing CSLR's own source code.

**The implementation MAY use every one of these internally.** This
principle is about the user-facing surface, not about forbidding these
mechanisms from CSLR's own implementation — `cslr update` doing a
migration internally is exactly the point; a user typing a migration
command themselves is what this principle removes.

## 3. `cslr doctor` (target)

A future `cslr doctor` SHOULD eventually check, at minimum:

- backend/service health;
- WebUI reachability;
- database/schema health;
- storage (mounted shares, free space, reachability);
- permission-grant state (`docs/PERMISSIONS_MODEL.md`);
- configured AI server reachability;
- planner/coder/reviewer role qualification status (evidence-driven, per
  `CODE_SLAYER_VISION.md`§41–§43 — never merely "is a model loaded");
- installed model availability;
- disk space;
- network configuration;
- update state (current version, available updates, signature/checksum
  status).

A future `cslr doctor --fix` MAY perform only safe, reversible repairs
automatically (e.g. restarting a crashed service, re-running a completed
migration check). Any repair that is not safely reversible MUST require
explicit user approval before it runs — `--fix` is never a blanket "do
whatever it takes" switch.

**`cslr doctor` is not implemented in this slice.**

## 4. NAS / storage UX target

Future target commands:

```
cslr storage discover
cslr storage add
```

Discovery MUST first pass through the consent/permission system in
[`docs/PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md) — `cslr storage
discover` is not a bare network scan; it is the user-facing trigger for
the `network.discovery.local` → `storage.discover` consent flow described
there.

Conceptual flow (every arrow is a separate, non-transitive permission
grant — see
[`docs/PERMISSIONS_MODEL.md`§3](PERMISSIONS_MODEL.md#3-discover--connect--authenticate--read--write--configure--execute)):

```
user grants network discovery
        |
        v
compatible storage found
        |
        v
user selects one specific storage device
        |
        v
connect permission granted, for that specific device
        |
        v
authentication
        |
        v
specific read and/or write scope granted
        |
        v
optional mount/configure permission granted
```

These steps MUST NEVER be collapsed into one blanket "allow CSLR to use
this NAS" approval. A user who connects a NAS for read-only browsing has
not thereby authorized write access, and has certainly not authorized
CSLR to reconfigure its shares.

**No NAS/storage integration is implemented in this slice.**

## 5. AI-server UX target

Future target commands:

```
cslr ai discover
cslr ai add
cslr ai test
```

CSLR SHOULD eventually be able to detect compatible local AI runtimes and
report, for each:

- reachability;
- provider/runtime type (e.g. an Ollama-compatible endpoint);
- available models;
- latency;
- structured-output capability;
- tool-call qualification (evidence-based — see
  `CODE_SLAYER_VISION.md`§41 Model preflight/conformance, and the real,
  current diagnostic precedent in Phase 8.2c's investigation of
  structured tool-call reliability, `docs/ENGINEERING_PLANNING.md`);
- known safe roles (which roles this server/model combination has
  actually earned trust for — never a role assumed from general
  reputation, per `CODE_SLAYER_VISION.md`§43).

**Discovery itself does not authorize inference.** Finding a compatible
AI server does not grant `ai.connect`, and connecting does not grant
`ai.inference` — each is its own permission
(`docs/PERMISSIONS_MODEL.md`§2).

**No AI-server discovery/integration is implemented in this slice.**

## 6. Model management UX target

Future target commands:

```
cslr models
cslr models search
cslr models install
cslr models test
```

Users SHOULD eventually be able to:

- search for compatible models;
- see hardware-fit recommendations before downloading anything large;
- download/install models;
- qualify an installed model for specific CSLR roles;
- remove a model safely.

**A model being installed MUST NOT automatically mean it is qualified for
any role** — planner, coder, reviewer, or any capability requiring
mutation trust. Qualification remains evidence-driven, exactly as
`CODE_SLAYER_VISION.md`§41/§42 already require for any model reaching any
role: conformance evidence first, trust earned from it, never granted
because installation succeeded.

Downloading a model requires explicit network/download authority
(`models.download:<source>`, `docs/PERMISSIONS_MODEL.md`§2) — installing
CSLR does not pre-authorize downloading arbitrary models from arbitrary
sources.

**No model search/download/install tooling is implemented in this
slice.**

## 7. Update UX target

A future `cslr update` SHOULD aim for this sequence:

```
preflight
    |
    v
backup
    |
    v
artifact verification (signature/checksum)
    |
    v
safe migration
    |
    v
restart
    |
    v
health verification
    |
    v
rollback on failure
```

`curl | sh`-style installation SHOULD NOT be the long-term primary
installation model. The long-term target prefers signed, verifiable
releases — see
[`docs/SECURITY_PRIVACY_ARCHITECTURE.md`§12 Supply chain](SECURITY_PRIVACY_ARCHITECTURE.md#12-supply-chain-future-requirements).

**No updater is implemented in this slice.**

## 8. Privacy & Security UI target

A future WebUI area, conceptually:

```
Privacy & Security
    Permissions
    Connected devices
    Network activity
    Credentials
    External services
    Updates & signatures
    Audit
    Lockdown mode
```

Possible future profiles a user could select from, illustrative only:

```
Strict Local
Local Trusted
Custom
```

**These are future UX targets. No such WebUI area, and no such profile
system, exists today.**

## 9. What this document does not do

This document does not implement any command, subsystem, or UI area
listed above. It fixes the target shape and the constraints (child-simple
surface, evidence-gated qualification, non-transitive consent, safe
update sequencing) so that whichever future slice builds each piece
builds it consistently with the others, and consistently with
[`docs/SECURITY_PRIVACY_ARCHITECTURE.md`](SECURITY_PRIVACY_ARCHITECTURE.md)
and [`docs/PERMISSIONS_MODEL.md`](PERMISSIONS_MODEL.md).
