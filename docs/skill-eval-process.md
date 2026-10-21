# Skill-eval process (provider-agnostic)

**Every change to a Khenrix-authored skill in this repo must be eval-tested and
blind-reviewed before it is committed.** The harness compares explicit skill guidance
with a native baseline, grades assertions, and records a blind A/B verdict per provider.

This repo ships a portable, stdlib-only harness (`scripts/eval_harness.py`) that delivers
that loop for the three provider CLIs: Claude, Codex, and agy. Claude's
`skill-creator` and Codex's native skill tooling are optional accelerators on top
(see below). Maka consumes the same installed skill bodies, but its invocation
check is a separate smoke test rather than a fourth harness provider.

The immutable `shared/superpowers/` snapshot has a different gate because Khenrix does
not author or independently tune those 15 bodies. Review and advance it as one bundle
with `skills:upstream-diff` and `skills:upstream-sync`; the provenance suite verifies
every selected path, byte, executable bit, member, license, and declared privacy overlay,
then selective-delivery tests and native routing smoke cover the local integration. Do
not create a local per-skill receipt for one vendored member.

## The loop

1. **Edit the skill.** For Khenrix-authored shared skills, edit
   `shared/skills/<name>/SKILL.md`. For the
   templated per-CLI skills, edit `shared/skill-templates/<skill>/SKILL.md.tmpl` (shared
   prose) and/or the `[skill_facts.<skill>.<cli>]` facts in `capabilities.toml`. Then
   `python3 scripts/render.py` so the rendered bodies the harness runs are current.
