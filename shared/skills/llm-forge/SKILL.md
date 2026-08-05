---
name: llm-forge
description: Run ONE build task across all three agentic CLIs on this machine (Claude Code, Codex, agy) in isolated clones, verify each candidate in a fresh verifier clone it never had access to, then FUSE the results into a new best-of-all answer — not pick a winner. Use when the user wants a hard change built several ways and merged into the best version, maximum confidence on risky implementation work, "forge this", "llm-forge", "build this three ways", "have all three CLIs implement it and combine them", "fuse the results", or asks to collect/clean up a previous forge run. Also trigger on "--start", "--collect" or "--gc" against a forge run id. Expensive and slow — a default run is ~19 provider calls across 19 coexisting clones and peaks near 63.3 GB of disk, so it is for changes that justify the spend; `--start` prints the full quote before anything is spent, and `--gc <run-id>` afterwards is mandatory, not tidy. NOT for a question or a second opinion — that is llm-council, which is read-only and ~3x one turn.
allowed-tools: Bash, Read
---

# llm-forge — build it three ways, then fuse

One task. Three agentic CLIs — `claude`, `codex`, `agy` — each building it in its own
clone of the repository: a separate checkout of the same baseline, with no git remote and no
branch of another seat's work. **They are not isolated from each other by the operating
system** — the clones are sibling directories under one run root, owned by your user, and the
seats run one after another, so a seat that goes looking can read the others'. Treat the
diversity as a property of three different models answering the same prompt, not as an
enforced boundary. Every candidate is
then verified in a **fresh verifier clone the builder never touched**, and you, the
orchestrator, **fuse** them.

**Fusion, not selection.** The deliverable is a new answer assembled from the best of all
three — one seat's structure with another's edge-case handling and a third's test — not
the strongest candidate promoted as-is. The engine deliberately has no `--synthesize`: it
stops with the candidates verified on disk, hands you a synthesis worktree, and waits.
Picking a winner is the thing this skill exists not to do.

**And that is an instruction to you, not a property the collector checks.** `--collect`
refuses a synthesis tree identical to B1 and nothing more: it never compares your tree
against the candidates, never requires claims from more than one, and `--strategy` is
recorded as you typed it — the CLI says outright that it cannot be verified. So a wholesale
copy of one candidate plus a whitespace change passes, and `--strategy from_scratch` is
accepted over winner promotion. The guarantee is yours to keep; the engine can only refuse
the one case where you kept nothing at all.

> **Cost.** A default run (3 seats × 3 attempts, 2 review rounds, cloud ultrareview on) is
> **19 provider calls worst case**, 18 setup runs, 9 verify runs, and a peak of **~63.3 GB
> under `XDG_STATE_HOME` across 19 coexisting clones** — 1 calibration + 9 builder + 7
> verifier + 2 review — none of which is reclaimed until you run `--gc`. The cloud ultrareview is
> priced separately in **usage credits ($5–25, or one of three one-time free runs)**.
>
> The gate also prints a **wall-clock upper bound** for the builders — `seats × attempts ×`
> the §19 window, ~9 h on a default run, because the seats run one after another and each
> attempt gets the whole window. It is a bound, not an estimate, and it excludes setup, verify,
> review and the cloud review, so the real ceiling is above it.
>
> **That quote is an UPPER BOUND, and about half of it cannot be spent today.** §13's review
> rounds and their post-round fixes — 9 of the 19 calls — are priced for a stage that has no
> production caller: `review.run_round` and `review.loop` are built and tested and nothing
> convenes them, so `--review-rounds N` parses, is recorded as a budget, and buys nothing. A
> real default run spends about **10** provider calls: 9 builders and the one synthesis turn
> you make yourself. The quote is not corrected here because repricing changes what `--start`
> asks you to agree to, and that is its own change; what the packaging suite guarantees is
> that no NEW unspendable term can be added without a test failing.
> `--start` prints that whole quote, plus every refusal and gap, **before a token is
> spent**, and will not open a run until the answer sheet agrees with what was priced.
>
> Use `llm-council` instead for anything read-only — a second opinion, a review, a
> judgement call. Forge is for building.

## 1. Locate the engine

The skill body is identical in all three plugins, but each CLI exposes its plugin root
differently. Run this first to set `$FORGE`:

