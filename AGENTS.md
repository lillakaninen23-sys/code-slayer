# AGENTS.md — rules for any agent changing this project

**Read this before changing Code Slayer (CSLR).** It is short on purpose —
the full authoritative documents it points to are not, and you are
expected to actually open them when your change touches what they cover.

## Read first, in order of what your change touches

| If your change touches... | Read |
| --- | --- |
| product direction, long-term architecture | [`docs/CODE_SLAYER_VISION.md`](docs/CODE_SLAYER_VISION.md) |
| networking, discovery, secrets, telemetry, cloud, supply chain | [`docs/SECURITY_PRIVACY_ARCHITECTURE.md`](docs/SECURITY_PRIVACY_ARCHITECTURE.md) |
| what an action is allowed to do, consent, scopes | [`docs/PERMISSIONS_MODEL.md`](docs/PERMISSIONS_MODEL.md) |
| UX, simplicity, what a user should never need to know | [`docs/PRODUCT_PRINCIPLES.md`](docs/PRODUCT_PRINCIPLES.md) |
| CLI/install/update/operational surface | [`docs/OPERATIONS_UX.md`](docs/OPERATIONS_UX.md) |
| tool/policy/trust/lease/checkpoint mechanics | `docs/TOOLS_AND_POLICY.md`, `docs/LEASES_AND_RECOVERY.md`, `docs/CHECKPOINTS.md` |
| ordering/dependencies between phases | [`docs/ROADMAP.md`](docs/ROADMAP.md) |

These five documents (Vision, Security & Privacy, Permissions, Product
Principles, Operations UX) state **normative requirements, not
suggestions**. A change that violates one of them is a defect, even if
it makes a feature or a demo work.

## Non-negotiable rules

- **Privacy/security requirements are normative, not suggestions.** They
  do not yield to a deadline, a convenience, or "the model wanted to."
- **Model convenience never overrides an authority boundary.** A model
  producing plausible-looking output is never itself permission to do
  anything a policy/permission check would otherwise deny.
- **Missing authority fails closed.** If the system cannot prove it is
  allowed to do something, it MUST do nothing — never treat an unknown
  or ambiguous permission state as an implicit allow.
- **No new network behavior without explicit design review against
  [`docs/PERMISSIONS_MODEL.md`](docs/PERMISSIONS_MODEL.md).** Adding an
  HTTP call, a discovery protocol, or any other network-reaching code
  path is a permissions-model change, not a routine one.
- **No secret/raw-credential leakage** into logs, model prompts (beyond
  what one explicitly authorized operation strictly requires), training
  data, audit summaries, diagnostics exports, or ordinary WebUI/API
  responses.
- **No silent widening of existing permissions.** A capability someone
  granted for one scope/purpose never quietly starts covering a broader
  one in a later change.
- **No prose/tool-call recovery around an established structured
  protocol.** If a model emits free text where a structured call was
  required, that is a protocol failure to record and contain — never a
  string to parse, regex, or otherwise "rescue" into a trusted action.
- **Browser/client lifetime must not own durable work.** Once the
  server has durably accepted a job, a disconnecting/backgrounded/
  closed client MUST NOT change whether that work continues (Phase
  8.2d is the current implementation precedent).
- **Preserve unrelated user files.** Never touch `opencode.json`, or
  any other clearly-local/user-owned untracked file, unless a task
  explicitly asks you to. Check `git status` before you start, and
  never stage or commit files a task didn't ask for.

## When in doubt

Prefer the smaller, more conservative change, cite the section of the
authoritative document your change follows, and — for anything touching
security/privacy/permissions — say so plainly in your final report
rather than assuming silence means it was fine.