2. **Write/refresh evals.** `evals/<skill>/evals.json` — 2-7 cases (prefer 2-5; go higher only for a genuinely
   separate contract, never to pad), each with `prompt`,
   optional `files`, and `assertions`. Make assertions **discriminating**: things a
   no-skill baseline would structurally fail (that gap is the skill's measured value).
   Objective and answer-only — "runs the engine read-only first", not "is well written".
3. **Run with-skill vs baseline, per provider.** `make eval SKILL=<name>` (defaults to
   the `claude` executor; add `PROVIDERS=claude,codex,agy` for the full panel). The
   harness gives `with_skill` a private copy of the complete selected skill directory
   (SKILL.md plus references, scripts, and assets). It also inlines the bounded UTF-8
   textual closure with relative path labels. Scripts and opaque assets are named for
   closure transparency; an answer may claim it read or ran them only when actual tool
   evidence supports that claim. Binary text candidates and oversized closures fail.
   Each eval's declared fixtures are likewise bounded, decoded as UTF-8, and inlined as
   labeled untrusted data into both conditions and the judge prompt. Missing, unsafe,
   symlinked, duplicate, binary, non-UTF-8, or oversized fixture closures fail closed.
   Baseline receives the same fixture data but no skill closure.
   Both conditions use fresh working directories and normal installed skills, plugins,
   MCPs, HOME, configuration and authentication. `without_skill` has no explicit copy,
   but may discover installed guidance. The comparison measures explicit guidance under
   native configuration, not target absence or causal skill uplift. Credential values
   are not copied into workspaces or capture metadata.

   For a campaign whose task inputs and references are complete inline, the optional
   `--agy-answer-only` flag sets `cfg.agy.answer_only`. It disables AGY slash/skill
   expansion on initial and resumed commands and installs a private condition-local
   wildcard `PreToolUse` denial hook. Other providers and ordinary `run_text` callers
   retain native reads. AGY judges inherit this option through their provider config.
   The control is qualified only for AGY 1.4.2; another version requires a new bounded
   native canary. The observed canary proved `read_file` denial under native permission
   bypass, while the hook config covers all model tools. Each captured turn must join
   every attempted tool to its hook denial and native error; zero-call turns report no
   observed hook execution. Policy, hook config, handler, hashes, and native evidence
   remain in the capture. This is not OS confinement, and it is unsuitable for evals
   that need tools or disk-only inputs.
   A full campaign first captures every provider, case and condition. Only after the
   final executor returns does it publish derived `answer.md`, `grading.json`, or
   comparison outputs, or call a judge. The pair API `run_eval_for_provider` similarly
   waits for both conditions; the full runner uses `capture_eval_for_provider` for all
   pairs before calling `evaluate_captured_pair`. Judge retries evaluate retained
   answers and never replay executors. A nonempty iteration directory is rejected so
   retained historical answers cannot enter a fresh campaign's publication boundary.
   Raw private native captures and prompts still exist during execution and need a
   separate audit; this boundary does not provide OS confinement or prove the absence
   of peer reads. Provider failures remain executor errors, while judge retry failures
   retain their own classification and raw attempts and stop receipt issuance.
4. **Grade.** An LLM judge scores each output against the assertions → `grading.json`
   (`text`/`passed`/`evidence`).
5. **Blind A/B.** The two outputs are shuffled into A/B (with a hidden key) and the judge
   picks the better one blind → `comparison.json`, then de-anonymized.
6. **Iterate** until with_skill matches or beats baseline on the discriminating assertions
   (`run_summary.delta.pass_rate >= 0` — the commit gate; zero passes, negative fails).
   The blind A/B winner is **recorded but advisory** (not a gate): on a strong executor it
   rewards the tighter baseline over a correct-but-more-thorough skill answer, so a
   non-negative-delta run is never failed on a blind tie/loss. Use the recorded winner to
   triage a weak/zero delta.
7. **Only then** run `mise run verify`, `mise exec -- make eval-test`, the final
   three-provider eval, and `mise exec -- make precommit` before committing.

For a composed skill, start with `mise run skills:upstream-status` and inspect a
changed source with `mise run skills:upstream-diff -- SOURCE`. Review and accept
the commit, then use `mise run skills:upstream-record -- SOURCE
FULL_40_CHARACTER_COMMIT` before adapting the local reference and running the eval
loop. Recording a pin does not copy upstream instructions into the skill; it updates the
declared vendored license to the exact reviewed upstream bytes alongside the manifest and
notice. Changed license bytes require an explicit `--accept-license-change` after reviewing
the terms and attribution wording. It must precede eval because all three are inside the
receipt's source closure.

## CLI lifecycle and release measurements

An unchanged installed skill remains discoverable and usable after a compatible host
CLI update. A version newer than its last development measurement is informational;
it does not require a downgrade, instruction rewrite or paid reevaluation. A concrete
capability, protocol or loader failure still stops the affected operation. Recorded
fanout runs retain their profiles and inputs. Dispatch checks their recorded CLI
versions before intent and launch; executable drift requires a reviewed profile
transition or a fresh admission, not an implicit rewrite of the recorded run.

## Receipt policy: delivered behavior and shared measurement proof

When instructions, metadata, helpers or shared runtime change, identify every affected
skill using `scripts/lib/checks.py`'s delivered `source_manifest` closure. Run the relevant
deterministic tests, current target CLI discovery/metadata checks and bounded behavior
or pressure evaluations before publishing or claiming the revision verified. Include
Claude, Codex, agy and Maka where they provide the relevant capability; Maka remains
outside the three-provider harness. Report an unavailable target as unverified.
New V3 receipts store the evaluated per-file behavior, acceptance-criteria and measurement
instrument manifests separately. Behavior includes skill/template/facts and bundled
helpers, plus the fanout/memory runtime for fanout skills and the receipt/consumer
dependencies used by skill-tuneup. Criteria include the target's assertions, fixtures and
declared deterministic gate/tests. The instrument includes the harness, its entire eager
fanout import surface, renderer, acceptance checks, Makefile and test surface. Runtime
dependencies are not removed from a skill's behavior merely because the evaluator uses
them too. `legacy_source_manifest` preserves the old combined closure for diagnostics.

Capture all three snapshots before loading campaign criteria or the first paid turn,
verify the loaded criteria still match before launch, and compare them again after
measurement and any certifying suite, before receipt issuance. A changed source,
criterion or instrument retains raw results but earns no receipt. V3 records the original
snapshots, actual requested evaluator `judge_config`, measured CLI versions and hashes of real private raw files, including actual
deterministic-suite output. Unknown native usage/counts remain `null`; requested model
configuration is never served-model evidence. Passing assertions, invalid-run refusal,
judge failure handling, deterministic count requirements and final full-panel rules
are unchanged.

`evals/legacy-adoption-baseline.json` is an immutable, explicit adoption boundary from
commit `60ff42b5f0a566bcf27b4c1a8d94c02bdc7a1ecf`, not a recertification. Its per-active-skill
manifests were computed from that unchanged clean source, with hashes of the exact old
receipt bytes. Missing receipts are recorded as missing and cannot be adopted. Its file
digest and commit are pinned by the validator; there is no auto-rebaseline or membership
extension. Existing receipt files are not rewritten.

An old receipt can only produce a `HISTORICAL_UNVERIFIED` non-final warning when its
delivered behavior, criteria/fixtures and exact old bytes still match that entry, its
certification did not fail, and the shared current instrument proof passes. A behavior,
criterion, fixture or old-receipt edit requires fresh V3 evidence. Restamping an old hash
does not qualify. Historical adoption never passes `final=True`, and is never described
as earned, fresh or verified.

One current `evals/instrument-proof.json` owner record covers the shared measurement
instrument; instrument-only changes invalidate that proof without demanding unrelated
unchanged skill-body campaigns. The record is schema version 1 and binds the exact
`instrument_manifest` and `instrument_hash`. It requires nonempty `tests` entries with
portable argv `command`, actual positive `counts.tests_run`, zero `failed`/`skipped` and
`evidence_sha256`; valid sessions for each of `claude`, `codex` and `agy`, each recording
`session_id`, observed `cli_version`, `role: "evaluator"`, actual requested `judge_config`
(`model` and `thinking`: Claude/Codex `xhigh`, agy `high`), `raw_capture_sha256` and
`read_tool_evidence_sha256`; and `private_artifact_manifest_sha256` for the complete
private artifact manifest. This is an owner record of actual captures, not a signature
service, inferred tool-use claim or automatic proof issuer. An executor canary cannot
stand in for this evaluator proof. Root creates it only from
observed evidence after the final instrument freeze. No proof is bundled by this change.

Each real harness campaign observes the selected executors and judge with `--version`
before the first paid turn. It records the deduplicated, bounded UTF-8 single-line
observations as `metadata.cli_versions` in `benchmark.json` and `cli_versions` in a
successfully earned receipt. It observes the same CLIs again before receipt issuance
and retains `metadata.cli_versions_after`. A failed initial measurement stops before
spend; later failed measurement or version drift retains the benchmark and raw attempts,
returns failure, and writes no receipt or automatic replay. Retain the actual commands,
invocation identities, outcomes and raw private evidence with the evaluated revision.

CLI metadata describes the measured release; receipt readers do not compare it with
the user's current host version. Older and seeded receipts must never be restamped to
claim a newer binary was measured. Unchanged behavior/criteria remain valid across a
CLI-only update, while changed source or eval inputs require new evidence. The historical
development observation remains provenance, not a host-version equality gate.

Requested model names in a receipt record configuration, not the model actually served.
Missing positive served-model observations remain separate release-proof requirements;
version measurements, source tests and native discovery cannot satisfy them. Fresh
receipts for changed closures belong to the approved final-source release sequence,
alongside scoped native reads, independent Maka analysis and owner-application checks.

## Layout

```
evals/<skill>/evals.json          # committed: id / name / prompt / files / assertions
evals/<skill>/workspace/          # gitignored (evals/*/workspace/)
  iteration-N/
    eval-<id>-<name>/
      <provider>__with_skill/     {prompt.txt, answer.md, grading.json, <fanout artifacts>}
      <provider>__without_skill/  {prompt.txt, answer.md, grading.json, …}
      compare-<provider>/         # judge artifacts for that provider's blind A/B
      comparison.<provider>.json  # blind A/B verdict, de-anonymized, PER EXECUTOR
    benchmark.json                # metadata + runs[] + run_summary{…,by_provider} + blind_winner
```

The comparison file is per-provider because this directory is shared by all three: a
single `comparison.json` had each provider silently overwrite the previous one's verdict,
so a per-provider blind tally was not reconstructible from the artifacts at all.

The artifact schema matches `skill-creator`'s (`grading.json` / `benchmark.json` /
`comparison.json`), so the two interoperate — you can grade with one and aggregate with
the other.

