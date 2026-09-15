"""Explicit, code-owned planner-turn context bounds (Phase 8.2b).

## Why this exists

A live production planning turn against a real local model (Devstral,
over `workers.openai_compatible_adapter.OpenAICompatibleAdapter`) sent a
durable planner input of **151211 bytes** — the model, faced with that
much prompt content, ignored the `tool_choice: "required"` structured
`emit_engineering_plan` tool entirely and emitted ~4 KB of free-form
prose instead, containing unsupported/hallucinated repository claims.
The failure was `invalid_transport_response:text_response` — a
transport-shaped diagnosis of what was actually a context-budgeting
defect: `planning.service` was handing `intelligence.service.
RepositoryIntelligenceService.build_context_pack()` no explicit bounds
at all, so it silently fell back to `intelligence.limits`'s own
*indexing-pipeline* defaults (`DEFAULT_CONTEXT_PACK_MAX_FILES=20`,
`DEFAULT_CONTEXT_PACK_MAX_BYTES=200_000`,
`DEFAULT_CONTEXT_PACK_PER_FILE_BYTES=20_000`) — bounds sized for a
general-purpose context consumer, not for one planner turn's prompt.

## Deliberately separate from `intelligence.limits`

These bounds govern specifically how much of Repository Intelligence's
already-bounded, already-ranked evidence one planner turn is ever
handed — never the broader indexing pipeline itself
(`intelligence.limits`, unmodified). A planner-turn budget is always
allowed to be *tighter* than the indexing pipeline's own ceiling; it
must never need to be wider, since a planner never needs more context
than a human reviewer would find useful to read at once.

## Byte limits only, never token estimation

Tokenization is model/tokenizer-specific and non-deterministic across
providers (a byte count is not a token count, and the ratio varies by
model family and even by content). Byte limits are what `intelligence.
query.build_context_pack()` already enforces deterministically; reusing
that same authority boundary here — rather than inventing a second,
approximate token-counting one — keeps this bound testable and exactly
reproducible on every machine, with no dependency on knowing which
model is actually being talked to.

## Values

Start conservative: `PLANNER_MAX_FILES * PLANNER_MAX_PER_FILE_BYTES`
already bounds the file-content share of one planner turn to at most
`8 * 8192 = 65536` bytes, well under the `32768`-byte total-file budget
even in the worst case (`PLANNER_MAX_TOTAL_FILE_BYTES` is the tighter,
binding constraint in practice) — and both are more than an order of
magnitude below the 151211-byte failure this phase investigates. Any of
these three may be tuned in a later phase with real production evidence
justifying a different value; today they are deliberately conservative.
"""

from __future__ import annotations

PLANNER_MAX_FILES = 8
PLANNER_MAX_TOTAL_FILE_BYTES = 32_768
PLANNER_MAX_PER_FILE_BYTES = 8_192
