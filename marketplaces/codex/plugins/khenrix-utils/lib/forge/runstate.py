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
hash, the checkout files, all non-forge refs, REMOTES, and CONFIGURATION. Three of those seven
are not recorded here, and each is a §9 GAP rather than a boundary this file chose — written
down so the sentence and the code do not disagree:

  * REMOTES and CONFIGURATION. Nothing here reads `remote.*` or any other config key, so a
    seat that adds a remote or sets a repo-local value leaves no t0 fact to be judged
    against, and `drift` cannot speak for either.
  * The INDEX HASH. No digest of `.git/index` is stored, so the index is covered only through
    its effects on the porcelain, and three classes of index move have no effect there.
    Measured, each on git 2.53. A stat-only refresh moves the index and not the digest — an
    ordinary `git status` over stale stat data does it, and it is not the user's work at all,
    which is what the READONLY pins below stop forge itself causing. Index-format churn
    (`update-index --split-index`) moves the index and not the digest, and changes nothing
    the digest could have seen: `ls-files -s` and the porcelain come back identical. Setting
    a per-path skip bit (`update-index --assume-unchanged` or `--skip-worktree`) moves the
    index and not the digest, and IS the user's work — that class is UNCOVERED, not a
    boundary chosen here.

§9 also asks that forge's own refs be whitelisted by exact name AND the exact OID recorded at
creation, because a namespace whitelist lets a seat write into forge's own namespace
invisibly. What is implemented is the namespace whitelist: `snapshot_refs` drops
`refs/khenrix-forge/**` and `refs/heads/forge/**` BEFORE recording, so no forge ref has either
a name or an OID in `protected_refs`, and a forge ref that existed at t0 and then moved is
invisible — measured, and pinned by
`test_a_forge_ref_that_existed_at_t0_and_moved_is_invisible_rather_than_whitelisted`. Closing
it needs a producer that records each forge ref as forge creates it, which this plan has none
of. Without one the only alternative available here is to record forge's refs as PROTECTED,
and that is worse in the direction that matters: the run creates refs after t0, so each would
read as a ref the user made and every run would diverge from itself.

THE CONTENT BEHIND `--assume-unchanged` is a second question from the index move above, and a
different answer: not a gap of this file's, but a floor it shares with B. Measured with the bit
set and the path clean, the user's own later edit moves neither the porcelain nor the digest,
and `baseline.materialize` does not carry that content into B either — with nothing else dirty
it never reaches `add` at all, since an empty porcelain makes `dirty` False and B HEAD itself,
and when a selection does make the run dirty `git add -u -- :/` exits ZERO and skips the path
on that same stat comparison. The user's own `git status`, `git diff` and `git stash` are
equally blind (`stash` answers "No local changes to save" and leaves the edit where it was).
Two ways out are ones this file already has: a path that was DIRTY at t0 leaves the porcelain
when the bit is set, which moves the digest, and a SELECTED path stays in the content set
whatever the porcelain says. The selected case is where the sharing stops — `add -f` does not
carry the hidden edit into B while the digest does see it, so the two disagree in the direction
that stops the run rather than the one that hands over.

`--skip-worktree` shares that floor, but the two bits are NOT INTERCHANGEABLE and nothing here
may treat them as one. Measured on git 2.53.0: the porcelain and the digest cannot tell them
apart, and `rejections` and `git add` can. `facts.sparse` is True under skip-worktree and
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
does not carry it into B either, by whichever of the two routes above the bit takes —
neither method sees it, and neither can. An untracked
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
ref that is NEW since t0 are drift as much as one that moved. The new-ref case needs no forge
exclusion of its own: the fresh side is `snapshot_refs`, which names no forge ref at all, so
forge's own baseline cannot arrive as a ref the user made — a `drift` reading a raw
`show-ref` instead would report every run as diverging from itself.

