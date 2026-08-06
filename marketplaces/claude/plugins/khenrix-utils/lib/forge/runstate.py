"""What the run agreed to do, where it got to, and the repository state it is judged against
(§9, §14, §14.1, §14.2).

The manifest and the snapshot live here together because they are recorded at the same instant
and are worth nothing apart. The manifest says what the run committed to — repo path,
`base_commit`, the baseline's identity, the selected paths, the confirmed steps. The snapshot
says what the user's repository looked like when it committed to that. A manifest without the
snapshot describes a deliverable with no way to ask whether it still applies; a snapshot
without the manifest is a photograph of nothing in particular.

The rest of this file is what a RESUME needs and neither of those carries: §14's phase tuple,
which says where the run had got to; the per-seat record, which says what each seat last
managed to write down; and the two reads that put a crashed run back together — `drift`, which
asks whether the snapshot still describes the repository, and `reconstruct`, which answers
that and everything above it out of the run directory, taking no fact from the caller that
the directory already holds. Each is described at the end of this docstring and implemented
at the end of the file.

WRITTEN ONCE. §14.2 requires the manifest be written at `confirmed` and never rewritten, so
commands are never re-detected. That is not a style rule about callers: `--collect` resuming
from disk and a run continuing in memory have to be the same code path, and a second write
would make them differ exactly when they must not — after a crash, where re-detection would
quietly substitute today's answer for the one the user approved. So the refusal is the file
system's, not a check this module performs: `storage.exclusive_write` links the name into
place and the kernel refuses a name that already exists.

READ BACK AS WHAT WAS WRITTEN. JSON has one sequence type, so every tuple returns as a list
and every argv would arrive as something that compares unequal to what was confirmed. Each
field is decoded explicitly, and a field added to `Manifest` without a decoder makes the next
read fail rather than arrive as whatever JSON happened to make of it.

WHAT THE SNAPSHOT IS. §9's protected list is the user's current branch ref, `HEAD`, the index
hash, the checkout files, all non-forge refs, REMOTES, and CONFIGURATION. Five of those seven
are recorded; the remaining pair is a §9 GAP rather than a boundary this file chose — written
down so the sentence and the code do not disagree:

  * REMOTES and CONFIGURATION. Nothing here reads `remote.*` or any other config key, so a
    seat that adds a remote or sets a repo-local value leaves no t0 fact to be judged
    against, and `drift` cannot speak for either.

The INDEX HASH is `index_digest`, and it is DERIVED rather than taken, because the file and
its meaning move for different reasons. Measured on git 2.53.0: `update-index --refresh` over
stale stat data rewrites `.git/index` — an ordinary `git status` does the same — and
`update-index --split-index` rewrites it again, while neither changes an index entry's tag,
mode, staged OID, stage or path. So `sha256(.git/index)` reports drift on a repository nobody
touched, on every run, which costs §9 the reports that mean something. `snapshot_index` hashes
`ls-files -v -s -z` instead: the same measurement says `update-index --assume-unchanged` moves
it (the `-v` tag goes `H` to `h`) and `--skip-worktree` moves it (`H` to `S`). That closes the
one class of index move the porcelain cannot see and that IS the user's work — without it, a
skip bit makes their own later edits to that path invisible for the life of the run. The index's
stat cache and extension records are deliberately outside it: they are a cache OF the worktree,
which `_carried_digest` already measures directly.

§9 also asks that forge's own refs be whitelisted by exact name AND the exact OID recorded at
creation, because a namespace whitelist lets a seat write into forge's own namespace invisibly.
That is `Manifest.forge_refs`, name to OID, and `_FORGE_REF_PREFIXES` is now a CEILING on what
a run may claim rather than the whitelist itself. Nothing is dropped for being under a forge
namespace: `refs/heads/forge/r99/impostor` is protected like any other ref, because forge never
declared it, and `drift` reports it as a ref that appeared.

The ORDERING that shape has to survive is that the ref is created after t0. `baseline.materialize`
makes `refs/khenrix-forge/<run_id>/base` with `update-ref <ref> <oid> <zero>`, and it is the only
forge ref written into the USER's repository at all — a seat's `refs/heads/forge/<run-id>/<name>`
is created in the seat's own clone by `fleet.clone_seat`, and nothing fetches it back. "Recorded
at creation" therefore means recorded from `materialize`'s own return value, and the manifest
cannot be written any earlier than that whatever a caller intends: `baseline_ref` and
`baseline_commit` sit beside this field and are that same return value, and §14.2 lets the
manifest be written once. So a declared name whose OID is empty is refused rather than treated
as not-yet-created — that spelling is the fail-open one, and it is what would let a seat create
the ref and have forge vouch for whatever it pointed at.

Why a seat's ref cannot pass as forge's own, in the three ways it could try. Creating a ref in
forge's namespace: not declared, so protected, so reported as new. Moving forge's declared ref:
the recorded OID is compared, so reported. Pre-empting the name before forge creates it: the
`<zero>` in `materialize`'s update-ref is a compare-and-swap against "must not already exist",
so creation fails loudly rather than adopting whatever was there.

THE CONTENT BEHIND `--assume-unchanged` is a second question from the index move above, and a
different answer: not a gap of this file's, but a floor it shares with B. Measured with the bit
set and the path clean, the user's own later edit moves neither the porcelain nor the digest,
and `baseline.materialize` does not carry that content into B either — with nothing else dirty
it never reaches `add` at all, since an empty porcelain makes `dirty` False and B HEAD itself,
and when a selection does make the run dirty `git add -u -- :/` exits ZERO and skips the path
on that same stat comparison. The user's own `git status`, `git diff` and `git stash` are
equally blind (`stash` answers "No local changes to save" and leaves the edit where it was).
Three ways out: a path that was DIRTY at t0 leaves the porcelain when the bit is set, which
moves the status digest; a SELECTED path stays in the content set whatever the porcelain says;
and the bit going ON during the run moves `index_digest`, which is what that field was added
for. The last covers only the bit set MID-RUN — a bit already set at t0 is recorded as set, and
the edit behind it stays this file's floor. The selected case is where the sharing with B stops
— `add -f` does not carry the hidden edit into B while the digest does see it, so the two
disagree in the direction that stops the run rather than the one that hands over.

`--skip-worktree` shares that floor, but the two bits are NOT INTERCHANGEABLE and nothing here
may treat them as one. Measured on git 2.53.0: the porcelain and the CARRIED digest cannot tell
them apart, and `index_digest`, `rejections` and `git add` can — the `ls-files -v` tag is `h`
for one and `S` for the other. `facts.sparse` is True under skip-worktree and
False under assume-unchanged, so §2.3's line fires for one bit and not the other. And
`git add -u -- :/`
exits ONE on a skip-worktree path — "paths ... exist outside of your sparse-checkout
definition", in ambient env, under `GIT_OPTIONAL_LOCKS=0`, and under `materialize`'s own env
with its alternate index — where the same command exits zero and skips on an assume-unchanged
one. So a DIRTY run raises `GitError` out of `materialize` under skip-worktree rather than
building a B that quietly lacks the content. What the two bits do share is the clean-tree
branch, where neither edit reaches the porcelain, and the two ways out above.

The reading it replaces was that preflight refuses the repository outright, and for a while
preflight refused nothing: it NAMED the condition — the index tag is `S`, `facts.sparse` is
True, `inspect.rejections` returns a line for it, and spec §2.3 lists skip-worktree state
among the conditions that fail closed before a run starts — while `rejections` was a policy
with zero consumers. `preflight.refusals` is that consumer now, so the original reading holds
again for any caller that runs preflight and obeys it.

A run entered BELOW preflight loses only what was edited AFTER the baseline was taken. An edit
already hidden at t0 does not get through: `baseline`'s manifest hashes the raw worktree bytes
while B's tree does not carry them, so `fleet.clone_seat` raises `SeatError: seat content
differs from the baseline manifest` — measured under both bits on a clean tree, and under
assume-unchanged with a selection as well. Under skip-worktree a dirty run never reaches a
seat at all, because `materialize` raises out of `add -u -- :/` before one is cloned. So
something downstream does stand in for the refusal; what it stands in with is a §4
infrastructure failure attributed to a seat, which is what preflight exists to replace, not a
reason to leave the refusal unwired. The post-t0 edit is the case nothing sees, and there the
bit is working as documented. The mid-run case remains the index-move class already covered.

What IS recorded is `protected_refs`, name-to-OID for every ref that is not forge's, and
`status_digest`, over the four parts of the checkout state that move independently:

  * the porcelain, which is a PATH/STATUS LISTING — it says which paths differ from `HEAD`
    and in which direction, and carries none of their content;
  * `HEAD`, because a clean tree, an edit and a commit leaves the porcelain byte for byte
    what it was — measured — so on the porcelain alone an entire commit of the user's work
    reads as no drift at all;
  * the branch `HEAD` points at, because switching between two branches at one commit moves
    the porcelain, `HEAD`'s OID and every ref not at all, and handover targets the branch that
    was current;
  * the CONTENT of the paths the run is carrying, because the listing above cannot see an
    edit to a path it already lists. Measured: a tree with `seed.txt` modified and `notes.txt`
    untracked-and-selected, rewritten in both, leaves the porcelain, the refs and the first
    three parts identical — and that is §9's own worked example, "you have since changed 4 of
    the files it touches", on the tree shape forge treats as normal.

WHAT THE RUN CARRIES, exactly, is what that fourth part ranges over: the tracked entries the
porcelain names, plus `selected_paths`. That is `baseline.materialize`'s domain read back —
`add -u -- :/` then `add -f` over the selection. Recomputing the tree OID is the other way to
ask the same question, and the two were measured against each other over thirteen cases: they
agree on twelve, and the one they differ on is a branch switch at one commit, which the tree
OID cannot see at all. What the recomputation costs is a write into the USER's object store on
every drift check — measured at +21 loose objects per call on a twenty-file dirty tree, one
blob each plus the tree, unreachable until git's two-week gc grace expires. Reading the paths
back writes nothing, so this is the read-only half of the same answer.

Two boundaries follow from that domain rather than being chosen. A tracked path whose content
moves while the porcelain does NOT list it is one git itself calls clean, so `materialize`
does not carry it into B either — by the clean early return under `--assume-unchanged`, and
under `--skip-worktree` by that same return or by `add -u -- :/` failing outright when a
selection makes the tree dirty. Three outcomes, no fourth in which B holds the edit: neither
method sees it, and neither can. An untracked
path nobody selected is deliberately outside it: no tree forge writes contains it, so no merge
of forge's work can revert it, and hashing it would put every stray editor swap file and test
log into a drift report whose only value is that it means something.

Ignored paths follow from that same rule rather than being a separate gap. `git status` does
not list them without `--ignored`, which stays off: a repository with a watcher or a dev
server rewrites ignored build output continuously, and a digest that moved on all of it would
make the one drift report that means something unreadable. An ignored path the run IS carrying
is covered anyway, because selection is what puts a path in the content set — so the case that
was open here, a caller hand-selecting an ignored path, is closed by the selection it made.

WHERE THE RUN GOT TO is five dimensions and not one. §14: `(phase, round, attempt,
verified_checkpoint, deliverable_checkpoint)`, "separate dimensions, with the
`reviewing → synthesizing` back-edge declared. A single enum cannot represent 'fixing after
review round 2.'" The phase reads `synthesizing` for a first synthesis and again for a fix
after the second review round; what tells those apart is a counter the phase does not move.
So `advance` moves the phase and NOTHING else, and each of the other four has its own reason
for not riding along: a `round` incremented on the back-edge would spend a council round §14.2
says is never spent automatically, an `attempt` reset would lose the retry count on the one
operation being retried, and either checkpoint cleared on the way back to `synthesizing` would
drop a commit §14.2 depends on having — the last verified one is what it keeps as the
deliverable when a resumed fix fails to verify.

WHAT A SEAT LAST SAID has one rule, and §14.1 states it as the thing the whole discipline is
for: a SIGTERM landing mid-rewrite of `seat-codex.json` "must not leave truncated JSON
indistinguishable from a seat that never wrote". Distinguishability is what §14.1 puts in
place of an exactly-once it opens by conceding is not deliverable, so `read_seat` answers None
for exactly ONE condition — no such file — and raises for every other way a record can fail to
be one. Three routes reach None, or an answer a caller cannot tell from it, without damaging
a byte — and they are the reason the rule is written as a whitelist: a JSON `null` parses cleanly and hands back the same object a
missing file would, an unreadable record is an OSError that a blanket `except OSError` would
answer with the same silence, and an empty record is falsy exactly as None is — so the
shortest question a caller can ask, `if read_seat(...)`, gets the wrong answer from a seat
that did write. The last of those is refused at BOTH ends, because the reader meets records
this writer did not write.

WHAT `--collect` READS is the run directory, and §14 makes that a requirement rather than an
implementation detail: resuming "always from disk and never from conversation state ... makes
compaction and restart indistinguishable, one code path instead of two". A reconstruction that
consulted anything held in memory would be two code paths, and the one that runs after a crash
is the one nobody tested. So `reconstruct` opens the manifest, the run's own position, every
seat record and the journal, and asks `drift` what has moved. The repository it is handed is
the one thing in that list a caller supplies rather than the disk, and the manifest records it
too — so `drift` refuses one that is not the recorded repository instead of comparing the
snapshot against a stranger. Three ordinary ABSENCES are states rather than errors — a journal
with no records, a run that has not moved since `confirmed`, and a seat that never wrote — and
everything else propagates in the vocabulary of whichever reader met it, because a resume that
swallowed a damaged record would carry on from a position nobody recorded.

WHAT DRIFT IS. §9 protects the checkout and "all non-forge refs", and says a protected branch
or the checkout moving during the run transitions to `source_diverged` and does NOT continue
to handover automatically — because "a clean merge can silently revert the user's own
subsequent work on any hunk forge also touched". A ref whose OID moved is the obvious case.
Two more were measured, and each is invisible to every other fact this file records: deleting
a tag leaves every surviving ref and the whole checkout digest byte for byte where they were,
and so does a branch the user creates while the run is out. So a ref that DISAPPEARED and a
ref that is NEW since t0 are drift as much as one that moved. The new-ref case is where a
namespace whitelist would have shown: with forge's refs dropped from both sides, forge's own
baseline could not arrive as a ref the user made — and neither could a seat's write into that
namespace. Both sides now drop only the DECLARED names, so the first is still silent and the
second is not.

Naming it is where this file stops. `reconstruct` reports what moved; §9's transition and its
refusal to hand over are decisions, and they belong to whoever is sequencing the run — which
is `runner.run`, and it takes both: it advances to `source_diverged` and then refuses to
carry on. §14 declares `source_diverged` out of every non-terminal phase, so that sequencer
makes the transition from wherever the run had got to when the move was seen, rather than
having to carry the fact as far as `reviewing` first — see `_EDGES`.
"""
import dataclasses
import hashlib
import json
import os
import stat
from dataclasses import dataclass