```bash
FORGE=""
for c in \
  "${CLAUDE_PLUGIN_ROOT:-}/skills/llm-forge/scripts/forge.py" \
  "${PLUGIN_ROOT:-}/skills/llm-forge/scripts/forge.py" \
  "$HOME/.gemini/config/plugins/khenrix-utils/skills/llm-forge/scripts/forge.py"; do
  [ -f "$c" ] && FORGE="$c" && break
done
[ -z "$FORGE" ] && echo "forge.py not found — is khenrix-utils installed?" && exit 1
echo "engine: $FORGE"
```

No `PYTHONPATH` export is needed: `forge.py` resolves the bundled engine (and the council
engine it imports) relative to its own location, in the repo tree and in an installed
plugin alike. Everything below is `python3 "$FORGE" …`.

## 2. `--start` — the gate, the fleet, the verification

```bash
python3 "$FORGE" --start \
  --repo /path/to/repo \
  --task /path/to/task-bundle \        # a DIRECTORY, not a string
  --entrypoint TASK.md \               # the bundle entry the seats are told to read
  --answers /path/to/answers.json \
  --select scratch/notes.md \          # repeatable; untracked paths to carry into B1
  --seats 3 --attempts 3 --review-rounds 2
```

`--start` runs the static preflight, prints the quote, opens the run, builds the baseline
**B1**, launches the fleet, verifies each candidate — and **stops at `comparing`**. It
prints the run id, the synthesis worktree path, and a seat table.

It also writes a **fusion brief** for you and prints its absolute path: each seat's verify
outcome, the paths each seat changed, the pairwise path-overlap matrix, and the paths exactly
one seat touched. It is membership, not rank — nothing in it says which seat is strongest. A
seat whose path set was not recorded is named as such and is excluded from the overlap counts
and from the sole-toucher list, because "only this seat touched that file" is a claim about
every seat.

It is written **beside** the synthesis worktree rather than inside it, in the worktree's own
git directory. A file inside the tree would be an untracked artifact, and the handover reads
untracked artifacts as things you have to copy out by hand — so scaffolding in the worktree
would downgrade every delivery to a patch and hand you a `cp` command for the engine's own
notes. Open it at the path `--start` prints; `--gc` removes it with the worktree.

## 3. `--ledger` — hand the engine the claim ledger you wrote

```bash
python3 "$FORGE" --ledger <run-id> --repo /path/to/repo --ledger-file /path/to/ledger.json
```

The ledger is **yours**, not the engine's: writing it means reading all three artifact sets,
which is the fusion work. The engine validates it and stores it — every refusal you see is a
row that would otherwise be ranked on or asserted blind against.

Do this **before** anything that reviews or ranks. A run with no ledger cannot be reviewed at
all — the blindness assertion refuses a check whose evidence is missing.

Storing it is not trusting it. This engine validates the ledger's structure and **reads nothing
back off it**: the handover reports that no strongest seat can be named, and that is
**permanent, not pending** — §12.5's rubric ranks seats on four dimensions including a
per-seat review risk, and nothing in this design produces one: `--review`'s panel reads your
*synthesis*, after fusion, never the candidate seats. A rank taken off a ledger you wrote,
over a rubric missing its third dimension by construction, would be a verdict with nothing
behind it. The "no strongest seat" line is the rubric's honest output, not a stage to wait
for.


### The answer sheet

A JSON object. Every command is a **list of argv lists** — nothing here runs a shell, so
`"make verify"` is `[["make","verify"]]` and a `cd x && y` is two steps.

```json
{
  "setup":  [["make", "install"]],
  "verify": [["make", "verify"]],
  "on_calibration_failure": "abort",
  "strategy": "size-gated",
  "author": ["Your Name", "you@example.com"],
  "accepted_gaps": []
}
```

- `on_calibration_failure` is `abort` or `degraded`; `strategy` is `size-gated`, `fusion`
  or `base-and-port`. Neither is defaulted — the gate is asked once and a value chosen for
  you would be your decision recorded as the operator's.
- `accepted_gaps` may be omitted, and its silence reads as **accepted none**. Legal ids are
  `gate-surface-empty`, `remotes-and-configuration-unrecorded`, `generator-contract-empty`.
- **Do not put `ultrareview` in the sheet.** It is decided by `--no-ultra`, which also
  **re-prices the run**; two spellings of one decision are free to disagree, and the
  disagreement is money. The sheet is refused outright if it carries the key.

### `--no-ultra` is a repricing, not a skip

