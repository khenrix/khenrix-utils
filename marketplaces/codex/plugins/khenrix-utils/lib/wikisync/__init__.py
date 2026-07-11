"""wikisync — deterministic core for the khenrix wiki-add / wiki-sync skills.

Stdlib-only. The LLM/tool edges (fetch, extract, classify) live in the SKILL.md
bodies; everything reproducible — URL canonicalization, the SQLite ledger, the
capture cache, page rendering, and the JSON job protocol — lives here so behavior
is identical across CLIs and testable without tokens.
"""

__version__ = "0.1.0"
SCHEMA_VERSION = 1