from . import bundle as bundlemod
from . import gitcmd, journal, storage
from .inspect import GeneratorContract
from .verify import Step, VerifyError


class ManifestError(RuntimeError):
    """A run's identity that cannot be recorded, cannot be recovered, or would be rewritten."""


class StateError(RuntimeError):
    """A seat's record that is damaged, unreadable, or written in a shape its reader could not
    tell from a seat that never wrote at all."""


class TransitionError(RuntimeError):
    """A move §14's graph does not declare."""


# §9's two explicitly-allowed namespaces, read as a CEILING on what may be whitelisted rather
# than as the whitelist. Nothing is excluded for being under one of these: a run excludes the
# exact names it declares, and this pair only says which names it is allowed to declare. A
# caller that named `refs/heads/main` as one of its own refs would drop the user's branch out
# of `protected_refs` entirely, which is the largest hole in this file reachable by one wrong
# argument.
#
# Prefixes rather than a substring test: a user's `refs/heads/forgery-experiments` is theirs.
_FORGE_REF_PREFIXES = ("refs/khenrix-forge/", "refs/heads/forge/")


@dataclass(frozen=True)
class Manifest:
    """The run's identity, as agreed at the §5 gate.

    `setup` and `verify` are sequences of `verify.Step`, which is §5.1's step record whole —
    `{argv, cwd, env, timeout}`. All four, not argv alone: §5.1's motivating sentence is that
    "real monorepos need several steps with different cwds", so a manifest that recorded the
    argv and left the rest to a resume would hand `--collect` a `cwd` of "" and a timeout of
    600 SUPPLIED BY THE READER rather than agreed by the user — this file's own failure mode,
    three fields per step instead of one field per manifest. The type is `verify`'s own rather
    than a record this module invents, so there is no conversion between what is written and
    what the gate runs for a field to go missing in.

    `generator_contract` holds `inspect.GeneratorContract` for the same reason and it is the
    stronger case, because this field decides which verify-origin rewrites a candidate is
    allowed to have made and `verify.fixed_point` compares its `id` against the candidate's.
    Carried as a free-form dict it had exactly the conversion the paragraph above rules out,
    in all three directions: the natural `dataclasses.asdict(contract)` was REFUSED by the
    round-trip check because `relations` is a tuple of tuples; the hand conversion callers
    were pushed into read back as lists of lists, and rebuilding the obvious way gave a tuple
    of LISTS that `GeneratorContract.__post_init__` accepts in silence and that compares
    unequal to the contract confirmed at the gate; and any dict at all was a valid contract,
    so a manifest recording `{"totally": "unrelated"}` round-tripped clean.

    Never a string, at any level: §5.1 rejects shell metacharacter syntax rather than
    reinterpreting it, and a manifest holding `"cd frontend && npm ci"` would hand a resume a
    command it has to re-parse — which is re-detection under another name, at the one moment
    nobody is watching.

    `protected_refs`, `status_digest` and `index_digest` are the t0 snapshot: what the user's
    repository looked like at the moment of agreement, kept so `drift` can ask whether it still
    does. `selected_paths` is an input to the second of those as well as a record — which is
    why `drift` re-derives the digest from THIS field and not from an argument. See
    `snapshot_refs` and `snapshot_index`.

    `forge_refs` is §9's whitelist and the only field whose effect is to REMOVE something from
    protection, so it is the one decoded most strictly. Name to the OID forge created the ref
    at — never a name alone, because a name alone is a namespace whitelist one entry wide and
    a seat moving the ref afterwards would pass. Every key must lie under THIS run's own
    `refs/khenrix-forge/<run_id>/` or `refs/heads/forge/<run_id>/`, and every value must be a
    hex OID: an empty one would mean "declared but not yet created", which is the shape that
    would let a seat create the ref and have forge vouch for whatever it pointed at.

    `repo_path` says WHICH repository those describe, and is read for that: `drift` refuses a
    repository that is not this one, so the snapshot is never compared against a copy that
    would answer "nothing moved" on the run's behalf.

    `seats` and `attempts` are the run's SHAPE, and they are here because §5.2 prices the run
    by them and §5 step 5 forbids asking again — a launcher that read a seat count from its
    own default would build a fleet the operator never agreed to, at a cost the quote never
    showed. They are the parameter §5.2's disk figure is answered "by parameter, not by
    shrinking" with, so a manifest without them records the agreement and drops the number the
    agreement was about. Neither is derivable from the rest: a run harvested down to two
    surviving seats is indistinguishable from a two-seat run once the third's directory is
    gone. `gate.confirm` takes both off the `Quote` that was shown rather than from a second
    argument, so the priced number and the recorded number are the same number.

    `review_rounds` and `synthesis_fix_cap` are the same agreement for §12.3's post-review
    loop, and the cap is its OWN field rather than a second reading of `attempts`: `attempts`
    is the builder budget `quote` multiplies by `seats`, so spending it on synthesis fixes
    would re-price a 3-attempt run as 9 and leave a resume unable to say which budget a
    number was drawn against. Both are floored at zero rather than at one — a run that priced
    no review rounds is a run, and its cap is 0 rather than a field nobody wrote.
    """
    run_id: str
    repo_path: str
    base_commit: str
    baseline_ref: str
    baseline_commit: str
    tracked_tree_oid: str
    selected_paths: tuple[str, ...]
    generator_contract: GeneratorContract
    setup: tuple[Step, ...]
    verify: tuple[Step, ...]
    protected_refs: dict
    forge_refs: dict
    status_digest: str
    index_digest: str
    created_at: str
    seats: int
    attempts: int
    review_rounds: int
    synthesis_fix_cap: int


