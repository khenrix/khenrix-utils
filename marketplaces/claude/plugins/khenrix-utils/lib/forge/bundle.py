"""The CandidateBundle: exactly what crosses from a seat into a verifier (spec §6).

A verifier clone is worthless if the thing verified is the seat's own tree — the builder
could have rigged it. So the candidate crosses as DATA, and the bundle is that data.

Two channels, because `tracked_diff` is a partial view of `paths` for three causes
(untracked, binary/-diff, submodule content):
  * tracked_patch  — `git diff --binary` bytes, applied with `git apply --index`
  * sidecars       — everything else, carried as literal payload with its mode

Anything neither channel can carry honestly goes in `omitted`, so a verifier failure
caused by a missing input is never mistaken for a candidate defect.

D-1 — A TRACKED SYMLINK IS ITS TARGET TEXT, AND IT CROSSES.
A symlink is an artefact an agent legitimately produces (`docs/latest -> v2`,
`.bin/tool -> ../pkg/tool`), so dropping every one of them would silently mutilate ordinary
candidates. It crosses as `kind="symlink"` with the TARGET TEXT as payload — the same
identity `snapshot._symlink_entry` already gives it — and never as its target's content:
reading through the link would put content from outside the candidate into the candidate.

Two things were required to make that one answer rather than a third opinion. First,
`baseline`'s manifest hashed a tracked link THROUGH itself (`ls-files` + `is_file()`), so B
described a link as its target's CONTENT while `snapshot` described it as the target's TEXT
and `fleet` skipped it rather than choose. `baseline._entry_digest` digests the target TEXT
and `fleet` VERIFIES a seat's link against that digest rather than stepping over it, so
`baseline`, `snapshot`, `fleet` and this module give one shape one identity. Second,
containment: an ESCAPING link does NOT cross. Materializing `out -> ../../user-repo` into a
verifier and then running the confirmed verify command there would write outside the clone,
which is the one thing §4 and §6 exist to prevent — so an escaping link goes to `omitted`,
where a verifier failure attributable to it is at least honest.

The escape test is LEXICAL, over the target text: that text is the only thing that crosses,
and where it lands is decided in the VERIFIER's tree, not the seat's. It composes because
each link is tested on its own — `a -> b` with `b -> ../../x` leaves `a` carried and `b`
omitted, so in the verifier `a` dangles rather than escapes.

WHAT THIS CONTAINMENT DOES NOT COVER. Only an AGENT-PRODUCED link reaches the test: a link
already in B is in neither the diff against B nor `artifacts.paths`, so `_escaping_link`
never sees it. That scope leans on `inspect.rejections` refusing a tracked escaping link at
preflight, and `rejections` is a POLICY nothing calls — measured in
`test_forge_seams.py::test_nothing_in_the_chain_consults_either_refusal`. So such a link
does reach a baseline, a seat and a verifier, and a verify command run there reads AND
writes a host path through it (`test_forge_seams.py::_harm_of_escaping_link`). Widening this
module is not the fix: the link is B's, so B is where the decision belongs.

D-2 — THE ESCAPE-VS-INTERNAL DISCRIMINATION IS THE CALLER'S, NOT `screen`'s. See
`screen.screen_tree`'s docstring, which states the sound predicate. It is NOT the one this
module uses, and the two are answering different questions: this module asks "can a write
through this link leave the tree" (a property of the target text), while the screen asks
"was the content behind this link read" (a property of the caller's selection). Reusing the
containment predicate there would certify unread content clean; see
`tests/test_forge_screen.py::test_an_in_tree_link_to_an_unselected_path_is_still_a_breach`.
"""
import os
import stat
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import gitcmd

# Bumped when the wire shape changes. `materialize` refuses anything else rather than
# guessing: a bundle written by a newer engine and applied by an older one would silently
# drop whichever channel the older one does not know about.
VERSION = 1


class BundleError(RuntimeError):
    """The candidate cannot be carried, or cannot be laid down where it was asked to."""