`--no-ultra` belongs on `--start`. It does not merely turn the cloud review off — it moves
the quote's **provider calls, setup runs, verify runs and peak disk**, because the review's
findings would have earned a post-round fix plus a fresh verifier setup and verify — measured
on a default run, 19 calls / 18 setup / 9 verify / ~63.3 GB become 18 / 17 / 8 / ~60.0. Note
that those are movements in the UPPER BOUND: the post-review fix it subtracts is one of the
calls the cost note above says cannot be spent today, so what `--no-ultra` actually saves
right now is the **$5–25 in usage credits**, not a provider call. The
gate then refuses if the quote and the answer
disagree at all: the quote you were shown is the one the run may spend, and the fix is to
re-price and show it again, never to answer past it.

The decision is journalled on the run. `--collect` reads it **back off disk** and fails
closed if it is missing or is not a boolean — it never re-derives it from a flag, so passing
`--no-ultra` at `--collect` changes nothing.

### What the preflight refuses, and what it does not screen

The credential screen covers **the paths you `--select` and nothing else**. It is not
"forge scans your repository for secrets": tracked content — including an uncommitted edit
to a tracked file — is not screened. A selected path that escapes the repository is refused
before anything is spent, and so is one the screen could not read (a breach is a refusal,
because a screen that certifies what it did not open is worth no more than one that found
nothing).

### Portable task bundles

The task is a **directory** whose entrypoint states the work in provider-neutral terms,
with every file it references beside it. The whole bundle is copied into the run directory,
hashed, and materialized into each seat — so `--collect` never depends on conversation
context that has since vanished.

