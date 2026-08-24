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

- **converged** — the newest cycle applied nothing `blocking` or `serious`. Applied
  `minor` findings do not block when the review examined them; a new minor finding from
  that review is deferred, because fixing it afterward would start another cycle.
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

`run-convergence` is terminal bookkeeping, not a candidate gate. On a converged run, append
it only after the final receipt/native gate and the applicable precommit are clean; until
then an in-scope gate failure can still return to Step 8 and earn a reviewed cycle. After
the terminal append, only mechanical log/convergence and staged-diff checks remain. If one
fails, stop for handover rather than adding findings to or re-entering the closed run. A
stalled run records its deferred terminal immediately before handover because it will not
ship.

The number is not decoration. Without it a duplicate marker is indistinguishable from a
legitimate zero-finding cycle — and a zero-finding cycle IS convergence, so any check
strict enough to catch the duplicate would also make converging impossible.

`convergence-status` additionally refuses to converge while APPLIED findings sit after the
last `cycle-end` (a `deferred` or `rejected` tail does not open a cycle): an in-flight cycle
is not a clean one. A log it cannot parse cleanly blocks convergence too — see its warnings.

## Resolving an ambiguous pre-start gap without erasing reviewed cycles

The ambiguity warning is computed from history BEFORE the newest `run-start`. An ordinary
append cannot change that prefix, while appending another `run-start` changes the active
scope and drops this run's completed cycles. Therefore neither is a repair. Inspect every
record in `run_gap.applied_records` first. Its `gap_index` is the zero-based position in the
exact fingerprinted `between` array, not its position among applied findings.
Closing another run does not erase an unresolved gap: the engine carries its original
boundary and exact ambiguous record set through later run-start/run-convergence pairs until
one marker accounts it. Already-counted findings from intervening runs are not added.
Unclosed applied occurrences from those runs are queued behind the immutable gap instead;
resolving the head immediately exposes that sparse debt as the next exact gap, so each
occurrence is either cycle-counted or explicitly accounted and never both or neither.

Partition **every emitted applied index exactly once** into prior history or current-run
work. Re-record each current ordinary occurrence after the active `run-start`, preserving
target, finding ID, and effective severity (`minor` stays minor; every other value is
serious here). Reserved lifecycle IDs cannot safely be replayed as themselves. For each
current lifecycle occurrence, append the exact ordinary serious surrogate printed by the
CLI, add its non-empty reason, then append the v3 `run-gap-resolution`:

```json
{
  "target": "<log_target>",
  "finding_id": "run-gap-resolution",
  "decision": "applied",
  "schema_version": 3,
  "run_start_ts": "<exact emitted value>",
  "gap_sha256": "<exact emitted value>",
  "gap_records": 0,
  "applied_findings": 0,
  "resolution": "pre-start-gap-accounted",
  "prior_gap_indices": [0, 3],
  "current_gap_indices": [1, 4],
  "title": "resolved pre-start gap without changing current run scope",
  "reason": "<why every emitted applied index is prior or current>"
}
```

Replace both zeroes with the emitted counts. `title` and `reason` must be non-empty;
`severity` is forbidden because this is a structural adjudication, not an applied finding.
Both lists contain plain integers (not booleans), strictly increasing and without
duplicates. They must be disjoint and their union must equal every `gap_index` in
`applied_records`; an all-prior gap therefore puts **all** emitted indices in
`prior_gap_indices`, not in an implicit remainder. Duplicate IDs are occurrences, not a
set: two current occurrences require two matching re-records before the marker. A later
re-record cannot validate an earlier marker.

The emitted lifecycle surrogate is bound to the gap SHA, index, active target, original
target, and original finding ID. It is an ordinary applied serious finding, so existing
cycle/tail/rollover accounting carries it. If another `run-start` occurs before a valid
cycle counts it, a later gap must classify and replay it as current; it can never be
relabelled prior. This prevents a malformed delimiter or stray resolution from becoming a
clean cycle merely because the run was interrupted again.

The fingerprint is SHA-256 over UTF-8 canonical JSON (`sort_keys=True`, compact separators,
`ensure_ascii=False`) of `{"after": <trusted accounting boundary or null>, "between":
<original unresolved interval>, "start": <this run-start>}`. The boundary may be a terminal,
valid cycle-end, or sparse-debt envelope anchor. On rollover the old interval
stays fixed while `start` becomes the new active anchor. It binds the exact ordered old
record content without relabeling already-counted later-run findings as ambiguous. A queued
debt batch uses one continuous fingerprinted envelope plus sparse `applied_records`; records
inside that envelope which a valid intervening cycle already counted are excluded.

Both `log append` and the reader apply the same checks: target, decision, resolution value,
fingerprint, run-start timestamp, counts, audit text, index schema, and occurrence coverage
must match. The marker must occur after the anchored `run-start` and before its
`run-convergence`, and exactly one valid candidate may exist. Only that candidate is
structural. Any other applied `run-gap-resolution` is forced serious inside the run, even
if hand-edited with `"severity":"minor"`; after the terminal marker it is stray work. Thus
malformed, wrong-target, duplicate, no-gap, and post-terminal records cannot fail open.
New log writes require a non-empty string timestamp. An exact historical anchor with an
older invalid timestamp remains resolvable because the fingerprint binds that value too.

A valid structural marker also becomes an accounted boundary if an interrupted run starts
again without writing `run-convergence`: the resolved historical gap does not reappear
merely because the terminal marker is absent. This is not a synthetic terminal. Applied
work after the resolution forms a fresh exact gap; so does an unclosed applied tail before
the marker, while a tail already closed by `cycle-end` is not counted twice.

`convergence-status` emits a recipe only while `run_gap.resolution_open` is true. Once the
run is terminal it reports that the window is closed; history must not be waived after the
fact. Existing v1 all-prior and v2 `re_recorded_gap_indices` markers remain reader-compatible
only when the gap contains no schema-v3 lifecycle-surrogate debt; the writer emits and
accepts only exhaustive v3. A valid marker stays outside cycle, tail, and stray counts, so
the already-reviewed cycle series remains intact.

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
