# Operations UX

**Status:** authoritative governance specification (Governance Foundation,
slice G1) — a **target specification for a future operational surface**,
with a **first implemented slice as of service-web-admin-v1**.
The current CLI remains `codeslayer` (`src/code_slayer/cli/main.py`) and
is also invoked as `./cslr` / the `cslr` entry point — this document
does **not** rename the Python package. Implemented today:
`inspect`, `serve`, `install-service`, `status`, `start`, `stop`,
`restart`. **Not implemented:** `cslr doctor`, NAS/storage management,
LAN AI-server discovery, model download/install, backup/restore.
See [Current vs. future](#0-current-vs-future).
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

Every command name and subsystem in this document remains a **required
future target** unless listed as implemented above. Nothing here
deprecates the existing `codeslayer` Python entry point. `./cslr`
is a checkout wrapper around the same CLI.

Implemented in the service-web-admin-v1 baseline plus the reconciled
Engineering Control Room frontend:

- `./cslr install-service` — managed venv, user systemd unit,
  enable+start, persistent XDG config, loopback bind 127.0.0.1:8765
- `./cslr status|start|stop|restart`
- one Control Room with Dashboard, Projects, Tasks, Models, Intelligence,
  Planning, Privacy & Security, Audit, and Settings navigation
- Runtime configuration and explicit live attestation under Models
- Certification Center v1 under Privacy & Security
- System/deployment and Tailscale Serve administration under Settings
- fail-closed `Check for update` / optional fast-forward apply
- read-only Dashboard control-plane summaries for deployment, runtime
  configuration, certification projection, and remote access

Dashboard summaries are deliberately observation-only. They use separate GET
snapshots and do not live-attest a model, approve identity, start/preflight
certification, restart/apply an update, or mutate Tailscale. Runtime,
certification, deployment, and remote-access authority remain in their
dedicated backend projections and explicit controls.

Still specification-only: doctor, storage, LAN discovery, model
catalog download, backup/restore, and the rest of §1.

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