## Commands

```bash
mise exec -- make eval-test
mise exec -- make eval SKILL=khenrix-quality
mise exec -- make eval SKILL=khenrix-quality PROVIDERS=claude,codex,agy MODE=deep
```

Executors use the shared native read profile at medium effort; grading and blind
comparison use evaluator xhigh. Gemini uses high for both roles. Both retain normal
installed read tools, skills, plugins, MCPs and authentication.

Known write tools are denied where the native CLI supports it. Native plan permissions
and the read-only prompt express the authorized task; a trusted shell-capable CLI is
not OS confinement. Evaluations authorize no repository/service writes, messages or
configuration changes. `--no-readonly` omits the prompt reminder, not these role controls.

Native transport retries are zero. A successful judge turn with malformed verdict JSON
gets at most one fresh semantic attempt, with both raw attempts retained. Timeouts,
provider failures and uncertain launches are terminal; they never trigger an automatic
whole-campaign replay.

After each turn, artifact screening checks stdout, stderr, answer, manifest and other
capture files for the selected canary, known credentials and credential shapes. Unsafe
paths or a match fail closed, redact captured files and leave a generic failure marker.
This checks disclosure in retained artifacts; it does not prove tool confinement.

Three-provider runs cost more than a single-provider iteration. Use the final configured
panel for the affected proof, with the existing spend allocation. Keep native discovery,
actual service reads, answer pressure and owner-application evidence separate.

