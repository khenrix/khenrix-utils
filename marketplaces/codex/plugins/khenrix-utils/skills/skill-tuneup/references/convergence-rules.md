# Convergence — why the rules are shaped this way

Step 10 states the rules; this is the reasoning behind them.

`convergence-status` enforces only the MECHANICAL subset: cycle markers, applied-finding
severity counts, an open APPLIED-finding tail, a warning-bearing (ambiguous) log, and stall
detection. Where it speaks, it is right and this page is wrong. But it never compares
finding ids to each other and has no category or causality input, so the freeze, the
cycle-2 admissibility bar and the causality rule below are OPERATOR-enforced — nothing
will stop you breaking them.

## Why severity and not a cycle count

A count-based cap stops at an arbitrary number. What you actually want to know is whether
anything worth finding is left, and a zero-finding cycle answers that directly — it is
positive evidence, which a counter never gave you.

- **converged** — the newest cycle applied nothing `blocking` or `serious`. `minor`
  findings are logged `deferred` and do NOT block; fixing them would just start another
  cycle on a candidate the reviews never examined.
- **stalled** — the BEST (lowest) serious-count has not improved for two cycles. Stop and
  hand over: the loop is not approaching zero, so the next cycle buys another defect rather
  than convergence. Observed 2026-07-26: three consecutive cycles each found a P0 *in the
  previous cycle's own fixes*.
- **keep-iterating** — otherwise.

Improvement-of-best, not merely "did not increase", is what makes this a real termination
guarantee: the minimum is a non-negative integer that must strictly fall to keep the loop
alive, so an oscillation like `2,1,2,1,…` halts instead of running forever.

## Why severity is assigned at RECORD time

Before you know whether fixing it ends the run. Relabelling a defect `minor` to stop
iterating is the failure this ordering prevents — if you are tempted, that is the signal to
hand over instead. An applied finding with no `severity` counts as **serious**, so
forgetting the tag can never end a run early.

## Why two markers, not one

`run-convergence` is the run's OUTCOME, written once per run — counting cycles on it
silently measures runs instead. So:

- `run-start` once at Step 1. `convergence-status` scopes to the newest one, so a fresh run
  cannot inherit a previous run's stall state.
- `cycle-end` after EACH cycle's council review, carrying a REQUIRED monotonic `cycle`
  number.

The number is not decoration. Without it a duplicate marker is indistinguishable from a
legitimate zero-finding cycle — and a zero-finding cycle IS convergence, so any check
strict enough to catch the duplicate would also make converging impossible.

`convergence-status` additionally refuses to converge while APPLIED findings sit after the
last `cycle-end` (a `deferred` or `rejected` tail does not open a cycle): an in-flight cycle
is not a clean one. A log it cannot parse cleanly blocks convergence too — see its warnings.

## Why decisions freeze

A decided `finding_id` may not be re-opened or reversed by a later cycle; reversal urges
become disagreement notes for the commit message. The freeze is not what guarantees
termination — the STALL rule is. What the freeze prevents is relitigation and
apply→revert oscillation, which burn deep-mode fan-outs on settled questions.

A regression of an applied fix, or genuinely new evidence, is a NEW finding id that
references the old one. Those are always admissible, and in practice they are where the
real defects have come from.

## Why cycles ≥2 raise the bar

New findings from any defect category (Bug / Inconsistency / Stale / Missing-edge-case /
Eval-gap / Over-engineering) — but no Best-practice-update or polish. A clean pass stated
plainly beats a manufactured caveat; never invent findings to keep the loop alive.

## Why out-of-scope findings are judged by causality

Not by which file they live in. A confirmed defect the candidate did not cause is logged
`deferred`-with-trigger and handed over; it never blocks convergence. But one the candidate
**activates** — a latent gap that goes live only because you shipped — is a ship-gate item:
fix it in its own commit or get explicit sign-off first. Either way the candidate stays
byte-identical, so this never re-opens the cycle.
