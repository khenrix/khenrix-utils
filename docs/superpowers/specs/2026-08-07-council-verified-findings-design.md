# llm-council: verified findings, a structured payload, named limits — design addendum

**Date:** 2026-08-07  **Status:** design addendum (pre-plan)  **Repo:** khenrix-utils
**Parent:** `2026-08-02-skill-flowcharts-design.md` (Task 10 of its plan)

## What this is

A clean-room study of the **documented mechanics** of Claude Code's cloud review and
planning features, and which of them llm-council should absorb. It is not a port and
contains no recovered prompt text.

## Source policy (decided before any research)

The repo's standing practice is that the **licensed install is the source, and leaked or
reconstructed mirrors are out of bounds for any vendor**. Applied here:

| Source | Status |
|---|---|
| Official docs (`code.claude.com/docs/en/ultrareview`, `/ultraplan`), fetched 2026-08-02/05 | **Primary.** Documented behavior is the clean-room spec. |
| Observed behavior of the licensed local client (`claude ultrareview --help`, launch dialogs, our own runs) | **In bounds.** |
| Claude Code Unleashed (`ccu.galdoron.com`) | Behavioral concepts only; never quote its recovered prompt text. |
| `6missedcalls/ultraplan` | **Tainted for text** — its README states it derives from recovered internal prompts including Anthropic-employee-only sections. Citable as evidence such workflows exist; its rule text is never read into a design. |
| `xorespesp/claude-code` (reconstructed client source) | **Out of bounds entirely.** |
| `majiayu000/claude-skill-registry` ultraplan | Community-original recipe — prior art, explicitly not the extracted original. |

## The five mechanics worth absorbing

1. **Independent verification before reporting.** The documented core claim is that
   "every reported finding is independently reproduced and verified", which is what buys
   the higher signal. llm-council has no verification phase at all: a seat's finding goes
   straight into synthesis.
2. **A machine-readable findings payload** (`bugs.json`, surfaced by `--json`). Council
   output is prose, so no consumer can act on it mechanically.
3. **Named limits in refusals.** The cloud review refuses an oversized diff naming the
   limit in effect, the diff's size, and the files with the most changed lines.
   `review-material` fails closed but names nothing.
4. **A cost quote before launch.** forge's `--start` already does this; the council does not.
5. **Sectioned review output** — per-section verdicts rather than one blob, which is what
   makes targeted follow-up possible.

## Proposals

### P1 — `--verify-findings`: a second, refuting pass (the load-bearing one)

After the answer pass, each finding is re-put to a **different-family** seat with a
refute-first prompt ("try to refute this; default to refuted if uncertain"). Only
confirmed findings are reported; refuted ones are listed with their refutation.

Cross-family assignment is the point: a same-family verifier inherits the producer's
blind spots, which is the repo's own briefing rule for verifiers ("never the producer's
reasoning"). In-house prior art already exists and worked — a 5-reviewer + per-finding
confidence pass over these very plans on 2026-08-04 caught 12 real defects, and the
harness's blind A/B is a cousin of the same idea.

**Cost:** roughly doubles seat calls on a findings-shaped run. Therefore opt-in per run,
not the default, and quoted up front (P4).

### P2 — A structured findings contract

An optional `--findings-json <path>` writing
`{finding: {id, severity, file, line, claim, evidence, verdict, refutation}}`. This is
what would let forge's gate and skill-tuneup consume council verdicts mechanically
instead of re-reading prose. Prose synthesis stays the default and is unaffected.

### P3 — Named limits in `review-material` refusals

Today it exits 2 on a git error and says little. It should name the cap in effect, the
material's measured size, and the largest contributors — the same shape the cloud refusal
uses, and the difference between "too big" and "too big, here's what to cut".

### P4 — A pre-flight quote

Print seats × mode × estimated tokens before spending, mirroring forge's `--start`. With
Fable now on the claude seat in both modes and the weekly Fable sub-cap the binding budget
on this machine, an unquoted deep council is a real surprise.

### P5 — Per-category verdict contract, promoted from convention to documentation

Council review prompts already ask for a verdict per admissible category ad hoc (both
review rounds in this very plan series did). Write it down as the documented prompt shape
for review-type asks, so a clean category is stated *with its evidence* rather than as a
bare "nothing found".

## Explicitly NOT proposed

- **Ultraplan as a council seat.** It is interactive-only (slash command, keyword, or the
  plan-approval dialog), has no headless subcommand, requires a claude.ai account plus a
  GitHub repo, and disconnects Remote Control while active. Council seats are headless,
  parallel and unattended, so a seat cannot run it. The composition that *does* work is
  the reverse: draft/review a plan there, then have the council adversarially review the
  resulting artifact — which is exactly the workflow this plan series went through.
- **A remote fleet.** The council's value is *cross-family independence* on one machine;
  scaling agent count within one family is a different product.

## Risks

- **P1 doubles cost on the runs most likely to be deep already.** Opt-in and quoted.
- **A refuting verifier can be too aggressive**, killing true findings. Mitigate by
  reporting refuted findings with their refutation rather than dropping them silently, so
  the operator sees what was killed and why.
- **P2 invites over-structuring.** The prose synthesis is what humans read; JSON is for
  machine consumers, and if no consumer materializes, P2 should be dropped rather than
  maintained.

## Next step

Implementation belongs to a future **llm-council tuneup run**, whose gate is
`fanout.py --self-test` + a live `--smoke` + `make council-test` + the self-test-gated
receipt — never the judge harness. This addendum is that run's input. Recommended order:
P3 and P4 first (cheap, no cost change), then P1 behind its flag, then P2 only if a
consumer exists.
