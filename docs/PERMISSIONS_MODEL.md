# Permissions Model

**Status:** authoritative governance specification (Governance Foundation,
slice G1). Defines the *vocabulary and semantics* a future machine-enforced
Permission Engine MUST implement. **No Permission Engine runtime exists
yet** — this document specifies what it must mean once built; it does not
implement tables, an API, or enforcement code.
**Relationship to other documents:** [`docs/SECURITY_PRIVACY_ARCHITECTURE.md`](SECURITY_PRIVACY_ARCHITECTURE.md)
establishes *why* (local-first, consent-before-discovery, non-transitive
consent, fail-closed, revocation); this document establishes the *shape*
those invariants are expressed through once implemented. Today's real,
already-implemented authorization mechanism remains
`policy.engine.PolicyEngine` (`docs/TOOLS_AND_POLICY.md`) plus
`workers.trust.WorkerTrustManager` (`CODE_SLAYER_VISION.md`§42) — this
document does not replace either; a future Permission Engine governs a
different, broader class of resource (network/storage/AI-server/model/
update/telemetry scope) that those two do not currently cover.

**Keywords:** MUST/MUST NOT/SHOULD/SHOULD NOT/MAY as in
[`SECURITY_PRIVACY_ARCHITECTURE.md`](SECURITY_PRIVACY_ARCHITECTURE.md).

---

## 1. Permission record shape

A future permission record MUST carry at minimum the following fields.
Exact field names/types/storage may evolve during implementation; the
*semantics* below MUST NOT be weakened by that evolution.