A task naming provider-specific machinery is **refused, never translated**: a subagent
type, `${CLAUDE_PLUGIN_ROOT}`/`${PLUGIN_ROOT}`, `codex exec`, an `mcp__…` tool name, a
provider-only permission flag, a `~/.claude`-style config directory. Two of the three seats
do not have it, and a bundle cannot give it to them. Rewrite the task; do not work around
the detector. A task relying on a **named ambient skill** (`/markitdown`, "use the X
skill") is permitted only when all three installed copies hash identically — otherwise the
same refusal, because three CLIs running "the same skill" is a claim the engine checks.

## 4. Fuse — your job, in the synthesis worktree

`--start` leaves a worktree at `<run-dir>/synthesis` on a new branch
`forge/<run-id>/synthesis`, created at B1 with the task bundle materialized into it. Read
each seat's candidate, then **write the fused answer there and commit it**. Nothing else
writes that tree.

If you commit nothing, `--collect` refuses: a synthesis tree whose tree oid is still B1's
own is a run nobody fused into, and describing it would report a merge-ready branch over an
empty delivery.

## 5. `--review` — convene §13's blind panel over your fusion

```bash
python3 "$FORGE" --review <run-id> --repo /path/to/repo
```

**This spends.** Three provider calls — one per reviewer, at the `--retries 0` §13 wires —
priced in the quote `--start` already showed you. Run it after you have fused and committed
in the synthesis worktree and handed over the ledger; it refuses, before spending anything,
a run with no ledger, a synthesis tree nobody fused into, and a round beyond the number the
operator confirmed.

The panel reads **its own clone** taken at your fusion commit, never the synthesis worktree
— that worktree is a linked worktree of your repository, where `.git` is a file pointing at
the parent's git directory, and three unattended reviewers in it reach `hooks/`, `config`
and the object store by an ordinary relative path.

**What its blindness is and is not.** Reviewers run as your user with a shell, and §13's
blindness is asserted against the ledger's path rather than enforced by the operating
system. A reviewer that goes looking can read the run's ledger, journal and seat clones by
absolute path. Treat the panel as **blind by construction, not by containment** — it is not
given the path, the bytes are not in its tree, and the prompt does not name them. Moving the
clone elsewhere would not change this, which is why it is not done.

One round per invocation. `--collect` reads the record and classifies it; a round **no
reviewer answered** is `review_blocked`, not `degraded`, because no review happened at all.

## 6. `--collect` — mergeability and the handover

```bash
python3 "$FORGE" --collect <run-id> --repo /path/to/repo \
  --handover-target refs/heads/main \   # or: --accept
  --verified-at <synthesis HEAD> \      # the OID your verify ran at
  --verify-exit 0 \                     # the status it returned
  --strategy from_scratch
```

**Two different strategy vocabularies, and mixing them is refused.** The answer sheet's
`strategy` is the *rule* the run was confirmed under — `size-gated`, `fusion`,
`base-and-port`. `--collect --strategy` is the strategy the fusion actually *followed* —
`from_scratch`, `partition`, `base_and_port`. They are not the same list and `fusion` is not
a value the second one accepts.

`--collect` reads the run back **off disk**, enumerates the synthesis tree's out-of-band
files, decides mergeability, runs the cloud ultrareview once (if it was confirmed), and
prints the handover.

**Get `--strategy` right the first time.** Every refusal `--collect` can make from disk — an
unfused synthesis tree, a tree identical to one seat's candidate, an out-of-band set it cannot
enumerate, a delivery naming neither a target nor an acceptance, a partial evidence set — comes
*before* the cloud review, so a run with nothing to hand over does not pay for it. `--strategy`
is validated when the provenance record is built, which `--collect` does **before** it invokes
the review — so a misspelled value refuses without spending anything.

### Reporting your verify result

The engine does not run verify over the fusion, and the handover says so in words. What it
takes is **evidence**, not a word:

`--verified-at` and `--verify-exit` go together — a lone one is refused, because a partial
evidence set is a verdict lost under a header that would say none was offered. Pass neither
and the header reports that no verdict was given, which is the honest reading and never a PASS.

There is no `FLAKY` here: that is a claim about two runs and this is one exit status.

### The fusion must be a fusion

`--collect` refuses a synthesis tree byte-identical to any single seat's candidate, with the
sentence *"this is seat X's candidate, not a fusion."* Promoting the strongest candidate as-is
is the thing this skill exists not to do. If that genuinely is the answer, deliver it out of
band rather than under a fusion's header.

Pass **`--handover-target`** (where the work went) or **`--accept`** (you took delivery
with no merge target). One of the two is required — without it "unmerged" is undefined and
`--gc` can never safely reclaim the run.

### Reading the handover honestly

Relay the header **as printed**. Six things in it will look like defects and are not:

- **`Synthesis: the orchestrator reports PASS … this engine did not run it`.** That is the
  truth: the engine builds no verifier clone for the fusion and runs no confirmed command
  over it, so the outcome it prints is a **report**, and the header says so instead of
  calling it verified. Never upgrade that sentence when you relay it. The
  `"Verified" here means` paragraph is deliberately absent beside it, and there is no flag
  that flips this.
- **`Fusion: no strongest seat can be named …`.** The ordinary outcome, not an error — and
  today it is the ONLY outcome. Ranking seats needs a coverage report over a claim ledger,
  and no production path writes one, so the rubric never has inputs to rank; separately, a
  criterion no predicate ran on makes a report unrankable by design. The header prints the
  reason **attached**. A named winner would be the winner of a comparison nobody ran.
- **`§11 agreement: differently-prompted`.** Also ordinary, and meaningful. The panel is
  heterogeneous on purpose — three different CLIs at three different versions — so their
  prompt fingerprints differ by construction. Agreement here is **provenance, never a
  correctness argument**; `identically-prompted` is what two attempts of one seat get.
- **`Council: no review round was convened.`** Printed when you did not run `--review`.
  It is not a round that failed or was skipped for cause — it is a stage nobody asked for.
  Run `--review <run-id>` (below) to convene one; the post-round FIX loop is still unbuilt,
  so a second round cannot be bought and `--review-rounds` above 1 prices calls that cannot
  be spent.
- **`Ultrareview: N finding(s) reported`.** Reported, and nothing more: §13.1's findings get
  no post-round fix, no fresh verification and no terminal, because that wiring does not
  exist. **Do not relay `0 finding(s)` as "the review found nothing wrong"** — the number is
  what the cloud review returned, not a verdict this engine acted on.
- **`(patch)` rather than a merge-ready branch.** A dirty baseline, or any out-of-band
  artifact, makes the delivery patch-only. **Merging the branch does not install out-of-band
  artifacts** — they are ignored files, were never added to the object store, and the
  handover prints an explicit `cp` command per file with a sha256 and a byte count. Say so
  when you relay it; a user who merges and stops has an incomplete delivery.

Files listed as **baseline-owned** are the selected untracked/ignored files carried into the
baseline: they are the user's, not forge's, and only their B→S changes are forge-authored.
The label is about **provenance, not content** — the list is every selected path, whether or
not the synthesis touched it, so do not read an entry as "this file is unchanged".

When a verdict **was** measured by this engine, the handover prints exactly this and
nothing stronger:

> "Verified" here means exactly this and no more: the confirmed verify command exited 0 on
> a fresh verifier clone at the final checkpoint. It does not mean the change has no new
> defects, and it is not a review.

## 7. `--gc` — mandatory, not tidy

```bash
python3 "$FORGE" --gc all  --repo /path/to/repo    # disk report, deletes nothing
python3 "$FORGE" --gc <run-id> --repo /path/to/repo [--force]
```

A finished run keeps every seat clone, every verifier clone and every interrupted write —
the ~63.3 GB peak is what accumulates until you reclaim it. `--gc all` reports total disk
held per run and marks each **handed over** / **NOT handed over** / **handover record
UNREADABLE** (three states, because an unreadable record is not a "no"). It sums only the
runs it could measure and says how many it could not.

`--gc <run-id>` removes three things, which is why deleting the run directory by hand is not
the same operation: the **registered synthesis worktree** (removing the directory alone
strands git's admin entry, and the repo-wide `git worktree prune` that would reclaim it is
forbidden), **this run's refs** under `refs/heads/forge/<run-id>/` and
`refs/khenrix-forge/<run-id>/` (which pin every object the seats produced — leave them and
the bulk of what the run cost stays on disk with nothing naming it), and only then the run
directory. It **refuses** rather than removing anything, and nothing is deleted on any of
these:

| Refusal | Why | What to do |
|---|---|---|
| no handover record | the deliverable is not marked handed over | `--collect <run-id> --accept`, or `--force` if the work is genuinely abandoned |
| the worktree holds uncommitted or untracked work | `git worktree remove` exits 128; forcing it would delete a fusion that exists nowhere else | commit or discard it, or `git worktree remove --force` by hand once you have read what would go |
| `git worktree list` does not name the tree | removing the directory would strand the admin entry, and a repo-wide `worktree prune` is forbidden | re-register or remove it by hand |
| a ref is checked out by another worktree | `update-ref -d` would leave that tree on an unborn HEAD | remove those worktrees first |

**`--force` waives the not-handed-over refusal and nothing else.** The others are the engine
being unable to describe what it would delete, and no flag clears one. An ignored file in
the tree is fine; a modified tracked file or an untracked one is not.

**The orphan case.** If `--start` fails at the synthesis worktree — the one failure that
leaves a registered worktree and branch behind — that run has no handover record, so
`--gc <run-id>` refuses it. `--gc <run-id> --force` is the reclaim for exactly that shape.

## 8. Rules for you, the orchestrator

- **Everything is argv, never a shell.** Every command the engine runs and every command it
  prints is a token list. If you are composing a `&&`, you are composing two steps.
- **Fail closed.** The engine refuses rather than guessing, and its refusals are sentences —
  relay the sentence. "Nothing was measured" is never reported as "nothing was found."
- **A verdict must never read cleaner than its evidence.** Every hedge in the handover is
  load-bearing: an orchestrator-reported outcome, an unnameable strongest seat, an
  unmeasured tree. Do not smooth them out into a summary.
- **Never re-run a seat by hand** to paper over a failure, and never re-admit a candidate
  the engine marked unusable.
- **`--collect` reads from disk, always.** After a context loss, `--collect <run-id>` is the
  whole recovery — do not reconstruct state from the conversation.

## Maintaining this skill

The engine is `shared/lib/forge/` in the repo and `<plugin>/lib/forge/` once rendered;
`scripts/forge.py` is a façade that resolves that directory and delegates, with no logic of
its own. Its receipt is earned by the **whole hermetic forge suite** — all 31 modules, every
test executed and none skipped, with the counts recorded in the receipt — not by the judge
harness:
a read-only with-skill/baseline eval cannot drive a clone fleet, three providers and a
fresh verifier, so the judged delta on this skill is **advisory**. Note what that self-test
buys and what it does not — **the self-test gates wiring, not judgment**: it proves the
façade resolves, the CLI parses, the handover record round-trips and `--gc` refuses what it
must. It says nothing about whether a fusion was good.

Run `make forge-test-slow` for the clone- and process-heavy half of that suite; `make
verify` deliberately runs only the fast half, because `make verify` is this repository's own
confirmed verify command and a forge run would otherwise spawn clone fleets inside its own
verifier clones. `make precommit` runs both.

## The provider path is proven by one opt-in smoke

The engine's own suite invokes **no** provider — every seat launch is a fake, deliberately, so
the suite costs nothing. That means the suite proves the engine and not the adapter. One
target proves the adapter:

```bash
make smoke-llm-forge      # costs tokens, needs auth — three provider calls
```

It builds a disposable repository, gives each CLI its own clone, asks each to write a distinct
marker and quote its proof token, harvests them, and shows the original checkout unchanged. On
success it writes a receipt naming the adapter source hash and all three CLI versions, so the
receipt goes stale when either moves. Run it whenever adapter or provider wiring changes.
