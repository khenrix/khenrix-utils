# llm-forge — design

**Status:** design, revision 3. Two deep council rounds folded in (2/3 and 2/2 seats).
**Scope decision:** full scope. Both round-2 seats recommended cutting ~40% for v1; the
owner reaffirmed the complete design, accepting that it needs further review rounds before
implementation. Their cut lists are preserved in *Deferred by owner decision* so the
trade-off stays visible rather than lost.

---

## 1. What this is

`llm-council` fans one *prompt* to all three CLIs on this machine (Claude Code, Codex,
Antigravity/`agy`) read-only and in parallel, and the invoking CLI synthesizes one best
*answer*.

`llm-forge` gives all three the same *task*, each in its own isolated repository, then
merges three implementations into a fourth that is verified and independently reviewed
before handover. The user's working tree is never written to by the engine.

Invocation accepts a slash command (`/llm-forge /chunk-map`), a plan file
(`/llm-forge docs/plans/foo.md`), or free-form text.

### Fixed decisions

1. Deliverable is a fourth synthesis tree, verified.
2. New skill sharing the existing engine — not a mode of `llm-council`, not a fork.
3. Setup and verify commands are **detected**, then **confirmed by the user once**.
4. Merge operator is **claim-level fusion written from scratch**; base-and-port is the
   diagnosed fallback.
5. Review is a plain `llm-council --mode deep` run.

### What this design does not establish

Nothing here proves claim-level fusion produces good code. The hermetic suite gates
*wiring*, not *judgment*. The first several real runs are the only evidence that will exist
on the question that actually matters — so the ledger and the event journal must be good
enough to read afterwards. They are the experiment's instrumentation, not just its audit
trail.

---

## 2. The baseline `B` is composite, not one OID

A git tree is only git's *tracked* snapshot. It cannot represent empty directories, full
POSIX modes, ACLs, xattrs, or untracked/ignored sidecars. Treating `B` as a single tree OID
breaks clone fidelity, review reproducibility, checkpoint identity and crash recovery.

```
B = {
  base_commit,                 # HEAD at t0
  tracked_tree_oid,            # HEAD + staged + unstaged + selected untracked
  synthetic_baseline_commit,   # internal execution anchor ONLY
  sidecar_manifest,            # declared ignored inputs, empty dirs, special files
  filesystem_manifest,         # per-path content hash + mode + size
  original_status_and_refs     # t0 snapshot, for drift and tripwire
}
```

The synthetic commit exists so clones and worktrees have a commit-ish to start from —
`git clone` transfers only ref-reachable history and `worktree add` needs a commit. It is
**never** presented to the user as history.

### 2.1 Stratified anchor

- **B₀** = `base_commit` (HEAD, untouched).
- **B₁** = B₀ + the user's dirty state, committed with **the user's** authorship and a
  message stating exactly what it is: *"forge: snapshot of your uncommitted working tree at
  `<time>` — this commit is yours, not forge's."*

Forge's work stacks on B₁. This exists because the synthesis branch is rooted at the
baseline, so without stratification, merging the deliverable would permanently commit the
user's debug prints, local config hacks and half-finished scratch files into `main` **as
forge's commit**. Stratifying gives handover a real choice: take the whole stack, or
cherry-pick only forge-authored commits.

> **Open — owner's call at the confirmation gate.** Authoring B₁ as the user is honest
> about provenance but produces a commit they did not write; authoring it as forge
> misattributes their work. Recommendation: the user's identity with an unmistakable
> message.

### 2.2 Construction

Argv lists with explicit environment dicts and NUL handling — never shell pipelines. The
alternate index path must be **absent** initially, not an empty invalid index file.

```
git rev-parse --verify HEAD^{commit}
git ls-files --unmerged -z                       # must be empty
git write-tree                                   # staged snapshot, real index

GIT_INDEX_FILE=$RUN/index GIT_OPTIONAL_LOCKS=0 \
  git -c core.fsmonitor=false -c core.untrackedCache=false read-tree <staged-tree>

GIT_INDEX_FILE=$RUN/index GIT_OPTIONAL_LOCKS=0 \
  git -c core.fsmonitor=false -c core.untrackedCache=false add -u -- .

GIT_INDEX_FILE=$RUN/index GIT_LITERAL_PATHSPECS=1 \
  git add -f --pathspec-from-file=<NUL file> --pathspec-file-nul

GIT_INDEX_FILE=$RUN/index git write-tree         # -> tracked_tree_oid
git commit-tree <tree> -p <base_commit> -m 'forge baseline <run-id>'
git update-ref refs/khenrix-forge/<run-id>/base <commit> <expected-old>
```

- **`add -u` plus explicit pathspecs, never `add -A`.** `-A` sweeps in every non-ignored
  untracked file, bypassing the selection the user controls at the gate.
- **`update-ref` immediately follows `commit-tree`.** Until the ref exists the commit is
  unreachable and a concurrent `git gc --prune=now` can drop it.
- **`GIT_OPTIONAL_LOCKS=0`** stops read-oriented commands opportunistically refreshing the
  user's real index.
- **fsmonitor and untracked-cache disabled** on every snapshot/harvest call, so the baseline
  never depends on daemon state.
- Raw filesystem hashes taken **before and after** to detect a concurrent editor write;
  abort if the source changed mid-snapshot.
- Validate materialized trees against `filesystem_manifest`, not merely the tree OID.

### 2.3 Fail closed in preflight

Custom `filter=` attributes (including LFS) · `working-tree-encoding` that does not
round-trip · partial clones / promisor objects · shallow repositories · **all** submodules ·
nested repositories · sparse checkout · unmerged and intent-to-add index entries ·
skip-worktree state · symlinks whose normalized target escapes the tree · symlink cycles ·
non-regular special files · non-git directories.