**Invalid runs.** Each entry in `benchmark.json`'s `runs[]` carries `result.errors` (1 when
the executor timed out or died, **or the judge returned no verdict** — `result.reason`
distinguishes them) and `result.reason`. An invalid run is graded 0/N — on an empty answer
when the executor died, on a missing verdict when the judge did — and averaged into its own
side's mean, so it BIASES the delta — a `with_skill` error
sinks it, a `without_skill` error inflates it and would otherwise earn a receipt off a
baseline that never answered. The gate therefore fails closed whenever any run is invalid,
for every skill whose gate IS the delta; `DETERMINISTIC_GATED` skills
are exempt because their receipts are earned by a self-test / unit suite instead. The
run summary prints a `⚠ INVALID RUN` line per occurrence. If one recurs with nothing else in
flight, the eval is under-timed — raise the per-attempt cap with `make eval … TIMEOUT=<secs>`
rather than `MODE=deep`, which would also change reasoning depth.

## Per-provider measurement

After one fresh semantic retry, an exhausted malformed judge reply stops further
campaign turns. The partial benchmark retains completed runs/comparisons and the
current executor condition, with unusable grading marked invalid and both raw attempt
paths and reasons retained. Ordinary and mandatory quality/review verdicts still fail
closed, including deterministic-gated campaigns after an exhausted judge failure.
Their actual owning suite still must succeed with nonzero executed tests and no skips;
a partial judge result is not
the certification evidence.

`run_summary.by_provider` reports each executor's own `with_skill` / `without_skill` /
`delta`, plus `n_evals`, `quantum` and `status`; the receipt carries the same as
`per_provider`. Pooling across executors hid real regressions — `khenrix-upgrade` pooled
to `+0.0972` while claude sat at `-0.1250`.

**The pooled `delta.pass_rate` is still the gate.** The per-provider numbers are a
measurement, not yet a gate. Why: measured run-to-run drift on *unchanged* skill bodies is
0.06–0.08, and splitting by provider triples single-flip sensitivity, so a gate at
threshold 0 would fire on noise. Demonstrated live — the same unchanged `khenrix-upgrade`
gave `claude -0.125 / codex +0.125 / agy +0.292` one week and
`claude +0.25 / codex -0.042 / agy -0.083` the next. **When pooled and per-provider
disagree, the pooled number is authoritative for the gate and the per-provider numbers are
authoritative for diagnosis.**

`quantum` is the noise floor: `1 / (n_evals × smallest assertion count)`, floored at 0.05 —
the largest mean shift one assertion flip can produce. A per-provider delta smaller than
one quantum is a judge verdict, not a signal, and the summary annotates it as such. Eval
sets with 2 cases have a quantum near 0.15, nearly their whole measurable range; grow them
before reading their per-provider numbers as anything.

`status` is `invalid` when that **executor** failed. A **judge** failure invalidates the
whole run instead — the judge is a shared instrument (always `DEFAULT_JUDGE`), so blaming
one executor for it would be wrong. `runs[].result` carries `executor_error` and
`judge_error` separately for exactly this reason; `errors` remains the OR of the two.

## Trigger and routing evals (the other axis)