@dataclass(frozen=True)
class SidecarEntry:
    """One path the tracked patch cannot carry.

    `kind` is "file" or "symlink" — the two shapes with an honest payload. Everything else
    is named in `CandidateBundle.omitted` instead, never given a fabricated payload here.

    `mode` is the real `st_mode & 0o777` for a file. For a symlink it is a FABRICATED 0,
    matching `snapshot.Entry`'s symlink branch: a link's own mode is not meaningful and is
    not what `materialize` reproduces — the target text is.

    `payload` is the file's bytes, or the link's target text encoded with surrogateescape
    (a target is a filesystem name, not necessarily valid UTF-8).
    """
    path: str
    kind: str
    mode: int
    payload: bytes


@dataclass(frozen=True)
class CandidateBundle:
    version: int
    baseline_ref: str
    baseline_commit: str
    tracked_patch: bytes = b""
    sidecars: tuple[SidecarEntry, ...] = ()
    # None, never (). `gate_delta` is a DIFFERENCE between two trees' gate surfaces. The
    # detector is `verify.gate_surface`, which answers ONE tree; what produces the delta is
    # `verify.build_verifier`, the one place both trees exist — the clone holds exactly the
    # baseline and `materialize` turns it into exactly the candidate — and it writes the
    # result back through `with_gate_measurement`. `build` is not that caller: it is handed
    # a seat path, an `ArtifactSet` and a `Baseline`, never a checkout the builder did not
    # write. So it leaves this field unset. `Baseline.sidecars` is the precedent: an empty
    # tuple says "the candidate changed nothing that defines the gate" when the truth is
    # "nobody looked", which is the fail-OPEN reading. A consumer classifying
    # `GATE_CHANGED` must treat None as UNKNOWN — not as a clean gate.
    gate_delta: tuple[str, ...] | None = None
    # WHAT THE DELTA ABOVE RANGED OVER: the union of the two trees' gate surfaces, so
    # `gate_delta == ()` over two gate files and `gate_delta == ()` over a tree in which
    # nothing matched a gate-surface rule stop being the same record. They are not the same
    # claim — the first found the gate untouched, the second found no gate to look at — and
    # a consumer holding only the delta has to pick one of them to say. Three states again,
    # for `gate_delta`'s reason and written by the same call: None is "nobody looked", `()`
    # is "looked, and this tree defines its gate somewhere no rule and no command reaches",
    # and a non-empty tuple is the paths that were compared.
    gate_surface: tuple[str, ...] | None = None
    # "" means the run declared no GeneratorContract, which admits NOTHING as a permitted
    # verify-origin output (spec §7.2). That is the fail-closed reading, so it is safe as a
    # default in a way `gate_delta=()` is not.
    generator_contract_id: str = ""
    # A consumer classifying an outcome must treat a non-empty `omitted` as HARVEST_INCOMPLETE
    # before it reads the exit code: the gate can pass BECAUSE something is missing, and the
    # omission is invisible at the gate itself. Measured on the case that motivated the field
    # — a nested repo, whose gitlink is omitted while its content crosses as ordinary
    # sidecars — `git -C sub rev-parse HEAD` inside the verifier exits 0 and answers the
    # VERIFIER's HEAD, because git walks up past the missing gitlink into the parent.
    omitted: tuple[str, ...] = ()