`.gitattributes` is the hard boundary: `git add` runs the **clean** filter and checkout runs
**smudge**, but `filter.<name>.*` lives in `.git/config`, which `git clone` does not copy. A
non-required filter silently degrades to pass-through; a required one fails the checkout.
Either way the seat is not working on the user's content.

Plain EOL normalization *is* supported, by capturing the effective safe checkout config and
validating byte hashes after every checkout.

---

## 3. Secret screen runs before any provider starts

The user's uncommitted edits are exactly where an unscrubbed token lives, and they enter `B`
where three full-permission cloud-backed seats read them. A post-harvest scan is too late —
the exposure is already irreversible.

1. **Before any provider launches:** scan the entire selected baseline, the task bundle, and
   the resolved resource closure.
2. Path-sensitive blocking for `.env*`, credential files, private keys, auth exports, and
   any user-selected ignored file.
3. **Fail closed**, or require an explicit per-path override recorded in the manifest.
4. Re-scan seat reports, stdout/stderr, ledgers, patches and final artifacts before
   persistence and again before handover.

`checks.scan_path()` (checks.py:129–145) is a **single-file** scanner — `read_text` then a
line loop, no size cap, no binary guard, no directory walk. Pointing it at three artifact
trees would decode a 400 MB build log into memory. Write a directory-walking variant reusing
`SECRET_FAIL` / `SECRET_ALLOW_SHA` (do not fork the patterns) with a per-file byte cap, a
NUL-byte binary skip, and an aggregate cap that fails closed with a report line.

**Stated honestly in SKILL.md:** this is a high-confidence screen, not proof the baseline is
secret-free, and it does not contain full-permission agents that retain access to the real
`$HOME`.

---

## 4. The clone fleet

Linked worktrees share the parent's `.git` — refs, objects, config, hooks. A
permission-bypassed seat can `git branch -f main <sha>`, write `.git/hooks/pre-commit`, or
`git push`, without leaving its cwd. Seats therefore get **independent clones**; only the
synthesis tree is a worktree, because it is the handover surface.

Construction, in order:

1. `git clone --no-local --no-checkout --single-branch --no-tags` from the synthetic
   baseline ref. **`--no-local` / `--no-hardlinks` unconditionally.**
2. Empty template directory, so globally configured template hooks are not installed.
3. **`git remote remove origin`** — before setup, before the agent. `git clone <path>` always
   writes `remote.origin.url`, so without this every seat ships with a working push target
   aimed at the user's repository; `receive.denyCurrentBranch` blocks only the checked-out
   branch, so `push origin HEAD:refs/heads/x`, `push origin +HEAD:refs/tags/v1` and
   `push origin --delete` all still succeed. Asserted by a hermetic test: `git remote` is
   empty in every seat.
4. Sanitize checkout configuration; replay only the ignore-semantics subset that affects
   correctness (`.git/info/exclude`, `core.excludesFile`) and record what was replayed.
5. Scrub inherited path-bearing environment (`VIRTUAL_ENV`, `PYTHONPATH`, npm prefix,
   absolute tool paths) that resolves into the original checkout.
6. The seat gets only its own branch.

> **`--local` is deleted, not made optional.** Against git's own operations hardlinks are
> safe — loose objects are mode-444 and content-addressed, and `gc`/`prune` in the clone only
> unlink its own directory entries. The hazard is *non-git* writes, which is forge's entire
> threat model: `chmod -R u+w .git` then `truncate` or `sed -i` under `.git/objects` corrupts
> the **user's** repository through the shared inode, invisibly until the next `git fsck`.

Reject the run if the disk estimate cannot sustain independent clones. Do not trade
source-object safety for space.

### 4.1 What clones do not carry

Local `.git/config` (hooks, `core.hooksPath`, `filter.*`, `diff.*`, `merge.*`,
`url.*.insteadOf`, `core.autocrlf`), `.git/info/exclude`, `.git/hooks/`, and `.git/lfs/`.
`.envrc` is tracked and *is* cloned, but `direnv allow` is path-scoped and is not — so a repo
whose build depends on direnv env silently loses it in every seat. These are enumerated at
preflight; the safe subset is replayed and the rest fails closed.

### 4.2 Posture, depth guard, tripwire

- A **write posture line**, identical for every seat (mirroring `READONLY_POSTURE`,
  fanout.py:1323): confine writes to your working directory; no `git push`; no branches but
  your own; no writes outside this directory; no changes to CLI config under `$HOME`.
- An **`LLM_FORGE_DEPTH` guard** in `child_env()` (fanout.py:1083 increments only
  `LLM_COUNCIL_DEPTH`) plus a member note barring forge. Without it a seat reaching for
  `/llm-forge` spawns three more write-enabled seats, recursively.
- A **breakout tripwire** — see §9.

**SKILL.md says the true thing:** the forge *engine* never targets the original checkout;
full-permission seats and setup scripts are not *prevented* from doing so. Arbitrary project
code can still address absolute external paths directly. The tripwire is detection, never
prevention.

---

## 5. Confirmation chronology

The single gate could not show baseline calibration results, because calibration runs the
very setup and verify commands the user has not yet authorized. And the strategy size gate
needed artifacts that do not exist until after the builders run.

1. **Static, read-only preflight.** Baseline composition, selected paths and hashes, detected
   argv/cwd/env, unsupported-feature rejections, secret screen, disk/time/provider-attempt
   estimates.
2. **Ask once.** The user confirms both command sequences *and* a policy for calibration
   failure (`abort` | `continue as degraded`) *and* the strategy rule to be applied later.
3. **Calibrate** in a sacrificial clone built through the seat code path (§6).
4. **Build.**
5. **Apply the already-confirmed deterministic strategy rule** to measured artifact size.
   Record the decision; do not ask again.