`eval_harness.py` injects a skill body and grades the output — it assumes the skill
already triggered. `scripts/eval_trigger.py` covers the complementary axis: given only a
skill's name and description, would the agent pick it?

```bash
mise exec -- make eval-trigger SKILL=khenrix-quality
mise exec -- make eval-arena SKILLS=khenrix-quality,khenrix-writing
```

`eval-trigger` reads `evals/<skill>/triggers.json`
(`{"should_trigger": [...], "near_miss": [...]}`) and scores correct fires plus correct
abstains. It reads the selected provider's rendered `SKILL.md` when one exists, preserving
provider-specific metadata for templated skills. A Khenrix-authored native-only skill intentionally excluded
from plugin bundles falls back to its canonical `shared/skills/<name>/SKILL.md`; raw
`SKILL.md.tmpl` files are never evaluated. Near-misses should be prompts belonging to an
*adjacent* skill:
`khenrix-setup` and `khenrix-upgrade` are deliberately **mirrors** — each one's
`should_trigger` cases are the other's `near_miss` set — so a description edit that blurs
the boundary fails on both sides rather than silently passing one.

`eval-arena` reads `evals/<first-skill>/arena.json` and asks which skill on a roster wins
each prompt. Neither is a receipt gate; run them when editing a skill's `description`,
which the behaviour harness cannot evaluate at all.

**Two live findings recorded 2026-08-07, both for a future tuneup rather than a quick fix:**

- `khenrix-upgrade` scores 0.909 (passes) but misses `"which model should this machine
  default to now?"` — a *question-shaped* prompt inside its stated "pick a newer/better
  model" territory. Its description may under-cover interrogative phrasing.
- The arena scores 0.667 on the committed roster because the judge answers
  `off-roster:khenrix-upgrade` and `off-roster:claude-obsidian:wiki-lint` where
  `arena.json` expects `none`. That is `parse_arena_verdict` behaving exactly as its
  docstring specifies (an off-roster name is a readable answer *and* a routing failure),
  and the judge is arguably being *more* informative than the contract permits — but it
  makes the committed arena set unpassable as authored. Either widen the roster or
  re-express those cases; do not "fix" it by rounding off-roster back to `none`, which is
  the exact collapse that docstring exists to prevent.

## Maka routing smoke

Maka does not run through `scripts/eval_harness.py` and has no native eval receipt
in this repository. After the selective skills are applied and their install
receipt is current, run:

```bash
mise run skills:maka-smoke
```

The smoke runner covers nine bounded routing cases: explicit and natural routes for the
four required skills, with two grouped natural quality cases covering every former skill
name. A non-bootstrap natural case explicitly loads `using-superpowers` in a first turn,
then continues the same session with the target request. It also runs a three-turn ADHD
flow that enables the mode, checks an action-first continuation in the same session, and
checks a bounded acknowledgement of the opt-out. The runner never passes `--yolo`, uses
fresh temporary working directories, caps steps and time, and verifies Maka's root-Turn
admission and `skill_loaded` records. A pass
writes `~/.local/state/khenrix-utils/skills/maka-smoke-receipt.json`, bound to the
case manifest, installed skill hashes, install plan, and Maka version.

This proves that Maka found and loaded the intended skill and checks two narrow
session behaviors. It does not replace the Claude/Codex/agy behavior, trigger,
and arena evals.

## Per-provider tooling (accelerators, not the gate)

- **Claude** — `skill-creator` (installed via `claude-plugins-official`) has the richest
  loop: grader/comparator/analyzer subagents, an eval viewer, benchmark variance, and
  trigger-description optimization. Prefer it when authoring on Claude. We **reference** it;
  we do not vendor it. Its artifact schema is the one this harness emits.
- **Codex** — `~/.codex/skills/.system/skill-creator/` scaffolds + validates
  (`quick_validate.py`), but has **no** structured evals/blind-review. Use it for
  scaffolding/validation; use this harness for the eval loop.
- **agy** — no native skill tooling. The harness is the only eval path; `agy plugin
  validate` covers manifest validation.

## Native fanout skills

Planner and executor answer-pressure runs complement their deterministic contract suites.
They cannot alone prove native skill discovery, authorized service reads, durable peer
exchange or owner-only application. Retain exact native session/tool evidence and every
configured final identity for those behaviors. Missing or invalid final seats block owner
reconciliation; identical answers from separate identities remain valid.

