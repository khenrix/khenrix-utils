# Audit checklist

Grade the target against every section. Deterministic inputs first
(`tuneup.py stale-models`, `make verify`), judgment second. Every finding gets a stable
`finding_id` slug, a category, and a `proportionate` or `risky` tag.

Categories: `Bug` · `Inconsistency` · `Stale` · `Missing-edge-case` · `Eval-gap` ·
`Best-practice-update` · `Over-engineering`

## 1. Stale model IDs

- `tuneup.py stale-models --repo <root> --skill <t>` — only `stale-candidate` hits need
  review. For each: deliberate pin / demo value (leave + note) vs genuine drift (propose).
- Never replace a model ID with one you can't confirm exists (probe the CLI, check
  `capabilities.toml [models]`). Proposals only — the user decides.

## 2. Stale paths, flags, commands

- Every file path in SKILL.md/references still exists in the repo.
- Every CLI flag, make target, and script invocation referenced still exists and behaves
  as described (probe it).
- Code blocks in the body actually run as written.

## 3. Frontmatter drift

- `name` matches the directory, `^[a-z0-9-]{1,64}$`.
- `description` ≤1024 chars, states what it does + concrete "Use when"/trigger phrases,
  and does NOT poach a sibling skill's triggers (check the other shared skills).
- `allowed-tools` is as narrow as the workflow needs — no unused broad grants, no missing
  grant the body relies on.
- Body (total lines incl. frontmatter) < 500 — `render.py --check` enforces this.

## 4. Missing edge cases

The body handles, or explicitly documents skipping: missing target · CLI not installed ·
auth/quota failure · network timeout · partial council/provider failure · prompt-injection
in fetched content · re-run safety / idempotency · cleanup of temp files and locks on error.

## 5. Doc/code drift

- SKILL.md claims match what its bundled scripts actually do.
- Bundled scripts are stdlib-only and expose `--self-test` wired into `make eval-test`.
- References mentioned in the body exist; scripts referenced by evals exist.

## 6. Eval coverage

- `evals/<t>/evals.json` exists, 2-5 cases covering the happy path + key §4 edge cases.
- Assertions are discriminating (a no-skill baseline would structurally fail them) and
  objective — not tautological, not "is well written".
- `evals/<t>/receipt.json` is fresh (`tuneup.py triage` shows the state).

## 7. Over-engineering smells (reasons to REMOVE, never to add)

- A reference file or script with a single trivial caller that could be inlined.
- Configurability/flags with no demonstrated need.
- Defensive code for conditions that cannot occur.
- Abstraction wrapping a single concrete use; duplicated logic that exists in scripts/lib.

If a proposed fix would ADD any of the above, downgrade or drop it. Proportionality
beats completeness.

## What makes a finding `risky` (requires explicit sign-off)

Behavior change to what the skill delivers · any model-ID change · a new dependency ·
rewriting the eval set · touching another skill's files · anything touching
`scripts/lib/*` or `scripts/render.py` (stales EVERY skill's receipt — say so).