No arbitrary project setup code runs before authorization. If the actual calibration result
must be seen before consent, two gates are unavoidable — say so rather than reordering.

### 5.1 Commands are argv sequences

Real monorepos need several steps with different cwds. `cd frontend && npm ci` must not be
approximated with `shlex.split`.

```
[{argv: [...], cwd: "...", env: {...}, timeout: N}, ...]
```

Shell metacharacter syntax is **rejected**, not silently reinterpreted.

### 5.2 Cost quoted at that gate

Worst case, honestly: 3 builders × 3 attempts = 9, plus synthesis, plus review — and
`llm-council` itself defaults to `--retries 2` (fanout.py:2201), so two deep rounds is
2 × 3 × 3 = **18 review attempts** unless review passes `--retries 0`. **Forge invokes review
with `--retries 0`**, giving 3 + 1 + 6 = 10; the 22-attempt figure is quoted if that ever
changes. Also quote: baseline calibration setup+verify, builder setup, a fresh verifier
setup+verify per candidate, synthesis verification after each fix, peak disk (three
no-hardlink clones plus three dependency trees is plausibly 6–10 GB under `XDG_STATE_HOME`),
and wall clock. *"Ten agent runs"* does not describe four package installs and four test
suites; provider-call cost and shell-command time are quoted separately.

---

## 6. Verification runs where the builder never was

A seat can replace `.venv/bin/pytest` or `node_modules/.bin/jest` with a program that exits
zero, or delete an auto-discovered test file while leaving the Makefile untouched. Those
changes are deliberately excluded from harvest and invisible to a gate-definition tripwire.
Running the confirmed command in the seat's own clone therefore measures nothing.

**Every verification runs in a fresh verifier clone the builder never had access to:**

1. Harvest the seat (§7) — *before* verification.
2. Materialize the harvested candidate in a brand-new clone built through the same path
   as §4.
3. Run the confirmed setup command there.
4. Run the confirmed verify command there.
5. Repository hooks and any post-seat git configuration are disabled in verifier clones.

This applies identically to **baseline calibration** and to **synthesis verification**, which
also makes the verify claim uniform across all four trees. It costs one extra setup per
candidate; that cost is essential, not optional hardening.

### 6.1 Gate surface

Record changes to the whole gate surface, not just Makefiles: package-script definitions,
test runners, discovered test files, CI helpers, and test counts/identities where the
framework exposes them. A legitimate test edit is allowed but marks the candidate
`gate_changed` and requires review; it never silently retains "independent gate" status.

### 6.2 Verify outcomes

| Outcome | Meaning |
|---|---|
| `PASS` | exit 0 on a fresh verifier clone **and** no unexplained tracked delta (§7.2) |
| `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` | calibration was already red and no new failure identified — **never** reported as PASS |
| `FAIL` | a new failure relative to calibration |
| `FLAKY` | fail→pass on rerun; labelled indeterminate, never converted to a pass |

Two nonzero runs are not automatically equivalent: output ordering, truncated logs, flaky
tests and changed test discovery can hide a newly introduced failure. Structured test
identities are required where available; otherwise the comparison is manual and degraded.

The `ready` terminal state requires a clean `PASS`. A baseline-red continuation can only end
`degraded`, unless the user's confirmation explicitly chose otherwise.

Setup is serialized or concurrency-bounded even across clones — global package caches,
databases and port allocation are shared. Verification is serialized, so "re-run alone" no
longer removes inter-seat contention; a fail→pass rerun is `FLAKY`, not a pass.

---

## 7. Harvest — origin is provenance, not eligibility