def with_gate_measurement(candidate: CandidateBundle, *, surface,
                          delta) -> CandidateBundle:
    """The same candidate with §6.1's gate measurement recorded: what was examined, and
    what of it moved.

    ONE CALL FOR BOTH FIELDS, and that is the whole of why this is not two functions. A
    delta recorded without its surface is a clean-looking measurement with no record of what
    it ranged over, which is the fail-open reading `gate_delta`'s own three states exist to
    refuse; a surface recorded without a delta says what was examined and not what came of
    it. Neither half is a state the engine should be able to produce, so neither half can be
    written alone.

    A new instance rather than a mutation because `CandidateBundle` is frozen, and it is
    frozen because a candidate that can be edited after it is built is a candidate whose
    manifest describes something else.

    Refuses when either field is already recorded, INCLUDING an empty one. Two measurements
    of one tree pair agree or one of them is wrong, and taking the second silently would
    make which answer survives depend on call order. `()` is a measurement — it is the whole
    difference between "the candidate changed nothing that defines the gate" and "nobody
    looked" — so it is exactly as unoverwritable as a non-empty one.
    """
    # A str is iterable, so `tuple("Makefile")` is eight one-character paths and no caller
    # would see the mistake until a delta named `M`, `a`, `k`. Refused for the reason
    # `Command.parse` refuses a spec that is one string.
    for what, value in (("gate surface", surface), ("gate delta", delta)):
        if isinstance(value, (str, bytes)):
            raise BundleError(
                f"a {what} is a sequence of paths, not one path: {value!r}. Write "
                f"({value!r},) for a single one.")
    for field_name, value in (("gate_delta", delta), ("gate_surface", surface)):
        recorded = getattr(candidate, field_name)
        if recorded is not None:
            raise BundleError(
                f"this candidate already records a {field_name} ({recorded!r}); a second "
                f"measurement ({tuple(value)!r}) is a disagreement, not an update")
    return replace(candidate, gate_delta=tuple(delta), gate_surface=tuple(surface))


def _patch_paths(repo, patch: bytes) -> tuple[frozenset, frozenset]:
    """`(postimages, preimages)` — the paths the patch WRITES and the ones it consumes.

    KEPT APART, not unioned. They mean different things and only the postimage set is
    "carried": a preimage that is not also a postimage is a rename source, and all the patch
    does with it is DELETE it. See `build` for what happens when the seat still has content
    at that name.

    `git apply --numstat -z` is the parse, not a hand-written one: a `diff --git` header
    quotes an unusual path with C escapes, so reading names off the header text means
    reimplementing `unquote_c_style` — and getting it wrong means a path silently
    reclassified as uncarried. Under `-z` git emits `<added> TAB <deleted> TAB <path> NUL`
    with the name raw.

    The reversed pass is what surfaces preimages at all. Measured on git 2.53, a
    `git mv old.txt new.txt` patch:

        git apply --numstat -z      ->  `0\\t0\\tnew.txt\\0`
        git apply -R --numstat -z   ->  `0\\t0\\told.txt\\0`

    Without it `old.txt` — which IS in `artifacts.paths` (snapshot saw it removed) and IS
    deleted by the patch — is invisible here. `git diff` detects renames by default and
    `gitcmd` does not pin `diff.renames`, so this is the ordinary case, not an exotic one.

    Derived from the patch BYTES rather than from a second `git diff` of the seat: this set
    must describe what `materialize` will actually apply, and a seat re-read would answer a
    question about the seat's state now instead.
    """
    if not patch:
        # Not an optimisation. `git apply` exits 128 with "No valid patches in input" on an
        # empty one, and — more importantly — an empty patch carries nothing, so answering
        # from the bytes keeps this and the apply in `materialize` in agreement by
        # construction rather than by two commands happening to concur.
        return frozenset(), frozenset()
    with tempfile.TemporaryDirectory() as td:
        # NOT inside the seat or the verifier: a patch file dropped in either tree is an
        # untracked file the next inventory would report as the agent's work.
        f = Path(td) / "candidate.patch"
        f.write_bytes(patch)
        sides = []
        for extra in ((), ("-R",)):
            r = gitcmd.git(repo, "apply", *extra, "--numstat", "-z", str(f),
                           env_extra=gitcmd.READONLY, check=False, binary=True)
            if r.returncode != 0:
                raise BundleError(
                    "cannot determine what the tracked patch carries: "
                    f"git apply --numstat -> {r.returncode}: "
                    f"{r.stderr.decode('utf-8', 'replace').strip()}")
            # split(maxsplit=2): a path may itself contain a TAB, the two counts never can.
            sides.append(frozenset(rec.split("\t", 2)[2] for rec
                                   in r.stdout.decode("utf-8", "surrogateescape").split("\0")
                                   if rec))
    return sides[0], sides[1]


def _escaping_link(root: Path, rel: str) -> bool:
    """True when `rel` is a symlink under `root` whose target leaves it."""
    try:
        st = (root / rel).lstat()
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) and _escapes(rel, os.readlink(root / rel))