| Field | Meaning |
| --- | --- |
| `subject` | who/what this permission is granted to (the CSLR instance, a specific integration, a specific automated subsystem — never a model itself, per [`SECURITY_PRIVACY_ARCHITECTURE.md`§10](SECURITY_PRIVACY_ARCHITECTURE.md#10-model-authority)). |
| `resource` | the specific thing the permission concerns — a specific NAS share, a specific AI-server endpoint, a specific model source — never a bare category standing in for "everything of this kind." |
| `action` | the specific operation authorized — `discover`, `connect`, `authenticate`, `read`, `write`, `configure`, `execute`, `download`, `install`, `send`, etc. |
| `scope` | the permission string itself (§2) — the durable, versioned name for exactly what was granted. |
| `origin_of_authority` | what actually granted this — an explicit user consent interaction, a signed policy file, an administrator action. Never "the system assumed it was fine." |
| `granted_at` | when the grant occurred. |
| `expiry` (optional) | when the grant stops being valid, if it is not indefinite. |
| `version` | the semantic version of *this scope's own meaning* (§4) at the time it was granted. |
| `revoked_at` (optional) | when the grant was revoked, if it has been (§5). A revoked record MUST be retained, not deleted — revocation is itself evidence. |

## 2. Scope vocabulary (illustrative, not final syntax)

Exact syntax MAY evolve during implementation; the **scope semantics**
below — what is, and is not, covered by each — are what MUST remain
stable. Every scope below is a **required future** vocabulary item; none
is implemented today.

```
network.discovery.local              # local-network scanning/discovery only
network.connect:<resource>            # an actual connection to one specific resource

storage.discover                      # discovering storage devices/shares
storage.authenticate:<resource>       # authenticating to one specific store
storage.read:<resource>               # reading from one specific, already-authorized store
storage.write:<resource>              # writing to one specific, already-authorized store
storage.configure:<resource>          # mount options, share configuration, etc.

ai.discover                           # discovering AI-server endpoints on the network
ai.connect:<server>                   # connecting to one specific AI server
ai.inference:<server>                 # sending inference requests to one specific server
ai.training:<server>                  # sending training/fine-tuning work to one specific server

models.search                        # searching a model catalog/source
models.download:<source>             # downloading from one specific source
models.install                       # installing a downloaded model locally
models.remove                        # removing an installed model

internet.connect:<purpose>            # general internet access, scoped to one declared purpose

cloud.ai:<provider>                   # sending data to one specific cloud AI provider
                                       # (distinct from, and layered on top of, the existing
                                       # cloud_escalation policy dimension —
                                       # CODE_SLAYER_VISION.md §4)

updates.check                        # checking for available updates
updates.download                     # downloading an update artifact
updates.install                      # installing a verified update artifact

telemetry.send                       # sending any telemetry/analytics event
```

Each `<resource>`/`<server>`/`<source>`/`<provider>`/`<purpose>` placeholder
names one *specific* thing — never a wildcard standing in for "any." A
scope with no placeholder (`network.discovery.local`, `models.search`,
`updates.check`) is a genuinely resource-independent action (discovery
itself does not name what it might find).

## 3. Discover / connect / authenticate / read / write / configure / execute

This chain, already stated as a core invariant in
[`SECURITY_PRIVACY_ARCHITECTURE.md`§3](SECURITY_PRIVACY_ARCHITECTURE.md#3-consent-is-not-transitive),
is restated here as the concrete separation the scope vocabulary above
MUST enforce:

```
discover != connect != authenticate != read != write != configure != execute
```

A worked example for a future NAS integration (every step below is a
**separate** permission grant, obtained one at a time, never bundled):

```
network.discovery.local granted
        |
        v
CSLR finds a compatible NAS on the LAN (discovery only; nothing read)
        |
        v
storage.discover granted, user selects the specific NAS found
        |
        v
storage.authenticate:<nas-id> granted; user supplies/approves credentials
        |
        v
storage.read:<nas-id> and/or storage.write:<nas-id> granted, SEPARATELY,
for exactly the scope the user intends (read-only browsing is not the
same grant as read-write project storage)
        |
        v
storage.configure:<nas-id> granted only if CSLR needs to change mount
options / share configuration — a distinct, later, optional grant
```

Granting an earlier step in this chain MUST NOT be interpreted, by any
future code, as having implicitly granted a later one. A user who
grants `storage.read:<nas-id>` has not granted `storage.write:<nas-id>`,
regardless of how natural that might seem from an engineering
convenience standpoint.

## 4. Versioned scope meaning

Because revocation must never be reinterpreted into something broader
later ([`SECURITY_PRIVACY_ARCHITECTURE.md`§8](SECURITY_PRIVACY_ARCHITECTURE.md#8-revocation)),
each scope's *meaning* MUST itself be versioned:

- If a future release needs `storage.read:<resource>` to cover more than
  it used to (e.g. extending it to cover metadata that previously
  required a separate scope), that is a **new scope version**, and
  existing grants at the old version MUST continue to mean exactly what
  they meant when granted — they are not silently upgraded to the new,
  broader meaning.
- A permission record's `version` field (§1) is what makes "what did the
  user actually agree to, at the time they agreed to it" answerable
  forever, even after the scope's meaning has since evolved.

## 5. Revocation semantics

- Revoking a scope MUST mark it revoked (with `revoked_at`), never
  delete the record — the grant-then-revoke history is itself durable
  evidence, in the same spirit as the append-only audit log
  (`CODE_SLAYER_VISION.md`§60).
- Any future operation that would require a revoked scope MUST fail
  closed (`SECURITY_PRIVACY_ARCHITECTURE.md`§5), exactly as if the scope
  had never been granted.
- Revocation MUST be an action a user can take without needing to
  understand the underlying scope syntax — see Consent UX requirements
  below.

## 6. Consent UX requirements

The following UX-level rules constrain any future permission-request
surface, whichever specific product surface implements it (CLI prompt,
WebUI dialog, first-run wizard):

The UX MUST clearly distinguish, as separate, never-merged moments:

- the **permission request** itself (what is being asked for, right now);
- the **explanation** of what will happen if granted (plain language,
  before the user decides);
- **technical details** (protocols, destinations, ports/service types
  where applicable, retained metadata, retention behavior, and which
  source module implements the behavior) — available on request, never
  required reading to proceed, never omitted entirely;
- **denial** — declining MUST be a fully supported, first-class outcome,
  not a dead end or a degraded "are you sure?" loop;
- **later revocation** — a durable place the user can find and undo a
  grant made earlier, independent of whichever flow originally asked for
  it.

Example local-discovery flow (illustrative wording, from
[`SECURITY_PRIVACY_ARCHITECTURE.md`§2](SECURITY_PRIVACY_ARCHITECTURE.md#2-consent-before-discovery)):

```
CSLR wants permission to search your local network for compatible devices.

  [Allow]   [Not now]   [What will CSLR do?]
```

Non-negotiable UX constraints:

- **No dark patterns.**
- **No preselected "Allow."** The default, unforced state MUST be
  undecided, never a pre-checked affirmative a user must notice and
  uncheck.
- **No punishment for choosing "Not now."** Declining MUST NOT degrade
  unrelated functionality, nag on every subsequent screen, or be treated
  as a lesser-supported path than accepting.

## 7. What this document does not do

Consistent with [`SECURITY_PRIVACY_ARCHITECTURE.md`§13](SECURITY_PRIVACY_ARCHITECTURE.md#13-boundaries-this-document-intentionally-does-not-cross),
this document does not implement a permissions table, a permissions API,
a storage schema, or enforcement code. It fixes the vocabulary and
semantics so that whichever future slice actually builds the Permission
Engine has an unambiguous specification to build against, and so that
"does this new scope collapse two things that must stay separate" has an
answer before code is written, not after.
