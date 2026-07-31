---
name: khenrix-audit
description: >-
  Cross-CLI setup conflict finder: maps every installed plugin, skill, MCP server, hook and instruction file across Claude Code, Codex and agy, then finds duplicate or overlapping skills (which skill fires for a prompt), shadowed or endpoint-deduped MCP servers, conflicting hooks, and checks whether the skill-listing context budget is over its 1% window (descriptions being dropped). FINDS (never applies) drift between the declared capabilities.toml and each CLI's live config — khenrix-setup applies, khenrix-audit detects — with a guided per-finding apply and a synced decision ledger. Use when the user asks which skill fires, why the wrong skill fired, or whether installed skills/plugins/MCP servers overlap. NOT for one skill's content (use skill-tuneup), NOT for the wiki (use wiki-lint). Invoke as /khenrix-audit.
allowed-tools: Bash, Read, Grep
---

# khenrix-audit — cross-CLI setup audit (Claude Code)

One run: deterministic engine → model adjudication → evidence → report → guided
apply. The engine is read-only except its ledger subcommands. Full check
catalog: `references/checks.md`. Spec: docs/superpowers/specs/2026-07-30-khenrix-audit-design.md.

All phases available: this CLI can run trigger probes (Phase D tier 2) and produce token counts via `claude plugin details`.

## 1. Run the engine

Locate the engine and the canonical checkout (repo writes NEVER target rendered
copies — resolve ~/git/khenrix-utils and pass it explicitly):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/khenrix-audit/scripts/setup_audit.py" findings --repo-root "$HOME/git/khenrix-utils" --out /tmp/audit-findings.json --report-dir "$HOME/git/khenrix-utils/docs/setup-audit"
```

If the claude binary is on PATH (`capabilities.can_token_count`), FIRST produce
token counts so B7 runs — otherwise skip; B7 then reports NOT EVALUATED, which
is correct:

```bash
claude plugin list
```

For each installed plugin run `claude plugin details <name>`, collect the
"Always-on" numbers into `/tmp/audit-tokens.json` as `{"<plugin>": <tokens>}`,
then re-run the engine with `--tokens-file /tmp/audit-tokens.json`.

## 2. Read findings.json — branch on `capabilities`, not CLI name

- `errors` non-empty → report them first (a walker crash is a finding, not noise).
- Findings marked `informational` (semantics unverified) NEVER reach the apply loop.

## 3. Adjudicate B6 nominations (Phase C)

For each B6 finding, read ONLY the two trigger surfaces + frontmatter from the
evidence, classify DISTINCT / AMBIGUOUS / DUPLICATE with one sentence of
reasoning. Full-body reads: at most 3 pairs per run. Do not act on a
nomination without adjudication.

## 4. Evidence for AMBIGUOUS pairs (Phase D)

Tier 1 (any CLI): arena eval — see `references/probe-protocol.md` §Arena.
Tier 2 (`capabilities.can_probe` only): live probes — §Live-probes. k ≥ 3 per
prompt, report fire-rates, INCONCLUSIVE when unstable.

## 5. Ecosystem discovery (Phase E)

Follow `references/ecosystem-evidence.md`. Serve from
docs/setup-audit/ecosystem-cache.json when fresh (30-day TTL); refresh on
request. Every claim needs a citation; no citation → no finding. Web failure →
report DISCOVERY INCOMPLETE.

## 6. Report (Phase F)

The engine already wrote latest.md + runs/. Add model annotations (adjudications,
probe results, ecosystem findings) BELOW the mechanical findings — never delete
or renumber them.

## 7. Guided apply (Phase G)

Walk non-informational findings by severity. Per finding offer:
apply / not now / waive (reason + until) / accept current state / reject / detail.
Rules, all mandatory — see `references/remediation-ladder.md` for the rung table:

- Every action binds to the inventory hash in findings.json; if config changed
  since, re-run the engine first.
- Destructive ops (any removal/disable): confirm individually, write a restore
  bundle (`*.khenrix-backup` convention) BEFORE, record in the ledger AFTER.
- Repo edits: only in the canonical checkout, description-only changes eval-gated
  via arena (`references/remediation-ladder.md` §Rung-1 gate); body changes hand
  off to skill-tuneup.
- Waive → record via the engine, never by hand:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/khenrix-audit/scripts/setup_audit.py" ledger-add --repo-root "$HOME/git/khenrix-utils" --id <finding-id> --state waived --fingerprint <fp> --reason "<why>" --until <ISO8601>
```

- After any live mutation, re-run the engine and mark superseded queue items
  stale — do not keep applying from the old snapshot.
