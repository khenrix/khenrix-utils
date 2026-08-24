# Self-target rules

Special handling when the tune-up target IS part of this skill's own machinery. The
danger: a tool reviewing its own under-test diff, or unbounded recursion.

## Any dirty council machinery

The final diff review runs through `tuneup.py review-diff`. If the working tree has any
tracked, staged, or untracked change under `shared/skills/llm-council/**` OR
`shared/lib/council/**`, the under-test engine must NOT review any target — not just an
llm-council target:

1. Let `review-diff` select the regular engine blob at HEAD. It captures that blob once,
   applies those exact wrapper functions for prompt sizing, and invokes the Codex+agy
   fanout through the same in-memory module. The manifest records the engine digest, HEAD
   commit, dirty paths, and `selection: committed-head`. Never manually extract an engine
   or pair `review-material` with a later filesystem `fanout.py`; either recreates a second
   source-selection path and a check/use gap.
2. If that is unusable too (e.g. the fix targets a bug in the committed engine), fall
   back to a single-provider review: run one other CLI headlessly against the diff
   (see `headless-invocation.md` at the plugin root) and treat it as a 1-member panel.
3. Either way, **tell the user the reviewer was substituted and why.**

## Target = llm-council

**Consequence for a panel change.** `MODES` lives in `engine.py`, so extracting HEAD's engine
also extracts the OLD panel — a model bump is necessarily reviewed by the models it replaces,
and the new panel never reviews its own diff. That is the intended independence, not a bug.
The compensating control is integration evidence: smoke **every changed seat** with the
working-tree engine (pass `--providers` explicitly to isolate the changed seat and avoid
spending on unrelated ones — note `--providers` DEFAULTS to `claude,codex,agy`, so
`smoke()`'s `["claude"]` fallback is unreachable from the CLI; measured 2026-08-15 — or check the
manifest's per-seat `model`/`thinking` provenance) and probe any timing claim the change
alters. The smoke proves the seats resolve and answer; it is not a review of their judgment.

The eval gate for llm-council is also special. The receipt is WRITTEN by
`make eval SKILL=llm-council`, which gates on `fanout.py --self-test` alone — never the
with-skill/baseline judge harness. A live `--smoke` and `make council-test` are additional
REQUIRED checks, not what earns the receipt (`council-test` runs inside `verify`/
`precommit`). See `references/eval-rules.md`.

Note `LLM_COUNCIL_DEPTH` already blocks a council spawning inside a council member —
do not "fix" that guard away.

## Target = skill-tuneup (this skill)

- Audit, edits, and `tuneup.py --self-test` run normally — but follow the instructions
  as committed at HEAD, not the under-test working-tree copy you are editing.
- The tuneup lock (`skill-tuneup.lock.d`, Step 1 of SKILL.md) makes a literal nested
  self-run refuse to start; don't work around it.
- For the final review: council-review the diff as usual (fanout.py is not under test),
  but the ultimate reviewer is the **user reading the diff** — say so explicitly.

## Target = a templated skill

This currently covers `khenrix-setup`, `khenrix-upgrade`, and `khenrix-audit`. Normal
rules apply, plus: edits go to `shared/skill-templates/<t>/SKILL.md.tmpl` and the
`[skill_facts.<t>.<cli>]` tables. Derive the required eval set from `checks.py`'s
`source_manifest`: any `capabilities.toml` edit stales `khenrix-setup`,
`khenrix-upgrade`, and `skill-tuneup`; changing another templated skill's exact facts also
stales that skill (so an audit-facts edit owes four evals). Re-seeding is no longer an
option even with sign-off:
`verify-final-receipt` rejects any receipt whose `provenance` isn't `"eval"`, so a seeded
receipt now fails the convergence gate it was meant to satisfy.