Naming it is where this file stops. `reconstruct` reports what moved; §9's transition and its
refusal to hand over are decisions, and they belong to whoever is sequencing the run. §14
declares `source_diverged` out of every non-terminal phase, so that sequencer can make the
transition from wherever the run had got to when the move was seen, rather than having to
carry the fact as far as `reviewing` first — see `_EDGES`.
"""
import dataclasses
import hashlib
import json
import os
import stat
from dataclasses import dataclass

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


# §9's two explicitly-allowed namespaces, excluded WHOLESALE — which is the namespace
# whitelist §9 warns about rather than its name-and-OID one; see the module docstring for the
# measurement and what closing it needs. Prefixes rather than a substring test: a user's
# `refs/heads/forgery-experiments` is theirs, and dropping it here would leave a moved ref
# with nothing to compare against — the fail-OPEN direction of the same decision.
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

    `protected_refs` and `status_digest` are the t0 snapshot: what the user's repository
    looked like at the moment of agreement, kept so `drift` can ask whether it still does.
    `selected_paths` is an input to the second of those as well as a record — which is why
    `drift` re-derives the digest from THIS field and not from an argument. See
    `snapshot_refs`.

    `repo_path` says WHICH repository those two describe, and is read for that: `drift`
    refuses a repository that is not this one, so the snapshot is never compared against a
    copy that would answer "nothing moved" on the run's behalf.
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
    status_digest: str
    created_at: str


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


def snapshot_refs(repo, selected_paths) -> tuple[dict, str]:
    """`(protected_refs, status_digest)` for `repo` as it is right now.

    `selected_paths` is REQUIRED rather than defaulted, on `verify.build_verifier`'s argument
    for its `contract`: a default here is a policy, and the policy is the one a human
    confirmed at the §5 gate. A caller that has selected nothing passes `()`, which is a
    statement that the run carries no untracked path — and a caller that HAS selected
    something and forgets is the fail-open case the fourth digest part exists to close.

    Every call is read-only against the USER's repository, which is why `READONLY` is on all
    of them rather than on the status call alone: measured on git 2.53, a plain `git status`
    over a tracked file whose stat data had gone stale rewrote `.git/index`, and the same
    command under `GIT_OPTIONAL_LOCKS=0` left it byte for byte alone. §9 protects the index
    hash, so a snapshot that refreshed it would be forge moving the very thing it is here to
    watch — once at t0 and again at every drift check.
    """
    refs = {}
    # `git show-ref` exits 1 in a repository that has no refs at all, which is a fact about
    # the repository rather than a failure of the call.
    listed = gitcmd.git(repo, "show-ref", env_extra=gitcmd.READONLY, check=False)
    for line in listed.stdout.splitlines():
        oid, _, name = line.partition(" ")
        if name and not name.startswith(_FORGE_REF_PREFIXES):
            refs[name] = oid
    head = _head(repo)
    if head:
        refs["HEAD"] = head
    return refs, _status_digest(repo, head, selected_paths)


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
    porcelain = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "status", "--porcelain=v1", "-z",
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
        value = _path_digest(os.path.join(root, rel))
        for part in (rel, value):
            h.update(b"%d:" % len(part))
            h.update(part)
    return h.hexdigest().encode("ascii")


def _path_digest(path: bytes) -> bytes:
    """One path's content identity — total over every shape the OS will answer about, and
    raising for none of THOSE.

    The totality stops at paths the OS refuses to be asked about, and deliberately. A NUL in a
    `selected_paths` entry survives `write_manifest`/`read_manifest` intact and raises
    `ValueError: embedded null character` out of `os.lstat` before any of the branches below
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
        st = os.lstat(path)
    except OSError as e:
        return b"error:%d" % e.errno
    try:
        if stat.S_ISLNK(st.st_mode):
            return b"link:" + hashlib.sha256(os.readlink(path)).hexdigest().encode("ascii")
        if stat.S_ISDIR(st.st_mode):
            return _dir_digest(path)
        if stat.S_ISREG(st.st_mode):
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 16), b""):
                    h.update(chunk)
            return b"file:%d:" % bool(st.st_mode & 0o111) + h.hexdigest().encode("ascii")
    except OSError as e:
        return b"error:%d" % e.errno
    return b"special:%d" % stat.S_IFMT(st.st_mode)