## Deterministically-gated skills (`DETERMINISTIC_GATED`)

Some skills the judge harness cannot fairly gate route their receipt through a real test
suite instead. `eval_harness.DETERMINISTIC_GATED` maps the skill to the command, and
`DETERMINISTIC_GATE_NAMES` maps it to the NAME the receipt records — two tables rather than
one, because a single hardcoded gate name became a false provenance string the moment a
third skill was routed through the dict, on the one artifact whose whole job is to say what
ran. A skill in the first table and not the second raises a `KeyError` rather than writing a
receipt that cannot name its gate; `make eval-test` catches that before a paid run does.

| Skill | Why the delta cannot gate it | Gate |
|---|---|---|
| `khenrix-wiki-add` / `khenrix-wiki-sync` | a read-only answer-quality run cannot exercise the stateful engine's commit, lock, ledger, and render behavior | the wikisync unit suite |
| `llm-fanout-plan` | answer pressure alone cannot establish admission, skill resolution or source dependency mapping | named planner/compiler/source/skill contract suites |
| `llm-fanout-execute` | answer pressure alone cannot establish durable recovery or owner-only application | named executor/recovery/repository/verification suites |

**The judge run still executes.** `gate_ok = True` is applied *after* it in `run()`, so
routing makes the delta **advisory, not free** — the cost control is a cheap eval set. Read
the recorded delta when triaging; do not treat it as the evidence in the receipt. The
evidence is the suite named in `deterministic_gate`, which `_write_receipt` runs and refuses
to write on.

## Maintenance runs (skill-tuneup)

The `skill-tuneup` skill automates this loop for periodic maintenance of an existing
skill: it researches upstream drift since the target's last substantive commit, audits,
applies user-approved fixes, scaffolds a missing eval set per this doc, and iterates
`make eval` to a fresh receipt before committing. Its per-target decisions live in
`docs/tuneups/log/`.


## Khenrix quality patch and grading acceptance

The ten core quality cases remain the full panel. Focused factory and authorized-auth
holdouts are separate in `evals/khenrix-quality/fixtures/holdouts.json`. Semantic judge
calibration pairs in `fixtures/calibration.json` include equivalent benchmark mechanics
and actual omissions, prior authorization versus unnecessary reapproval, and proven
unused factories versus legitimate public extension contracts. Grade each supplied
answer against its supplied task/assertions and compare the boolean list to
`expected_passed`; these are semantic controls, not phrase-matching tests. Both files
are bound through the criteria fixture tree. Loading them performs no model calls:

```bash
mise exec -- python3 scripts/eval_patch_acceptance.py --calibration
mise exec -- python3 scripts/eval_patch_acceptance.py --holdouts
```

Run executable acceptance on a retained raw shared-helper answer, without editing it:

```bash
mise exec -- python3 scripts/eval_patch_acceptance.py /private/evidence/answer.md
```

The root-usable API is `eval_patch_acceptance.check_answer(answer, fixture=FIXTURE)`.
It extracts exactly one fenced diff without changing hunk counts or repairing content,
rejects extra/escaping paths and ambiguous diffs, and requires the exact test command
and answer-only disclosure requested by this case. It copies the clean fixture, checks
and applies the same patch with strict `git apply --check` and `git apply`, preserves
original test statements in order while allowing added assertions and extended setup,
runs the complete stdlib unittest suite and replays the untouched original suite against
patched production, then restores the original implementation with the expanded tests
to prove RED. It restores the patched
implementation and independently checks Unicode, trimming, invalid inputs and retained
lookup/order contracts at the helper and both public callers. JSON output retains
commands, return codes and output for original, apply, retained-original, RED, GREEN
and contract phases;
exit 0 means accepted. A failed patch is rejected as emitted, never repaired for scoring.
The semantic grader still checks the requested test coverage and smallest shared-owner
fix; executable acceptance does not establish every qualitative criterion.

Subprocesses use a minimal environment and a 20-second per-command timeout in disposable
copied directories. Candidate Python executes locally: these controls are not an OS
sandbox, and acceptance does not authorize unreviewed code, network operations, service
writes or messages. Historical answer/grade artifacts and the original eight-case
151/162 result remain immutable; corrected future criteria do not retroactively change
those scores.