def write_manifest(run_dir, manifest: Manifest) -> None:
    """Record the run's identity, once. Raises ManifestError if one is already recorded."""
    if not isinstance(manifest, Manifest):
        raise ManifestError(f"a manifest is required, not {type(manifest).__name__}")
    path = storage.manifest_path(run_dir)
    row = dataclasses.asdict(manifest)
    try:
        # sort_keys so one run identity has one spelling on disk, whatever order the fields
        # were declared in; indented because the first reader of this file is usually a human
        # working out what a crashed run had agreed to.
        blob = json.dumps(row, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as e:
        raise ManifestError(f"the manifest carries a value json cannot serialize: {e}") from e
    restored = _decode(json.loads(blob), path)
    if restored != manifest:
        # The check JSON's own types cannot make for the caller. A sequence field handed in as
        # a list where the type says tuple serializes happily and reads back a list, after
        # which the manifest on disk stops equalling the one in memory and nothing says so
        # until a resume hours later compares them. The differing fields are named because "it
        # does not round trip" sends the caller looking through every field there is.
        differing = [f.name for f in dataclasses.fields(Manifest)
                     if getattr(restored, f.name) != getattr(manifest, f.name)]
        raise ManifestError(
            f"this manifest does not survive its own round trip; {differing} come back as a "
            "different type. JSON has one sequence type: pass the declared tuples as tuples.")
    try:
        storage.exclusive_write(path, blob)
    except FileExistsError as e:
        raise ManifestError(
            f"{path} already records this run's identity and is never rewritten (§14.2): "
            "the commands, the baseline and the selected paths were agreed once, and "
            "re-recording them would let a resume proceed on facts nobody approved.") from e


def read_manifest(run_dir) -> Manifest:
    """The run's identity as recorded. Raises ManifestError if it is absent or unreadable."""
    path = storage.manifest_path(run_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise ManifestError(
            f"{path} does not exist: this run never reached `confirmed`, so there is no "
            "record of what it agreed to do and nothing to resume from.") from e
    try:
        row = json.loads(raw)
    except ValueError as e:
        raise ManifestError(f"{path} is not readable as JSON: {e}") from e
    return _decode(row, path)


def _declared(forge_refs) -> frozenset:
    """The exact ref names a run says are its own, refusing any this run may not claim.

    A `str` is refused before anything else on `_texts`' argument, one level up from where it
    makes it: `frozenset("refs/heads/x")` is nine single-character ref names, none of which
    matches anything, so the whole declaration would silently do nothing and every forge ref
    would read as the user's.

    The ceiling is checked HERE rather than only in the decoder because this is the surface
    that removes refs from protection, and it is reached by callers that never wrote a
    manifest — the §5 gate takes the t0 snapshot before there is one to read back.

    The RUN-ID half of §9's whitelist is the decoder's, not this function's: a `Manifest`
    built by hand with another run's forge ref passes here and is caught at `_decode`. That
    split is deliberate — every manifest that reached disk went through the decoder — but it
    means this function enforces the namespace ceiling alone.
    """
    if isinstance(forge_refs, (str, bytes)):
        raise ManifestError(
            f"forge_refs is {forge_refs!r}, a single string; iterating one yields its "
            "characters, so the declaration would match no ref and quietly protect nothing")
    names = frozenset(forge_refs)
    outside = sorted(n for n in names if not str(n).startswith(_FORGE_REF_PREFIXES))
    if outside:
        raise ManifestError(
            f"{outside} cannot be declared as forge's own refs: §9 allows exactly "
            f"{list(_FORGE_REF_PREFIXES)}, and a name outside them would be dropped from "
            "`protected_refs` — so a write to it would be invisible for the life of the run.")
    return names


def _show_ref(repo) -> dict:
    """Every ref in `repo`, name to OID. `HEAD` is not among them — `show-ref` lists it only
    under `--head`, and `_head` reads it separately because an unborn HEAD is a state rather
    than a failure."""
    # `git show-ref` exits 1 in a repository that has no refs at all, which is a fact about
    # the repository rather than a failure of the call.
    listed = gitcmd.git(repo, "show-ref", env_extra=gitcmd.READONLY, check=False)
    refs = {}
    for line in listed.stdout.splitlines():
        oid, _, name = line.partition(" ")
        if name:
            refs[name] = oid
    return refs


def _partition(repo, selected_paths, forge_refs) -> tuple[dict, dict, str]:
    """`(protected, forge_present, status_digest)` from ONE ref listing.

    One listing rather than two because `drift` reads both halves and a second `show-ref`
    would sample a later repository: a ref moved between the two reads would be missing from
    one answer and present in the other, and the report would describe a state that existed at
    neither instant.

    The `forge_present` filter is EQUIVALENT to returning the whole listing, and no test can
    pin it — `drift` only ever looks up names it declared, and those carry the same OID either
    way. It is here so the returned value matches its name, because the next reader to iterate
    it would otherwise be iterating the user's refs under the word "forge".
    """
    declared = _declared(forge_refs)
    listed = _show_ref(repo)
    protected = {n: o for n, o in listed.items() if n not in declared}
    forge_present = {n: o for n, o in listed.items() if n in declared}
    head = _head(repo)
    if head:
        protected["HEAD"] = head
    return protected, forge_present, _status_digest(repo, head, selected_paths)


def snapshot_refs(repo, selected_paths, *, forge_refs=()) -> tuple[dict, str]:
    """`(protected_refs, status_digest)` for `repo` as it is right now.

    `forge_refs` is the EXACT NAMES this run created in the user's repository, and everything
    not in that set is protected — including a name under §9's own namespaces that this run
    never made. That is the difference between §9's whitelist and the namespace whitelist it
    warns about: `refs/heads/forge/r99/impostor` is not forge's ref because forge never
    declared it, so it lands in `protected_refs` and `drift` reports it as a ref that appeared.
    The OIDs are the caller's to record, because only the caller knows what it created the ref
    AT — see `Manifest.forge_refs`, which is what `drift` compares against.

    Empty by default, and that default is the SAFE one for once: a caller that forgets it
    protects forge's own refs and reports its own baseline as drift, loudly, on the first
    check. `selected_paths` is REQUIRED rather than defaulted for the opposite reason — on
    `verify.build_verifier`'s argument for its `contract`, a default there is a policy, and the
    policy is the one a human confirmed at the §5 gate. A caller that has selected nothing
    passes `()`, which is a statement that the run carries no untracked path — and a caller
    that HAS selected something and forgets is the fail-open case the fourth digest part exists
    to close.

    Every call is read-only against the USER's repository, which is why `READONLY` is on all
    of them rather than on the status call alone: measured on git 2.53, a plain `git status`
    over a tracked file whose stat data had gone stale rewrote `.git/index`, and the same
    command under `GIT_OPTIONAL_LOCKS=0` left it byte for byte alone. §9 protects the index
    hash, so a snapshot that refreshed it would be forge moving the very thing it is here to
    watch — once at t0 and again at every drift check.
    """
    protected, _, digest = _partition(repo, selected_paths, forge_refs)
    return protected, digest


def snapshot_index(repo) -> str:
    """sha256 over what the index MEANS — §9's protected "index hash", derived rather than
    taken.

    NOT `sha256(.git/index)`, and the difference is the whole of this function. Measured on git
    2.53.0, over `git update-index --refresh` on an index whose stat data had gone stale: the
    file's bytes MOVED and this digest did not. A raw file hash would therefore report drift on
    a repository nobody touched — every `git status` anyone ran while the run was out — and a
    drift check that fires on every run is one people edit around, which costs §9 the case it
    exists for. The same measurement over `update-index --split-index` and back says the same:
    the file moves, the meaning does not.

    What it IS: `ls-files -v -s -z`, which is one record per index entry — the `-v` tag
    (`H` cached, `h` assume-unchanged, `S` skip-worktree, `M` unmerged), the mode, the staged
    OID, the stage number and the path. Measured on the same git: `update-index
    --assume-unchanged` moves it (`H` to `h`) and `--skip-worktree` moves it (`H` to `S`), so
    the class the module docstring used to name as UNCOVERED is covered — a per-path skip bit
    is the user's own work, and without this digest their later edits to that path stay
    invisible for the life of the run.

    What it is NOT is the index's stat cache and its extension records. Those are a cache OF
    the worktree, and the worktree is `_carried_digest`'s question; hashing them here would
    answer that question a second time and worse, in the always-fires direction.

    `--full-name -- :/` is not tidiness: `ls-files` defaults to the CWD's subtree, so from a
    subdirectory — which `_status_digest` accepts and answers for the whole repository from —
    the bare command lists that subdirectory alone, and a t0 taken at the root would then be
    compared against a digest over a fraction of the index. `GIT_LITERAL_PATHSPECS` is pinned
    OFF for `baseline.materialize`'s reason, and here it is worse than there: measured, an
    ambient `1` makes `-- :/` a directory name that matches nothing and `ls-files` exits ZERO
    with EMPTY output, so the digest would be the digest of nothing and would never move again.

    Read as BYTES: a repository may hold a path that is not valid UTF-8, and a drift check that
    raises on decoding is a drift check that does not run. The records are self-delimiting —
    fixed-width tag, mode, OID and stage before a tab, then the path, then NUL — so no framing
    is added on top.

    `-z` carries a second reason, and it is `--no-renames`' reason one digest over. Without it
    git C-QUOTES the path, and the quoting obeys `core.quotePath` — a DISPLAY preference, set
    repo-locally, so it survives the /dev/null pins and a seat can flip it. Measured: flipping
    it rewrote `"caf\\303\\251.txt"` to `café.txt` and moved a `-z`-less digest while no index
    entry changed, which is a drift report about the user's work caused by nobody's work.
    """
    listing = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "ls-files", "-v", "-s", "-z",
                         "--full-name", "--", ":/",
                         env_extra={**gitcmd.READONLY, "GIT_LITERAL_PATHSPECS": "0"},
                         binary=True).stdout
    return hashlib.sha256(listing).hexdigest()


def _head(repo) -> str:
    """HEAD's OID, or "" for an unborn HEAD — an ordinary state of a fresh repository, which
    preflight rejects with a sentence a user can act on. Raising here would replace that
    sentence with a git stderr dump."""
    r = gitcmd.git(repo, "rev-parse", "--verify", "HEAD", env_extra=gitcmd.READONLY,
                   check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def _status_digest(repo, head: str, selected_paths) -> str:
    """sha256 over the four parts of the checkout state that move independently.

    Read as BYTES. Under `-z` a path is git's raw bytes with no quoting, and a repository is
    allowed to hold a path that is not valid UTF-8; decoding to compute a digest would raise
    on the one repository that most needs a drift check to work.

    Each part is fed in with its name and its LENGTH, and that framing is still defence in
    depth rather than load-bearing: the porcelain is the only part that may contain a NUL and
    is empty or NUL-terminated, `head` and the content digest are fixed-width hex or empty, and
    a ref name is empty or begins `refs/`, so the boundaries stay recoverable from the ends
    inward without it. What the framing buys is that the argument stops having to be re-derived
    over the value domains of whichever parts happen to be here — it took three steps for three
    parts and four for four, and a fifth with none of those properties would reopen it silently.
    """
    # The porcelain's paths are REPOSITORY-ROOT-relative even when git runs in a subdirectory
    # — measured — while `repo` only has to name the repository. Joining content paths onto
    # the argument would read them from the wrong directory whenever a caller passes a
    # subdirectory, which is the same slip `baseline.materialize` resolves `facts.root` for.
    # Binary, because a repository is allowed to live under a path that is not valid UTF-8 and
    # a text-mode read of it raises before any digest exists.
    root = gitcmd.git(repo, "rev-parse", "--show-toplevel", env_extra=gitcmd.READONLY,
                      binary=True).stdout.strip()
    # NO_HOOKS although READONLY is what already stops this: `GIT_OPTIONAL_LOCKS=0` keeps the
    # index from being rewritten, and `post-index-change` only fires on a rewrite (measured,
    # both ways). This runs in the USER's repository, where firing their hooks for forge's
    # bookkeeping is the thing the decision refuses, so the suppression is not left resting on
    # a second argument that a later editor of `env_extra` would not know they were carrying.
    porcelain = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                           "status", "--porcelain=v1", "-z",
                           "--untracked-files=all", "--no-renames",
                           env_extra=gitcmd.READONLY, binary=True).stdout
    # `--untracked-files=all` is required, not tuning: git's default collapses an untracked
    # directory to a single `?? dir/` record, so a file appearing inside one leaves the
    # porcelain identical — measured — and untracked paths are selectable into the baseline.
    #
    # `--no-renames` carries BOTH of the reasons this package has for it. `status.renames` is
    # repo-local config, so it survives the /dev/null pins on the global and system files;
    # measured, flipping it rewrote an unchanged staged rename from `R renamed\0big` to
    # `D big\0A renamed`, and inheriting it would let a display preference the user changed
    # report as drift in their work. And `_carried_digest` PARSES these records — which this
    # call did not do before it existed, and which is `inspect.repo_facts`'s reason — so a
    # rename record's second NUL-separated path would be read as a status code plus a path.
    branch = gitcmd.git(repo, "symbolic-ref", "-q", "HEAD", env_extra=gitcmd.READONLY,
                        check=False).stdout.strip()   # "" when HEAD is detached
    h = hashlib.sha256()
    for label, value in (("porcelain", porcelain), ("head", head.encode()),
                         ("branch", branch.encode()),
                         ("carried", _carried_digest(root, porcelain, selected_paths))):
        h.update(f"{label}:{len(value)}:".encode("utf-8"))
        h.update(value)
    return h.hexdigest()


def _carried_digest(root: bytes, porcelain: bytes, selected_paths) -> bytes:
    """The content identity of every path the run is carrying, as one fixed-width digest.

    The set is the porcelain's TRACKED entries plus the selection, which is
    `baseline.materialize`'s `add -u -- :/` plus its `add -f` over `selected_untracked` read
    back — see the module docstring for why that is the right domain and what a recomputed
    tree OID would cost instead. `??` is dropped because forge carries no unselected untracked
    path; `!!` cannot appear without `--ignored` and is dropped for the same reason it would
    be if it could.

    Paths stay BYTES from git's output to `os.lstat`, never decoded: a repository may hold a
    path that is not valid UTF-8, and a drift check that raises is a drift check that does not
    run. `selected_paths` are `str` on both call paths — the caller's at the confirmation
    gate, and a manifest's, where JSON has only text — so `os.fsencode` puts them in the same
    alphabet.

    AND ONE OF THOSE TWO CALL PATHS IS NOT TRUSTED, which is why `_path_digest` is handed the
    root and the relative path rather than the join of them. The porcelain half is git's own
    output and can be neither absolute nor `..`, and git tracks no path THROUGH a symlink; the
    manifest half is `Manifest.selected_paths`, which `_texts` type-checks and nothing else.
    `screen.py`'s guard covers the OPERATOR route into this selection, at the §5 gate; it is
    not on the decoded route, and §8.1's model is a record edited between runs. Measured
    through `write_manifest` -> `read_manifest` -> `drift`, all three reaching a read outside
    the repository: `../../outside/secret.txt`, an absolute path, and `link/secret.txt` where
    `link` is an in-repo symlink. The last is the one no lexical rule sees, and a selected
    DIRECTORY compounds it — `_dir_digest` would then walk a whole host subtree.

    The length framing here is LOAD-BEARING, unlike `_status_digest`'s over the four parts. A
    path may contain anything but NUL, including the text of another entry's digest, so
    unframed `"a" + digest(a) + "b" + digest(b)` is byte for byte what one path literally named
    `a<digest(a)>b` produces when its own content digests to `digest(b)` — constructed, not
    hypothesised, and pinned by a test. Framing the digest as one value outside cannot help:
    the collision has already happened by the time that framing is applied.
    """
    paths = {rec[3:] for rec in porcelain.split(b"\0")
             if rec and rec[:2] not in (b"??", b"!!")}
    paths |= {os.fsencode(p) for p in selected_paths}
    h = hashlib.sha256()
    for rel in sorted(paths):
        value = _path_digest(root, rel)
        for part in (rel, value):
            h.update(b"%d:" % len(part))
            h.update(part)
    return h.hexdigest().encode("ascii")


def _path_digest(root: bytes, rel: bytes) -> bytes:
    """One carried path's content identity — total over every shape the OS will answer about,
    and raising for none of THOSE.

    THE ROOT AND THE RELATIVE PATH, NEVER THE JOIN. `os.path.join(root, rel)` hands the kernel
    a name assembled from a record §8.1 does not trust, and every branch below then describes
    whatever that name resolved to. `bundle.contained` descends one component at a time with
    `O_NOFOLLOW` against the previous component's descriptor, so the leaf operations run at
    `dir_fd=` and there is no name left to redirect — see `_carried_digest` for the three
    routes this was measured on.

    AN ESCAPE IS AN ANSWER, on the same argument the OSError arm makes below, and a DISTINCT
    one. `uncontained` is not `error:2`: a path that left the tree and a path that is missing
    are different facts about the record, and giving them one value would let either become
    the other without moving the digest. It is stable across checks by construction — the
    refusal is a property of the string — which is exactly what makes it safe to answer with
    rather than raise: a forged selection reads as itself at t0 and at every check after.

    The totality stops at paths the OS refuses to be asked about, and deliberately. A NUL in a
    `selected_paths` entry survives `write_manifest`/`read_manifest` intact and raises
    `ValueError: embedded null character` out of the descent before any of the branches below
    run — measured, and it escapes `drift` and `reconstruct`. Catching it here would be worse
    than the crash: the value returned would be identical at t0 and at every later check, so a
    selected path that CANNOT exist would read as "nothing moved" for the life of the run.
    That is the fail-open direction of the one question this digest exists to answer.

    NEVER READ THROUGH A LINK, and never OPEN what is not a regular file. A symlink is its
    target TEXT on `baseline._sha256_link`'s argument: following it would put content from
    outside the tree into a measurement that claims to describe the tree. A FIFO is its file
    TYPE on `verify._surface_state`'s: a read-open on one blocks until a writer appears, and
    there is no timeout anywhere in this call path.

    The executable BIT rides along with a regular file's bytes because that is the only part
    of the mode git records, so a `chmod +x` on a carried script is work a merge could revert.
    The rest of the mode is left out deliberately: git ignores it, so hashing it would report
    a umask difference as drift in the user's work.

    An OSError is an ANSWER, not an exception: this runs at t0 and again at every drift check,
    and one raised here would take the whole check with it — on the repository least able to
    spare it. Every one contributes its ERRNO rather than a shared "missing" value, so a
    carried file the user deleted (ENOENT) and one they made unreadable (EACCES) stay different
    answers; collapsing them would let either become the other without moving the digest.
    """
    try:
        at = bundlemod.contained(root, rel, "a carried path")
    except bundlemod.BundleError:
        return b"uncontained"
    with at:
        return _digest_at(at.fd, at.leaf)


def _digest_at(fd: int, name: bytes) -> bytes:
    """`_path_digest`'s branches, over a leaf NAME and the descriptor of the directory holding
    it — the shape both callers already have.

    Separate from `_path_digest` because `_dir_digest` recurses into it with the descriptor
    `os.fwalk` hands back, and re-joining a path there to call the descent again would undo
    the descent for every entry underneath a selected directory.
    """
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except OSError as e:
        return b"error:%d" % e.errno
    try:
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(name, dir_fd=fd)
            return b"link:" + hashlib.sha256(target).hexdigest().encode("ascii")
        if stat.S_ISDIR(st.st_mode):
            return _dir_digest(fd, name)
        if stat.S_ISREG(st.st_mode):
            # `O_NOFOLLOW` because the `stat` above and this open are two syscalls, and
            # `O_NONBLOCK` for `bundle.open_leaf`'s reason: a read-open on a FIFO that
            # replaced this name in between would block with no timeout in the call path.
            dfd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                h = hashlib.sha256()
                while chunk := os.read(dfd, 1 << 16):
                    h.update(chunk)
            finally:
                os.close(dfd)
            return b"file:%d:" % bool(st.st_mode & 0o111) + h.hexdigest().encode("ascii")
    except OSError as e:
        return b"error:%d" % e.errno
    return b"special:%d" % stat.S_IFMT(st.st_mode)


def _dir_digest(fd: int, name: bytes) -> bytes:
    """A selected DIRECTORY's contents. §2.2 contemplates selecting one explicitly, and
    `baseline.materialize`'s literal pathspec sweeps its whole contents into B — so a digest
    that stopped at the directory itself would be blind to every file the run is carrying
    inside it, which was measured against a recomputed tree OID and is why this exists.

    `baseline._walk_selected`'s rules, for its reasons: `.git` is pruned rather than
    post-filtered, and a linked directory is reported by its own target text rather than
    descended into. A directory that cannot be listed contributes its errno rather than
    nothing, because the walk's default is to swallow the error and yield an empty tree —
    which would make an unreadable directory digest the same as an empty one.

    `os.fwalk` RATHER THAN `os.walk`, and it is the half of the escape a fix at the caller
    alone would have missed. This used to be handed a joined path and re-join one for every
    leaf underneath it, so a selected directory that escaped once escaped for its whole
    subtree — the digest then described an entire HOST tree. `fwalk` walks relative to a
    descriptor and hands back the descriptor of each directory it is inside, so every leaf
    below is named at a `dir_fd=` that descends from the one already proven contained.
    """
    h = hashlib.sha256()
    failures = []
    for dirpath, dirnames, filenames, dirfd in os.fwalk(
            name, dir_fd=fd, follow_symlinks=False, onerror=failures.append):
        dirnames[:] = sorted(n for n in dirnames if n != b".git")
        leaves = [n for n in list(dirnames)
                  if stat.S_ISLNK(os.stat(n, dir_fd=dirfd,
                                          follow_symlinks=False).st_mode)]
        for n in leaves:
            dirnames.remove(n)
        for leaf in leaves + sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, leaf), name)
            for part in (rel, _digest_at(dirfd, leaf)):
                h.update(b"%d:" % len(part))
                h.update(part)
    for e in failures:
        h.update(b"error:%d:" % (e.errno or 0))
    return b"dir:" + h.hexdigest().encode("ascii")