def _dir_digest(path: bytes) -> bytes:
    """A selected DIRECTORY's contents. §2.2 contemplates selecting one explicitly, and
    `baseline.materialize`'s literal pathspec sweeps its whole contents into B — so a digest
    that stopped at the directory itself would be blind to every file the run is carrying
    inside it, which was measured against a recomputed tree OID and is why this exists.

    `baseline._walk_selected`'s rules, for its reasons: `.git` is pruned rather than
    post-filtered, and a linked directory is reported by its own target text rather than
    descended into. A directory that cannot be listed contributes its errno rather than
    nothing, because os.walk's default is to swallow the error and yield an empty tree — which
    would make an unreadable directory digest the same as an empty one.
    """
    h = hashlib.sha256()
    failures = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False,
                                                onerror=failures.append):
        dirnames[:] = sorted(n for n in dirnames if n != b".git")
        leaves = [n for n in list(dirnames) if os.path.islink(os.path.join(dirpath, n))]
        for n in leaves:
            dirnames.remove(n)
        for name in leaves + sorted(filenames):
            leaf = os.path.join(dirpath, name)
            rel = os.path.relpath(leaf, path)
            for part in (rel, _path_digest(leaf)):
                h.update(b"%d:" % len(part))
                h.update(part)
    for e in failures:
        h.update(b"error:%d:" % (e.errno or 0))
    return b"dir:" + h.hexdigest().encode("ascii")


def _text(name, value, source):
    if not isinstance(value, str):
        raise ManifestError(f"{source}: {name} must be a string, not {value!r}")
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
    "status_digest": _text,
    "created_at": _text,
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

    Deliberately not closed in this wave: nothing produces a seat record yet, so any field set
    named here would be invented rather than observed, and a schema guessed ahead of its
    producer is the fail-CLOSED-looking version of the same silence — it would refuse the
    fields the orchestrator turns out to need and admit the ones it does not.
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
    it yet; whoever writes it owns that precedence, and should not read a position here as
    evidence against what the branch says.

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
    """What has moved in `repo` since the manifest recorded it: §9's ref names, sorted, plus
    `"status"` when the checkout digest moved. `()` when the snapshot still describes it.

    `repo` is CHECKED against the one the manifest records before anything is compared, and
    here rather than in `reconstruct`: this call is public and a check one level up would
    leave it answering `()` for a stranger, while `reconstruct` reaches the same refusal by
    handing its own argument straight through.

    THREE WAYS A REF DRIFTS, not one — see the module docstring for the measurements. A
    recorded ref whose OID differs covers two of them at once, because a ref that is GONE
    reads back as no OID at all; the third is a ref present now and absent at t0.

    The selection comes from the MANIFEST rather than from an argument, because the t0 digest
    ranges over it: re-derived with a different selection the two digests describe different
    file sets, and the comparison would report drift on every run or on none.

    `"status"` cannot collide with a ref name — every ref here is `HEAD` or begins `refs/`.
    """
    if not isinstance(manifest, Manifest):
        raise ManifestError(f"a manifest is required, not {type(manifest).__name__}")
    _refuse_a_repository_the_manifest_did_not_record(manifest, repo)
    refs, digest = snapshot_refs(repo, manifest.selected_paths)
    moved = {name for name, oid in manifest.protected_refs.items() if refs.get(name) != oid}
    moved |= set(refs) - set(manifest.protected_refs)
    out = sorted(moved)
    if digest != manifest.status_digest:
        out.append("status")
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
    return Reconstruction(
        manifest=manifest,
        state=read_state(run_dir),
        seats={name: read_seat(run_dir, name) for name in storage.seat_names(run_dir)},
        orphans=journal.orphans(journal.Journal(storage.journal_path(run_dir)).read()),
        diverged=drift(manifest, repo))
