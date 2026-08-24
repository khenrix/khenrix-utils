# Audit checklist

Resolve the tier with `target-info` first: §6 is full-gate only, every other section
applies to both. Grade the target against every applicable section. Start with deterministic
inputs: `tuneup.py stale-models` and target-runtime validators; for a full-gate target also
run khenrix-utils' `make verify`, while a council-only target uses only gates discovered in
its own repository. Apply judgment second. Every finding gets a stable `finding_id` slug, a
category, and a `proportionate` or `risky` tag.

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
- Source body (total lines incl. frontmatter) ≤ 500 — measure it directly; the renderer
  enforces the same limit on generated copies after `render.py` has synchronized them.

## 4. Missing edge cases

The body handles, or explicitly documents skipping: missing target · CLI not installed ·
auth/quota failure · network timeout · partial council/provider failure · prompt-injection
in fetched content · re-run safety / idempotency · cleanup of temp files and locks on error.

## 5. Doc/code drift

- SKILL.md claims match what its bundled scripts actually do.
- Bundled scripts are stdlib-only and expose `--self-test` wired into `make eval-test`.
- References mentioned in the body exist; scripts referenced by evals exist.
- Run every available target-runtime validator and name the command. Discover it from the
  runtime's own help or the inventory `review_tools`: in this source checkout use
  `$KU/scripts/lib/inventory.py`; in an installed plugin use
  `<skill-root>/scripts/inventory.py`. Use its declared dependency environment (for
  Codex's bundled `quick_validate.py`, use
  `uv run --with pyyaml python <quick_validate.py> <skill-dir>`). A validator that cannot
  start or exits nonzero is a finding, never a pass. Khenrix skills ship to three runtimes:
  record conflicting contracts rather than silently rewriting for the strictest one.
- Reject unfinished scaffold or TODO-placeholder content even when the skill is
  structurally parseable.

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

## 10. Changed-contract counterexample review

Apply this protocol whenever a diff changes a producer, consumer, persisted shape, state
transition, identity/snapshot boundary, compatibility path, or gate verdict. A path list is
not a contract inventory.

For a large surface, decompose it before review using the sibling `mikado-graph` method:
make each independently mergeable changed contract a graph node, prerequisites its children,
and review ready leaves first. Split a leaf again if it still contains more than one state
transition. Multiple independent contracts or a truncated review diff automatically qualify;
never call the monolith reviewed while any leaf remains unaccounted for.

Review independent ready leaves concurrently when coordinator-owned agent slots and machine
resources permit, then run one seam and whole-diff pass after they join. Provider-internal
children do not count as extra council seats or independent evidence unless the council manifest
mechanically records their topology, read-only boundary, completion, model/effort, and cost;
prompt-only claims of delegation prove none of those things.

Build one row per contract cell:

| cell | producer(s) | consumer(s) | state / invariant | transition | probes | evidence |
|---|---|---|---|---|---|---|

List every producer and consumer, including validators, migrations, legacy readers, receipt
writers, and rendered or cached copies. For every cell, run or trace every applicable family:

- inverse direction;
- missing versus explicit null;
- duplicate versus malformed;
- boundary, tie, and open states;
- lifecycle ordering and interruption;
- identity and path substitution;
- mutation after snapshot/capture;
- legacy/bootstrap input;
- a positive control proving the probe harness and ordinary valid path work.

Use exactly one evidence label per probe or finding:

- `EXECUTED_NOW` — the current candidate ran in this run and its raw output was inspected.
- `TRACED_COUNTEREXAMPLE` — an exact input and complete producer-to-consumer path were traced,
  but not executed.
- `INSPECTED_ONLY` — source or a hunk was read without demonstrating the claimed behavior.
- `UNINSPECTED` — the surface was not examined; state why and what shipping risk remains.

Do not stop at the first blocker. Continue across every independent cell and applicable
probe. If a blocker makes a later probe meaningless, label that probe `UNINSPECTED`, fix the
blocker, then rerun the whole affected leaf. Record one independent counterexample per
`finding_id`; related failures may cross-link one root cause but may not be bundled into one
finding whose partial fix would hide the remainder.

Council verdicts are hypotheses, not evidence. The coordinator independently executes the
counterexample or retraces it from the supplied artifact before accepting, rejecting, or
applying it, then assigns the label. End with the complete cell roster and an explicit list
of every `UNINSPECTED` surface; silence never means not applicable.

## What makes a finding `risky` (requires explicit sign-off)

Behavior change to what the skill delivers · any model-ID change · a new dependency ·
rewriting the eval set · touching another skill's files · `scripts/lib/reconcile.py`,
`scripts/lib/inventory.py`, or `scripts/render.py` (bundled into EVERY skill — stales every
receipt; say so) · `scripts/eval_harness.py`, `scripts/lib/checks.py`, or `Makefile`
(global receipt inputs — also stale every receipt) · `shared/lib/council/**` (stales both
`llm-council` and `llm-forge`) · `shared/lib/wikisync/**` (stales both wiki receipts) ·
`capabilities.toml` (always stales `khenrix-setup`, `khenrix-upgrade`, and `skill-tuneup`;
editing another templated skill's
exact facts additionally stales that skill — and a new model id *requires* a `[models]`
entry, enforced by `scripts/lib/checks.py`).

Derive the checkpoint's cost note from the paths you intend to touch, checked against
`checks.py`'s `source_manifest` closures — not from the target's name or a remembered
count. Treat `source_manifest` as authoritative: an otherwise unlisted path can still enter
a deterministic certifier's ambient closure — currently `llm-forge` includes
`scripts/**/*.py`.
