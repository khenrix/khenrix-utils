"""§16 — handover: what leaves this engine and how the user takes delivery of it.

THE TREES THIS MODULE TOUCHES ARE THE USER'S OWN, which is the difference between it and every
other module here. `fleet`, `verify` and `runner` run git in clones this engine created; every
call below runs in the repository the operator opened the run against, or in a worktree sharing
its object store. So each one carries `NO_DAEMON_CACHE` and `NO_HOOKS`, on the measurement
recorded in `tests/test_forge_seams.py`, and `NO_DIFF_DRIVERS` is not spelled anywhere:
`--no-ext-diff` is rc=129 for both verbs in every argv position, and both were measured to run
no diff driver.

THE SYNTHESIS TREE MAY SHARE THE USER'S `.git` WHERE A SEAT CLONE MUST NOT, and §16 states the
reason as a property of who is writing rather than of what is written: §4's hazard analysis is
about unattended full-permission agents, and the synthesis author is the trusted invoking
orchestrator under its normal approval boundary. It is not a fourth seat.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path

from . import (fingerprint, gitcmd, review as reviewmod, seat as seatmod, storage,
               strategy as strategymod, taskbundle, deepreview as deepmod, verify as verifymod)

SYNTHESIS = "synthesis"

MERGE_READY = "merge-ready-branch"
PATCH_ONLY = "patch"
KINDS = (MERGE_READY, PATCH_ONLY)

# EVERY VOCABULARY §16.1'S HEADER VALIDATES AGAINST IS REFERENCED, NEVER RESPELLED. The module
# that decides a value owns the strings that spell it, and a copy here is a second place to be
# right about which strings end a review, describe a seat or name a fusion — with the copy
# silently admitting a value nothing upstream produces, which is a header reporting a state no
# other module can reach. `seat`'s two are private because nothing outside it had needed to
# name them; the alias is where that reach is stated rather than buried at a use site.
_TERMINALS = reviewmod.TERMINALS
_REVIEW_BLOCKED = reviewmod.REVIEW_BLOCKED
_SEAT_FORGE = seatmod._FORGE
_SEAT_ARTIFACTS = seatmod._ARTIFACTS


class HandoverError(RuntimeError):
    """A delivery this module will not describe on the evidence it was given."""


def branch(run_id: str, name: str) -> str:
    """`forge/<run-id>/<name>` — one spelling, so `fleet`'s seat branch, the transport refspec
    and `--gc`'s walk cannot drift about where a seat's work lives."""
    if not run_id or not name:
        raise HandoverError(f"a branch needs a run id and a name, not {run_id!r}/{name!r}")
    return f"forge/{run_id}/{name}"


def create_synthesis_worktree(repo, dest, *, run_id: str, at: str, run_dir) -> str:
    """§16's git-deliverable surface: a worktree on a NEW BRANCH at `at`. Returns the branch.

    `-b`, NEVER THE DETACH FLAG, and §16 gives the reason in one sentence: a detached HEAD
    leaves the commits unreachable and the next `git gc` deletes the deliverable. A test
    asserts the flag's spelling appears nowhere in this file, because the argument is about a
    caller who adds it later and not about this line today — which is also why the flag is
    named in words here rather than written out. A docstring that spelled it would fail that
    test, and a test weakened to let the docstring through would stop catching the call site
    it exists for.

    THE TWO PRESETS ARE NOT DECORATION. Measured on git 2.53.0 against a repository with all
    hooks planted and `core.fsmonitor` armed, `worktree add -b` fires the monitor once,
    `post-checkout` once, `post-index-change` once and `reference-transaction` FOURTEEN times;
    `NO_DAEMON_CACHE` and `NO_HOOKS` together silence all four. Both are the user's own
    programs, and §5 step 1 admits no repository-supplied code the operator did not authorize.

    `run_dir` IS REQUIRED BECAUSE §20's BUNDLE HAS TO ARRIVE HERE TOO, and nothing else was
    going to bring it. `review.run_round` asks the checkout it is reviewing whether
    `taskbundle.task_dir` exists and tells the panel "there is no task bundle in this
    checkout" when it does not — so with the bundle materialized into three seats and into no
    synthesis tree, §13's reviewers judged a candidate without the task it was given. The
    materialization is inside this call rather than beside it for the reason the rest of this
    package puts a rule at the site instead of in prose: a caller that has to remember is a
    caller that will forget, and the failure is silent in exactly the direction that reads
    clean.

    THE BUNDLE IS READ BEFORE THE WORKTREE EXISTS, on `fleet.clone_seat`'s precedent of asking
    git to validate the branch name before paying for the clone. A recorded bundle this engine
    cannot decode is the likely failure and it costs nothing here. What a LATER failure costs
    is a registered worktree this module does not remove — §9 forbids the prune and `worktree
    remove` is `--gc`'s verb, not this one's — so the refusal names the tree it left standing
    rather than pretending it unwound.
    """
    b = branch(run_id, SYNTHESIS)
    bundle = taskbundle.read_task_bundle_if_recorded(run_dir)
    gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
               "worktree", "add", "-b", b, str(dest), at,
               env_extra=gitcmd.READONLY, timeout=300)
    if bundle is not None:
        # The seat's three calls, over the synthesis tree. `materialize` copies from the run's
        # own recorded bytes and `verify_materialized` re-derives the manifest FROM THIS TREE,
        # so "laid down" and "laid down what the manifest describes" stay two claims. The pair
        # is spelled here and in `runner._materialize_the_task` and nowhere else, and
        # `test_both_materializers_hand_a_tree_the_same_bundle_by_the_same_route` holds the
        # two together.
        try:
            taskbundle.materialize(bundle, storage.task_source_path(run_dir), dest)
            taskbundle.verify_materialized(bundle, dest)
        except taskbundle.TaskBundleError as e:
            raise HandoverError(
                f"the synthesis worktree {dest} was created on {b} and this run's task bundle "
                f"did not arrive in it ({e}). The worktree is registered and is left standing "
                "— removing it is `--gc`'s verb, not this one's — but §13's reviewers would "
                "be told this checkout holds no task, so the run stops here.") from e
    return b


def transport_seat(repo, clone_path, *, run_id: str, seat: str) -> str:
    """Bring one seat's committed work out of its remote-less clone. Returns the fetched OID.

    THE REFSPEC IS ALWAYS EXPLICIT AND THE DIRECTION IS ALWAYS OUT OF THE CLONE. §16 spells
    the whole call, and both halves are load-bearing. A bare `git fetch <path>` applies git's
    DEFAULT refspecs, which pull whatever refs the seat created into the user's repository —
    the write path §4 closed at the front, reopened at the back. And the fetch runs from the
    USER's side rather than as a push from the seat's, so nothing inside a bypass-permissions
    clone ever names a destination in the operator's repository.

    AN EXPLICIT REFSPEC IS NOT THE WHOLE DOOR, AND THE REST OF IT IS `--no-tags`. Measured on
    git 2.53.0 with exactly the call above: a seat carrying `refs/tags/seat-tag` and an
    annotated `refs/tags/annot` on the fetched commit had BOTH land in the user's repository,
    with and without the two presets. Tag auto-following is on by default and it follows into
    the history the refspec names, so the one channel a refspec closes is branches; `sneaky`
    stayed out and two tags the seat authored did not. `--no-tags` closes it, measured.

    `--no-write-fetch-head` IS THE SAME ARGUMENT ONE FILE OVER. A fetch overwrites the user's
    `.git/FETCH_HEAD`, which is the record of the last fetch THEY ran; measured, the flag
    leaves it untouched. Forge's bookkeeping does not get to destroy the user's.

    `check=True` IS THE DEFAULT AND IS NOT OVERRIDDEN. A missing seat branch makes `fetch`
    exit non-zero (measured: rc=128, "couldn't find remote ref"); with `check=False` the
    caller would get rc≠0, empty stdout, and no way to tell "the seat produced nothing" from
    "the fetch failed" — the same collapse the rest of this package refuses.
    """
    src = branch(run_id, seat)
    dst = f"refs/khenrix-forge/{run_id}/{seat}"
    gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
               "fetch", "--no-tags", "--no-write-fetch-head", str(clone_path),
               f"+refs/heads/{src}:{dst}",
               env_extra=gitcmd.READONLY, timeout=300)
    # `--verify <ref>^{commit}` rather than a bare rev-parse: it fails when the ref names no
    # object this repository actually holds, which is the one way a fetch can report success
    # over work that did not cross. The emptiness test below is what is left after that —
    # `check=True` has already raised for every failure, so a blank answer is the only
    # remaining shape, and it fails closed instead of being returned as an oid.
    oid = gitcmd.git(repo, "rev-parse", "--verify", f"{dst}^{{commit}}",
                     env_extra=gitcmd.READONLY).stdout.strip()
    if not oid:
        raise HandoverError(
            f"{dst} was fetched and names no object. The fetch reported success, so this is "
            "the state where the record would say a seat's work crossed and nothing did.")
    return oid


@dataclass(frozen=True)
class Mergeability:
    """§16's table as a value, with the reason attached rather than left re-derivable.

    `integration` is the argv sequence the user runs, as a tuple of tokens and never a shell
    string: this package's own rule, and a handover line a user pastes is exactly where a
    string would acquire a `&&`.

    A `MERGE_READY` carrying an `integration` is not a contradiction — the merge command IS
    the integration — but either kind carrying an EMPTY one leaves the user holding a
    deliverable with no next step, so `__post_init__` refuses it.
    """
    kind: str
    why: str
    integration: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise HandoverError(f"kind is one of {list(KINDS)}, not {self.kind!r}")
        if not self.why.strip():
            raise HandoverError("a mergeability decision states the condition it read")
        if not self.integration:
            raise HandoverError(
                f"a {self.kind!r} handover with no integration command tells the user what "
                "they have and not what to do with it")


def _measured(label: str, value) -> str:
    """`value` as an oid, or a refusal naming what was not measured.

    NOT AN `is None` TEST, and the difference is the whole reason this is a function. Every
    oid this module compares arrives as `gitcmd.git(...).stdout.strip()`, whose failure shape
    is the EMPTY STRING and not `None` — so a guard written against `None` alone lets `""`
    through to a `!=` that answers "these two trees differ" about a tree nobody looked at.
    `None` and `""` are the same fact here and they get the same refusal.
    """
    if not isinstance(value, str) or not value.strip():
        raise HandoverError(
            f"{label} was not measured ({value!r}), so §16's conditions cannot be read. An "
            "unmeasured tree compares unequal to every oid, which would report a measured "
            "mismatch about a tree nobody looked at.")
    return value.strip()


def mergeability(manifest, *, synthesis_tree_oid, sidecars) -> Mergeability:
    """§16's table: which of the two deliverable shapes this run can offer.

    THREE READINGS, AND `synthesis_tree_oid` IS READ BY ONE OF THEM RATHER THAN ONLY
    LIVENESS-CHECKED. In order:

    1. THE SYNTHESIS TREE AGAINST B1's TRACKED TREE. `cli.start` creates the worktree at
       `manifest.baseline_commit`, whose tree is `manifest.tracked_tree_oid`, so a synthesis
       oid EQUAL to it is a worktree nobody fused into. Without this reading the parameter is
       validated and then discarded, and a run in which the orchestrator produced nothing
       reports "the branch merges as it stands" over an empty delivery — *does nothing leave
       the same record as nobody?* answered wrong in the flagship artifact. It raises, because
       there is no delivery to describe and a third `kind` would put an empty one into
       `Handover` and thence into `--gc`'s licence to delete.
    2. TWO CONDITIONS FOR MERGE-READY, NOT ONE. `tracked_tree_oid == base_commit^{tree}` AND
       no sidecars. A tree that matches while carrying an out-of-band artifact is merge-ready
       for its tracked half and silently incomplete for the other, which is the reading the
       second condition exists to forbid — and §16 closes the same hole in prose one paragraph
       on ("merging the branch alone does not install out-of-band artifacts").
    3. Everything else is `PATCH_ONLY`, with every reason it read attached.

    A TREE NOBODY MEASURED IS NOT A TREE THAT DID NOT MATCH — see `_measured`, which is where
    that refusal is spelled and why it covers the empty string as well as `None`. It runs
    first, before either comparison, and it runs over the MANIFEST's own oids too: a manifest
    whose `tracked_tree_oid` is blank would otherwise make reading (2) report a dirty baseline
    about a baseline nobody measured.

    `sidecars` IS THE CANDIDATE'S OUT-OF-BAND SET AND `None` IS NOT `()`. An empty tuple is
    "this candidate produced no ignored artifacts"; `None` is "nobody enumerated them", and
    the second may not be read as the first.
    """
    synthesis_tree_oid = _measured("the synthesis tree's oid", synthesis_tree_oid)
    tracked_tree_oid = _measured("the run's baseline tracked tree", manifest.tracked_tree_oid)
    base_commit = _measured("the run's base commit", manifest.base_commit)
    if sidecars is None:
        raise HandoverError(
            "the out-of-band set was not enumerated. `()` is a candidate that produced no "
            "ignored artifacts; `None` is nobody having looked, and §16's second condition "
            "cannot be read off it.")
    if synthesis_tree_oid == tracked_tree_oid:
        raise HandoverError(
            f"the synthesis worktree's tree is {synthesis_tree_oid}, which is B1's own tracked "
            "tree: nothing was fused into it. There is no delivery to describe, and describing "
            "one would report a merge-ready branch over an empty worktree — a run in which "
            "NOTHING happened leaving the same record as one in which NOBODY did. Fuse in the "
            "synthesis worktree and commit there, then re-run `--collect`.")
    base_tree = gitcmd.git(manifest.repo_path, "rev-parse", f"{base_commit}^{{tree}}",
                           env_extra=gitcmd.READONLY).stdout.strip()
    b = branch(manifest.run_id, SYNTHESIS)
    if tracked_tree_oid == base_tree and not sidecars:
        return Mergeability(
            MERGE_READY,
            f"the run's baseline tracked tree is exactly {base_commit}^{{tree}} and "
            "this candidate carries no out-of-band artifacts, so the branch merges as it "
            "stands",
            ("git", "merge", "--no-ff", b))
    reasons = []
    if tracked_tree_oid != base_tree:
        reasons.append(
            f"the baseline was dirty: B1's tracked tree {tracked_tree_oid} is not "
            f"{base_commit}^{{tree}} ({base_tree}), so a merge would carry the "
            "user's uncommitted work as though forge had authored it")
    if sidecars:
        paths = sorted(s.path for s in sidecars)
        # The count is stated beside the names AND the truncation is marked. `len(sidecars)`
        # with five names and no ellipsis reads as a complete list to anyone who does not
        # count them, which is a reason that describes less evidence than it was given.
        named = ", ".join(paths[:5]) + (", …" if len(paths) > 5 else "")
        reasons.append(
            f"{len(sidecars)} out-of-band artifact(s) are not in the tracked tree at all "
            f"({named}); merging the branch would not install them")
    return Mergeability(
        PATCH_ONLY, "; ".join(reasons),
        ("git", "apply", "--binary", "--index", "<run-dir>/handover/B-to-S.patch"))


@dataclass(frozen=True)
class OutOfBand:
    """One ignored artifact, retained rather than committed.

    `size` SITS BESIDE `sha256` BECAUSE AN EMPTY FILE HASHES TO SOMETHING. `sha256(b"")` is a
    real, stable, valid-looking digest, so a consumer comparing hashes cannot tell a file with
    no bytes from a file nobody read. The pair can.

    `copy_command` is argv tokens. §16 asks for "an explicit copy command"; a string would be
    the one line in this package a user is invited to paste into a shell.

    WHAT THE HASH IS OF: the CANDIDATE's payload, as `bundle.build` read it out of the seat.
    The copy command names where the orchestrator put that artifact in the synthesis tree.
    Those are two facts and this record does not check them against each other — nothing here
    opens the synthesis tree — so a reader comparing the hash to the file must do that
    comparison itself rather than take this record for having done it.
    """
    path: str
    sha256: str
    size: int
    copy_command: tuple[str, ...]


def out_of_band(sidecars, *, synthesis_path) -> tuple[OutOfBand, ...]:
    """§16's out-of-band deliverables, enumerated with hashes and a copy command each.

    NEVER FORCE-ADDED. §16: "that violates the originating skill's contract and would put
    `node_modules` in the object store forever." Nothing here stages anything; the artifacts
    stay in the synthesis tree and the user copies them. The force flags in both their
    spellings appear nowhere in this module and a test parses the source to say so.

    `None` RAISES AND `()` DOES NOT. See `mergeability` for the same distinction one condition
    over: an empty enumeration and an absent one are the states §16's second table row turns
    on.

    A PAYLOAD THAT IS NOT BYTES RAISES rather than being stringified. `str(None).encode()` is
    four bytes with a real, stable digest, so coercion would give a sidecar nobody read the
    same shape as one that was read — the same collapse the `size` field exists to prevent,
    arriving through the field the size is computed from.
    """
    if sidecars is None:
        raise HandoverError(
            "the out-of-band set was not enumerated; `()` is what a candidate with no ignored "
            "artifacts carries, and reporting that here for a set nobody assembled would tell "
            "the user there is nothing to copy")
    out = []
    for s in sidecars:
        if not isinstance(s.payload, bytes):
            raise HandoverError(
                f"the sidecar {s.path!r} carries {type(s.payload).__name__} where "
                "`bundle.SidecarEntry` declares bytes; hashing what it stringifies to would "
                "give an unread artifact a real digest and a plausible size")
        out.append(OutOfBand(
            path=s.path,
            sha256=hashlib.sha256(s.payload).hexdigest(),
            size=len(s.payload),
            copy_command=("cp", "-p", str(Path(synthesis_path) / s.path), s.path)))
    return tuple(sorted(out, key=lambda o: o.path))


@dataclass(frozen=True)
class Handover:
    """What was delivered, recorded so `--gc` can say what "unmerged" means.

    `handover_target` IS §15's WHOLE POINT. "It refuses to delete a synthesis worktree/branch
    not marked handed over, and `handover_target` (or explicit user acceptance) is recorded so
    'unmerged' is well-defined — a patch-based handover may intentionally never merge the
    internal branch." So a run whose delivery was a PATCH is handed over even though its
    branch will never appear in the user's history, and `--gc` must not read the absent merge
    as unfinished work.

    `accepted` is the explicit-user-acceptance half. Both fields exist because §15 names both
    and they answer different questions: `handover_target` is where the work went, `accepted`
    is whether the user said so. `__post_init__` refuses a record carrying NEITHER, because
    that is the state in which "unmerged" is undefined again.

    NEITHER FIELD IS READ FOR TRUTHINESS. `accepted="no"` is a true value in Python and would
    have licensed `--gc` to delete a run nobody accepted; `handover_target=""` names no ref
    while passing an `is None` test. Both are the shape where two different failures compare
    equal, on the one record whose whole job is to keep them apart, so the types are checked
    rather than the truth values.

    `baseline_owned` is §16's other sentence: unchanged selected untracked/ignored files are
    the BASELINE's, and only their B→S changes are forge-authored. Carried as the explicit
    path list rather than derived at render time, so the handover text and this record cannot
    disagree about which files the run claims authorship of.

    `b1_files` is §16's "the B1 file list is enumerated in the handover text, not only at a
    confirmation gate an hour earlier".
    """
    run_id: str
    branch: str
    kind: str
    handover_target: str | None
    accepted: bool
    out_of_band: tuple
    baseline_owned: tuple[str, ...]
    b1_files: tuple[str, ...]
    why: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise HandoverError(f"kind is one of {list(KINDS)}, not {self.kind!r}")
        if not isinstance(self.accepted, bool):
            raise HandoverError(
                f"accepted is a bool, not {self.accepted!r}. Every non-empty string is true "
                "here, so `accepted='no'` would record a run the user refused as one they "
                "took delivery of, and `--gc` reads this field for its licence to delete.")
        if self.handover_target is not None and not (
                isinstance(self.handover_target, str) and self.handover_target.strip()):
            raise HandoverError(
                f"handover_target is the ref the work went to, or None because there was "
                f"none, not {self.handover_target!r}. An empty string names nothing while "
                "passing every test for being present.")
        if not self.why.strip():
            raise HandoverError("a handover states the condition it was decided on")
        if self.handover_target is None and not self.accepted:
            raise HandoverError(
                "a handover names where the work went or records that the user accepted it "
                "without one. Neither is `--gc`'s licence to delete a synthesis tree, and a "
                "record carrying neither makes 'unmerged' undefined — which is exactly what "
                "§15 says this field exists to prevent.")


def _row(h: Handover) -> dict:
    row = {f.name: getattr(h, f.name) for f in fields(Handover)}
    row["out_of_band"] = [{"path": o.path, "sha256": o.sha256, "size": o.size,
                           "copy_command": list(o.copy_command)} for o in h.out_of_band]
    row["baseline_owned"] = list(h.baseline_owned)
    row["b1_files"] = list(h.b1_files)
    return row


def write_handover(run_dir, h: Handover) -> None:
    """Persist the delivery. `atomic_write`, not `exclusive_write`: an acceptance recorded
    later rewrites this record, and the manifest is the run's write-once identity, not this.

    THE ROUND TRIP IS CHECKED BEFORE THE WRITE, on `taskbundle.write_task_bundle`'s precedent
    and for its reason: JSON has one sequence type, so a tuple of `OutOfBand` written naively
    reads back as a list of dicts and compares unequal in silence.
    """
    if not isinstance(h, Handover):
        raise HandoverError(f"a Handover is required, not {type(h).__name__}")
    blob = json.dumps(_row(h), sort_keys=True, indent=2).encode("utf-8") + b"\n"
    restored = _decode(json.loads(blob))
    if restored != h:
        differing = [f.name for f in fields(Handover)
                     if getattr(restored, f.name) != getattr(h, f.name)]
        raise HandoverError(
            f"this handover does not survive its own round trip; {differing} come back as a "
            "different type")
    storage.atomic_write(storage.handover_path(run_dir), blob)


def _decode(row) -> Handover:
    if not isinstance(row, dict):
        raise HandoverError(f"a handover record is an object, not a {type(row).__name__}")
    try:
        oob = tuple(OutOfBand(path=r["path"], sha256=r["sha256"], size=r["size"],
                              copy_command=tuple(r["copy_command"]))
                    for r in row["out_of_band"])
        return Handover(run_id=row["run_id"], branch=row["branch"], kind=row["kind"],
                        handover_target=row["handover_target"], accepted=row["accepted"],
                        out_of_band=oob,
                        baseline_owned=tuple(row["baseline_owned"]),
                        b1_files=tuple(row["b1_files"]), why=row["why"])
    except (KeyError, TypeError) as e:
        raise HandoverError(
            f"this is not a handover record this engine can read ({e}); a partial read would "
            "hand `--gc` a decision about a tree it cannot describe") from e


def read_handover(run_dir):
    """The delivery record, or `None` because this run has not been handed over.

    `None` FOR THAT AND FOR NOTHING ELSE, on `taskbundle.read_task_bundle_if_recorded`'s rule.
    `--gc` reads this to decide whether it may delete a synthesis worktree. Folding a corrupt
    record into `None` would look exactly like a run that was never delivered, whose tree
    `--gc` then refuses to touch — fail-closed for safety, and silent for the operator, who
    would find a run they did hand over become undeletable with no message saying why.
    """
    path = storage.handover_path(run_dir)
    if not path.exists():
        return None
    try:
        row = json.loads(path.read_bytes())
    except ValueError as e:
        raise HandoverError(f"{path} is not readable as JSON: {e}") from e
    return _decode(row)


@dataclass(frozen=True)
class SeatLine:
    """One seat's contribution to §16.1's first line.

    `verify_outcome` is `None` for a seat that was never verified, and that is DIFFERENT from
    every one of `verify.OUTCOMES`: §8 fixes a `failed` seat's verdict whatever §6 would find,
    and a seat whose verification was refused keeps its pre-verification verdict. Counting
    `None` as a non-PASS is correct for "how many passed"; reporting it as FAIL would be a
    verdict about a gate that never ran, and the header states the two separately.
    """
    name: str
    forge: str
    artifacts: str
    verify_outcome: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise HandoverError(
                f"a seat row names the seat it counts, not {self.name!r}. §16.1's first line "
                "is a count over these rows, and a row naming nobody is a seat the operator "
                "cannot ask about — nor one this record can tell apart from another like it.")
        if self.forge not in _SEAT_FORGE:
            raise HandoverError(f"forge is one of {list(_SEAT_FORGE)}, not {self.forge!r}")
        if self.artifacts not in _SEAT_ARTIFACTS:
            raise HandoverError(
                f"artifacts is one of {list(_SEAT_ARTIFACTS)}, not {self.artifacts!r}")
        if self.verify_outcome is not None and self.verify_outcome not in verifymod.OUTCOMES:
            raise HandoverError(
                f"verify_outcome is one of {list(verifymod.OUTCOMES)} or None, not "
                f"{self.verify_outcome!r}; None is a seat §6 never verified and is not a "
                "seventh verdict")


@dataclass(frozen=True)
class Provenance:
    """Everything §16.1's header reports, validated before a line of it is rendered.

    THE VALIDATION IS THE POINT. §18 asks that "failure cannot render a success header" be
    tested, and the only way that holds for inputs no fixture reaches is for the RECORD to
    refuse the combination rather than for the renderer to remember not to print it.

    `strongest` IS §12.5's `(seat | None, why)` PAIR AND BOTH HALVES ARE REQUIRED. `None` is
    the ordinary answer, not an error: `coverage._schema` is `unresolved` by construction, and
    `coverage._prose` is `unresolved` without a recorded trace and `manual_trace_confirmed`
    with one — both of which `coverage.unmeasured` counts — so a real ledger carrying either
    kind makes the report incomplete and `rubric.strongest` names nobody. §16.1's header therefore renders the reason as a LINE. A header that dropped the
    line when there was no winner would read as a header with nothing to say about strength,
    which is the fail-open this whole record exists to close one field over.

    `agreement` is §11's label and is likewise always rendered. §11 makes agreement provenance
    and never a correctness argument, so `differently-prompted` — the ordinary answer for a
    real fleet, since three seats are three different CLIs and `cli_version` differs — is
    information, not a defect.

    `synthesis_measured` IS THE FIELD THAT KEEPS THE HEADER HONEST, and it exists because
    §16's synthesis author is the ORCHESTRATOR and not this engine. `--collect` is handed a
    verdict; it does not create a verifier clone for the fusion and does not run the confirmed
    command over it. So `synthesis_outcome` has two provenances that must never render alike:
    MEASURED by this engine, or ASSERTED by whoever fused. `header` renders them as different
    sentences and `text` prints the "Verified means" paragraph only beside the first —
    otherwise the one artifact whose job is to say what was verified would be the one asserting
    a verification that never happened.

    A MEASURED VERDICT CARRIES ITS MEASUREMENT AND AN UNMEASURED ONE CARRIES NO DURATION.
    `synthesis_measured=True` with no outcome, or with no `verify_seconds`, is a record
    claiming a measurement it cannot show, and it is the exact route by which a caller passing
    the wrong flag would re-label an asserted verdict as a measured one. The other direction is
    the same defect read backwards: a duration beside `synthesis_measured=False` sits under a
    line that says in words that this engine did not run the command, so the record would be
    timing a run it states it did not perform.
    """
    seats: tuple
    synthesis_outcome: str | None
    synthesis_measured: bool
    verify_command: str
    verify_seconds: float | None
    strategy: str | None
    strongest: tuple
    agreement: str
    review_terminal: str | None
    review_rounds: int
    unresolved_findings: int
    deep_review: object

    def __post_init__(self) -> None:
        if not self.seats:
            raise HandoverError(
                "a run reports the seats it ran. An empty tuple renders §16.1's first line as "
                "'0 of 0', which reads as a completed run rather than as one nobody described.")
        wrong = sorted({type(s).__name__ for s in self.seats if not isinstance(s, SeatLine)})
        if wrong:
            raise HandoverError(f"a provenance record's seats are SeatLine rows, not {wrong}")
        names = [s.name for s in self.seats]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            raise HandoverError(
                f"{repeated} appear more than once. §16.1's first line counts these rows, so "
                "one seat filed twice reads as two seats — a fleet of one reporting '2 of 2 "
                "completed' over the single clone anybody paid for.")
        if self.agreement not in fingerprint.LABELS:
            raise HandoverError(
                f"agreement is one of {list(fingerprint.LABELS)}, not {self.agreement!r}")
        if not (isinstance(self.strongest, tuple) and len(self.strongest) == 2):
            raise HandoverError(
                "strongest is §12.5's (seat | None, why) pair; a bare name would leave the "
                "header unable to say why nobody was named, which is the ordinary outcome")
        if not str(self.strongest[1]).strip():
            raise HandoverError("§12.5's answer carries the reason it reached it")
        if self.strongest[0] is not None and self.strongest[0] not in names:
            raise HandoverError(
                f"the strongest seat is {self.strongest[0]!r}, which is not one of this run's "
                f"seats ({names}). §12.5 ranks the fleet that ran, and a header naming a seat "
                "outside it reports a comparison over a clone this run never made.")
        if not isinstance(self.verify_command, str) or not self.verify_command.strip():
            raise HandoverError(
                f"the confirmed verify command is what §16.1's header quotes and what its "
                f"\"Verified\" sentence is about, not {self.verify_command!r}; empty backticks "
                "in that line describe a gate with no command in it")
        if self.strategy is not None and self.strategy not in strategymod.STRATEGIES:
            raise HandoverError(
                f"strategy is one of {list(strategymod.STRATEGIES)} or None because none was "
                f"recorded, not {self.strategy!r}")
        if self.synthesis_outcome is not None and \
                self.synthesis_outcome not in verifymod.OUTCOMES:
            raise HandoverError(
                f"synthesis_outcome is one of {list(verifymod.OUTCOMES)} or None, not "
                f"{self.synthesis_outcome!r}")
        if not isinstance(self.synthesis_measured, bool):
            raise HandoverError(
                f"synthesis_measured is a bool, not {self.synthesis_measured!r}: a truthy "
                "string here would print an asserted verdict under the Verified sentence")
        _seconds(self.verify_seconds)
        if self.synthesis_measured and (self.synthesis_outcome is None
                                        or self.verify_seconds is None):
            raise HandoverError(
                "this record claims the synthesis verdict was MEASURED by this engine and "
                "carries no outcome or no duration. A measurement this engine cannot show is "
                "not one, and the Verified sentence is printed on the strength of this flag.")
        if self.verify_seconds is not None and not self.synthesis_measured:
            raise HandoverError(
                f"this record times {self.verify_seconds!r}s of verifying beside a verdict it "
                "says this engine did not measure — and both of the lines an unmeasured "
                "verdict renders state in words that the command was not run here")
        completed = [s for s in self.seats if s.forge == "completed"]
        if self.synthesis_outcome is not None and not completed:
            raise HandoverError(
                "this record reports a synthesis verdict over a fleet in which no seat "
                "completed. §18 asks that a failure cannot render a success header, and this "
                "is the combination that does it.")
        if not isinstance(self.deep_review, deepmod.DeepReview):
            raise HandoverError("deep_review is a deepreview.DeepReview, not "
                                f"{type(self.deep_review).__name__}")
        for label in ("review_rounds", "unresolved_findings"):
            v = getattr(self, label)
            # `bool` explicitly: `True` is an `int` and renders as a count of one, so a record
            # answering "were there rounds?" would be printed as "1 round(s)".
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise HandoverError(f"{label} is a count, not {v!r}")
        if self.review_terminal is not None and self.review_terminal not in _TERMINALS:
            raise HandoverError(
                f"review_terminal is one of {list(_TERMINALS)} or None, not "
                f"{self.review_terminal!r}")
        if self.review_terminal is None and (self.review_rounds or self.unresolved_findings):
            raise HandoverError(
                "no terminal was reached, which is the record of a review nobody convened — "
                "and the line that renders says exactly that while printing neither number. A "
                f"record carrying {self.review_rounds} round(s) and "
                f"{self.unresolved_findings} unresolved finding(s) under it has its blockers "
                "disappear from the one document the operator acts on.")
        if self.review_terminal is not None and self.review_rounds < 1:
            raise HandoverError(
                f"{self.review_terminal!r} is a conclusion §13 draws from at least one round — "
                "`review._check_rounds_run` refuses anything less on its own side — so this "
                "record reports a review's verdict over a review that was never convened")
        if self.review_terminal == _REVIEW_BLOCKED and self.unresolved_findings < 1:
            raise HandoverError(
                "§13's `review_blocked` is a terminal a run reaches BECAUSE a blocker is "
                "unresolved, so a record carrying it beside zero unresolved findings would "
                "render a clean provenance header with an unresolved blocker — the one thing "
                "§13 says must never happen")


def _seconds(v) -> None:
    """`verify_seconds` is a duration or nothing at all.

    RENDERED THROUGH `:.0f`, which is why the type is checked rather than trusted: a `bool`
    is an `int` and formats as `1s`, so a record answering "did it run?" would be printed as a
    one-second verify; a string raises out of the middle of the header; and a negative reads
    as a measurement taken backwards. All three describe a run nobody timed.
    """
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise HandoverError(
            f"verify_seconds is how long the confirmed command took, or None because nothing "
            f"here timed it, not {v!r}")


def _deep_review_line(u) -> str:
    """§13.1's three statuses, three renderings. §16.1's example shows only the first, and the
    other three are not decoration: an operator reading `Deep review:` needs to know whether
    the deep review found nothing, was never asked, or could not be asked
    somewhere with a URL they can open.

    THE SKIPPED LINE READS THE RECORD'S OWN REASON RATHER THAN NAMING THE FLAG. `run_deep_review`
    skips for `--skip-deep-review` and for nothing else, but `skipped` is the status ANY caller writes
    for a review it declined to request — a run with no candidate to review, for one — and a
    line spelling `--skip-deep-review` over such a record tells the operator they opted out of
    something they did not.

    A STATUS THIS FUNCTION DOES NOT KNOW RAISES rather than falling through to the cheapest of
    the four. `deepreview.STATUSES` is the vocabulary; a fifth member added there would otherwise
    render here as a review nobody requested, which is the collapse the branches exist for.
    """
    if u.status == deepmod.RAN:
        n = len(u.findings)
        # WHICH SEATS ANSWERED IS PART OF THE CLAIM. A 1-of-3 panel and a full one produce
        # the same finding count, and the cloud review this replaced had no such
        # distinction to report — so a header that omitted it would let a degraded review
        # read as a complete one.
        who = ", ".join(u.seats) if u.seats else "no named"
        return (f"Deep review: {n} finding(s) reported by {who} seat(s)"
                + ("" if u.diff_measured else " (over a diff whose size could not be measured)"))
    if u.status == deepmod.UNAVAILABLE:
        return f"Deep review: unavailable ({u.reason}) — the run proceeded to handover"
    if u.status == deepmod.SKIPPED:
        why = u.detail.strip() if isinstance(u.detail, str) else ""
        return (f"Deep review: not requested ({why})" if why else
                "Deep review: not requested, and this record does not say why")
    raise HandoverError(
        f"{u.status!r} is not one of §13.1's statuses, so this header has no rendering "
        "for it. Falling through to one of the four would report an unknown state as the "
        "review nobody asked for, which is the cheapest of them to read.")


def header(p: Provenance) -> str:
    """§16.1's provenance header.

    §16.1'S FORBIDDEN VERB APPEARS NOWHERE IN THIS MODULE — not in the rendered text, not in a
    docstring, and not in this sentence. It is forbidden for a seat that produced artifacts and
    failed verify, and the enforcement is a source-level assertion over the whole file rather
    than this function remembering which seats it may use it for: a rule that holds only on the
    branches its author remembered is the shape this package refuses everywhere else. That
    assertion is also why the word is described here instead of quoted.

    THE FUSION AND AGREEMENT LINES ARE ALWAYS PRESENT. §12.5's `strongest` names nobody
    whenever any seat is unrankable, and a real ledger carrying a `schema` or untraced `prose`
    criterion makes that the ORDINARY answer. A header that printed the line only when there
    was a winner would leave the operator reading a document with no strength claim in it and
    no way to tell that from a tool that had not looked.
    """
    n = len(p.seats)
    completed = sum(1 for s in p.seats if s.forge == "completed")
    usable = sum(1 for s in p.seats if s.artifacts == "usable")
    passed = sum(1 for s in p.seats if s.verify_outcome == verifymod.PASS)
    unverified = sum(1 for s in p.seats if s.verify_outcome is None)
    first = (f"**Forge: {completed} of {n} seats completed; {usable} artifact set(s) usable; "
             f"{passed} of {n} passed verify")
    if unverified:
        # "did not pass" and "was never verified" are different facts about a paid clone, and
        # a bare `passed/n` spells them the same way.
        first += f" ({unverified} never verified)"
    first += "."

    strat = p.strategy or "strategy not recorded"
    if p.synthesis_outcome is None and not completed:
        second = ("Synthesis: no seat produced a candidate, so no synthesis was attempted and "
                  "there is no verify result to report.")
    elif p.synthesis_outcome is None:
        # NO VERDICT IS NOT NO CANDIDATE. Seats completed here; nobody reported an outcome for
        # the fusion — `--collect` was run without one. Saying "no seat produced a candidate"
        # would be this header inventing a fleet failure out of a missing argument.
        second = (f"Synthesis: {completed} of {n} seats produced a candidate and no verify "
                  "verdict was reported for the fusion. This engine did not run one either, "
                  "so there is nothing here about whether the fused work passes.")
    elif p.synthesis_measured:
        second = (f"Synthesis: verify {p.synthesis_outcome} (`{p.verify_command}`, "
                  f"{p.verify_seconds:.0f}s) — {strat}.")
    else:
        # THE ASSERTED RENDERING, AND IT DOES NOT BORROW THE MEASURED ONE'S WORDS. §16 makes
        # the orchestrator the synthesis author; `--collect` is handed this verdict and never
        # creates a verifier clone for the fusion. "verify PASS" would be this engine's own
        # vocabulary for something it ran, spent on something it did not.
        second = (f"Synthesis: the orchestrator reports {p.synthesis_outcome} for "
                  f"`{p.verify_command}` — {strat}; this engine did not run it: no verifier "
                  "clone was made for the fusion, so this line is a report and not a "
                  "verification.")

    name, why = p.strongest
    fusion = (f"Fusion: strongest seat: {name} — {why}." if name else f"Fusion: {why}.")
    fusion += f" §11 agreement: {p.agreement}."

    if p.review_terminal is None:
        third = "Council: no review round was convened."
    else:
        third = (f"Council: {p.review_rounds} round(s), {p.unresolved_findings} finding(s) "
                 f"unresolved ({p.review_terminal}).")

    return "\n".join([first, second, fusion, third, _deep_review_line(p.deep_review) + "**"])


_VERIFIED_MEANS = (
    "\"Verified\" here means exactly this and no more: the confirmed verify command exited 0 "
    "on a fresh verifier clone at the final checkpoint. It does not mean the change has no "
    "new defects, and it is not a review."
)

# THE OTHER PARAGRAPH, AND THE REASON THERE ARE TWO. The sentence above describes a
# measurement THIS ENGINE TOOK. A synthesis verdict reaches `--collect` from the orchestrator
# that fused, and printing that under the sentence above would claim a verifier clone nobody
# made. Neither paragraph is a hedge of the other: one says what a PASS is, the other says
# who said so.
_ASSERTED_MEANS = (
    "This run's synthesis verdict was reported by the orchestrator that fused the candidates; "
    "this engine did not run it: no verifier clone was made for the fusion and the confirmed "
    "verify command was not executed here, so nothing above is a verification. To get one, run "
    "the confirmed command yourself in a fresh clone of the synthesis branch."
)

_NO_VERDICT_MEANS = (
    "There is no synthesis verdict here at all — not a failing one, and not one somebody else "
    "reported. Nothing above says whether the fused work passes this repository's gate, and "
    "the absence is not a pass: run the confirmed verify command yourself in a fresh clone of "
    "the synthesis branch before you treat any of this as working."
)


def _means(p: Provenance) -> str:
    """The one provenance paragraph this run has earned. THREE STATES, THREE RECORDS: a verdict
    this engine measured, a verdict somebody else reported, and no verdict. Folding the third
    into the second would say the orchestrator reported something over a run where nobody
    reported anything."""
    if p.synthesis_outcome is None:
        return _NO_VERDICT_MEANS
    return _VERIFIED_MEANS if p.synthesis_measured else _ASSERTED_MEANS


def text(h: Handover, p: Provenance) -> str:
    """The whole handover message: the header, what was delivered, and what merging does not do.

    §16 requires the B1 file list HERE rather than only at a confirmation gate an hour earlier,
    and it requires the sentence about out-of-band artifacts to be stated plainly rather than
    left to be inferred from the fact that they are listed separately.

    EXACTLY ONE OF THE THREE PROVENANCE PARAGRAPHS, NEVER TWO AND NEVER NONE — measured,
    asserted, or no verdict at all. Three states, three records; collapsing the third into the
    second would tell the user the orchestrator reported something when nobody reported
    anything. Which one is `_means`'s decision and not this line's discretion, and `Provenance`
    has already refused a `synthesis_measured=True` it cannot show a measurement for.
    """
    if not isinstance(h, Handover):
        raise HandoverError(f"a Handover is required, not {type(h).__name__}")
    if not isinstance(p, Provenance):
        raise HandoverError(f"a Provenance is required, not {type(p).__name__}")
    parts = [header(p), "", _means(p), "",
             f"Branch: `{h.branch}` ({h.kind}) — {h.why}"]
    if h.handover_target:
        parts.append(f"Handed over to: {h.handover_target}")
    elif h.accepted:
        parts.append("Handed over: accepted by the user without a merge target — a "
                     "patch-based handover may intentionally never merge this branch.")
    parts += ["", "B1 — the baseline this run started from:"]
    parts += [f"  {f}" for f in h.b1_files] or ["  (no files)"]
    if h.baseline_owned:
        parts += ["", "Baseline-owned — unchanged selected untracked/ignored files. These are "
                      "yours, not forge's; only their B→S changes are forge-authored:"]
        parts += [f"  {f}" for f in h.baseline_owned]
    if h.out_of_band:
        parts += ["", "Out-of-band artifacts. MERGING THE BRANCH ALONE DOES NOT INSTALL THESE "
                      "— they are ignored files and were never added to the object store:"]
        for o in h.out_of_band:
            parts.append(f"  {o.path}  sha256={o.sha256}  {o.size} byte(s)")
            parts.append(f"    {' '.join(o.copy_command)}")
    else:
        parts += ["", "No out-of-band artifacts: this delivery is entirely in the branch."]
    return "\n".join(parts) + "\n"