def _rediff(seat: Path, base_commit: str, paths) -> bytes:
    """`git diff --binary <B> -- <paths>` again, over a NARROWED path set.

    Called only when the harvested patch carries a link that must not cross (see `build`).
    Excising one file from a unified diff by rewriting its bytes is not something to
    hand-roll, and `git apply --exclude=` takes an fnmatch PATTERN — so a path containing
    `[`, `*` or `?` would silently exclude its neighbours, which is the same class of bug
    `harvest._literal` exists to prevent. Asking git for the diff it would have produced is
    the one route with no parsing and no glob.

    Deliberately mirrors `harvest.artifact_set`'s invocation, flag for flag, and the
    reasons are all recorded there: `--binary` (a `-diff` file yields no content at exit
    0), `:(literal)` (a pathspec is a glob with magic), `check=True` (git exits 0 for a
    pathspec matching nothing, so nonzero is a real failure). Bytes are taken raw rather
    than decoded and re-encoded — the round trip is exact, but not performing it cannot be
    wrong.

    The empty guard is load-bearing, not tidiness: `git diff <B> --` with NO pathspec diffs
    the whole tree, so a candidate whose every path was banned would hand back the seat's
    entire delta instead of nothing.
    """
    if not paths:
        return b""
    return gitcmd.git(seat, "diff", "--binary", base_commit, "--",
                      *(f":(literal){p}" for p in paths),
                      env_extra=gitcmd.READONLY, binary=True).stdout


def _escapes(rel: str, target: str) -> bool:
    """True when a link at `rel` pointing at `target` resolves outside the tree root.

    Lexical and text-only, for the reason the module docstring gives: the target text is
    what crosses, and it is resolved in the verifier's tree, not the seat's. An absolute
    target always escapes — even one that happens to name a path inside the SEAT, since the
    verifier is a different directory and the link would still point at the builder's tree.
    """
    if os.path.isabs(target):
        return True
    joined = os.path.normpath(os.path.join(os.path.dirname(rel), target))
    return joined == ".." or joined.startswith(".." + os.sep)


def _is_dotgit(part: str) -> bool:
    """True when `part` names a git directory to any filesystem that could hold it.

    The spellings are git's own, not invented here. Measured on git 2.53 with default
    config, `git apply --index` answers `invalid path` for a patch writing under `.GiT`,
    `.git.`, `.git ` or `git~1` at ANY depth — the case-folded, NTFS-trailing-junk and
    8.3-shortname forms that all resolve to `.git` somewhere. `rstrip(". ")` covers the
    trailing forms and `lower()` the case ones.
    """
    return part.rstrip(". ").lower() in (".git", "git~1")


def _names_dotgit(rel: str) -> bool:
    """True when any COMPONENT of `rel` is a git directory.

    Any depth, not just the first, even though only the first is a destination clone's own
    config: `git apply --index` answers `invalid path` for a patch writing under `.git` at
    any depth (measured — see `_is_dotgit`), so a first-component rule would leave the two
    channels disagreeing about the same path, which is the shape `build`'s escaping-link
    comment already refuses to ship.
    """
    return any(_is_dotgit(p) for p in Path(rel).parts)


def _assert_contained(rel: str, what: str) -> None:
    """`rel` stays inside the tree it is joined onto, or a refusal.

    `Path(root) / "../../.ssh/id_rsa"` escapes without Path complaining, and it does damage
    in BOTH directions: on the way in it reads a host file into the bundle, and on the way
    out it writes one outside the verifier. `artifacts.paths` comes from `snapshot`, whose
    keys are `relative_to(root)` and cannot take this shape — so both callers guard the
    OTHER one, since a `CandidateBundle` and an `ArtifactSet` are plain dataclasses a later
    stage may deserialize from a ledger.

    Refused rather than normalised, and rather than routed to `omitted`: a path that
    escapes is not a path this module should be reasoning about, while `omitted` means
    "we looked and could not carry it".
    """
    if os.path.isabs(rel) or ".." in Path(rel).parts:
        raise BundleError(f"{what} escapes the tree: {rel!r}")


