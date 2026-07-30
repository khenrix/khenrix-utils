# Check catalog — B1–B16

Field names below are read straight from `setup_audit.py` (`scripts/khenrix-audit/scripts/setup_audit.py`
in this checkout) — if the engine changes an `evidence={...}` dict, update this file in the same
change; this doc is the sync contract, not a paraphrase.

Checks gated by an unverified `SEMANTICS` axis (`RULE_NEEDS`: B1→`precedence_verified`,
B2→`namespacing`, B3→`dedupe_rule`) come back `informational` for any CLI where that axis is
`False` — currently codex and agy for all three. Informational findings never reach guided apply.

## B1 — Name collisions + shadowing
Detects the same bare skill name reachable from two different owners on one CLI (plugin
skills are reachable at their bare name too, per verified Claude precedence).
Evidence: `bare_name`, `paths` (sorted source paths of every colliding owner).
Grouped per `(cli, bare_name)` — a same-named skill on a *different* CLI is not a collision here.
Gated on `precedence_verified`: informational on codex/agy until that axis is verified.

## B2 — Bare-key MCP references
Detects a permission rule or hook matcher naming a plugin-namespaced MCP server by its bare
key (`mcp__<server>__…` instead of `mcp__plugin_<plugin>_<server>__…`), which silently never
matches. Evidence: `reference` (the matched string), `config` (source file), `expected_prefix`.
Only permission rules and hook matchers are scanned — allowed-tools lines and subagent tool
lists (both named in the design spec) are not yet walked, so a bare-key reference living only
there is currently invisible. Gated on `namespacing`: informational on codex/agy.

## B3 — Endpoint-dedupe hazards
Detects the same normalized MCP endpoint (command+args, or URL) reachable at two scopes —
the lower-precedence copy is silently dropped. Evidence: `endpoint_hash`, `configs`.
`endpoint_hash` is an 8-hex-char truncated sha256 (`vhash`); two genuinely different endpoints
colliding on that truncation is possible though unlikely. No `definite`/`possible` tiering yet —
any hash collision within a CLI is reported flat. Gated on `dedupe_rule`: informational on
codex/agy.

## B4 — Declared↔live drift
Detects capabilities.toml declared-but-not-live, live-but-managed-absent (policy-driven), and
live-but-undeclared ("unmanaged") MCP servers, per CLI. Evidence varies by direction:
`{"direction": "declared-not-live"}`, `{"direction": "managed-absent-but-live", "reason": ...}`,
or `{"direction": "unmanaged"}`. Without `--repo-root` the whole check is
`{"error": "no canonical repo root"}` and marked `not_evaluated` (never a silent clean pass).
The platform gate (`capabilities.toml [mcp_servers.*] platform=`) applies ONLY to the
declared-not-live direction — a Windows-only server absent on Linux is not drift. "unmanaged"
findings are low-confidence and re-reported every run by design (deliberately not
self-suppressing) unless a ledger policy exists for that subject.

## B5 — Cross-CLI capability drift
Detects a declared MCP server present on at least one installed CLI but missing on another,
respecting the same platform gate as B4 (not naive set equality). Evidence: `present_on`
(the CLIs that do have it). A CLI-specific server correctly gated by `platform=` never fires
here for the CLIs it's not meant to run on.

## B6 — Trigger-surface overlap (nomination only)
Nominates skill pairs by TF-IDF cosine over each skill's extracted trigger surface (quoted
phrases, `Triggers:` lists, "use when" clauses), top-K per CLI (K≈15), requiring ≥2 shared
non-stopword tokens to nominate at all. Evidence: `cosine`, `shared_tokens`, `shared_phrases`
(exact quoted-phrase overlap), `same_plugin`, `corpus_size`, `nominated` (pool size).
Every B6 finding carries `note: "heuristic nomination — adjudicate before acting"` — it is
never itself a confirmed collision; SKILL.md Phase C adjudication is mandatory before acting.
Intra-plugin pairs (`same_plugin=true`) ARE nominated, not filtered out.

## B7 — Context-budget analysis (Claude only)
Detects always-on skill-listing tokens exceeding 1% of the context window. Without
`--tokens-file`: `{"budget": budget}`, `not_evaluated=True` — never a silent pass. Over
budget: `total_always_on`, `budget`, `over_by_pct`, `estimator`, `biggest_payers` (top 5),
`instruction_file_estimates_chars4`. The engine explicitly never predicts which description
gets dropped (drop order is unobservable) — only that the total is over.

## B8 — Hook-event collisions
Two variants. Exact duplicate hook body registered by two owners: `{"duplicate_body": true,
"event", "configs"}` — a real defect (the command fires twice). Same event+matcher slot with
different bodies from different owners: `{"duplicate_body": false, "event", "matcher"}` —
still a finding (not `informational`), but its `note` says "usually intentional composition";
treat it as advisory, not a confirmed conflict.

## B9 — Hygiene facts
Evidence: `{"issue": "version unknown"}` or `{"issue": "installPath missing"}`. Currently only
these two plugin-level facts are implemented; the spec's broader hygiene surface (stale
backups, disabled-but-cached, empty dirs) is not yet walked — absence of a B9 finding does not
mean the machine has none of those.

## B10 — Config parse/schema validity
Evidence: `{"error": err}`, subject = the failing walker's name (`err.split(":")[0]`).
Only catches a walker-level exception (e.g. `json.loads` raising on malformed JSON/TOML).
Duplicate keys in one JSON/TOML object parse silently with Python's last-wins semantics and
raise nothing — the spec's "duplicate keys" case is a known false-negative, not detected yet.

## B11 — Dangling references
Evidence: `{"missing": exe, "config": h["source_path"]}`. Only checks a hook's command head
when it is an absolute path (`/…`) that doesn't exist on disk. MCP env-var-unset checks,
allowed-tools naming nonexistent tools, and marketplace-source reachability (all named in the
spec) are not implemented — a hook invoked via bare command name (PATH lookup) is never
checked either way.

## B12 — SKILL.md frontmatter validity
Evidence: `{"issues": [...]}`, drawn from `"missing description"`, `"description N chars
(>1024)"`, `"body N lines (>=500)"`. The spec's name↔dir mismatch check is not implemented —
only description presence/length and body line count are enforced here.

## B13 — Managed-block divergence
Evidence: `{"hashes": files}` — map of instruction-file name to its managed-block hash, per
scope. Only instruction files that actually contain a `<!-- khenrix-managed:begin/end -->`
block are compared; a target missing the block entirely is silently excluded rather than
flagged as divergent.

## B14 — `~/.claude.json` health
Evidence: `{"bytes": size, "stale_projects": stale[:10]}` (capped at 10 even if more exist).
The size trigger is a flat 1 MiB threshold, not an actual growth/trend comparison despite the
spec's "size growth" wording — a large-but-legitimate file (many active projects) can trip it.

## B15 — Dual-path reachability
Evidence: `{"paths": paths}`. Grouping key is `(cli, bare_name, vhash(description))` — it only
fires when name AND description hash match exactly, so two skills sharing a bare name but with
genuinely different descriptions are left to B1 instead of double-reported here.

## B16 — Vendored skill, source still enabled
Evidence: `{"vendored_from": src}`. Depends entirely on the vendored skill's frontmatter
carrying a `vendored_from` key that names a currently-enabled plugin. A skill vendored without
recording that provenance is invisible to B16 — this is exactly why remediation-ladder.md rung
3 makes recording `vendored_from` mandatory at vendor time, not optional.
