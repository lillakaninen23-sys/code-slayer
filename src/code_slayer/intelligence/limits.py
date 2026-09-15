"""Explicit, code-owned bounds (Phase 8.1 §16 "Performance / bounds").

Every number here is a deliberate, testable ceiling — never a request
input. A caller can only ever narrow one of these bounds (e.g. a smaller
`max_files` for a context pack), never widen it.
"""

from __future__ import annotations

# -- inventory / snapshot ----------------------------------------------------

# Hard ceiling on how many candidate files one snapshot ever inspects.
# Beyond this, the inventory is truncated deterministically (path order)
# and the snapshot records that it was.
MAX_INVENTORY_FILES = 5000

# A file larger than this is still listed (path/size/language recorded),
# but its bytes are never read for content hashing, symbol extraction,
# or context-pack inclusion.
MAX_TEXT_FILE_BYTES = 512 * 1024  # 512 KiB

# Aggregate ceiling on bytes actually read from disk across one snapshot
# build (content hashing + symbol extraction combined) — bounds total
# I/O and memory even when every individual file is under the per-file
# cap above.
MAX_TOTAL_INDEXED_BYTES = 32 * 1024 * 1024  # 32 MiB

# How many leading bytes are sniffed to decide binary vs. text.
BINARY_SNIFF_BYTES = 8192

# -- query --------------------------------------------------------------

MAX_QUERY_RESULTS = 50
DEFAULT_QUERY_RESULTS = 20

# -- context pack ---------------------------------------------------------

DEFAULT_CONTEXT_PACK_MAX_FILES = 20
MAX_CONTEXT_PACK_MAX_FILES = 50
DEFAULT_CONTEXT_PACK_MAX_BYTES = 200_000
MAX_CONTEXT_PACK_MAX_BYTES = 1_000_000
DEFAULT_CONTEXT_PACK_PER_FILE_BYTES = 20_000
MAX_CONTEXT_PACK_PER_FILE_BYTES = 100_000

INDEX_VERSION = "phase8.1-v1"