def _safe_rel(rel: str, what: str) -> str:
    """A sidecar path this module is willing to lay down, or a refusal.

    A `.git` COMPONENT is refused here by the same argument the module docstring makes about
    escaping links: a refusal that holds on one channel and not the other is not a refusal.
    The tracked patch already cannot carry one (see `_names_dotgit`), while a SIDECAR named
    `.git/config` was written straight over the destination clone's config — taking its
    hooks pin, its identity, and admitting `core.fsmonitor` and `core.sshCommand`, both of
    which name a command git EXECUTES on an ordinary `git status`. Measured: that config was
    materialized into a verifier and the fsmonitor program ran.

    A raise rather than a skip, because by this point the bundle CLAIMS to carry the path:
    dropping it silently is the missing-input-read-as-candidate-defect confusion `omitted`
    exists to prevent, and there is no way to un-claim it from here. `build` meets the same
    paths one stage earlier, where `omitted` is still reachable, and answers differently.
    """
    _assert_contained(rel, what)
    if _names_dotgit(rel):
        raise BundleError(
            f"{what} names git's own directory, which is not a candidate artifact: {rel!r}")
    return rel


def build(seat_path, artifacts, baseline, *, contract=None) -> CandidateBundle:
    """The candidate, as data, from a seat that has already been harvested.

    `artifacts.paths` is the whole claim; every entry ends up in exactly one of the three
    channels — the patch, a sidecar, or `omitted`.

    `contract` is the RUN's `inspect.GeneratorContract`, and it arrives as an argument
    rather than being read out of the seat for §7.2's reason: a seat-declared relation
    would let a candidate write its own success criterion. All this function does with it
    is record its id, so the verifier that later admits output under a contract and the
    manifest that says which one are comparable rather than two independent claims.
    `None` records the fail-closed sentinel — the same value the empty contract carries.

    Raises `BundleError` if the patch cannot be parsed. A file that cannot be READ is not a
    raise: it is one path of many, and `omitted` is precisely the honest place for it.

    `omitted` is "the bundle carries nothing for this path", which is a SUPERSET of "the
    verifier will be missing something". A file the agent created and then renamed away
    inside the work phase is named, because it never existed at B either and the patch has
    no record of it — from `paths` + the patch + the seat's disk, that is indistinguishable
    from an agent deleting a file setup had created, which IS a real gap. The superset is
    the fail-closed direction for THIS module; a consumer narrowing it must do so with the
    phase inventories, which `ArtifactSet` does not carry.

    No cap is applied to a sidecar payload. `snapshot.take` already bounded every path here
    against `Quota.for_harvest` (per-file and cumulative) before it could enter `paths`, so
    a second, differently-calibrated limit would only fail runs the harvest accepted.
    """
    seat = Path(seat_path)
    # The caller obligation `harvest` documents and nothing enforces: `tracked_diff` was
    # decoded with surrogateescape, so only this encoding reproduces git's exact bytes.
    # `.encode()` raises UnicodeEncodeError on the surrogates and errors="replace" would
    # produce a patch that no longer applies.
    patch = artifacts.tracked_diff.encode("utf-8", "surrogateescape")
    post, pre = _patch_paths(seat, patch)

    # An escaping link is refused on BOTH channels, or it is not refused. A seat is given a
    # branch and an identity precisely so it can commit, so `ln -s ../outside esc` +
    # `git add esc` puts a mode-120000 entry straight into the tracked patch — where the
    # sidecar branch's `_escapes` test never looks. Measured before this guard: `omitted`
    # empty, `sidecars` empty, the verifier carrying `esc -> ../outside`, and a write
    # through it landing outside the clone. Testing only the channel that was already
    # guarded is how the first version of this module passed its own containment test.
    banned = [rel for rel in sorted(post | pre) if _escaping_link(seat, rel)]
    if banned:
        # The narrowed diff is asked of git rather than cut out of the bytes; see `_rediff`.
        # The banned paths are NOT appended to `omitted` here — they are in
        # `artifacts.paths`, so the loop below meets them with the patch no longer claiming
        # them and routes them through the one `_escapes` test that names them.
        patch = _rediff(seat, baseline.commit,
                        [p for p in artifacts.paths if p not in set(banned)])
        post, pre = _patch_paths(seat, patch)

    covered = set(post)
    for rel in pre - post:
        # A preimage that is not also a postimage is a rename SOURCE, and all the patch does
        # with it is delete it. That is the whole carriage only while the seat has nothing
        # at that name. Move a file and leave a shim or a re-export behind — an ordinary
        # refactor — and unioning the two sides instead says "carried" about a path the
        # verifier ends up MISSING, with `omitted` empty: fail-open, and exactly the
        # confusion between a harvesting gap and a candidate defect this field exists to
        # prevent. `lexists`, so a dangling link at the name still counts as content.
        if not os.path.lexists(seat / rel):
            covered.add(rel)

    sidecars, omitted = [], []
    for rel in artifacts.paths:
        # BEFORE the read, not only before the write: `seat / "../../.ssh/id_rsa"` would
        # otherwise be slurped into a sidecar payload and carried wherever this bundle goes.
        _assert_contained(rel, "artifact path")
        if _names_dotgit(rel):
            # `snapshot.take(skip_dirs=(".git",))` prunes DIRNAMES, so the `.git` that
            # `git worktree add` and `git submodule add` leave behind — a FILE holding
            # `gitdir: <path>` — arrives here. Measured: a seat whose work is
            # `git worktree add --detach wt` inventories as `('wt/.git', 'wt/seed.txt')`,
            # and with this branch removed that gitlink materialized into the verifier as
            # `gitdir: <SEAT>/.git/worktrees/wt`, a live pointer back into the tree the
            # verifier exists to be independent of.
            #
            # `omitted` rather than the raise `materialize` uses, on the escaping-link
            # precedent below: adding a worktree is an ordinary agent command, so a raise
            # would discard a whole candidate over one path the bundle merely cannot carry,
            # and `omitted` is what lets the outcome classifier answer HARVEST_INCOMPLETE
            # if the gate then fails on the gap.
            omitted.append(rel)
            continue
        if rel in covered:
            continue
        p = seat / rel
        try:
            st = p.lstat()
        except OSError:
            # Absent. The ordinary case is a path the agent DELETED that the patch does not
            # carry — an untracked file, or one under a submodule. The bundle has no
            # delete-an-untracked-path channel and inventing one would not help: a verifier
            # runs setup AFTER materialization, so setup would recreate it anyway. Naming
            # it is the honest answer; silence would be the fail-open one.
            omitted.append(rel)
            continue
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(p)
            if _escapes(rel, target):
                omitted.append(rel)
                continue
            sidecars.append(SidecarEntry(rel, "symlink", 0,
                                         target.encode("utf-8", "surrogateescape")))
        elif stat.S_ISREG(st.st_mode):
            try:
                payload = p.read_bytes()
            except OSError:
                omitted.append(rel)
                continue
            sidecars.append(SidecarEntry(rel, "file", st.st_mode & 0o777, payload))
        else:
            # A FIFO, socket or device node. There is no payload to carry, and `open()` on
            # a FIFO blocks until a writer appears — `snapshot._special_entry` and
            # `screen._walk` refuse the same shape for the same reason.
            omitted.append(rel)

    return CandidateBundle(
        version=VERSION,
        baseline_ref=baseline.ref,
        baseline_commit=baseline.commit,
        tracked_patch=patch,
        sidecars=tuple(sidecars),
        generator_contract_id=contract.id if contract is not None else "",
        omitted=tuple(omitted),
    )