Four inventories per seat: `F0` (baseline checkout) · `Fsetup` (after the engine's setup) ·
`Fwork` (immediately after the agent exits) · `Fverify` (after the engine's verify).

Each changed path carries its **origin**: `setup-origin`, `builder-origin`, `verify-origin`.
Origin is recorded and reported; it does not by itself decide inclusion.

### 7.1 Path set and content

- The artifact **path set** comes from `Fsetup → Fwork`.
- The artifact **content** comes from `git diff <B> <final> -- <those paths>`.
- Paths that setup also touched carry a `setup_overlap` flag, so the ledger states the churn
  component instead of absorbing it silently.

Stating only "the artifact set is `Fsetup → Fwork`" alongside "tracked delta is always
computed from `B`" left the two rules to disagree on every path both setup and the agent
touched, with nothing saying which wins.

`Fsetup → Fwork` means **"net state present when the builder exited"**, not "intentional
builder artifacts" — a model can legitimately run generators or tests during its turn.

### 7.2 Verify-origin changes can be required deliverables

This repository is the counterexample. `make verify` runs `render` (Makefile:74) and
`precommit` requires the tracked `marketplaces/` output to match (Makefile:78). If synthesis
changes a shared skill, then either the builder never ran `make verify` and the engine
generates the required marketplace copies during `Fverify` — which, categorically excluded,
hands over an **incomplete branch** — or the builder ran it normally and those same required
files land in `Fwork` mislabelled as builder noise.

- If verification changes tracked files, the command is **partly a generator** and has not
  reached a clean fixed point.
- Admit only **declared deterministic generated outputs** with a known source→output
  relationship (here: `marketplaces/**` from `shared/**` + `capabilities.toml`).
- Checkpoint those outputs, then re-run setup and verify in a **fresh** verifier clone.
- A clean `PASS` requires a **bounded fixed point**: exit 0 *and* no unexplained tracked delta.
- `.venv/` and `node_modules/` remain excluded; tracked generated clients, rendered plugin
  copies, lockfiles, snapshots and golden files may be legitimate.
- If builder and verifier both touch a path: label `work+verify overlap`, resolve explicitly.

### 7.3 Change predicate

**Content hash + mode + size only.** mtime, ctime and inode are recorded as provenance and
are *never* a change signal — `render`'s `rmtree` + `copytree` replaces all 188 tracked files
under `marketplaces/` with new inodes and mtimes on every run while their content is
byte-identical, which an lstat-keyed diff would report as 188 phantom changes per seat, 564
across the panel.

Hermetic assertion: snapshot, `rmtree`+`copytree` an identical tree, re-snapshot, assert zero
changes.

### 7.4 Ignored artifacts

Included when **declared** — by the task, by the user at the gate, or by a skill contract
(`.chunkmap/map.md` qualifies; `node_modules` does not). Task-declared ignored *inputs* are
selectable into the baseline sidecar, without which "modified or deleted pre-existing ignored
file" cannot work at all.

Inventory via `lstat` + content hash + mode + size, **never following symlinks out of the
tree**. Path, count and byte caps **fail closed** with a report line.

---

## 8. Seat status

Four independent dimensions, because collapsing them is what let a silently-failed seat read
as success:

| Dimension | Values |
|---|---|
| process result | valid / invalid |
| artifacts | usable / unusable |
| task bundle | proven read / not proven |
| forge status | `completed` / `partial` / `no_change` / `failed` |

plus `setup` and `verify`, each pass / fail / not-run.

A seat with useful artifacts but no proof token is **`partial`**, not completed. A
`no_change` requires a substantive rationale and independent verification — a correct
conclusion that the task needs no edit must not be discarded. Passing verify is **recorded,
not required** for a seat to inform synthesis. A setup failure does not proceed merely
because it produced files.

### 8.1 Validity is injectable; retries get a fresh clone

`evaluate()` is hard-called at fanout.py:1130 and runs `score_seat` with
`MIN_SUBSTANTIVE_CHARS = 400` plus the SENTINEL check — so a seat that implements flawlessly
for forty minutes and signs off with *"Done — `make verify` passes."* scores
`non_substantive`, which then triggers the retry loop (fanout.py:1180) to re-run the same
argv **in the same cwd**, on top of its own half-finished work, twice by default.

- Validity becomes injectable: `ProviderSpec.validator`. `min_chars = 0` and no sentinel
  invalidation for forge seats — `eval_harness.py:245` already does exactly this
  `min_chars = 0` opt-out, with a comment saying council policy is not a property of running
  a provider.
- **Every retry attempt gets a fresh clone.** The failed attempt is preserved as partial
  input. Never a reset-and-rerun in place.

---

## 9. Refs, the safety invariant, and drift

"The original refs are unchanged" is impossible — the synthesis branch and its worktree
necessarily write objects, forge refs and worktree administration into the user's repository.

**Protected:** the user's current branch ref, `HEAD`, index hash, checkout files, all
non-forge refs, remotes, and configuration.
**Explicitly allowed:** `refs/heads/forge/<run-id>/*` and `refs/khenrix-forge/<run-id>/*`,
whitelisted by **exact ref name *and* the exact OID recorded at creation** — a namespace
whitelist would let a seat write into forge's own namespace invisibly.

Expected forge-ref movement is reported separately from unexpected protected-ref movement. If
the user's checkout or protected branch changes during the run, transition to
**`source_diverged`** and do not continue to handover automatically.

**No global `git worktree prune`.** It is repository-wide and can remove administration for
worktrees forge does not own, including intentionally offline ones. Instead:
`git worktree add --lock --reason "llm-forge <run-id>"`, `git worktree remove` by exact owned
path, and `--dry-run` only for reporting.

**Drift at handover:** re-run the status snapshot, diff against the recorded t0 snapshot, and
print the diverged paths plus elapsed time prominently — *"this deliverable is based on a
snapshot from 2h14m ago; you have since changed 4 of the files it touches."* Without it, a
clean merge can silently revert the user's own subsequent work on any hunk forge also touched.

---

## 10. The claim ledger

```
id                    # sha256(requirement_id || semantic_claim)[:12] — content-derived
requirement_id        # + source span/hash
kind                  # behavior|api|schema|migration|security|test|architecture|seam
component
semantic_claim
status                # accepted|rejected|deferred|unresolved
dependencies          # [{id, requires|conflicts|blocks}] — cycle-checked
seat_evidence         # [{seat, stance: supports|contradicts|silent, evidence, prompt_sha256}]
counterevidence
acceptance_criteria
synthesis_evidence    # {oid, path, hunk/symbol, test}
verification_receipt
risk
rationale
```

- **IDs are content-derived, not counters.** Coverage checks compare across review rounds; if
  a round splits or inserts a claim and IDs shift, the check compares stale identity.
- **`seat_evidence` is a nested list, not flattened columns.** Prompt-identity conditioning
  (§11) is per seat and cannot be recorded any other way.
- **`rejected` is a first-class status.** If all three seats considered and rejected a cache
  layer, that is the most valuable signal in the run — and from-scratch synthesis, which reads
  only the ledger, would otherwise add it straight back. Coverage asserts no accepted row
  contradicts a unanimous rejection.
- **Dependencies are cycle-checked**, because topological ordering is a precondition of
  partitioned synthesis.

Claimed honestly as a **compaction-survivable spec and audit trail**, not a context-budget
control: writing it requires reading all three artifact sets, so peak context is unchanged.
Budget control is per-seat sub-reads emitting structured claim lists, merged mechanically by
key, with a hard degradation rule — above N KB union diff, drop to per-file summaries and
**say so in the report**.

### 10.1 Coverage is only mechanical where a predicate exists

A row reading *"crash-safe atomic state update"* is marked present because `os.replace`
appears, while `fsync` of the file and its directory is missing and the property is false. A
generic walk over natural-language rows is systematic review, not deterministic coverage —
calling it mechanical manufactures another false green.

Only criteria with a real predicate — test ID, schema query, exact symbol, file/hash
invariant — are mechanically checked. Everything else is marked `manual_trace_confirmed` or
`unresolved`.

---

## 11. Agreement is provenance, never a correctness argument

The trajectories are **not statistically independent**: they share the task text, the
repository's conventions and `CLAUDE.md`, the same test suite, the same skill instructions
and correlated model biases. A repeated mistake can be 3/3; the only correct fix can be
unique to one.

Per-seat identity is recorded as: exact prompt hash · nonce-stripped semantic prompt hash ·
task/resource bundle hash · CLI/model/plugin/version fingerprint. A text-only `prompt_sha256`
does not identify attachments or ambient capabilities.

Classification is conditioned on prompt identity, and agreement across differently-prompted
seats is labelled weaker. Agreement never substitutes for a correctness argument.

---

## 12. Strategy and fallback

Chosen by the rule confirmed at the gate, applied to measured artifact size after the
builders run.

1. **Size gate triggers analysis, it does not force partitioning.** Below ~400 changed lines
   / ~15 files, from-scratch fusion. Above it, partition *only where stable seams exist*; a
   tightly coupled change must not be forced apart, and where no stable seams exist
   base-and-port is the correct primary strategy.
2. **Seam claims are mandatory when partitioning** — public API/schema contracts, shared
   transaction and error invariants, migration ordering, cross-component data flow, and final
   integration claims. They are ledger rows of kind `seam`, owned by no partition, frozen
   before any component synthesis begins, and agreement with them is part of the coverage
   check. Without this, partitioning does not solve the coherence problem, it relocates it to
   the seams — where partition A satisfies "records carry a monotonic `seq`", partition B
   satisfies "list endpoints are cursor-paginated", both rows are present, verify is green
   because no test crossed the boundary, and pagination silently skips records.
3. **Every verify failure is classified**: baseline/infrastructure, synthesis-introduced, or
   requirement/test gap. **Never fall back on an infrastructure failure** — base-and-port
   cannot help. Fall back when synthesis is infeasible or has stopped making monotonic
   progress, not on an attempt count.
4. **Claim-coverage check** per §10.1 — a missing accepted row is a fallback trigger *and* a
   report line, regardless of verify. This is the only thing that catches false-green.
5. **Strongest-seat rubric declared in advance:** requirement coverage first, independent gate
   outcome second, review risk third, diff complexity last.

---

## 13. Review

A plain `llm-council --mode deep --retries 0` run against a **synthesis checkpoint commit**
containing every intended git deliverable.

Reviewers receive **only**: the immutable original task bundle · the baseline commit/tree
identity · the synthesis checkpoint OID · the out-of-band artifact manifest · review
instructions. They run `git diff <B>..<S>` themselves and must cite changed-file evidence.

- **The ledger path is not passed.** Briefing a verifier with the spec and the artifact only —
  never the producer's reasoning — is the strongest call in this design and matches house
  style. Passing the path and relying on prose telling reviewers not to read it would weaken
  blind review to nothing. The orchestrator consults the ledger *after* receiving independent
  findings.
- **A small launcher prompt** points to an engine-owned task bundle inside the clone, because
  claude and agy adapters place the prompt in argv (fanout.py:678, 724) and the task plus
  resolved closure can still hit `E2BIG` even without the raw diff. **The proof token lives
  inside the bundle, not the launcher** — otherwise quoting it proves only that the seat read
  argv.
- Re-run verify and cut a new checkpoint after every fix.

**A bounded review loop, not convergence.** Round-1 blocker → fix, verify, checkpoint,
round 2. Round-2 blocker → terminal state `review_blocked`, regardless of verify. A blocker
fixed after round 2 is reported *"verified but not independently reviewed."* **Never emit a
clean provenance header with unresolved blockers.** The last state that passed verify is the
deliverable; a fix that breaks verify is reverted and the finding reported unresolved.

---

## 14. Execution model and durable state

`--start` launches Phase 1. **`--collect <run-id>` is the only entry point to phases 2–5**,
always resuming from disk and never from conversation state — which makes compaction and
restart indistinguishable, one code path instead of two. This matters because phases 2–5 span
hours and the orchestrator's context will be compacted mid-synthesis long before any session
boundary is reached.

There is **no automatic orchestrator callback**. A Python engine returning from `--start`
cannot generically reawaken a Claude, Codex or agy turn; that needs provider-specific
mechanisms, and anything that launches a new CLI process is a different agent run with
different session context. v1 supports foreground polling within the current turn, or explicit
`--collect`.

### 14.1 Exactly-once is not deliverable

Arbitrary setup commands and LLM edits are not idempotent. A SIGKILL after setup mutated a
database but before its completion record landed is unrecoverable by inspection: the engine
cannot distinguish never-started from partly-ran from completed.

- **Append-only `events.jsonl`**, `O_APPEND`, newline-terminated JSON, fsync'd. A torn final
  line is discarded on read; everything before it is authoritative.
- **Write-ahead intent, then result.** Append `{"event":"council_round_start","round":2,…}`
  *before* invoking, and `…_done` after. A crash between them is distinguishable from a crash
  before — the only way idempotence can hold at all.
- Every operation records `operation_id`, input hashes/OIDs, argv/cwd/env, PID, **process
  start time**, **boot ID**, process-group identity.
- **The wrapper process — not the parent — writes the completion receipt** and output hashes.
- `started` with no receipt and no surviving process becomes **`outcome_unknown`**. It is
  never silently retried. Only operations declared replay-safe retry automatically.
- **Git is the ordering of record.** Each checkpoint commit message embeds the verify outcome
  and the ledger hash, so `git log --format=%H%n%B forge/<run>/synthesis` reconstructs the
  checkpoint sequence and the last verify-passing OID even if every JSON file is torn. Free —
  the commits are being made anyway.

State is `(phase, round, attempt, verified_checkpoint, deliverable_checkpoint)` — separate
dimensions, with the `reviewing → synthesizing` back-edge declared. A single enum cannot
represent "fixing after review round 2."

```
created → confirmed → setting_up → building → harvested → comparing
        → synthesizing ⇄ verifying → reviewing → ready | degraded | review_blocked
                                                       | source_diverged | failed
```

### 14.2 Worked crash: SIGKILL during a post-round-2 fix

`--collect` reconstructs from the immutable run manifest (repo path, `base_commit`, `B`
identity, selected paths, confirmed argv/cwd/env — written once at `confirmed`, never
rewritten, so commands are never re-detected); per-seat atomic files (status tuple, four
inventories, setup/verify logs and hashes, prompt fingerprints — safe because seats are
terminal before `comparing`); the ledger with content-addressed rows; the synthesis branch
HEAD plus dirty filesystem inventory; `git log` for the checkpoint sequence and last
verify-passing OID; and `events.jsonl` for `council_round_done ×2` plus a `synthesis_fix_start`
with no matching done.

It then: preserves the interrupted dirty tree as a WIP checkpoint **before touching it**;
never spends a third council round automatically; resumes *that one fix* from the task,
artifact, round-2 finding and interrupted filesystem state — it cannot reconstruct the lost
reasoning; verifies in a fresh verifier clone; and on success records a checkpoint as
"verified but not independently reviewed" with terminal state `review_blocked`, or on failure
keeps the earlier verified checkpoint as the deliverable and preserves the failed candidate
separately.

---

## 15. Storage

`${XDG_STATE_HOME:-~/.local/state}/khenrix-forge/<sha256(repo_path)[:12]>-<run-id>/`, mode
`0700`. Not `~/.cache`, which XDG defines as deletable without loss and which every cleanup
tool targets first; and hashed rather than basenamed, so `~/git/a/utils` and `~/work/b/utils`
do not collide.

File-count, per-file and aggregate-size quotas. Cleanup is `--gc <run-id>` plus automatic
removal of known-failed temporary clones only; it refuses to delete a synthesis
worktree/branch not marked handed over, and `handover_target` (or explicit user acceptance) is
recorded so "unmerged" is well-defined — a patch-based handover may intentionally never merge
the internal branch. Keep-last-N is deferred. Report total disk held by past runs.

---

## 16. Handover

Two deliverable classes:

- **Git deliverables** — committed to `forge/<run-id>/synthesis`, created with
  `git worktree add -b` (**never `--detach`**: a detached HEAD leaves commits unreachable and
  the next `git gc` deletes them).
- **Out-of-band deliverables** — ignored artifacts retained in the synthesis tree and run
  directory with hashes and an explicit copy command. **Never force-added**: that violates the
  originating skill's contract and would put `node_modules` in the object store forever.

Seat work is committed at harvest onto `forge/<run-id>/<seat>`.

**Mergeability depends on the baseline:**

| Condition | Handover |
|---|---|
| `tracked_tree_oid == base_commit^{tree}` and no sidecars | merge-ready branch |
| dirty baseline | `B→S` binary patch + per-file hashes + integration command; merge-ready branch only after the user commits an exact baseline or explicitly asks forge to integrate against a new commit in a disposable worktree |

Unchanged selected untracked/ignored files are **baseline-owned**; only their `B→S` changes
are forge-authored. The B₁ file list is enumerated in the handover text, not only at a
confirmation gate an hour earlier.

Handover states plainly that merging the branch alone does not install out-of-band artifacts.

### 16.1 Provenance header

```
**Forge: 3 of 3 seats completed; 3 artifact sets usable; 1 of 3 passed verify.
Synthesis: verify PASS (`make verify`, 47s) — subsystem-partitioned fusion.
Council: 2 rounds, 1 finding unresolved (review_blocked).**
```

"Built" is forbidden for a seat that produced artifacts but failed verify. **"Verified" means
the confirmed verify command exited 0 on a fresh verifier clone at the final checkpoint** — it
does not mean "no new defects", and SKILL.md says so in one sentence, because a provenance
header will otherwise be read as the stronger claim.

---

## 17. Engine sharing

Move **mechanism**, keep **policy**. `shared/lib/council/` as a package;
`shared/skills/llm-council/scripts/fanout.py` becomes an executable compatibility façade.

| Shared core | Council-specific | Forge-specific |
|---|---|---|
| provider argv adapters, structured-output parsers | `MODES`, `MODE_TIMEOUT` | composite baseline |
| process-group runner, timeout, teardown | `score_seat` / `MIN_SUBSTANTIVE_CHARS` | artifact inventory + ledger |
| failure provenance, usage extraction | `READONLY_POSTURE`, `MEMBER_SKILLS_NOTE` | durable state machine |
| atomic JSON/log helpers | `council_header`, `_render_text` | setup/verify orchestration |
| low-level git primitives, parameterized | council retry policy | fail-closed clone fleet |

**Freeze observable compatibility — CLI, JSON, logs, and the programmatic API — not source
bytes.** A `from council import *` façade changes the defining module of functions, so
`__file__`-relative resolution and monkeypatching can differ even when CLI output is
identical. Characterization tests are written **first**, and must cover exported names and
signatures, `ProviderSpec` construction and defaults, monkeypatch behaviour used by tests,
module globals such as `MODES`, manifest/log file names and ordering, and `fanout.__file__`
-relative behaviour.

Known breakages:

1. **The receipt gate silently stops protecting llm-council.** `checks._skill_source_files`
   (checks.py:200) builds the closure from `shared/skills/<skill>/` + `LIB_SCRIPTS` +
   `GLOBAL_INPUTS` + `SKILL_EXTRA`. Once the engine leaves that directory, editing it no
   longer changes llm-council's `source_hash` and `make precommit` stops flagging engine
   changes. Requires `SKILL_EXTRA_DIRS` entries for **both** skills, mirroring wikisync
   (checks.py:190–194). Tighten the closure self-test at checks.py:369 from a filename match
   (`any("fanout.py" in r …)`) to a full relative path, so it actually asserts location.
2. `eval_harness.py:52–55` hardcodes `sys.path.insert(.../llm-council/scripts)` +
   `import fanout`; `_write_receipt` (line 445) shells `FANOUT_DIR / "fanout.py" --self-test`.
3. **`--self-test` works in the rendered plugin today** — `ignore_patterns(…, "tests")` at
   render.py:216 is on the **`SHARED_LIBS`** path, while shared *skills* are copied wholesale
   at render.py:198 with no ignore argument, and
   `marketplaces/claude/.../llm-council/tests/stub_provider.py` exists. The exclusion is a
   **landmine this migration steps on**, not a pre-existing bug: moving the core plus its
   `tests/` into `shared/lib/council/` would strip the tests and kill `--self-test` in the
   plugin for the first time. Either keep `tests/` under `shared/skills/llm-council/tests/`
   so the wholesale skill copy keeps carrying it and `STUB` (fanout.py:1422) keeps resolving,
   or add a tests carve-out to `SHARED_LIBS` **before** the move. Add a packaging test that
   runs `--self-test` from a rendered plugin path — the assertion nobody currently makes.
4. **Do not use `LIB_SCRIPTS`** — it copies into *every* skill's `scripts/`
   (render.py:204–207); a 2,300-line engine × ~10 skills × 3 CLIs is ~70k duplicated lines.
   `SHARED_LIBS` is correct and proven by wikisync.
5. Plugin-root discovery: both skills need the `$FANOUT` loop *plus* a `PYTHONPATH` export —
   copy wikisync's SKILL.md idiom verbatim.
6. **`_signal_cleanup` (fanout.py:912–928) removes every registered worktree then
   `os._exit`** — SIGTERM on a forge run would delete its deliverable. Needs a per-handle
   disposition, and a "keep" handle must **fsync run state before `os._exit`**, or SIGTERM
   leaves trees on disk with a manifest still reading `building` and a live lock. The
   re-entry guard and kill-members-first ordering are preserved verbatim.

`eval_harness.py` is already a second consumer importing `fanout` as a module and reusing
`build_real_spec` / `make_readonly` / `isolate_agy_worktree` / `run_council`
(eval_harness.py:236–262) — "two skills, one engine" is already load-bearing here.

---

## 18. Evals

Exempt from the LLM-judge delta gate: a read-only with-skill/baseline harness cannot exercise
forge's defining behaviour, and an ordinary judge receipt would certify prose while leaving
the dangerous mechanics untouched.

- Route through the existing **`DETERMINISTIC_GATED`** dict (eval_harness.py:65) used by the
  wiki skills — not a third `if skill == …` branch. **Note:** judge runs execute *before* the
  override (`gate_ok = True` at eval_harness.py:533), so routing makes the delta advisory, not
  the run free. The cost control is the cheap fixture eval set.
- **`evals/llm-forge/evals.json` must exist.** `checks._evald_skills()` (checks.py:261) globs
  `evals/*/` gated on that file, so without it the skill is invisible to `receipt_gate` and
  the exemption silently becomes *no gate at all*; `eval_set_hash` (checks.py:250) also does
  an unconditional `read_bytes()`, so the gate raises rather than warns. Populate it with
  cheap, seat-free evals grading instruction behaviour against a fixture repo.
- **Add the forge hermetic suite to `make verify` and `precommit`.** Today `verify` runs only
  `council-test`; full `fanout.py --self-test` is under `make test` (Makefile:74, 84), and
  `_write_receipt` invokes the self-test but does not enforce a live smoke
  (eval_harness.py:423).
- **Live three-provider write smoke** whenever adapter wiring changes, producing a
  source-hashed smoke receipt (adapter hash, CLI versions, provider results, timestamp) —
  otherwise "mandatory smoke" is a release note. A tiny disposable repo; each provider writes a
  distinct marker in its own clone and quotes the proof token; the engine harvests, runs a
  trivial verify, and demonstrates the original checkout is unchanged. Include a **180 s silent
  step** to probe the agy print-timeout wall — run only when provider/timeout wiring changes.

Hermetic coverage: baseline fidelity (staged/unstaged, binary, CRLF/non-UTF-8, mode changes,
deletion, rename, selected untracked, all four trees byte-identical, every fail-closed
rejection in §2.3) · pinned comparison under seat commits/resets/moved HEAD · the four-phase
inventory with origin labelling, setup-only `.venv`/`node_modules` exclusion, intentional
ignored file created/modified/deleted, verify-origin generator fixed point, `setup_overlap`,
symlinks not followed, caps failing closed, and the content-hash-not-lstat assertion · seat
states (0–3 completed, timeout-with-partial, empty, justified no-op, setup failure, verify
failure, malformed output, proof-token failure) · execution (parallel start, per-seat cwd/env,
teardown incl. grandchildren, timeout, SIGINT/SIGTERM, missing binary, permission error,
`E2BIG`, disk failure, one seat's exception not suppressing others) · **no seat launches with
`cwd=None`**, asserted by forcing mirror failure and checking zero processes started · retries
always on a fresh clone · verifier-clone independence (a seat that sabotages `.venv/bin/pytest`
must not produce a PASS) · `git remote` empty in every seat · journal idempotence, stale-lock
and `outcome_unknown` handling · fallback (infrastructure failure does not trigger
base-and-port; non-progress does; rubric followed; both-paths-fail preserves everything) ·
review (exact invocation, correct OID visible, ledger *not* passed, blocker transitions, no
clean claim after an unreviewed final fix) · handover (branch at the verified checkpoint,
patch path for a dirty baseline, out-of-band enumerated, provenance exact, failure cannot
render a success header) · safety invariants stated as **engine** invariants, never as proof
against a bypassed agent · packaging (source-tree and rendered-plugin imports, Python 3.11,
shared-core changes stale **both** receipts, zero/skipped tests fail the gate).

**SKILL.md states plainly:** the self-test gates wiring, not judgment.

---

## 19. agy's print-timeout

`fanout.py:722` — `pt = max(5, min(int(timeout) - 5, 120))`. In deep mode `timeout=1200`, so
`min(1195, 120) = 120`: **agy gets 120 s regardless of mode.** Measured live during round 1 of
this design's own review — claude 533 s, codex 876 s, agy dead at 124 s with zero tokens and
the panel degraded to 2 of 3. The engine classified it `agy_error` rather than `timeout`, so
even the reason was wrong.

Fix: parameterize the cap by run type (forge: `max(5, timeout - 5)`, no ceiling); add a
`forge` entry to `MODE_TIMEOUT` (≥3600); map agy's self-reported timeout wording to reason
`timeout` with structured provenance — safe because `run_provider` gates only on
`structured and reason in STRUCTURED_TERMINAL_REASONS` (fanout.py:1163–1166), and
`STRUCTURED_TERMINAL_REASONS = {"auth_or_quota"}` (fanout.py:190) — **with a comment at the
mapping site warning that adding `timeout` to that set would silently remove council's timeout
retries.**

**Probe all three adapters** over an hour of silent subprocess waits before shipping, not only
agy. The 120 s wall was agy-specific, but nothing establishes that claude's and codex's
streaming or JSON-output paths are healthy across that duration.

---

## 20. Portability resolution

Resolving a task into a portable instruction is a closure, not a body — a skill may reference
scripts, assets, templates, sibling skills, env vars, provider-specific tools or subagent
semantics, and byte-identical *source* in this repo does not prove the three *installed plugin
copies* are current and identical.

- Resolve and hash the live installed closures before relying on ambient skill availability;
  use a named skill only when all three hash identically and it is declared provider-neutral.
- Compute the referenced-resource closure; inline or attach required scripts and assets.
- Do **not** automatically translate provider-specific tools or subagent semantics.
- **Fail preflight** for irreducibly provider-specific workflows rather than pretending they
  are portable, and ask for a portable task bundle instead.
- When a bundle is supplied to all seats, bar ambient invocation of the same skill.
- Persist the fully resolved instruction plus resource hashes so `--collect` never depends on
  vanished conversation context.

---

## 21. Deferred by owner decision

Both round-2 seats recommended cutting these for v1. The owner chose the full design; they are
recorded so the trade-off remains visible, and they are the first things to drop if the build
proves too large.

| Candidate cut | What it would have saved | Cost of keeping it |
|---|---|---|
| Ignored-file harvesting entirely | the largest single deletion — skill-contract table, ignored deletion tracking, symlink-safe walk, most caps machinery, most of out-of-band handover | keeps `/chunk-map` working; keeps the most intricate untested subsystem |
| The retry path | worst case 16 → 10 runs | fresh-clone retry code, preserved partial attempts |
| Review round 2 | the state-machine back-edge, re-checkpoint loop, "verified but not reviewed" state | second-round findings are caught |
| Partitioned from-scratch above the size gate | the least-verified path in the design | needs seam claims (§12.2) to be sound |
| Conditional agreement labelling | ceremony — nothing downstream acts on the label | §11 keeps the per-seat fingerprints regardless |
| General skill-closure translation (§20) | "its own product" per round 2 | full portability for Claude-only skills |
| `--gc` / keep-last-N | quota + documented path would suffice | disk hygiene |

---

## 22. Open questions

1. **Who authors B₁** (§2.1) — the user's identity with an explicit message, or forge's?
   Belongs at the confirmation gate, not in code.
2. **Does the synthesis worktree diverge from the seats' clone environment?** It has config,
   hooks and `info/exclude` the clones do not. Resolved by §6's fresh verifier clones for
   *all* candidates, leaving the worktree purely as the handover surface — confirm that is the
   intended reading.
3. **May a baseline-red run ever produce a deliverable** under a fixed "verified" promise?
   Recommendation: no — allow analysis and synthesis, terminal state `degraded`.
4. **Windows.** This design assumes Linux/POSIX, Python 3.11+, one user account, no container
   isolation. Windows process groups, symlink behaviour and file modes need a separate profile.
5. **Peak memory.** Three agent CLIs plus three builds plus verifier clones on a WSL2 box with
   a default memory cap is not obviously fine and has not been measured.

---

## 23. Provenance

Two deep council rounds. Round 1: 2 of 3 seats (agy lost to §19's own bug). Round 2: 2 of 2.
Round 2's five blockers all arose from *interactions between round-1 fixes* rather than from
any single one — the structural signature of a panel that converged. Round 3 is required
before implementation; both seats stated the full-scope design needs it.

Round 2 also corrected two claims this document previously carried: `--self-test` is **not**
broken in the rendered plugin today (§17.3), and `render` produces no git diff when sources
are unchanged, so the churn is proportional to a seat's own edit amplified ~3× rather than a
blanket 188-file rewrite (§7.3 keeps the fix; the reasoning is corrected).
