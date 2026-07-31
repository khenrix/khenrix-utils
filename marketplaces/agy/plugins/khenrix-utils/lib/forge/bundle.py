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
and `fleet` skipped it rather than choose; that is fixed in `baseline`/`fleet` in this same
commit, so all three modules now agree with this one. Second, containment: an ESCAPING link
does NOT cross. Materializing `out -> ../../user-repo` into a verifier and then running the
confirmed verify command there would write outside the clone, which is the one thing §4 and
§6 exist to prevent — so an escaping link goes to `omitted`, where a verifier failure
attributable to it is at least honest.

The escape test is LEXICAL, over the target text: that text is the only thing that crosses,
and where it lands is decided in the VERIFIER's tree, not the seat's. It composes because
each link is tested on its own — `a -> b` with `b -> ../../x` leaves `a` carried and `b`
omitted, so in the verifier `a` dangles rather than escapes — and because a tracked link
that escapes never reaches a baseline at all (`inspect.rejections`).

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
from dataclasses import dataclass, field
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
    sidecars: tuple = ()
    # None, never (). `gate_delta`'s producer is the gate-surface detector, which does not
    # exist yet, and `Baseline.sidecars` is the precedent: an empty tuple says "the
    # candidate changed nothing that defines the gate" when the truth is "nobody looked",
    # which is the fail-OPEN reading. A consumer classifying `GATE_CHANGED` must treat None
    # as UNKNOWN — not as a clean gate.
    gate_delta: tuple | None = None
    # "" means the run declared no GeneratorContract, which admits NOTHING as a permitted
    # verify-origin output (spec §7.2). That is the fail-closed reading, so it is safe as a
    # default in a way `gate_delta=()` is not.
    generator_contract_id: str = ""
    omitted: tuple = ()


def _covered(repo, patch: bytes) -> frozenset:
    """Every path the patch touches, on BOTH sides, read out of the patch's own bytes.

    `git apply --numstat -z` is the parse, not a hand-written one: a `diff --git` header
    quotes an unusual path with C escapes, so reading names off the header text means
    reimplementing `unquote_c_style` — and getting it wrong means a path silently
    reclassified as uncarried. Under `-z` git emits `<added> TAB <deleted> TAB <path> NUL`
    with the name raw.

    Run TWICE, forward and reversed, because forward `--numstat` reports only the POSTIMAGE
    of a rename. Measured on git 2.53, a `git mv old.txt new.txt` patch:

        git apply --numstat -z      ->  `0\\t0\\tnew.txt\\0`
        git apply -R --numstat -z   ->  `0\\t0\\told.txt\\0`

    Forward alone therefore loses `old.txt`, which IS in `artifacts.paths` (snapshot saw it
    removed) and IS carried by the patch — so it would have been named in `omitted`, and
    §6.2's `HARVEST_INCOMPLETE` would fire on a candidate with no harvesting gap at all.
    `git diff` detects renames by default and `gitcmd` does not pin `diff.renames`, so this
    is the ordinary case, not an exotic one.

    Derived from the patch BYTES rather than from a second `git diff` of the seat: this set
    must describe what `materialize` will actually apply, and a seat re-read would answer a
    question about the seat's state now instead.
    """
    if not patch:
        # Not an optimisation. `git apply` exits 128 with "No valid patches in input" on an
        # empty one, and — more importantly — an empty patch carries nothing, so answering
        # from the bytes keeps `_covered` and the apply in `materialize` in agreement by
        # construction rather than by two commands happening to concur.
        return frozenset()
    with tempfile.TemporaryDirectory() as td:
        # NOT inside the seat or the verifier: a patch file dropped in either tree is an
        # untracked file the next inventory would report as the agent's work.
        f = Path(td) / "candidate.patch"
        f.write_bytes(patch)
        out = set()
        for extra in ((), ("-R",)):
            r = gitcmd.git(repo, "apply", *extra, "--numstat", "-z", str(f),
                           env_extra=gitcmd.READONLY, check=False, binary=True)
            if r.returncode != 0:
                raise BundleError(
                    "cannot determine what the tracked patch carries: "
                    f"git apply --numstat -> {r.returncode}: "
                    f"{r.stderr.decode('utf-8', 'replace').strip()}")
            for rec in r.stdout.decode("utf-8", "surrogateescape").split("\0"):
                if rec:
                    # split(maxsplit=2): a path may itself contain a TAB, the two counts
                    # never can.
                    out.add(rec.split("\t", 2)[2])
    return frozenset(out)


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


def _safe_rel(rel: str) -> str:
    """A sidecar path, refused unless it lands inside the destination.

    `artifacts.paths` comes from `snapshot`, whose keys are `relative_to(root)` and cannot
    escape — so this guards the OTHER caller: a `CandidateBundle` is a plain dataclass a
    later stage may deserialize from a ledger, and `dest / "../../.ssh/authorized_keys"`
    writes outside the verifier without Path complaining. Refused, not normalised: a bundle
    naming such a path is not a bundle whose intent we should guess at.
    """
    if os.path.isabs(rel) or ".." in Path(rel).parts:
        raise BundleError(f"sidecar path escapes the destination: {rel!r}")
    return rel


def build(seat_path, artifacts, baseline) -> CandidateBundle:
    """The candidate, as data, from a seat that has already been harvested.

    `artifacts.paths` is the whole claim; every entry ends up in exactly one of the three
    channels — the patch, a sidecar, or `omitted`.

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
    covered = _covered(seat, patch)

    sidecars, omitted = [], []
    for rel in artifacts.paths:
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
        omitted=tuple(omitted),
    )


def materialize(bundle, dest) -> tuple:
    """Lay the candidate down in `dest`. Returns every path it touched.

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
    head = gitcmd.git(dest, "rev-parse", "HEAD", env_extra=gitcmd.READONLY).stdout.strip()
    if head != bundle.baseline_commit:
        raise BundleError(
            f"{dest} is at {head[:12]}, but the bundle was built against baseline "
            f"{bundle.baseline_commit[:12]} ({bundle.baseline_ref}); the candidate would be "
            "applied to a tree it was never measured against")
    # Every sidecar is validated before the first byte is written, for the same reason the
    # two checks above come first: a refusal must not leave a half-materialized tree.
    for e in bundle.sidecars:
        _safe_rel(e.path)
        if e.kind not in ("file", "symlink"):
            raise BundleError(f"sidecar {e.path!r} has unknown kind {e.kind!r}")

    written = list(_covered(dest, bundle.tracked_patch))
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
        written.append(e.path)
    return tuple(written)