def materialize(bundle, dest) -> tuple[str, ...]:
    """Lay the candidate down in `dest`. Returns every path it touched, sorted and once each.

    A SET of paths, not a log of touches: one path can be reached through both channels — a
    rename shim is deleted by the patch and put back by a sidecar — and counting it twice
    would make `len()` a number no tree matches.

    `dest` must be a clone sitting at the bundle's own baseline commit. That is checked
    BEFORE anything is written, so a mismatched pair leaves the destination untouched
    rather than half-patched: `git apply` would reject most of a wrong-baseline patch, but
    "most" is not a property anyone can rely on — a patch whose context happens to match
    applies cleanly onto the wrong tree.

    `git apply --index` rather than blob writes, because a blob write drops the executable
    bit (spec §6): this repo's own `make verify` needs `tests/bats-fallback.sh` to be +x,
    and a mode-dropping materialization turns a candidate into an infrastructure FAIL.
    """
    dest = Path(dest)
    if bundle.version != VERSION:
        raise BundleError(
            f"bundle version {bundle.version} is not {VERSION}; refusing to materialize a "
            "shape this engine does not know — an unknown channel would be dropped silently")
    # check=False: a `dest` that is not a repository, or has no HEAD, is a caller error this
    # function must report in its own vocabulary. A raw GitError out of here is a class the
    # §6.2 caller has no name for and would read as an engine crash.
    r = gitcmd.git(dest, "rev-parse", "HEAD", env_extra=gitcmd.READONLY, check=False)
    if r.returncode != 0:
        raise BundleError(f"cannot read HEAD of {dest}: {r.stderr.strip()}")
    head = r.stdout.strip()
    if head != bundle.baseline_commit:
        raise BundleError(
            f"{dest} is at {head[:12]}, but the bundle was built against baseline "
            f"{bundle.baseline_commit[:12]} ({bundle.baseline_ref}); the candidate would be "
            "applied to a tree it was never measured against")
    # Every sidecar is validated before the first byte is written, for the same reason the
    # two checks above come first: a refusal must not leave a half-materialized tree.
    for e in bundle.sidecars:
        _safe_rel(e.path, "sidecar path")
        if e.kind not in ("file", "symlink"):
            raise BundleError(f"sidecar {e.path!r} has unknown kind {e.kind!r}")
        # `_escapes` is re-applied here, not trusted from `build`, for the reason
        # `_safe_rel` is: under the deserialize-from-a-ledger model that guard is written
        # for, a bundle's sidecars are input. `../x` as a sidecar PATH was already refused
        # while `../x` as a symlink TARGET was not — and a link plus a file underneath it
        # writes outside the verifier just as effectively.
        if e.kind == "symlink" and _escapes(
                e.path, e.payload.decode("utf-8", "surrogateescape")):
            raise BundleError(
                f"sidecar symlink {e.path!r} points out of the tree: {e.payload!r}")

    post, pre = _patch_paths(dest, bundle.tracked_patch)
    # Both sides here, unlike in `build`: this answers "what did materializing touch", and a
    # rename preimage is deleted whether or not a sidecar then puts something back at it.
    written = set(post | pre)
    if bundle.tracked_patch:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "candidate.patch"
            f.write_bytes(bundle.tracked_patch)
            # --binary is a no-op on git 2.53 (measured: a `--binary` patch applies without
            # it), and is passed anyway as the second latch — it is the documented flag for
            # exactly this input, and nothing else in the call would notice if a future git
            # restored the historical refusal.
            # --index so the verifier's index moves with its worktree: §7.2 requires the
            # engine to be able to CHECKPOINT admitted generator output, and `git add` of a
            # path whose mode came from the patch is not the same thing as the patch's mode.
            r = gitcmd.git(dest, "apply", "--index", "--binary", str(f),
                           check=False, binary=True)
            if r.returncode != 0:
                raise BundleError(
                    "the tracked patch does not apply to the verifier clone: "
                    f"{r.stderr.decode('utf-8', 'replace').strip()}")

    for e in bundle.sidecars:
        p = dest / e.path
        p.parent.mkdir(parents=True, exist_ok=True)
        # lexists, not exists: a dangling link at the path is still something to replace,
        # and `exists()` answers False for one.
        if os.path.lexists(p):
            p.unlink()
        if e.kind == "symlink":
            os.symlink(e.payload.decode("utf-8", "surrogateescape"), p)
        else:
            p.write_bytes(e.payload)
            # After the write, never before: `write_bytes` creates with the process umask,
            # so a chmod first would be undone for a NEW file and the +x would be lost.
            p.chmod(e.mode)
        written.add(e.path)
    return tuple(sorted(written))
