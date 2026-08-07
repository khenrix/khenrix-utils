# Audit checklist

Resolve the tier with `target-info` first: §6 is full-gate only, every other section
applies to both. Grade the target against every applicable section. Deterministic inputs first
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

## 6. Eval coverage — FULL-GATE ONLY

Resolve the tier with `target-info` first. For a council-only target skip this section:
there is no khenrix eval set or receipt to grade, and `triage` refuses that repo. Grade it
instead on whatever gate the target repo itself provides, if any.

- `evals/<t>/evals.json` exists, 2-7 cases (prefer 2-5) covering the happy path + key
  §4 edge cases; a case above five earns its place by covering a contract nothing else does.
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

## 8. Chart drift — `docs/skill-charts/<target>.md`

- Does a chart exist for the target, and does its drawn flow match the CURRENT body —
  steps, gates, failure exits? Chart-vs-body drift is a **Stale-reference** finding like
  any other doc drift, and it is the one this repo's own defect history predicts: prose
  asserting behaviour the code does not have, in a form that reads more authoritative
  than prose.
- Is every drawn gate's evidence row still honest — `code` gates still backed by the named
  test, `agent` gates still covered by a live eval assertion or audit item? `make verify`
  resolves the references mechanically, so a *dangling* one is already caught; what the
  audit adds is whether a *resolvable* reference still describes the gate it is attached to.

## 9. Cross-CLI feedback loop

- Was every provider-specific finding this run probed on the other two CLIs, and does its
  run-log entry carry the one-sentence per-provider record?
  (`references/research-procedure.md` §Cross-CLI loop.)
- A fix applied to one provider's code path must say why the others need none — "not
  applicable" is a finding, silence is not.

## What makes a finding `risky` (requires explicit sign-off)

Behavior change to what the skill delivers · any model-ID change · a new dependency ·
rewriting the eval set · touching another skill's files · `scripts/lib/reconcile.py`,
`scripts/lib/inventory.py`, or `scripts/render.py` (bundled into EVERY skill — stales every
receipt; say so) · `capabilities.toml` (in BOTH templated skills' closures, so it owes a
`khenrix-setup` AND a `khenrix-upgrade` eval no matter which skill you are tuning — and a
new model id *requires* a `[models]` entry, enforced by `scripts/lib/checks.py`).

Derive the checkpoint's cost note from the paths you intend to touch, checked against
`checks.py`'s closures — not from the target's name. Other `scripts/lib/*` files and
`scripts/eval_harness.py` are in no closure and cost zero eval runs.