def _text(name, value, source):
    if not isinstance(value, str):
        raise ManifestError(f"{source}: {name} must be a string, not {value!r}")
    return value


def count(name, value, source, *, floor=1):
    """A whole count of at least `floor` — the shape a run's own numbers have to survive in.

    PUBLIC, AND CALLED RATHER THAN RE-SPELLED, which is the whole of what it is for. `gate`
    decides this about the same two fields at two earlier doors, and while it spelled its own
    version the two disagreed: the gate tested magnitude, this tested magnitude AND type, and
    `seats=True` therefore passed `quote`, passed `confirm`, opened a run directory, wrote
    `events.jsonl` and created a ref in the USER's repository before meeting this floor inside
    `write_manifest`. A run that will not happen must leave nothing behind, and two spellings
    of one predicate is how it left three things.

    `isinstance(True, int)`, so the bool test is not defensiveness: a JSON `true` would launch
    a one-seat fleet out of a field nobody wrote a number in. The floor is checked HERE and
    not only at the gate because `write_manifest` decodes what it is about to write, so a
    zero-seat run cannot reach the disk from either direction.

    `floor` is a parameter because `gate.quote` prices a THIRD number with the same type rule
    and a different bound: zero review rounds is a run, zero seats is not. It is deliberately
    not a second function — the type half is where every one of these values got in.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < floor:
        raise ManifestError(
            f"{source}: {name} is a whole count of at least {floor}, not {value!r}. The type "
            "is checked with the magnitude because `isinstance(True, int)` — a boolean would "
            'launch a fleet out of a field nobody wrote a number in, and `2.0` and `"3"` read '
            "as the right number to everything downstream that never compares them.")
    return value


def _texts(name, value, source):
    # The list check is not redundant with the element check below: a string iterates into
    # its characters, so `tuple("scratch")` yields a seven-element path list without a word
    # of complaint from anything downstream.
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{source}: {name} must be a list of strings, not {value!r}")
    return tuple(value)


# §5.1's step record, whole. Named here rather than derived from `verify.Step`'s fields so a
# field added there cannot start arriving in manifests this reader silently accepts — the same
# argument the unknown-field refusal below makes one level up.
_STEP_FIELDS = ("argv", "cwd", "env", "timeout")


def _steps(name, value, source):
    if not isinstance(value, list):
        raise ManifestError(f"{source}: {name} must be a list of step records, not {value!r}")
    steps = []
    for i, row in enumerate(value):
        where = f"{source}: {name}[{i}]"
        if not isinstance(row, dict):
            raise ManifestError(
                f"{where} is {row!r}, not a step record. §5.1's step is "
                "{argv, cwd, env, timeout} — an argv on its own leaves the cwd, the "
                "environment and the timeout for whoever resumes to supply, and a field the "
                "reader supplies is a fact the run never agreed to.")
        missing = [f for f in _STEP_FIELDS if f not in row]
        if missing:
            raise ManifestError(f"{where} is missing {missing}")
        unknown = sorted(set(row) - set(_STEP_FIELDS))
        if unknown:
            raise ManifestError(f"{where} carries fields this engine does not know: {unknown}")
        argv = row["argv"]
        if not isinstance(argv, list) or not all(isinstance(w, str) for w in argv):
            raise ManifestError(
                f"{where}: argv is {argv!r}, which is not a list of words. A command is the "
                "words it is made of — a string here would be re-parsed by whoever runs it, "
                "and §5.1 rejects shell syntax rather than reinterpreting it.")
        env = row["env"]
        if not isinstance(env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ManifestError(
                f"{where}: env must be an object of strings, not {env!r}; it is spliced into "
                "a process environment, where anything else raises at spawn time")
        timeout = row["timeout"]
        # `isinstance(True, int)` — a JSON `true` here would run the step under a one-second
        # budget rather than being refused.
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ManifestError(
                f"{where}: timeout must be a whole number of seconds, not {timeout!r}")
        try:
            steps.append(Step(argv=tuple(argv), cwd=_text("cwd", row["cwd"], where),
                              env=env, timeout=timeout))
        except VerifyError as e:
            # `Step` refuses an empty argv and a program name only a shell could resolve.
            # Re-raised in this module's vocabulary rather than propagated: `read_manifest`'s
            # documented contract is that a record it will not stand behind arrives as a
            # ManifestError, and the resume that catches it has no other name for this.
            raise ManifestError(f"{where}: {e}") from e
    return tuple(steps)


def _mapping(name, value, source):
    if not isinstance(value, dict):
        raise ManifestError(f"{source}: {name} must be an object, not {value!r}")
    return value


def _oid_mapping(name, value, source):
    """`forge_refs`, checked harder than `protected_refs` because it means the opposite.

    A `protected_refs` entry ADDS a ref to what is watched, so a damaged one reports drift.
    A `forge_refs` entry REMOVES one, so a damaged one reports nothing — for the life of the
    run, about the namespace §9 says is the one a seat would write into invisibly.

    A value that is not a hex OID is refused rather than carried, and `""` is the one that
    matters: it is what "declared but not yet created" would be spelled as, and it would make
    `drift` compare a ref's real OID against nothing. §9 says "the exact OID recorded AT
    CREATION" — a manifest cannot be written before creation, because `baseline_commit` beside
    this field is `baseline.materialize`'s own output.
    """
    if not isinstance(value, dict):
        raise ManifestError(f"{source}: {name} must be an object, not {value!r}")
    for ref, oid in value.items():
        if not isinstance(oid, str) or not oid or any(c not in "0123456789abcdef" for c in oid):
            raise ManifestError(
                f"{source}: {name}[{ref!r}] is {oid!r}, not the hex OID the ref was created "
                "at. §9 whitelists by exact name AND exact OID; a name whose OID is missing "
                "is a namespace whitelist one entry wide.")
    return value


def _contract(name, value, source):
    """Rebuild `inspect.GeneratorContract` from the object json made of it.

    Every field this decoder does NOT check would be a way for the run's own record of what a
    candidate may rewrite to differ from what the gate confirmed, and nothing would notice
    until a resume compared them. So the keys are exactly the contract's — an extra one is a
    fact this engine would drop, a missing one is a fact it would have to invent — and
    `relations` is rebuilt as a tuple of PAIRS rather than passed through as the lists json
    returns. A tuple of lists satisfies `__post_init__` in silence and compares unequal to the
    contract itself, which is this field's whole failure mode reached one step later.
    """
    if not isinstance(value, dict):
        raise ManifestError(f"{source}: {name} must be an object, not {value!r}")
    names = {f.name for f in dataclasses.fields(GeneratorContract)}
    if set(value) != names:
        raise ManifestError(
            f"{source}: {name} must carry exactly {sorted(names)}, not {sorted(value)}; it "
            "records which verify-origin rewrites this run admits, and a dict that merely "
            "parses is not a contract")
    relations = value["relations"]
    if not isinstance(relations, list):
        raise ManifestError(
            f"{source}: {name}.relations must be an array, not {relations!r}")
    for r in relations:
        if not isinstance(r, list):
            raise ManifestError(
                f"{source}: {name} relation {r!r} must be an array of two globs")
    try:
        return GeneratorContract(id=_text(f"{name}.id", value["id"], source),
                                 relations=tuple(tuple(r) for r in relations))
    except ValueError as e:
        # `GeneratorContract` refuses a relation that is not a pair of non-empty-output globs,
        # and relations without an id. Re-raised in this module's vocabulary for the same
        # reason `_steps` re-raises VerifyError: a record `read_manifest` will not stand behind
        # reaches the resume as a ManifestError or the resume has no name for it.
        raise ManifestError(f"{source}: {name}: {e}") from e


def _budget(name, value, source):
    """A post-review budget, floored at ZERO rather than at one.

    `count`'s floor of 1 is right for `seats` and `attempts` — a run with no seat is not a
    run. Zero review rounds IS a run (`gate.quote` says so at its own floor), and a run that
    priced no post-review synthesis has a cap of 0 rather than a missing field.
    """
    return count(name, value, source, floor=0)


# One decoder per field, by name. A field added to `Manifest` and not named here makes the
# next read fail loudly, which is the point: the alternative is that it arrives as whatever
# JSON made of it and compares unequal to what was written, silently, on a resume.
_DECODERS = {
    "run_id": _text,
    "repo_path": _text,
    "base_commit": _text,
    "baseline_ref": _text,
    "baseline_commit": _text,
    "tracked_tree_oid": _text,
    "selected_paths": _texts,
    "generator_contract": _contract,
    "setup": _steps,
    "verify": _steps,
    "protected_refs": _mapping,
    "forge_refs": _oid_mapping,
    "status_digest": _text,
    "index_digest": _text,
    "created_at": _text,
    "seats": count,
    "attempts": count,
    "review_rounds": _budget,
    "synthesis_fix_cap": _budget,
}


def _decode(row, source) -> Manifest:
    if not isinstance(row, dict):
        raise ManifestError(f"{source}: a manifest is an object, not {type(row).__name__}")
    names = [f.name for f in dataclasses.fields(Manifest)]
    missing = [n for n in names if n not in row]
    if missing:
        # Not defaulted. A field the reader supplies is a fact the run never agreed to, and
        # `base_commit` or `verify` invented at resume time is the whole failure this file
        # exists to prevent.
        raise ManifestError(f"{source} is missing {missing}")
    unknown = sorted(set(row) - set(names))
    if unknown:
        # A recorder that once wrote a fact this reader drops would let a resume answer
        # questions about the run out of a manifest it only partly understands.
        raise ManifestError(f"{source} carries fields this engine does not know: {unknown}")
    fields = {}
    for name in names:
        decode = _DECODERS.get(name)
        if decode is None:
            raise ManifestError(f"no decoder is declared for the manifest field {name!r}")
        fields[name] = decode(name, row[name], source)
    # The one CROSS-field check in this decoder, and `forge_refs` earns it by being the one
    # field that subtracts. `_declared`'s ceiling says a declared name is under one of §9's two
    # namespaces; §9's pattern is narrower than that — `refs/khenrix-forge/<run-id>/*` — and the
    # run-id is the segment that stops THIS run vouching for a ref belonging to a concurrent
    # one, whose OID it has no way to have recorded at creation.
    own = tuple(f"{p}{fields['run_id']}/" for p in _FORGE_REF_PREFIXES)
    stray = sorted(n for n in fields["forge_refs"] if not n.startswith(own))
    if stray:
        raise ManifestError(
            f"{source}: forge_refs claims {stray}, which run {fields['run_id']!r} did not "
            f"create; §9's namespaces are {list(own)}. A ref outside them is dropped from "
            "`protected_refs` on the word of a run that never made it.")
    return Manifest(**fields)


# §14's graph, as the successors of each phase — the one place it is written down. Read as a
# mapping rather than a list of pairs so a refusal can say what IS legal from where the run
# stands, which is the difference between a message a resume can act on and one that only
# says no. The chain is §14's wrapped line straightened out: `comparing → synthesizing` is
# the joint the wrap hides, `synthesizing ⇄ verifying` is two edges, and `reviewing →
# synthesizing` is the back-edge §14 declares in prose beside the diagram.
#
# `failed` and `source_diverged` are §14's UNIVERSAL endings, and each non-terminal phase
# carries both. They are universal because a terminal reachable from one phase cannot record
# where a run actually died: §5's confirmed calibration-failure policy has an `abort` branch
# and calibration runs inside `setting_up`, hours before any review, and §9's condition is
# continuous rather than checked at review — the user's checkout and their protected refs can
# move at any moment the run is out, so the transition has to leave from the phase that saw
# it. Spelled out per phase rather than folded in by a rule over the non-terminals, so this
# mapping stays the graph itself and not a computation over a second table — PHASES and
# TERMINAL are already read out of it, and a rule would put §14's chain somewhere else while
# leaving them looking as though it were here. What that costs is nine hand-copies of one
# pair, and `test_every_non_terminal_phase_can_reach_failed_and_source_diverged` is what
# stands where one going missing would. Terminals get neither: a run that reported an outcome
# and then left it is what the empty successor sets below refuse.
_EDGES = {
    "created": frozenset({"confirmed", "failed", "source_diverged"}),
    "confirmed": frozenset({"setting_up", "failed", "source_diverged"}),
    "setting_up": frozenset({"building", "failed", "source_diverged"}),
    "building": frozenset({"harvested", "failed", "source_diverged"}),
    "harvested": frozenset({"comparing", "failed", "source_diverged"}),
    "comparing": frozenset({"synthesizing", "failed", "source_diverged"}),
    "synthesizing": frozenset({"verifying", "failed", "source_diverged"}),
    "verifying": frozenset({"synthesizing", "reviewing", "failed", "source_diverged"}),
    "reviewing": frozenset({"synthesizing", "ready", "degraded", "review_blocked",
                            "source_diverged", "failed"}),
    "ready": frozenset(),
    "degraded": frozenset(),
    "review_blocked": frozenset(),
    "source_diverged": frozenset(),
    "failed": frozenset(),
}

# Both views are DERIVED from that one declaration. A separately spelled terminal list is a
# second place to be right about which phases end a run, and the two are then free to
# disagree — a phase listed terminal while holding a successor is a run that ends and then
# carries on. What derivation costs is that a phase whose successors were forgotten reads as
# terminal instead of failing; `test_every_terminal_the_spec_names_exists` is what stands
# there, by naming the five §14 declares rather than counting them.
PHASES = tuple(_EDGES)
TERMINAL = frozenset(name for name, successors in _EDGES.items() if not successors)

# §14.1's name for an operation that started, left no receipt and has no surviving process:
# "It is never silently retried." A SEAT's outcome, not a phase of the run — §14's graph has
# no such ending, so it is deliberately absent from PHASES and `advance` refuses it like any
# other name the graph does not declare.
OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class State:
    """§14's five dimensions, which are separate because one enum cannot hold them.

    No defaults, on `snapshot_refs`'s argument: a run at `created` with round 0 and a run
    resuming a fix after round 2 are both ordinary, and a dimension the constructor supplies
    is a fact nobody recorded.

    `phase` is NOT validated here. A state read back from a foreign or damaged record has to
    be constructible for a resume to report what it found, so the refusal lives at `advance`,
    where the question "what happens next" is actually being asked.
    """
    phase: str
    round: int
    attempt: int
    verified_checkpoint: str | None
    deliverable_checkpoint: str | None


def advance(state: State, phase: str) -> State:
    """`state` moved to `phase`, or TransitionError because §14 declares no such edge.

    ONLY the phase moves — see the module docstring for what each of the other four would cost
    if it rode along. A caller that means to spend a round or a retry says so itself, which is
    what "separate dimensions" buys and what a resume reads back to tell one fix from the next.
    """
    if not isinstance(state, State):
        raise TransitionError(f"a State is required, not {type(state).__name__}")
    if not isinstance(phase, str):
        # Checked rather than left to the lookup: an unhashable target raises TypeError out of
        # `in`, which no caller of this module is catching.
        raise TransitionError(f"a phase is one of §14's names, not {phase!r}")
    successors = _EDGES.get(state.phase)
    if successors is None:
        raise TransitionError(
            f"this run is in {state.phase!r}, which §14 does not declare, so what follows it "
            f"is not a question this graph can answer. The declared phases are {list(PHASES)}.")
    if phase not in successors:
        if not successors:
            raise TransitionError(
                f"{state.phase!r} ends the run; §14 declares no phase after a terminal, and a "
                f"run that left one would have reported an outcome it then went on to change.")
        unknown = "" if phase in _EDGES else " — which is not a declared phase at all"
        raise TransitionError(
            f"§14 declares no edge {state.phase!r} → {phase!r}{unknown}. From {state.phase!r} "
            f"a run may go to {sorted(successors)}.")
    return dataclasses.replace(state, phase=phase)


def write_seat(run_dir, name: str, payload: dict) -> None:
    """Record what seat `name` last knew, replacing whatever it last said.

    REWRITTEN, unlike the manifest: a seat's status moves the whole length of its run, and
    write-once here would make its second update a crash. `atomic_write` is what makes the
    rewrite safe to be interrupted — the record is published by a rename, so a reader arriving
    mid-write sees the previous record whole rather than a prefix of this one, and the killed
    writer of §14.1 leaves the seat saying something older rather than something torn.

    NOT THE SIGNAL PATH. §14.1 requires the handler write "only pre-formatted bytes it already
    holds, never serializes", and this call serializes; a handler owing a record hands the
    bytes it is already holding to `storage.atomic_write` itself.

    Refused BEFORE anything is published, on all four counts below, so a rejected record
    leaves the seat's last good one exactly where a resume will look for it.

    NO SCHEMA, AND THE SPEC PUTS ONE HERE. This is a decision left open, not an oversight, and
    it is stated because the next reader is the orchestrator's author and silence would read as
    "any payload is fine". §14.2 gives the SEAT the status tuple — status, four inventories,
    setup/verify logs and hashes, prompt fingerprints — while §14's five dimensions are the
    RUN's phase. This module has it the other way round: `write_state`/`_decode_state` enforce
    `_STATE_FIELDS` exactly, refusing a missing field, an unknown one and a wrong type, and
    everything above is all a seat record is ever checked for. Measured: a seat writing
    `{"phse": "biulding"}` is written, read and reconstructed with no complaint.

    THE PRODUCER NOW EXISTS and the decision is unchanged: `runner._record` writes one attempt
    object per attempt under `{"name", "attempts"}`, and `runner._prior_attempts` is what type-
    checks it — on the READ side, at the one caller that appends to it, where a damaged record
    costs nothing yet. Hoisting that shape into this writer would put the same rule in two
    places and make this module the authority on a record whose fields §14.2 assigns to the
    orchestrator; the argument for leaving it open was never that nobody wrote one.
    """
    path = storage.seat_state_path(run_dir, name)
    if not isinstance(payload, dict):
        raise StateError(
            f"{path}: a seat record is an object of facts about the seat, not "
            f"{type(payload).__name__}")
    if not payload:
        raise StateError(
            f"{path}: a seat record may not be empty. `{{}}` is falsy exactly as the None a "
            "seat that never wrote reads back as, and §14.1 requires those two stay apart.")
    try:
        # sort_keys so one seat state has one spelling on disk whatever order the caller built
        # it in; indented because the first reader of this file is usually a human working out
        # what a crashed seat had got to.
        # allow_nan=False because the default publishes `Infinity`/`NaN`, which THIS reader
        # accepts and no strict JSON reader does — a record only forge can read is one a
        # `--collect` in another language cannot. NaN was already refused, but by accident:
        # `nan != nan` trips the round-trip check, so it was reported as a shape failure
        # rather than as a value JSON cannot carry.
        blob = json.dumps(payload, sort_keys=True, indent=2,
                          allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as e:
        raise StateError(
            f"{path}: this record carries a value json cannot serialize: {e}") from e
    restored = json.loads(blob)
    if restored != payload:
        # `write_manifest`'s check, one file over and for the same reason: JSON has one
        # sequence type and one key type, so a tuple or an int key is accepted here and comes
        # back as something that compares unequal — with nothing saying so until a resume
        # compares them hours later. Keys are reported through repr() because the mismatched
        # pair is usually `1` and `"1"`, which do NOT sort against each other — without repr() the
        # diagnostic path raises TypeError while building the error it was reporting.
        differing = sorted(repr(k) for k in set(payload) | set(restored)
                           if payload.get(k) != restored.get(k))
        raise StateError(
            f"{path}: this record does not survive its own round trip; {differing} come back "
            "as something else. Store lists, strings and numbers under string keys.")
    storage.atomic_write(path, blob)


def read_seat(run_dir, name: str) -> dict | None:
    """What seat `name` last recorded, or None because it never recorded anything.

    None means ONE thing — no such file. Every other way a record can fail to be one raises,
    because §14.1's requirement is that a damaged seat and a silent one stay different
    answers; see the module docstring for the three routes that reach None — or an answer a
    caller cannot tell from it — without damaging a byte, which is why this is a whitelist
    rather than a fallback.
    """
    path = storage.seat_state_path(run_dir, name)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        # The one absence this call reports. A run directory that does not exist at all is
        # `read_manifest`'s refusal, which every resume passes through before it asks a seat
        # anything.
        return None
    except OSError as e:
        raise StateError(f"{path} exists and cannot be read: {e}") from e
    try:
        row = json.loads(raw)
    except ValueError as e:
        raise StateError(
            f"{path} is not readable as JSON: {e}. A record cut short by a killed writer "
            "lands here, and §14.1 requires that it read as a damaged seat and not a silent "
            "one.") from e
    if not isinstance(row, dict):
        raise StateError(
            f"{path} holds a {type(row).__name__}, which is not a seat record. A JSON `null` "
            "is the one that matters: it parses cleanly and reads back as the same None a "
            "missing file gives, so a reader trusting the parse would report a seat that "
            "recorded something as one that never ran.")
    if not row:
        raise StateError(
            f"{path} holds an empty record, which is falsy exactly as the None a seat that "
            "never wrote reads back as. `write_seat` refuses to produce one; this arrived by "
            "another route.")
    return row


# §14's tuple, named here rather than derived from `State`'s fields so a dimension added
# there cannot start arriving in records this reader silently accepts — `_STEP_FIELDS`'
# argument, one record over.
_STATE_FIELDS = ("phase", "round", "attempt", "verified_checkpoint", "deliverable_checkpoint")


def write_state(run_dir, state: State) -> None:
    """Record where the run has got to, replacing whatever it last said.

    REWRITTEN like a seat's record and unlike the manifest: the phase moves the whole length
    of a run, so write-once here would make the second advance a crash. `atomic_write`
    publishes by rename, so a reader arriving mid-write sees the previous position whole
    rather than a prefix of this one.

    ALL FIVE DIMENSIONS or none. A record carrying the phase alone would hand a resume a
    round and an attempt supplied by its READER — the failure `Manifest` records four fields
    per step to avoid, one file over.

    A CACHE, NOT THE AUTHORITY. §14.2 makes git the ordering of record — `--collect`
    "reconstructs the checkpoint sequence and the last verify-passing OID even if every JSON
    file is torn" — so this file is an optimisation over a derivation from the synthesis
    branch, and the derivation is the one that must win where they disagree. Nothing derives
    the branch side; whoever writes it owns that precedence, and should not read a position
    here as evidence against what the branch says.

    THE ARGUMENT ABOVE RESTS ON WHAT THE WRITERS WRITE, so they are named rather than counted:
    `runner._advance` and `review._persist`. It used to say "`runner.run` is the only writer",
    which a later change falsified while the sentence went on carrying the precedence
    argument. Both go through `advance`, which `dataclasses.replace`s the PHASE and nothing
    else, so neither can move a checkpoint dimension; and no caller of either sets one — the
    only construction that names them is `runner`'s initial `State`, with both `None`. So this
    file still cannot state a checkpoint the branch would have to be argued with about. That
    is a property of the two call sites, not of the graph: a third writer, or an existing one
    handed a `State` carrying a checkpoint, would give this record something to disagree with,
    and the derivation still wins.

    The body below is `write_seat`'s, one record over: the same publish-by-rename, the same
    refuse-before-publishing, the same round trip. They are not factored together because the
    decoders differ and each refusal names its own constraint — a third record here is the
    point at which that stops paying.
    """
    path = storage.state_path(run_dir)
    if not isinstance(state, State):
        raise StateError(f"{path}: a State is required, not {type(state).__name__}")
    try:
        # sort_keys and indent for `write_seat`'s reasons; allow_nan=False because the
        # default emits `Infinity`, which this reader would accept back and no strict JSON
        # reader will.
        blob = json.dumps(dataclasses.asdict(state), sort_keys=True, indent=2,
                          allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as e:
        raise StateError(
            f"{path}: this record carries a value json cannot serialize: {e}") from e
    # Decoded back before anything is published, so a dimension that would not survive its own
    # round trip — a checkpoint handed in as a tuple comes back a list — is refused while the
    # caller is still there, rather than at the resume that can no longer read its position.
    if _decode_state(json.loads(blob), path) != state:
        raise StateError(f"{path}: this record does not survive its own round trip")
    storage.atomic_write(path, blob)


def read_state(run_dir) -> State | None:
    """Where the run had got to, or None because it never recorded a position.

    None means ONE thing — no such file — on `read_seat`'s rule and for its reason. A run at
    `confirmed` has written its manifest and moved nowhere since, so no record is an ordinary
    state; a record cut short by a killed writer is not, and §14.1 requires those two stay
    different answers.
    """
    path = storage.state_path(run_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise StateError(f"{path} exists and cannot be read: {e}") from e
    try:
        row = json.loads(raw)
    except ValueError as e:
        raise StateError(
            f"{path} is not readable as JSON: {e}. A record cut short by a killed writer "
            "lands here, and a resume that read it as a run which never moved would restart "
            "one that was most of the way through.") from e
    return _decode_state(row, path)


def _decode_state(row, source) -> State:
    """§14's five dimensions as recorded, or StateError. Never defaulted: a dimension the
    reader supplies is a position nobody was in.

    `phase` is checked for being a STRING and not for being one of §14's names, on `State`'s
    own rule — a foreign or damaged record has to be constructible for a resume to report
    what it found, and `advance` is where the graph refuses.
    """
    if not isinstance(row, dict):
        raise StateError(f"{source}: a run state is an object, not {type(row).__name__}")
    missing = [n for n in _STATE_FIELDS if n not in row]
    if missing:
        raise StateError(f"{source} is missing {missing}")
    unknown = sorted(set(row) - set(_STATE_FIELDS))
    if unknown:
        raise StateError(f"{source} carries fields this engine does not know: {unknown}")
    if not isinstance(row["phase"], str):
        raise StateError(f"{source}: phase is one of §14's names, not {row['phase']!r}")
    for name in ("round", "attempt"):
        value = row[name]
        # `isinstance(True, int)` — a JSON `true` would resume the run at round 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise StateError(f"{source}: {name} is a whole count, not {value!r}")
    for name in ("verified_checkpoint", "deliverable_checkpoint"):
        value = row[name]
        if value is not None and not isinstance(value, str):
            raise StateError(f"{source}: {name} is a commit OID or null, not {value!r}")
    return State(**{n: row[n] for n in _STATE_FIELDS})


def _refuse_a_repository_the_manifest_did_not_record(manifest: Manifest, repo) -> None:
    """Raise unless `repo` is the repository the manifest names.

    The manifest already records the repository, so an unchecked argument is a second answer
    to a settled question — and the wrong one answers CLEANLY. Measured with this check
    removed: given a manifest recorded for A, `drift` against a byte copy of A taken at t0
    returns `()` while A itself has moved `HEAD`, its branch and the checkout. §9 says that
    run must stop before handover, and `()` is how it would sail through.

    IDENTITY IS GIT'S, not the string's. The toplevel is asked for rather than compared as
    written, because `_status_digest` accepts a subdirectory and answers for the whole
    repository — a literal comparison would refuse that caller. Measured, `--show-toplevel`
    gives ONE answer for the repository as recorded, with a trailing slash, through a `.`
    segment, through a symlink and from a subdirectory, and a DIFFERENT answer for a byte
    copy and for a linked worktree — which is a second checkout with its own HEAD, index and
    status, so the snapshot cannot be judged against it however many objects it shares.

    `realpath` on top, because the recorded side is a string nobody resolved: a manifest
    written under a symlinked path records the link while git answers with the target, and
    both sides are resolved so that resume still works. It resolves LINKS and not mounts, so
    one directory reachable at two mount points reads as two paths here and is refused; the
    message names both, and the resume the user meant is one path away.

    Bytes then decoded, on `_status_digest`'s argument: a repository may live under a path
    that is not valid UTF-8, and `os.fsdecode` spells one the way `str(Path)` spelled it into
    the manifest.
    """
    if not manifest.repo_path:
        # `os.path.realpath("")` is the PROCESS's working directory, so an empty recorded
        # path is met by whatever directory forge happens to be run from — the one input
        # here that would answer for itself.
        raise ManifestError(
            "this manifest records no repository, so nothing can be shown to be the one the "
            "run agreed to; a resume cannot judge the snapshot against a repository the "
            "manifest never named.")
    r = gitcmd.git(repo, "rev-parse", "--show-toplevel", env_extra=gitcmd.READONLY,
                   binary=True, check=False)
    if r.returncode != 0:
        raise ManifestError(
            f"{repo} cannot be shown to be the repository this run recorded "
            f"({manifest.repo_path!r}): {r.stderr.decode('utf-8', 'replace').strip()}")
    root = os.path.realpath(os.fsdecode(r.stdout.strip()))
    if root != os.path.realpath(manifest.repo_path):
        raise ManifestError(
            f"this run was recorded against {manifest.repo_path!r} and was handed {root!r}. "
            "The snapshot describes the first, so judging it against the second reports what "
            "moved in a repository nobody is resuming — and a copy taken at t0 reports "
            "nothing moved at all. Resume against the repository the manifest names.")


def drift(manifest: Manifest, repo) -> tuple[str, ...]:
    """What has moved in `repo` since the manifest recorded it: §9's ref names, sorted, then
    `"status"` when the checkout digest moved and `"index"` when the index digest did. `()`
    when the snapshot still describes it.

    `repo` is CHECKED against the one the manifest records before anything is compared, and
    here rather than in `reconstruct`: this call is public and a check one level up would
    leave it answering `()` for a stranger, while `reconstruct` reaches the same refusal by
    handing its own argument straight through.

    THREE WAYS A REF DRIFTS, not one — see the module docstring for the measurements. A
    recorded ref whose OID differs covers two of them at once, because a ref that is GONE
    reads back as no OID at all; the third is a ref present now and absent at t0.

    FORGE'S OWN REFS ARE COMPARED, not skipped. §9 whitelists them by exact name and the exact
    OID recorded at creation, so the same three ways apply to them: the run's baseline ref
    moved off the commit forge made it at, or deleted, is drift. What is NOT drift is that ref
    sitting where forge put it — which is why the whitelist exists at all. A ref under §9's
    namespaces that this manifest does not declare was never forge's, so it never reaches this
    comparison: `_partition` leaves it in the protected half, where the new-ref case has it.

    The selection comes from the MANIFEST rather than from an argument, because the t0 digest
    ranges over it: re-derived with a different selection the two digests describe different
    file sets, and the comparison would report drift on every run or on none. The declared
    forge refs come from the manifest for the sharper version of the same reason — passed in,
    a caller could hand this call a wider set than the one it recorded and be told nothing
    moved in the namespace §9 is about.

    Neither `"status"` nor `"index"` can collide with a ref name — every ref here is `HEAD` or
    begins `refs/`.
    """
    if not isinstance(manifest, Manifest):
        raise ManifestError(f"a manifest is required, not {type(manifest).__name__}")
    _refuse_a_repository_the_manifest_did_not_record(manifest, repo)
    refs, forge_now, digest = _partition(repo, manifest.selected_paths, manifest.forge_refs)
    moved = {name for name, oid in manifest.protected_refs.items() if refs.get(name) != oid}
    moved |= set(refs) - set(manifest.protected_refs)
    moved |= {name for name, oid in manifest.forge_refs.items() if forge_now.get(name) != oid}
    out = sorted(moved)
    if digest != manifest.status_digest:
        out.append("status")
    if snapshot_index(repo) != manifest.index_digest:
        out.append("index")
    return tuple(out)


@dataclass(frozen=True)
class Reconstruction:
    """A crashed run as its directory describes it, and nothing else.

    `seats` maps a seat's name to what it last recorded. A seat that never wrote is ABSENT
    rather than present with a placeholder, on `read_seat`'s rule: an entry the reader
    supplied is a fact no seat recorded.

    `orphans` are `journal.Event`s — a start with no result, which §14.1 names
    `outcome_unknown` and says is never silently retried. They are handed back whole, process
    identity included, because "no receipt AND no surviving process" is the test and the
    identity is the only half of it a later caller cannot re-derive.

    `state` is None when the run never recorded a position, and `diverged` is `drift`'s
    answer — a report, not a decision.
    """
    manifest: Manifest
    state: State | None
    seats: dict
    orphans: tuple
    diverged: tuple[str, ...]


def _refuse_a_manifest_the_confirm_record_does_not_describe(run_dir, manifest) -> None:
    """§14.2's manifest is what the operator confirmed, and this is the tripwire that says so.

    `write_manifest` is exclusive only at CREATION and `read_manifest` validates types and
    schema, never identity — so a same-UID process, or a confused cleanup script, replacing
    `manifest.json` with VALID JSON that changes the confirmed verify command from `pytest`
    to `true` passes every check this package makes, and a later `--collect` reports a
    decision nobody confirmed. The design requires accidental and provider-induced content
    changes to fail closed; this is the one control-plane file where nothing did.

    THE ANCHOR IS THE JOURNAL AND NOT A SIDECAR. A digest stored beside the file it describes
    is replaced along with it. `gate.open_run` writes the manifest's sha256 onto the `confirm`
    operation's `done` row at the same moment it writes the manifest, and the journal is
    append-only and fsynced per record — so it is the one copy a later rewrite cannot reach
    without leaving the log visibly inconsistent.

    A RUN WHOSE RECORD PREDATES THE ANCHOR IS NOT REFUSED. Runs opened before this field
    existed carry no `manifest_sha256`, and refusing them would strand every in-flight run on
    an upgrade. An absent anchor is "this run cannot be checked", which is honest; a PRESENT
    one that disagrees is the thing this exists to catch, and it fails closed.
    """
    events = journal.Journal(storage.journal_path(run_dir)).read()
    done = journal.done("confirm")
    want = None
    for e in reversed(list(events)):
        if e.event == done and e.operation_id == manifest.run_id:
            want = e.data.get("manifest_sha256")
            break
    if not isinstance(want, str) or not want:
        return
    have = hashlib.sha256(storage.manifest_path(run_dir).read_bytes()).hexdigest()
    if have != want:
        raise ManifestError(
            f"this run's manifest does not match the one its `confirm` record describes "
            f"({have[:12]} against {want[:12]}). §14.2's manifest is what the operator agreed "
            "to at §5 step 2 and is never rewritten, so a file that has changed since is a "
            "decision nobody confirmed — refusing rather than reporting on it.")


def reconstruct(run_dir, repo) -> Reconstruction:
    """The manifest, the run's position, every seat record and the journal — read from
    `run_dir` and judged against the repository the manifest names, not the one the caller
    believes in.

    Enumerated rather than summarised as "everything `--collect` knows", which would be false
    in the direction that matters. §14.2 lists SIX sources and three of them are read here;
    the ledger with its content-addressed rows, the synthesis branch HEAD with its dirty
    filesystem inventory, and `git log` for the checkpoint sequence and last verify-passing
    OID have no producer in this wave and so no reader here. A resume built on this record
    alone knows where the run got to, not what it had made.

    `repo` is the one argument that does not come out of the run directory, and the manifest
    records it too: `drift` refuses a repository that is not the recorded one, so a caller
    cannot substitute a twin and be told nothing moved.

    Raises whatever the record that failed raises: ManifestError with no manifest or a
    repository the manifest did not record, StateError for a damaged position or seat,
    JournalError for a log that is no longer a sequence of facts, StorageError for a seat
    file this engine could not have written. Unwrapped, on the precedent this package sets
    for `SeatError` and `BundleError` — a resume repairs those differently, and one class
    over all of them would say only that the run is unreadable.

    Not exhaustive, and the gap is named because the list above reads as though it were: a
    manifest path carrying a NUL reaches `os.lstat` through `drift` and raises ValueError,
    which is not one of the four. See `_path_digest` for why that is left to raise.
    """
    manifest = read_manifest(run_dir)
    _refuse_a_manifest_the_confirm_record_does_not_describe(run_dir, manifest)
    return Reconstruction(
        manifest=manifest,
        state=read_state(run_dir),
        seats={name: read_seat(run_dir, name) for name in storage.seat_names(run_dir)},
        orphans=journal.orphans(journal.Journal(storage.journal_path(run_dir)).read()),
        diverged=drift(manifest, repo))
