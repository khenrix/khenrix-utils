"""Properties that must hold ACROSS module boundaries.

Plan B's suites each exercised one module, with others present only as fixtures. Three
shipped defects lived in those seams: the manifest missed a selected directory's
contents, the seat's exclude replay silently no-opped in a linked worktree, and the
seat's environment re-admitted the redirectors gitcmd strips. Each is one assertion here.

Every assertion below names TWO modules and holds only if both agree. A test that would
still pass with either side broken belongs in that side's own suite, not this file.

THE LAST SEAM CLASS HERE HAS A DIFFERENT SHAPE: one side is a REFUSAL rather than a value
another module consumes. Nothing asserted that a repository preflight ADMITS is one the
chain completes, nor that one it REFUSES would have failed — so a refusal could be
unnecessary, or could be the only thing standing between the chain and a silent wrong
answer, and neither the per-module suites nor the seams above could tell those apart. Each
fixture below is the minimal repository that trips ONE `inspect.rejections` line, driven
through the chain with the policy bypassed; what it must produce is the harm that line's own
text claims. Measured: only one of the three RAISES. The other two complete and hand the
harm to the verifier in silence, which is why "the chain breaks" is the wrong assertion to
write for them and why each case names its own consequence.
"""
import ast
import contextlib
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import (baseline, bundle, fleet, gate, gitcmd, harvest, journal,  # noqa: E402
                   inspect as finspect, preflight, runner, runstate, screen, snapshot,
                   storage, verify)
from forge_fixtures import commit_all, git as _git, make_repo, write  # noqa: E402

IDENT = ("Forge Seat", "seat@forge.invalid")

# The nine names `gitcmd.REDIRECTING_ENV` declared when this seam was pinned. Restated
# here on purpose — see `test_the_seat_environment_admits_no_git_redirector` for why a
# test that only reads the live list cannot notice the list shrinking.
#
# `gitcmd` has since SPLIT that tuple: GIT_CONFIG_COUNT was never a redirector and now sits
# with the other config injectors, under the `HOSTILE_ENV` union every consumer strips. The
# literal set below is unchanged, because the property it guards is unchanged — each of
# these nine must still be stripped from a seat's environment, whichever tuple declares it.
_KNOWN_REDIRECTORS = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CONFIG_COUNT",
    "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
})


# A SUITE'S GREEN MAY NOT DEPEND ON THE TESTER'S FREE DISK. `gate.open_run` consults §4's disk
# rejection against the real filesystem, and `/tmp` on a developer machine is routinely
# smaller than a default run's ~63 GB peak — so without this a seam test raises a `GateError`
# here and is green on the machine beside it. The refusal itself is measured in
# `test_forge_gate.py`, against a `free_bytes` that suite controls.
@pytest.fixture(autouse=True)
def _enough_disk(monkeypatch):
    monkeypatch.setattr(gate, "free_bytes", lambda _p: 10 ** 15)

def _baseline(repo, run, selected=()):
    f = finspect.repo_facts(repo)
    return baseline.materialize(repo, run, f, list(selected), "r1")


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_everything_in_the_tree_is_in_the_manifest(tmp_path):
    """SEAM: baseline's tree vs its own manifest. A selected directory broke this.

    The fixture carries a SYMLINK because the original carried none — which is why this
    test asserted a property it could not violate. A link used to be the one shape where
    the two sets legitimately disagreed: git commits it into the tree, and the manifest
    refused to hash through it. That left a THIRD outcome available — in the tree, out of
    the manifest, and nothing anywhere saying so — which is exactly what `screen._walk`'s
    silent `continue` produced for a link pointing at the host.

    Since Plan D's D-1 the residue is EMPTY, which is the stronger closure: the manifest
    describes a link by its target TEXT, so it describes every path in the tree without
    ever reading through one. The screen still refuses to read through a link, and that
    refusal is asserted here too — it is the remaining, deliberate asymmetry between what
    B records and what the pre-launch screen vouched for.
    """
    repo = make_repo(tmp_path)
    write(repo, "scratch/a.txt", "a\n")
    write(repo, "scratch/sub/b.txt", "b\n")
    (Path(repo) / "scratch" / "alias.txt").symlink_to("a.txt")
    run = tmp_path / "run"; run.mkdir()
    b = _baseline(repo, run, selected=["scratch"])
    tree = set(gitcmd.git(repo, "ls-tree", "-r", "--name-only", b.tracked_tree_oid,
                          env_extra=gitcmd.READONLY).stdout.split())
    # The tree must actually carry the selected directory, or the set difference below is
    # vacuously empty and the assertion certifies nothing.
    assert {"scratch/a.txt", "scratch/sub/b.txt", "scratch/alias.txt"} <= tree
    missing = tree - set(b.filesystem_manifest)
    assert missing == set(), f"in the tree and nowhere in the manifest: {sorted(missing)}"
    assert b.filesystem_manifest["scratch/alias.txt"] == \
        hashlib.sha256(b"a.txt").hexdigest(), \
        "and described as the link it is, not as the file it names"
    _findings, breaches = screen.screen_tree(repo, ["scratch"])
    assert breaches == ["scratch/alias.txt: not screened — symlink; links are never "
                        "followed"], \
        "the screen still declines to read through it; B describing it is not B reading it"


def test_an_escaping_link_in_a_selection_is_named_by_two_refusals_and_stopped_by_neither(tmp_path):
    """SEAM: four modules individually consistent and jointly wrong, measured end to end.

    `inspect.rejections` tested only the top-level selected path; `screen._walk` dropped a
    nested link in silence; `baseline` kept it out of the manifest while `git add -f` put
    it in the TREE; `fleet`'s verification skipped symlinks. Result, measured on this exact
    fixture: rejections `[]`, breaches `[]`, `verified=True`, and a seat whose
    `scratch/creds` read a file outside the repository. Every module's own suite was green.

    The manifest and `fleet` now describe the link by its target text (Plan D, D-1), which
    changes nothing about the containment story: describing a link is not refusing one.

    BOTH refusals are asserted rather than one, because either alone would let a single
    edit reopen the whole path. Neither stops THIS chain, and the reason is now specific
    rather than universal: `preflight.refusals` consults both, and nothing between
    `baseline.materialize` and the verifier calls preflight — so the closing assertions still
    CHARACTERIZE what the tree does when the selection is carried without one, and the
    distance between the two halves is the finding, not a gap in the test.
    """
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "credentials").write_text("HOST-ONLY-CONTENT\n")
    write(repo, "scratch/a.txt", "a\n")
    (Path(repo) / "scratch" / "creds").symlink_to(outside / "credentials")

    f = finspect.repo_facts(repo)
    assert finspect.rejections(f, ["scratch"]) == [
        f"symlink escapes the repository: scratch/creds -> {outside / 'credentials'}"], \
        "preflight must refuse the selection before a baseline is ever built"
    _findings, breaches = screen.screen_tree(repo, ["scratch"])
    assert breaches == ["scratch/creds: not screened — symlink; links are never followed"], \
        "and the screen must not certify a selection it did not read through"

    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, f, ["scratch"], "r1")
    tree = set(gitcmd.git(repo, "ls-tree", "-r", "--name-only", b.tracked_tree_oid,
                          env_extra=gitcmd.READONLY).stdout.split())
    assert "scratch/creds" in tree
    assert b.filesystem_manifest["scratch/creds"] == \
        hashlib.sha256(str(outside / "credentials").encode()).hexdigest(), \
        "B describes the link by its target text — which is not a look at the target"
    seat = fleet.clone_seat(repo, b, tmp_path / "s1", name="claude", identity=IDENT)
    assert seat.verified is True, \
        "fleet compares the link's target TEXT, so an escaping link still verifies clean"
    assert (seat.path / "scratch" / "creds").read_text() == "HOST-ONLY-CONTENT\n", \
        "the seat really can read outside the repository — hence the two refusals above"


def test_a_subtree_neither_walk_could_list_cannot_clear_preflight(tmp_path):
    """SEAM: the two walks over a selection, and the gate that converts them into a verdict.

    The fixture above, one directory deeper. `inspect._escaping_links_under` and
    `screen._walk` both walked with `os.walk`'s default `onerror`, which swallows a listing
    error and yields nothing underneath — so each returned its own kind of nothing, no
    rejection and no breach, and `preflight.refusals()` returned `()`. Measured before
    either `onerror`: a permission error converted into "this run may start", in the one
    place that decides whether it may.

    ASSERTED AT `refusals` AND NOT AT EITHER MODULE, because the conversion is the seam. The
    per-module suites now pin each walk's own sentence; what only this file can say is that
    the two are independent measurements of the same subtree and that ONE of them going
    quiet is not enough to clear the run — which is why both lines are named here rather
    than one, and why `baseline.materialize` is asserted to refuse the same selection
    afterwards. Preflight is read-only and minutes may pass at the §5 gate, so B's own walk
    is the second measurement, not a duplicate of the first.
    """
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "credentials").write_text("HOST-ONLY-CONTENT\n")
    write(repo, "scratch/a.txt", "a\n")
    locked = Path(repo) / "scratch" / "locked"
    locked.mkdir(parents=True)
    (locked / "creds").symlink_to(outside / "credentials")
    locked.chmod(0o000)
    run = tmp_path / "run"; run.mkdir()
    try:
        # The fixture is measured with the operation the walks perform, not with `os.access`
        # or a euid comparison: root lists a mode-000 directory, and this test would then
        # certify the fix while nothing was ever denied.
        try:
            os.listdir(locked)
        except PermissionError:
            pass
        else:
            pytest.skip("this user lists a 0o000 directory, so the fixture denies nothing")
        lines = preflight.refusals(preflight.inspect_repo(repo, ["scratch"]))
        assert any(r.startswith("cannot list scratch/locked,") for r in lines), \
            "§2.3's walk went quiet on a subtree it could not look at"
        assert any(r.startswith("scratch/locked: not screened") for r in lines), \
            "§3's screen certified a subtree it never opened"
        with pytest.raises(baseline.BaselineError, match="cannot walk"):
            baseline.materialize(repo, run, finspect.repo_facts(repo), ["scratch"], "r1")
    finally:
        locked.chmod(0o755)
    # Non-vacuity: readable, this selection clears preflight and builds a B — so the three
    # refusals above are about the unreadable directory and not about the fixture.
    assert preflight.refusals(preflight.inspect_repo(repo, ["scratch"])) == (
        f"symlink escapes the repository: scratch/locked/creds -> {outside / 'credentials'}",
        "scratch/locked/creds: not screened — symlink; links are never followed")


def test_everything_the_manifest_names_is_screenable(tmp_path):
    """SEAM: baseline's manifest vs screen's selection contract.

    The screen's four breach classes (absolute path, `..` component, symlink, non-regular)
    are all shapes a manifest key could take. `breaches` is non-empty exactly when the
    screen read LESS than it claimed to, so a manifest key the screen refuses means the
    baseline recorded content that never went past the secret screen.

    Holds for ordinary files and selected directories. It does NOT hold for a symlink —
    see `test_a_tracked_symlink_is_its_target_text_in_every_module`, which pins that
    remaining gap rather than leaving this test to imply the property is universal. D-1
    narrowed the gap but did not close it: B now describes a link honestly, and the screen
    still declines to read through one.
    """
    repo = make_repo(tmp_path)
    write(repo, "cfg.py", "ok\n")
    write(repo, "scratch/nested.txt", "n\n")
    run = tmp_path / "run"; run.mkdir()
    b = _baseline(repo, run, selected=["cfg.py", "scratch"])
    assert set(b.filesystem_manifest) == {"seed.txt", "cfg.py", "scratch/nested.txt"}
    findings, breaches = screen.screen_tree(repo, sorted(b.filesystem_manifest))
    assert breaches == [], f"the screen could not read what the baseline recorded: {breaches}"
    assert findings == [], f"unexpected findings on a clean fixture: {findings}"


def test_the_seat_checkout_matches_the_baseline_manifest(tmp_path):
    """SEAM: baseline's manifest vs the seat fleet builds from it."""
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "dirty\n")
    run = tmp_path / "run"; run.mkdir()
    b = _baseline(repo, run, selected=["d.txt"])
    seat = fleet.clone_seat(repo, b, tmp_path / "s1", name="claude", identity=IDENT)
    # Both halves of the seam have to be non-trivial: an empty manifest would make the
    # loop below iterate zero times and report success, which is the vacuous pass this
    # whole file exists to rule out.
    assert set(b.filesystem_manifest) == {"seed.txt", "d.txt"}
    assert seat.verified is True
    checked = 0
    for rel, want in b.filesystem_manifest.items():
        p = seat.path / rel
        assert p.is_file() and not p.is_symlink(), f"seat is missing {rel}"
        assert _sha256(p) == want, f"seat content differs from the baseline: {rel}"
        checked += 1
    assert checked == len(b.filesystem_manifest)


def test_the_seats_first_inventory_agrees_with_the_baseline_manifest(tmp_path):
    """SEAM: baseline's manifest vs snapshot's F0 inventory of the seat.

    `baseline._sha256_file`, `fleet._sha256_file` and `snapshot._digest` each carry a
    docstring promising the other two that they agree byte-for-byte — three copies of one
    contract with nothing comparing them. F0 is where that promise is spent: §7.3's change
    predicate is the snapshot digest, and it is only meaningful against B if the two
    functions produce the same string for the same bytes.

    The fixture carries a SYMLINK, which it could not before D-1: the manifest held a
    link's target CONTENT while F0 held its target TEXT, so the two were unequal there by
    construction and this test had to be scoped to regular files to say anything at all.
    Both now digest the target text, so the promise is spent on every shape the fixture has
    rather than on the easy ones.
    """
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "dirty\n")
    write(repo, "pkg/mod.py", "content\n")
    (Path(repo) / "pkg" / "alias.py").symlink_to("mod.py")
    run = tmp_path / "run"; run.mkdir()
    b = _baseline(repo, run, selected=["d.txt", "pkg"])
    seat = fleet.clone_seat(repo, b, tmp_path / "s1", name="claude", identity=IDENT)
    f0, breaches = snapshot.take(seat.path)
    assert breaches == []
    # `.git` is skipped by the snapshot and absent from the manifest, so the two path sets
    # are directly comparable — not merely one contained in the other.
    assert set(f0) == set(b.filesystem_manifest)
    assert f0["pkg/alias.py"].kind == "symlink", "or the link half is vacuous"
    for rel, want in b.filesystem_manifest.items():
        assert f0[rel].digest == want, f"snapshot and baseline disagree on {rel}"


def test_a_tracked_symlink_is_its_target_text_in_every_module(tmp_path):
    """SEAM, closed by Plan D's D-1: four modules now give one shape one identity.

    Until this commit `baseline`'s `ls-files` loop guarded on `is_file()`, which FOLLOWS a
    link, so a TRACKED symlink entered the manifest carrying the sha256 of its TARGET'S
    CONTENT — read through the link, which `_walk_selected` refused to do for a SELECTED
    directory for exactly the reason that it "must not describe content from outside the
    tree it claims to describe". `snapshot` digested the target TEXT, `fleet` skipped the
    entry rather than choose between them, and `screen` breached on it. Four modules, three
    opinions, and a seam that was closed only for a manifest with no tracked symlink.

    `bundle` is what forced the answer: a candidate's symlink has to cross as SOMETHING, and
    carrying it as its target's content would put content from outside the candidate into
    the candidate. So a link is its target text, in `baseline`, `snapshot`, `fleet` and
    `bundle` alike — and this test is the one place all four are compared on one path.

    What remains, deliberately, is `screen`'s refusal: describing a link is not reading
    through it, so the pre-launch screen still declines to vouch for whatever it names. That
    is asserted here so it cannot lapse into silence.

    A tracked symlink that ESCAPES the repository is already rejected by
    `inspect.rejections`; this one points inside, so preflight passes it.
    """
    repo = make_repo(tmp_path)
    (Path(repo) / "link.txt").symlink_to("seed.txt")
    _git(repo, "add", "link.txt")
    _git(repo, "commit", "-qm", "link")
    run = tmp_path / "run"; run.mkdir()
    f = finspect.repo_facts(repo)
    assert f.escaping_symlinks == [], "precondition: preflight does not reject this one"
    b = baseline.materialize(repo, run, f, [], "r1")
    target_text = hashlib.sha256(b"seed.txt").hexdigest()
    assert b.filesystem_manifest["link.txt"] == target_text, \
        "B holds the digest of the TEXT the link carries, not of what that text reaches"
    assert b.filesystem_manifest["link.txt"] != hashlib.sha256(b"seed\n").hexdigest(), \
        "and specifically not the target's content, which is what it used to hold"

    _findings, breaches = screen.screen_tree(repo, sorted(b.filesystem_manifest))
    assert breaches == ["link.txt: not screened — symlink; links are never followed"], \
        "the screen still refuses; a clean list would mean it followed the link"

    seat = fleet.clone_seat(repo, b, tmp_path / "seats" / "s1",
                            name="claude", identity=IDENT)
    assert seat.verified is True, "and fleet VERIFIES the entry now rather than excusing it"
    f0, snap_breaches = snapshot.take(seat.path)
    assert snap_breaches == []
    assert f0["link.txt"].kind == "symlink"
    assert f0["link.txt"].digest == b.filesystem_manifest["link.txt"], \
        "F0 and B agree on the link, so content-keyed change detection covers it too"

    # The fourth module: what a candidate would carry for the same path.
    entry = bundle.build(
        seat.path,
        harvest.ArtifactSet(paths=("link.txt",)),
        b).sidecars[0]
    assert (entry.kind, entry.payload) == ("symlink", b"seed.txt"), \
        "the bundle carries the link as the link, so all four modules say one thing"
    assert hashlib.sha256(entry.payload).hexdigest() == target_text


def test_the_whole_chain_delivers_the_agents_work_to_the_harvest(tmp_path):
    """SEAM: baseline -> fleet -> snapshot -> harvest, end to end.

    Every harvest test builds its "seat" with `make_repo`, so nothing asserts that a seat
    `clone_seat` actually produced can be harvested. Two things only this composition
    pins: B1 must be reachable in the SEAT's object store (harvest runs
    `git diff <B1>` there under `check=True`, so an unreachable B1 is a GitError, not an
    empty diff), and B1 — not the seat's HEAD — must be what the content is measured
    against, which is observable only when the agent commits.
    """
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "dirty-from-the-user\n")
    run = tmp_path / "run"; run.mkdir()
    b = _baseline(repo, run, selected=["d.txt"])
    assert b.dirty is True and b.commit != b.base_commit
    seat = fleet.clone_seat(repo, b, tmp_path / "s1", name="claude", identity=IDENT)

    f0 = harvest.record(seat.path)
    fsetup = harvest.record(seat.path)
    write(seat.path, "seed.txt", "AGENT-WORK\n")
    # The agent commits, so `git diff HEAD` in the seat would be EMPTY. Only the pinned B1
    # yields the content.
    _git(seat.path, "add", "-A")
    _git(seat.path, "commit", "-q", "-m", "agent")
    fwork = harvest.record(seat.path)
    fverify = harvest.record(seat.path)

    a = harvest.artifact_set(harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork,
                                            fverify=fverify), seat.path, b.commit)
    assert a.paths == ("seed.txt",)
    assert a.origin["seed.txt"] == "builder"
    assert "AGENT-WORK" in a.tracked_diff
    # The user's uncommitted file rode into the seat via B1, so it is NOT part of the
    # agent's delta — the seam that would break if the seat were cloned from HEAD.
    assert "d.txt" not in a.paths
    assert "dirty-from-the-user" not in a.tracked_diff


def test_a_linked_worktree_survives_the_whole_chain(tmp_path):
    """SEAM: the .git-as-file bug shipped TWICE in Plan B. Pin the chain, not one module."""
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    gitcmd.git(repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    # JOINED ONTO `repo`, never used bare. Measured on git 2.53: from a MAIN worktree
    # `rev-parse --git-common-dir` answers the relative `.git`, so the bare form resolves
    # against the test process' cwd — this repository — and the fixture would overwrite
    # the developer's own .git/info/exclude. `fleet.clone_seat` joins for the same reason.
    excl = repo / gitcmd.git(repo, "rev-parse", "--git-common-dir",
                             env_extra=gitcmd.READONLY).stdout.strip() / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("scratch/\n")
    write(wt, "d.txt", "dirty\n")
    run = tmp_path / "run"; run.mkdir()
    f = finspect.repo_facts(wt)
    # inspect must have resolved the LINKED worktree's own git dir; if it fell back to
    # `<wt>/.git` (a FILE) the index hash would be "" and baseline's drift check would be
    # comparing nothing to nothing.
    # Resolved on both sides: `show-toplevel` answers the realpath, so a symlinked tmpdir
    # would fail this comparison for a reason that has nothing to do with the seam.
    assert Path(f.root).resolve() == wt.resolve() and f.index_sha
    b = baseline.materialize(wt, run, f, ["d.txt"], "r2")
    seat = fleet.clone_seat(wt, b, tmp_path / "s1", name="codex", identity=IDENT)
    assert "info/exclude" in seat.replayed
    assert (seat.path / ".git" / "info" / "exclude").read_text() == "scratch/\n"
    # The rest of the chain has to have survived too: the seat carries the worktree's
    # uncommitted file and vouches for it.
    assert seat.verified is True
    assert (seat.path / "d.txt").read_text() == "dirty\n"


def test_the_seat_environment_admits_no_git_redirector(tmp_path):
    """SEAM: gitcmd strips these for engine calls; the seat env must not re-admit them.

    The hostile environment is BUILT FROM the name list, never enumerated beside it.
    `forge_child_env` builds its result from the dict it is handed (`base = dict(env)`,
    then `pop`), so `out`'s keys are a subset of the input's: a name absent from the
    fixture cannot appear in `out`, and asserting it is missing is an assertion that
    cannot fail. Measured with a four-name fixture against gitcmd's nine, five of the
    nine assertions were vacuous — and a `fleet` stripping only those four while `gitcmd`
    declared nine still passed.

    The literal set is kept ALONGSIDE gitcmd's list rather than replaced by it, because
    the two guard opposite directions. A name ADDED to `gitcmd` is covered by reading the
    list; a name REMOVED from it would otherwise shrink this fixture in step with the hole
    it opened, since `fleet` pops exactly what `gitcmd` declares. Nothing else in the
    suite pins this list's contents.

    Note what this seam is NOT: it was never an oracle for whether the declared list is
    COMPLETE. It did not catch GIT_CONFIG_PARAMETERS or GIT_TEMPLATE_DIR reaching every
    seat, and could not have — both were absent from the list, so both were absent from the
    fixture built out of it. Completeness is pinned by effect instead, one variable at a
    time, in `test_forge_fleet.py` and `test_forge_verify.py`.
    """
    repo = make_repo(tmp_path)
    names = _KNOWN_REDIRECTORS | set(gitcmd.HOSTILE_ENV)
    # Every value is repo-EXTERNAL on purpose. `scrub_env` drops what points INTO the
    # checkout, so a repo-internal value would be removed by the scrub and the assertions
    # below would pass without the redirector strip running at all — which is the very
    # confusion `forge_child_env`'s docstring exists to name.
    hostile = {k: "/elsewhere" for k in names} | {"PATH": "/usr/bin"}
    out = fleet.forge_child_env(repo, hostile)
    for k in names:
        assert k not in out, f"{k} reached the seat"
    assert _KNOWN_REDIRECTORS <= set(gitcmd.HOSTILE_ENV), \
        "gitcmd stopped declaring a name it used to strip; fleet and verify pop what " \
        "gitcmd declares, so the engine's own calls are no longer narrowed either"
    # ...and the scrub is not achieving that by emptying the environment. A seat with no
    # PATH fails every candidate for an infrastructure reason (spec §4).
    assert out["PATH"] == "/usr/bin"
    assert out["GIT_CONFIG_GLOBAL"] == gitcmd.NO_USER_CONFIG["GIT_CONFIG_GLOBAL"]


def test_the_no_contract_sentinel_is_one_value_across_inspect_and_bundle():
    """`detect_generators` proposing "nothing declared" and a bundle recording "nothing was
    declared" have to be the SAME string, because `bundle.generator_contract_id` is the only
    channel the contract crosses into the manifest on.

    Two fail-closed defaults that merely happen to agree is one rename away from a manifest
    that says a contract was declared while the gate admitted nothing under it — or worse,
    the reverse. Nothing else pins the pair.
    """
    assert finspect.GeneratorContract().id == bundle.CandidateBundle(
        version=bundle.VERSION, baseline_ref="r", baseline_commit="c").generator_contract_id
    assert finspect.detect_generators(ROOT).id == finspect.GeneratorContract().id


@dataclass(frozen=True)
class _Chain:
    base: baseline.Baseline
    seat: fleet.Seat
    artifacts: harvest.ArtifactSet
    # THE BUNDLE AS THE SEAT'S SIDE BUILT IT, whose `gate_delta` is None. The measured one
    # is `built.candidate`, and the two are kept apart rather than collapsed because the
    # difference is a verdict: `classify` reads None as UNKNOWN, which displaces the PASS an
    # otherwise clean candidate would earn. A case that wants the run's verdict wants
    # `built.candidate`; a case about what crossed out of the seat wants this one.
    candidate: bundle.CandidateBundle
    built: verify.Verifier
    # The test's own tmp_path, so a case can bound where a fixture's escaping
    # link is allowed to reach — see `_harm_of_escaping_link`.
    tmp: Path

    @property
    def verifier(self) -> Path:
        return self.built.path


def _chain_to_verifier(repo, tmp, *, run_id="r1", contract=None, command=None,
                       setup=None) -> _Chain:
    """Everything an orchestrator does between preflight and the gate, on one repository.

    The agent's work is one new file, because the point of these cases is what the
    REPOSITORY carries into a seat and a verifier — a richer candidate would only add
    shapes the per-module suites already cover.

    `contract` reaches `bundle.build` and `build_verifier` together or neither: they are the
    two sides `ContractMismatch` compares, so a helper that fed one of them would answer a
    refusal to a caller who named a single contract. The default is the empty one, which is
    what `detect_generators` answers for every repository and what a bundle built without
    one records.

    `command` is the confirmed gate. It is optional because it only WIDENS the surface
    `build_verifier` differences — a case with nothing to say about the gate delta leaves it
    out, and a case whose gate is a script no naming rule reaches must pass it or measure a
    surface that does not hold that script.

    `setup` is the seat's SETUP phase, run before Fsetup is recorded, which is what puts its
    writes on the far side of the boundary `harvest.artifact_set` differences: that function
    takes the artifact paths from Fsetup->Fwork, so only the work crosses. Omitted, F0 and
    Fsetup are one inventory and the seat has no setup phase at all.

    This helper starts at `baseline.materialize` and never calls `preflight`, so nothing in
    here consults `inspect.rejections` — which is what makes the refusal cases below able to
    measure what an unrefused repository delivers. `preflight` is where the policy IS
    consulted; see `test_preflight_consults_both_refusals_and_what_they_name_it_refuses`.
    """
    contract = finspect.GeneratorContract() if contract is None else contract
    facts = finspect.repo_facts(repo)
    run = tmp / "run"
    run.mkdir(exist_ok=True)
    base = baseline.materialize(repo, run, facts, [], run_id)
    seat = fleet.clone_seat(repo, base, tmp / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(seat.path)
    if setup is not None:
        setup(seat.path)
    fsetup = harvest.record(seat.path) if setup is not None else f0
    write(seat.path, "src.py", "work\n")
    fwork = harvest.record(seat.path)
    artifacts = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork), seat.path,
        base.commit)
    candidate = bundle.build(seat.path, artifacts, base, contract=contract)
    built = verify.build_verifier(repo, base, candidate, tmp / "verifier",
                                  identity=IDENT, contract=contract, command=command)
    return _Chain(base, seat, artifacts, candidate, built, tmp)


def test_the_contract_the_gate_admits_under_is_the_one_the_manifest_records(tmp_path):
    """SEAM: `bundle.build` writes the id, `verify.build_verifier` refuses a disagreement,
    and `verify.fixed_point` admits under the contract that survived it.

    The ids here are NON-EMPTY and the two contracts declare DIFFERENT outputs, which is
    what the sentinel test above cannot do: while both sides were "", a manifest recording
    contract X while the gate admitted under Y was undetectable, and pinning that the two
    defaults agree says nothing about a run that declares one.

    The admission is the far end of the link. The id in the manifest is worth checking only
    because it decides what the gate may rewrite without failing the candidate, so the last
    assertion is the gate actually admitting `gen/out.txt` — and `_declared` is asked
    directly about the contract that was refused, so "Y would have answered differently" is
    a measurement here rather than a claim about a call nobody makes.
    """
    repo = make_repo(tmp_path)
    write(repo, "gen/out.txt", "v1\n")
    write(repo, "build.sh", "#!/bin/sh\necho v2 > gen/out.txt\n")
    (Path(repo) / "build.sh").chmod(0o755)
    commit_all(repo, "a declared generator")
    x = finspect.GeneratorContract(id="render-x", relations=(("src/*", "gen/*"),))
    y = finspect.GeneratorContract(id="render-y", relations=(("src/*", "other/*"),))
    assert verify._declared(x, "gen/out.txt") and not verify._declared(y, "gen/out.txt"), \
        "the two contracts must disagree about this path, or the refusal below costs nothing"

    chain = _chain_to_verifier(repo, tmp_path, contract=x)
    assert chain.candidate.generator_contract_id == "render-x", \
        "the manifest's record of the run's contract, written by `bundle.build`"
    with pytest.raises(verify.ContractMismatch):
        verify.build_verifier(repo, chain.base, chain.candidate, tmp_path / "v-y",
                              identity=IDENT, contract=y, command=None)
    assert not (tmp_path / "v-y").exists(), \
        "and refused before the clone, so no tree exists for a gate to run in under Y"

    assert chain.built.contract is x
    fp = verify.fixed_point(chain.built.path, verify.Command.parse([["./build.sh"]]),
                            chain.built.contract)
    assert fp.run.exit_code == 0 and fp.admitted == ("gen/out.txt",) and fp.unexplained == ()


def test_the_calibration_tree_is_not_a_seat(tmp_path):
    """SEAM: `verify.calibrate` and `fleet`. §6.2's baseline-red outcome is a claim about a
    tree the builder never had, and `_as_run` says a `Run` carries no provenance at all — so
    the tree is what must be checked, and this is where it is checked against a REAL seat
    from the same run rather than against a description of one.

    A real seat from the same run is what the branch and the tree are read against, because
    "not a seat" is a comparison and the other half of it has to exist: `clone_seat` gave
    the builder that tree, `fleet` named that branch, and the builder's work is in it. The
    remaining assertions are about the calibration alone and are named where they sit.
    """
    repo = make_repo(tmp_path)
    chain = _chain_to_verifier(repo, tmp_path)
    noop = verify.Command.parse([["true"]])
    cal = verify.calibrate(repo, chain.base, tmp_path / "cal", identity=IDENT,
                           contract=finspect.GeneratorContract(), setup=noop, command=noop)

    # Ordinary git, not `gitcmd`: a gate step runs the git the environment gives it, so the
    # property has to survive that one rather than the engine's hardened invocation.
    branch = _git(cal.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    seat_branch = _git(chain.seat.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert (branch, seat_branch) == ("forge/r1/verify", "forge/r1/claude"), \
        "the calibration is not on any branch this run gave a builder"
    assert _git(cal.path, "remote").stdout.strip() == "", \
        "no origin, so the calibration ships no push target back into the user's repository"

    # NO ORIGIN is only half of "the builder was never here". The other half is the tree:
    # the seat holds the agent's work and the calibration holds B1 and nothing else.
    assert (chain.seat.path / "src.py").read_text() == "work\n"
    assert not (cal.path / "src.py").exists(), "the builder's work reached the calibration"
    assert _git(cal.path, "rev-parse", "HEAD").stdout.strip() == chain.base.commit
    assert _git(cal.path, "status", "--porcelain").stdout == ""


def test_setup_replayed_in_the_verifier_sees_the_candidates_own_setup_state(tmp_path):
    """SEAM: `harvest` differences setup-phase state out, `verify.run_setup` regenerates it.

    That exclusion was an argument no code made until setup was replayed in the verifier: a
    seat's setup-phase `.venv` never crosses, so the toolchain the gate runs against is the
    one this call installs.

    THE WINDOW IS THE WHOLE FIXTURE, and it was measured both ways rather than assumed. The
    identical file written in the WORK window comes back in `artifacts.paths`, crosses as an
    ordinary sidecar, and is sitting in the verifier before setup runs — correct carriage of
    the agent's own output, and a different property. Only the SETUP placement asks the
    question this test is named for.

    Which is also what stops this being a test about an empty tree: on one seat and one
    phase boundary, the work crossed and the rig did not.
    """
    chain = _chain_to_verifier(
        make_repo(tmp_path), tmp_path,
        setup=lambda seat: write(seat, ".venv/bin/pytest", "#!/bin/sh\nexit 0\n"))
    assert chain.artifacts.paths == ("src.py",), \
        "harvest kept the rig out of the candidate's claim, and the work in it"
    v = chain.built
    assert (v.path / "src.py").read_text() == "work\n"
    assert not (v.path / ".venv").exists(), "the seat's setup state did not cross"

    verify.run_setup(v, verify.Command.parse(
        [[sys.executable, "-c",
          "import os; os.makedirs('.venv/bin'); "
          "open('.venv/bin/pytest','w').write('#!/bin/sh\\nexit 1\\n')"]]))
    assert (v.path / ".venv" / "bin" / "pytest").read_text() == "#!/bin/sh\nexit 1\n", \
        "and the verifier built its own, which is the file the gate would reach"


def _manifest_for(repo, base=None):
    """A manifest for `repo`, snapshotted now. `base` supplies B's identity when the case is
    about that agreement; the placeholder OIDs stand in when it is not."""
    # A real `base` supplies §9's whitelist the way production does — from `materialize`'s own
    # return value, which is the only thing that knows the OID the ref was created at.
    forge = {base.ref: base.commit} if base else {}
    refs, digest = runstate.snapshot_refs(repo, (), forge_refs=forge)
    return runstate.Manifest(
        run_id="r1", repo_path=str(repo),
        base_commit=base.base_commit if base else "a" * 40,
        baseline_ref=base.ref if base else "refs/khenrix-forge/r1/base",
        baseline_commit=base.commit if base else "b" * 40,
        tracked_tree_oid=base.tracked_tree_oid if base else "c" * 40,
        selected_paths=(), generator_contract=finspect.GeneratorContract(),
        setup=(verify.Step(argv=("true",)),), verify=(verify.Step(argv=("./check.sh",)),),
        protected_refs=refs, forge_refs=forge, status_digest=digest,
        index_digest=runstate.snapshot_index(repo), created_at="2026-08-01T00:00:00Z",
        seats=3, attempts=3, review_rounds=2, synthesis_fix_cap=3)


def test_a_crash_between_intent_and_result_is_not_a_completed_operation(tmp_path):
    """SEAM: `journal` and `runstate`. The write-ahead rule only means something if the
    reconstruction reads it — an orphan that reconstructs as "done" would let a resume skip
    an operation that never finished, which §14.1 says is unrecoverable by inspection.

    The orphan is the FACT and naming it `outcome_unknown` is the orchestrator's job, so
    what this pins on the runstate side is that the fact arrives whole. §14.1's rule is
    "started with no receipt AND NO SURVIVING PROCESS", and the process identity is the only
    part of that a later caller cannot re-derive: a reconstruction that handed back operation
    ids alone would leave the liveness half unanswerable, and an operation that cannot be
    asked whether it is running is one that gets retried.

    The receipt is then appended and the run reconstructed a second time, so the assertions
    above are about the write-ahead PAIRING rather than about "every start is an orphan" —
    which is a shape that would satisfy all of them and be wrong on every completed run.
    """
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    runstate.write_manifest(run, _manifest_for(repo))
    j = journal.Journal(storage.journal_path(run))
    j.record(journal.intent("verify"), operation_id="v1", seat="claude")
    # The killer writes nothing more — this is the SIGKILL §14.1 is about.
    r = runstate.reconstruct(run, repo)
    assert [e.operation_id for e in r.orphans] == ["v1"]
    assert (r.orphans[0].event, r.orphans[0].data["pid"]) == ("verify_start", os.getpid()), \
        "the orphan carries the identity §14.1's liveness question needs, not just an id"

    j.record(journal.done("verify"), operation_id="v1", exit_code=0)
    assert runstate.reconstruct(run, repo).orphans == (), \
        "and the receipt closes it, so the orphan is the pairing and not the start alone"


def test_the_manifest_and_the_baseline_agree_on_what_the_run_started_from(tmp_path):
    """SEAM: `runstate` and `baseline`. The manifest records B's identity so a resume never
    re-derives it; a manifest whose commit disagrees with the ref it names would send a
    verifier to the wrong tree with nothing to say so.

    The repository is DIRTY, which is what makes the agreement checkable: over a clean tree
    B1 IS the base commit and the baseline ref, `HEAD` and `refs/heads/main` all resolve to
    one OID, so a manifest that had recorded `base_commit` in `baseline_commit` would satisfy
    every assertion here. Dirty, the two are different commits and only the right one
    resolves.
    """
    repo = make_repo(tmp_path)
    write(repo, "seed.txt", "the user's uncommitted work\n")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    runstate.write_manifest(run, _manifest_for(repo, base=b))

    back = runstate.read_manifest(run)
    assert back.baseline_commit != back.base_commit, \
        "the premise: B1 is a commit of its own, so naming the wrong one is detectable"
    assert _git(repo, "rev-parse", back.baseline_ref).stdout.strip() == back.baseline_commit
    assert _git(repo, "rev-parse", f"{back.baseline_commit}^{{tree}}").stdout.strip() == \
        back.tracked_tree_oid, \
        "and the tree the manifest names is B1's own, not the base commit's"


def _answered(**kw):
    return {"setup": [["true"]], "verify": [["make", "verify"]],
            "on_calibration_failure": "abort", "strategy": "size-gated",
            "author": ("Ada Lovelace", "ada@example.invalid"), "ultrareview": True, **kw}


def test_the_manifest_records_the_commands_the_gate_confirmed(tmp_path, monkeypatch):
    """SEAM: `gate` and `runstate`. `verify.Step`'s four fields survive the round trip, so a
    resume runs what the human agreed to rather than what a reader supplied.

    FOUR IS WHAT `Step` HAS, not what §5.1 asks for. §5.1 also makes a per-step `retryable`
    mark contractual and nothing implements it, which for a run this gate opens is permanent
    rather than pending — §5 step 5 forbids re-asking, §14.2 forbids rewriting the manifest,
    and `read_manifest` refuses a step record carrying a key it does not know. So the round
    trip below is complete for the record that exists, and the record is short one field §12.3
    was promised. See `gate.Confirmation`'s docstring.

    The first verify step and the setup step give every field a NON-DEFAULT value, which is
    what makes the claim checkable: with `Step`'s own defaults on both sides, a manifest that
    dropped the cwd, the env and the timeout and let `read_manifest` rebuild them would
    satisfy an equality test exactly. The second verify step is all defaults on purpose — the
    ordinary shape has to survive the same trip.

    The second half is the seam the first cannot reach: a manifest is read back by a process
    that never saw the confirmation, so what `--collect` compares against is `read_manifest`'s
    output and not the `Confirmation` still in memory here.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = make_repo(tmp_path)
    (repo / "frontend").mkdir()
    write(repo, "frontend/build.sh", "#!/bin/sh\nexit 0\n")
    commit_all(repo, "a monorepo shape")
    report = preflight.inspect_repo(repo)
    steps = (verify.Step(argv=("./build.sh",), cwd="frontend", env={"CI": "1"}, timeout=45),
             verify.Step(argv=("make", "verify"), cwd="", env={}, timeout=600))
    setup = (verify.Step(argv=("npm", "ci"), cwd="frontend", env={"CI": "0"}, timeout=30),)
    confirmation = gate.confirm(report, gate.quote(report),
                                _answered(setup=list(setup), verify=list(steps)))

    run = gate.open_run(report, confirmation, "r1", quote_=gate.quote(report))
    back = runstate.read_manifest(run)
    # Both commands, and each asserted against its OWN steps: the two are the same type and a
    # manifest that recorded one of them twice round-trips perfectly.
    assert (back.setup, back.verify) == (setup, steps)
    assert [(s.argv, s.cwd, s.env, s.timeout) for s in back.verify] == \
        [(("./build.sh",), "frontend", {"CI": "1"}, 45), (("make", "verify"), "", {}, 600)], \
        "§5.1's step is {argv, cwd, env, timeout}, and a field the reader supplies is a fact " \
        "the run never agreed to"
    assert [(s.argv, s.cwd, s.env, s.timeout) for s in back.setup] == \
        [(("npm", "ci"), "frontend", {"CI": "0"}, 30)]


def test_a_repository_preflight_refuses_never_reaches_a_run_directory(tmp_path, monkeypatch):
    """SEAM: `preflight` and `gate` and `storage`. The refusal is what stops the run, and it
    stops it before anything is written — measured by the run root not existing.

    The exception alone would not show that. `open_run` could raise after creating the run
    directory, after materializing B into the user's object store, or after writing a
    manifest, and every one of those still raises — so what is asserted is the absence of
    each, against the same repository admitted one bit later as the discrimination check.
    """
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    refused = preflight.inspect_repo(repo)
    assert any("skip-worktree" in line for line in preflight.refusals(refused)), \
        preflight.refusals(refused)
    confirmation = gate.confirm(refused, gate.quote(refused), _answered(verify=[["true"]]))

    with pytest.raises(gate.GateError):
        gate.open_run(refused, confirmation, "r1", quote_=gate.quote(refused))
    assert not (state / "khenrix-forge").exists(), "a refused run left a run directory"
    assert "khenrix-forge" not in _git(repo, "show-ref").stdout, \
        "a refused run left a ref in the user's repository"

    _git(repo, "update-index", "--no-skip-worktree", "seed.txt")
    admitted = preflight.inspect_repo(repo)
    assert preflight.refusals(admitted) == ()
    assert runstate.read_manifest(gate.open_run(admitted, confirmation, "r1", quote_=gate.quote(admitted))).run_id == "r1", \
        "the discrimination check: one bit is the whole difference"


def _a_run_to_drive(tmp_path, *, gate_body="exit 0"):
    """A repository whose gate lives in the baseline, plus a run directory `runner.run` drives.

    The gate is a TRACKED script committed before B is taken, because §6 lets a gate come from
    the baseline and nowhere else — a candidate that supplied its own would be measured
    against a gate it wrote. One seat, one attempt: these two cases are about the loop's
    records, and a fleet of three would pay for three clones to say the same thing.
    """
    repo = make_repo(tmp_path)
    script = write(repo, "gate.sh", f"#!/bin/sh\n{gate_body}\n")
    script.chmod(0o755)
    commit_all(repo, "gate")
    run = tmp_path / "run"; run.mkdir()
    b = _baseline(repo, run)
    refs, digest = runstate.snapshot_refs(repo, (), forge_refs={b.ref: b.commit})
    runstate.write_manifest(run, runstate.Manifest(
        run_id="r1", repo_path=str(repo), base_commit=b.base_commit, baseline_ref=b.ref,
        baseline_commit=b.commit, tracked_tree_oid=b.tracked_tree_oid, selected_paths=(),
        generator_contract=finspect.GeneratorContract(),
        setup=(verify.Step(argv=("true",)),), verify=(verify.Step(argv=("./gate.sh",)),),
        protected_refs=refs, forge_refs={b.ref: b.commit}, status_digest=digest,
        index_digest=runstate.snapshot_index(repo), created_at="2026-08-03T00:00:00Z",
        seats=1, attempts=1, review_rounds=2, synthesis_fix_cap=3))
    return repo, run


def _launch(fn):
    """A `runner.run` launch adapter: run `fn(seat_path)`, answer as a provider would.

    NO REAL PROVIDER, here or anywhere in these suites — §5.2 prices a fleet in provider
    calls and a suite that spends them is one nobody runs. The answer clears
    `seat._MIN_RATIONALE_CHARS` once the sentinel is stripped, which is what keeps these
    cases about the seam rather than about §8's rationale rule.
    """
    def launch(*, name, seat_path, token, env):
        fn(Path(seat_path))
        return {"name": name, "status": "ok", "valid": True, "reason": "ok", "exit_code": 0,
                "duration_sec": 1.0, "structured": False, "attempts": 1,
                "result_text": f"the retry already backs off; adding one would "
                               f"double-sleep\n{token}"}
    return launch


def test_the_loops_own_write_ahead_record_reconstructs_as_an_orphan(tmp_path):
    """SEAM: `runner` and `journal`. The write-ahead pair is only worth anything if the
    records the LOOP writes are the ones `reconstruct` reads back as `outcome_unknown`.

    The seam above this one pins the same property with a hand-written journal record, which
    cannot fail if `runner` names its operations something `orphans` never pairs, records the
    receipt before the work, or writes one id for two attempts. Here the intent is the
    runner's own and the process dies inside `launch` — where a real one dies — so the orphan
    that comes back is evidence about the loop and not about the fixture.

    Both halves are asserted: the crashed operation is an orphan, and the operation that
    completed before it is NOT. A loop whose ids did not identify one operation each would
    satisfy the first and fail the second.
    """
    repo, run = _a_run_to_drive(tmp_path)

    def die(seat_path):
        raise RuntimeError("SIGKILL stands in for itself")

    with pytest.raises(RuntimeError, match="SIGKILL"):
        runner.run(run, repo, identity=IDENT, launch=_launch(die))

    r = runstate.reconstruct(run, repo)
    assert [(e.event, e.operation_id) for e in r.orphans] == \
        [(journal.intent("seat"), "r1/claude/attempt-1")]
    assert r.orphans[0].data["pid"] == os.getpid(), \
        "with the identity §14.1's liveness question needs, not just an id"
    events = journal.Journal(storage.journal_path(run)).read()
    assert journal.done("calibration") in [e.event for e in events], \
        "the premise: the operation that finished first closed, so the orphan is the pairing"


def test_the_phase_the_loop_wrote_is_one_the_graph_declares(tmp_path):
    """SEAM: `runner` and `runstate`. §14's graph lives in `runstate._EDGES` and `advance` is
    the only thing that knows it; a loop that moved the phase by assignment would hold a graph
    the spec does not, and every per-module suite would still pass.

    Read back through `read_state` rather than off the returned value, because what a resume
    consults is the FILE — a phase that reached the dataclass and not the disk is a position
    the next process cannot see. Then the whole route is replayed through `advance` from
    `confirmed`, so what is asserted is that the graph admits it rather than that this test
    agrees with it.
    """
    repo, run = _a_run_to_drive(tmp_path)
    runner.run(run, repo, identity=IDENT,
               launch=_launch(lambda p: write(p, "work.py", "the agent's edit\n")))

    state = runstate.read_state(run)
    assert state.phase == "comparing", "where a run with nothing left to fuse stops"
    at = runstate.State(phase="confirmed", round=0, attempt=0, verified_checkpoint=None,
                        deliverable_checkpoint=None)
    for phase in ("setting_up", "building", "harvested", "comparing"):
        at = runstate.advance(at, phase)
    assert at.phase == state.phase
    assert (state.round, state.attempt) == (0, 0), \
        "`advance` moves the phase and nothing else, so the loop spent no round and no retry"


# --------------------------------------------------------------------------- #
# SEAM CLASS: refusals — see the module docstring for what makes this class
# different from every seam above it.
# --------------------------------------------------------------------------- #

_HOST_ONLY = "HOST-ONLY-CONTENT\n"


@contextlib.contextmanager
def _refusals_disabled():
    """Bypass the POLICY — the `rejections` function itself — and nothing it protects.

    Replacing the function is the whole bypass and is deliberately the narrowest one
    available: every mechanism the chain has is left standing — the manifest check in
    `clone_seat`, the containment tests in `bundle`, the hooks pin in `verify` — so a case
    that comes through clean below comes through on a mechanism's own evidence.

    THIS CHANGES NOTHING FOR THE CHAIN BELOW, and the reason moved once already. It used to
    be that nothing anywhere consulted the policy; `preflight.refusals` now does, which is
    what `test_preflight_consults_both_refusals_and_what_they_name_it_refuses` measures. What
    holds instead is narrower and is the reason these cases still read the same:
    `_chain_to_verifier` starts at `baseline.materialize` and never runs preflight, so the
    fixtures below reach a seat and a verifier whether the policy is patched or not. The
    patch is what keeps that true deliberately rather than by accident — and what will keep
    these cases bypassing exactly the policy, and not the defence underneath it, on the day
    an orchestrator puts preflight in front of the chain.
    """
    original = finspect.rejections
    finspect.rejections = lambda facts, selected_untracked: []
    try:
        yield
    finally:
        finspect.rejections = original


_REFUSALS = ("rejections", "screen_tree")


def _references_the_policy(source: bytes) -> bool:
    """True when this source reaches for a refusal in CODE rather than in prose.

    Parsed rather than grepped: every occurrence of the name in `inspect` and `bundle` is
    prose or the definition itself, and not one is a call — so a text search answers yes for
    both modules and the question this asks, is the policy enforced anywhere, becomes
    unanswerable.

    A REFERENCE rather than a `Call`, so `f = finspect.rejections` followed by `f(facts, [])`
    counts: an enforcer that routes the policy through a local name or a dispatch table would
    otherwise read as green. `getattr(finspect, "rejections")` still passes — the name is a
    string there, and chasing it is a dataflow analysis with no end.

    BOTH refusals are watched, because they are enforced together or not at all: §2.3 and §3
    are two conditions at one step of §5's chronology, and a consumer that dropped either
    would leave the other looking enforced.

    The cost of matching a bare name over the whole scanned tree: an unrelated local called
    `rejections` or `screen_tree` anywhere under `shared/` or `scripts/` reds this gate with
    a message about the forge policy. Accepted rather than narrowed — the tripwire exists to fire on a day
    nobody is expecting it, so a false alarm someone must read beats a filter that quietly
    excludes the file the consumer actually lands in. `preflight.Report.rejections` is the
    predictable one: any module that reads a report's own field lands in the list, and the
    right response is to check that module obeys the refusal, not to narrow the match.
    """
    for node in ast.walk(ast.parse(source)):
        name = (node.attr if isinstance(node, ast.Attribute)
                else node.id if isinstance(node, ast.Name) else "")
        if name in _REFUSALS:
            return True
    return False


def test_the_policy_detector_sees_an_aliased_reference():
    """The tripwire's own eyesight, since a tripwire that cannot see is not one.

    Both forms are pinned because narrowing the detector back to `ast.Call` would leave the
    test below green and blind at the same time, with nothing to say so. The definition must
    NOT count, or `inspect.py` reports itself as its own consumer.
    """
    assert _references_the_policy(b"finspect.rejections(facts, [])\n")
    assert _references_the_policy(b"f = finspect.rejections\nf(facts, [])\n")
    assert _references_the_policy(b"screen.screen_tree(root, sel)\n")
    assert not _references_the_policy(b"def rejections(facts, sel):\n    return []\n")


# Every file allowed to reach for a refusal in code. Set equality against this list, not
# containment, so BOTH directions still fire: the consumer disappearing was the original
# subject of this tripwire, and an unvetted second one is what it watches for now. Adding a
# name here is the decision the test's docstring asks you to make deliberately.
_POLICY_CONSUMERS = ["shared/lib/forge/preflight.py"]


def test_preflight_consults_both_refusals_and_what_they_name_it_refuses():
    """SEAM: `inspect.rejections`, `screen.screen_tree`, and the module that obeys them.

    THIS TEST USED TO ASSERT THE OPPOSITE, and the change of subject is the point. Its
    predecessor measured that neither refusal had a single consumer anywhere under `shared/`
    or `scripts/` — a policy nothing calls is a policy that does not run — and it went red the
    day `preflight` imported them. What holds now is one step further on: the policy is wired
    to the thing that stops a run.

    Wiring is asserted BY MEASUREMENT and the two measurements are the tests directly below —
    `test_a_line_rejections_names_is_a_line_refusals_refuses` and
    `test_a_screened_secret_stops_the_run_rather_than_informing_it`. This one may not make
    them, because the property it can see is textual: "preflight imports inspect" would pass
    forever and pin nothing, since a module may import a policy and drop its answer on the
    floor — the shape the predecessor existed to catch, reintroduced one level up.

    What this half still buys is the sweep, over every file under `shared/` and `scripts/`
    including an orchestrator under `shared/skills/llm-forge/scripts/` that does not exist
    yet: it fires when the consumer goes away, and when a second one appears that nobody has
    checked obeys anything.

    WHEN THE SWEEP GOES RED with a new name, decide before adding it: does that module act on
    the refusal, or merely read it? `_refusals_disabled` bypasses the policy for the cases
    below, so a consumer that runs inside them changes what those cases measure.
    """
    scanned = sorted(p for root in (ROOT / "shared", ROOT / "scripts")
                     for p in root.glob("**/*.py"))
    # Both roots must really have been reached, or the comparison below is a statement about
    # an empty sweep — the failure a widened glob is meant to remove, arriving as a pass.
    assert ROOT / "shared" / "lib" / "forge" / "inspect.py" in scanned
    assert ROOT / "scripts" / "render.py" in scanned
    callers = [str(p.relative_to(ROOT)) for p in scanned
               if _references_the_policy(p.read_bytes())]
    assert callers == _POLICY_CONSUMERS, (
        f"{callers} reach for {' or '.join(_REFUSALS)} — see this test's docstring before "
        "adding a name to _POLICY_CONSUMERS")


def test_a_line_rejections_names_is_a_line_refusals_refuses(tmp_path):
    """The second half of the seam above, on a repository built for it.

    `escaping_symlink` rather than `shallow`, because a tracked escaping link is the case
    `bundle`'s docstring defers to preflight for: the link is B's, so B is where the decision
    belongs, and this is the assertion that the decision now exists.
    """
    repo = _mk_escaping_repo(tmp_path)
    facts = finspect.repo_facts(repo)
    named = [r for r in finspect.rejections(facts, [])
             if "tracked symlink escapes the repository" in r]
    assert named, "the fixture no longer trips the refusal it was built for"
    assert set(named) <= set(preflight.refusals(preflight.inspect_repo(repo))), \
        "the policy names it and the consumer drops it — which is the defect this closes"

    assert preflight.refusals(preflight.inspect_repo(_mk_escaping_near_miss(
        tmp_path / "near"))) == (), "and the in-tree link is admitted, or the refusal is noise"


def test_a_screened_secret_stops_the_run_rather_than_informing_it(tmp_path):
    """SEAM: `screen.screen_tree` and `preflight.refusals`. §3 runs the screen before any
    provider starts precisely so its answer can stop one; a finding that only informs is a
    finding that ships the credential to three full-permission cloud-backed agents.

    The near miss is the same directory without the credential file, so what is measured is
    the finding and not the act of selecting a directory.
    """
    repo = make_repo(tmp_path)
    write(repo, "scratch/notes.md", "nothing sensitive\n")
    assert preflight.refusals(preflight.inspect_repo(repo, ("scratch",))) == ()

    write(repo, "scratch/.env", "TOKEN=whatever\n")
    findings, _breaches = screen.screen_tree(repo, ["scratch"])
    assert [f.path for f in findings] == ["scratch/.env"], "the screen must name it first"
    assert preflight.refusals(preflight.inspect_repo(repo, ("scratch",))) == (
        "scratch/.env: high-risk-filename",)


# --- eol_no_roundtrip ------------------------------------------------------ #

def _mk_eol_repo(tmp):
    """The minimal repository whose worktree line endings a checkout would rewrite.

    One added file. `seed.txt` is already committed with LF, so declaring `eol=crlf` over
    it is the whole condition — index LF, worktree LF, and a checkout that would produce
    CRLF. `commit_all`'s `git add -A` does not disturb that: the clean direction is
    CRLF->LF, a no-op on an LF worktree, so the file re-adds to the blob already in the
    index and the tree stays clean.
    """
    repo = make_repo(tmp)
    write(repo, ".gitattributes", "seed.txt text eol=crlf\n")
    commit_all(repo, "declare crlf over an LF worktree")
    return repo


def _mk_eol_near_miss(tmp):
    """The same declaration over a worktree that DOES round-trip."""
    repo = make_repo(tmp)
    write(repo, ".gitattributes", "seed.txt text eol=crlf\n")
    write(repo, "seed.txt", "seed\r\n")
    commit_all(repo, "declare crlf over a crlf worktree")
    return repo


def _harm_of_eol(repo, tmp):
    """"a seat can never reproduce the bytes the baseline manifest records" — as raised.

    The one case of the three that BREAKS the chain. It breaks where the seat checks its own
    checkout against B: the manifest holds the raw worktree bytes, the checkout re-ran the
    smudge, and the two cannot agree for as long as the declaration and the worktree
    disagree.
    """
    with pytest.raises(fleet.SeatError, match="differs from the baseline manifest"):
        _chain_to_verifier(repo, tmp)


def _no_harm_of_eol(chain: _Chain):
    assert chain.seat.verified is True
    assert (chain.verifier / "seed.txt").read_bytes() == b"seed\r\n", \
        "and the bytes really are the converted ones, or the fixture is not the near miss"


# --- escaping_symlink ------------------------------------------------------ #

def _mk_escaping_repo(tmp):
    """The minimal repository holding a TRACKED symlink out of the tree.

    Distinct from
    `test_an_escaping_link_in_a_selection_is_named_by_two_refusals_and_stopped_by_neither`,
    which trips the SELECTED-untracked branch of `rejections` with a nested link and stops
    at the seat. This one trips `facts.escaping_symlinks` — the index-mode-120000 branch,
    which rejects unconditionally — and follows it into the verifier.
    """
    repo = make_repo(tmp)
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "credentials").write_text(_HOST_ONLY)
    (Path(repo) / "creds").symlink_to(outside / "credentials")
    _git(repo, "add", "creds")
    _git(repo, "commit", "-qm", "a tracked link out of the repository")
    return repo


def _mk_escaping_near_miss(tmp):
    """The same tracked link, pointing INSIDE the tree."""
    repo = make_repo(tmp)
    (Path(repo) / "creds").symlink_to("seed.txt")
    _git(repo, "add", "creds")
    _git(repo, "commit", "-qm", "a tracked link inside the repository")
    return repo


def _harm_of_escaping_link(repo, tmp):
    """"every seat would get a working path out of the repository" — and the verifier too.

    Nothing raises. Every module reports success on its own terms: the seat VERIFIES,
    because since D-1 a link is its target text everywhere and the seat's link carries the
    same text B recorded; the bundle omits nothing, because the link is B's, not the
    agent's. The refusal is earned by what the chain then hands over rather than by a break
    — which makes it a strictly more dangerous one to lose, since nothing would report it.

    The VERIFIER is asserted as well as the seat because it is the tree the gate runs in,
    built to be independent of the builder: a confirmed verify command executing there can
    read and write a host path through this link.

    WHEN THIS GOES RED, someone has closed the hole, and the red is good news wearing a
    regression's clothes: this case pins the ABSENCE of a defence (`verified is True`,
    `omitted == ()`, a host file reachable from the gate). Do not restore the absence — move
    each assertion onto whatever now refuses, and leave the case measuring the same seam.
    """
    chain = _chain_to_verifier(repo, tmp)
    assert chain.seat.verified is True and chain.candidate.omitted == (), \
        "no module in the chain objects, which is what makes the refusal load-bearing"
    for tree, what in ((chain.seat.path, "seat"), (chain.verifier, "verifier")):
        link = tree / "creds"
        assert link.is_symlink(), what
        assert link.read_text() == _HOST_ONLY, f"{what} reads outside the repository"

    # The WRITE, and through the engine's own runner: `run_command` is what executes a
    # confirmed verify command, so this is the gate itself — in the tree built to be
    # independent of the builder — reaching a path the repository never contained. Reading
    # is the lesser half; a gate that can write outside its clone can rewrite the thing it
    # was cloned to judge. argv, never a shell.
    host = (chain.verifier / "creds").readlink()
    assert not host.is_relative_to(chain.verifier), "the fixture's link must really escape"
    # And escapes only as far as the test's own tmp_path. Without this, a later fixture edit
    # that repoints the link would have this test overwrite whatever it then names.
    assert host.is_relative_to(chain.tmp), "the target must stay inside the test's tmp_path"
    run = verify.run_command(chain.verifier, verify.Command.parse(
        [[sys.executable, "-c", "open('creds', 'w').write('PWNED-BY-THE-GATE\\n')"]]))
    assert run.exit_code == 0, run.stderr
    assert host.read_text() == "PWNED-BY-THE-GATE\n", \
        "the gate wrote out of its own tree, onto a host file, at exit 0"


def _no_harm_of_escaping_link(chain: _Chain):
    for tree in (chain.seat.path, chain.verifier):
        link = tree / "creds"
        assert link.is_symlink() and link.read_text() == "seed\n", \
            "the near miss carries a link too, so the probe measures the TARGET, not its "\
            "presence"


# --- shallow --------------------------------------------------------------- #

def _mk_shallow_repo(tmp):
    """The minimal shallow repository: a depth-1 clone of a two-commit source.

    `file://` rather than a path, because git takes a plain local path as the LOCAL
    transport and ignores `--depth` there. Measured on git 2.53: the plain form prints
    "--depth is ignored in local clones; use file:// instead", exits 0, and produces a
    clone with no `.git/shallow` — a fixture that quietly stopped being shallow.
    """
    origin = make_repo(tmp, name="origin")
    write(origin, "b.txt", "b\n")
    commit_all(origin, "second")
    _git(tmp, "clone", "-q", "--depth", "1", f"file://{origin}", str(tmp / "repo"))
    return tmp / "repo"


def _mk_shallow_near_miss(tmp):
    """The same source, cloned whole."""
    origin = make_repo(tmp, name="origin")
    write(origin, "b.txt", "b\n")
    commit_all(origin, "second")
    _git(tmp, "clone", "-q", f"file://{origin}", str(tmp / "repo"))
    return tmp / "repo"


def _harm_of_shallow(repo, tmp):
    """"history is incomplete; clone semantics differ" — and the truncation propagates.

    Nothing raises here either: git 2.53 clones happily from a shallow repository, the seat
    verifies against B's manifest (which describes the TREE, and the tree is complete), and
    the verifier builds. What the chain delivers is two trees that are themselves shallow,
    where an ordinary history command exits 128 — so an agent asked to bisect, rebase, or
    read the log fails, and so does a verify command that runs one.

    Both trees are asserted because they fail differently: in the SEAT it lands on the
    agent, and in the VERIFIER it is an infrastructure failure of the gate, which §4 is most
    insistent must never be read as a verdict on the candidate.

    WHEN THIS GOES RED, as with the escaping link above: this case pins the ABSENCE of a
    defence — `verified is True` over a truncation nothing objects to — so a consumer for
    the refusal, or a fetch that deepens the clone, turns it red as a side effect of being
    fixed. Move the assertions onto whatever now refuses rather than deleting the case.
    """
    chain = _chain_to_verifier(repo, tmp)
    assert chain.seat.verified is True, \
        "the truncation is invisible to every check the chain makes"
    for tree in (chain.seat.path, chain.verifier):
        assert _git(tree, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
        with pytest.raises(RuntimeError, match="unknown revision"):
            _git(tree, "log", "-1", "HEAD~1")


def _no_harm_of_shallow(chain: _Chain):
    for tree in (chain.seat.path, chain.verifier):
        assert _git(tree, "rev-parse", "--is-shallow-repository").stdout.strip() == "false"
        assert _git(tree, "log", "-1", "--format=%s", "HEAD~1").stdout.strip() == "seed"


# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _Refusal:
    """One `inspect.rejections` line, and everything needed to hold it to account.

    `marker` is a substring of the line itself, so a fixture that trips a DIFFERENT
    refusal — easy to write by accident, since these repositories are unusual on purpose —
    does not read as this one firing.
    """
    name: str
    marker: str
    build: object
    near_miss: object
    harm: object
    no_harm: object


REFUSAL_FIXTURES = [
    _Refusal("eol_no_roundtrip", "worktree line endings do not round-trip",
             _mk_eol_repo, _mk_eol_near_miss, _harm_of_eol, _no_harm_of_eol),
    _Refusal("escaping_symlink", "tracked symlink escapes the repository",
             _mk_escaping_repo, _mk_escaping_near_miss,
             _harm_of_escaping_link, _no_harm_of_escaping_link),
    _Refusal("shallow", "shallow repository: history is incomplete",
             _mk_shallow_repo, _mk_shallow_near_miss, _harm_of_shallow, _no_harm_of_shallow),
]


@pytest.mark.parametrize("case", REFUSAL_FIXTURES, ids=lambda c: c.name)
def test_a_repo_preflight_refuses_would_have_failed_downstream(case, tmp_path):
    """Every refusal is EARNED: with the policy bypassed, the chain delivers the harm the
    refusal's own text names.

    Not `pytest.raises` for all three, which is what this task set out to write. Measured:
    one raises and two complete, so a shared raises-anything assertion would have had to be
    weakened to something two of the three could satisfy — and an assertion satisfied by
    "the chain finished" is satisfied by a chain that finished wrongly. Each case names its
    consequence instead.
    """
    repo = case.build(tmp_path)
    facts = finspect.repo_facts(repo)
    named = [r for r in finspect.rejections(facts, []) if case.marker in r]
    assert named, (f"{case.name}: the fixture no longer trips its refusal — "
                   f"rejections said {finspect.rejections(facts, [])}")
    with _refusals_disabled():
        case.harm(repo, tmp_path)


@pytest.mark.parametrize("case", REFUSAL_FIXTURES, ids=lambda c: c.name)
def test_each_refusal_fixture_is_one_property_from_an_admitted_repository(case, tmp_path):
    """The discrimination check for the test above: the near miss must DISAGREE with it.

    A fixture and the behaviour it claims to trip can be wrong together — the fixture is
    exotic, the refusal fires, and neither the fixture nor the assertion notices it fired
    for a reason nobody meant. So each fixture is paired with a repository that differs in
    exactly the one property the refusal names, and that one must be admitted AND arrive at
    a verifier without the harm.

    That second half is what makes this more than a `rejections == []` check: it is also
    the admission half of this seam class, run on three repositories that are individually
    strange — a CRLF worktree, a tracked symlink, a full clone with a remote.
    """
    repo = case.near_miss(tmp_path)
    facts = finspect.repo_facts(repo)
    assert finspect.rejections(facts, []) == []
    chain = _chain_to_verifier(repo, tmp_path)
    case.no_harm(chain)


def test_a_repo_preflight_admits_reaches_a_clean_pass(tmp_path):
    """The other half: a clean repository must reach a VERDICT, and the verdict is `PASS`.

    This case used to assert `GATE_CHANGED` with the reason "nobody measured the gate
    surface", by equality on both, so that it would go red rather than drift once the chain
    could answer. IT DID NOT GO RED, and that is worth more than the pin was: the producers
    landed and this test kept passing, because it classified the bundle the SEAT's side
    built — where `gate_delta` is None whatever the verifier went on to measure. An equality
    is only as sharp as the value it is read off, and a chain helper that handed back a
    `Path` instead of the `Verifier` is what kept the measured value out of reach.

    Every value here now comes from the chain. The delta is `build_verifier`'s, the only
    caller that holds the baseline tree and the candidate tree one after the other; the
    baseline run is `calibrate`'s, the only producer of one; and the candidate's run is a
    `fixed_point`, which is what makes the PASS §6.2's rather than the weaker form
    `classify` gives a bare `Run`.

    `command=gate` is not decoration. `./check.sh` matches no role rule, so without it both
    surfaces are empty, the delta is a clean `()` measured over nothing, and this test would
    certify a PASS on an unexamined gate — which is why `baseline_surface` is asserted.

    WHAT THIS PASS IS NOT. The contract is `detect_generators`', which is empty for every
    repository, so the gate here earns its PASS by rewriting NO tracked file rather than by
    having its rewrites admitted. A gate whose verify command regenerates tracked output —
    this repository's own `make verify` — still cannot reach PASS, and closing that needs a
    contract nothing yet derives.
    """
    repo = make_repo(tmp_path)
    write(repo, "check.sh", "#!/bin/sh\nexit 0\n")
    (Path(repo) / "check.sh").chmod(0o755)
    commit_all(repo, "gate")
    facts = finspect.repo_facts(repo)
    assert finspect.rejections(facts, []) == []

    contract = finspect.detect_generators(repo)
    setup = verify.Command.parse([["true"]])
    gate = verify.Command.parse([["./check.sh"]])
    chain = _chain_to_verifier(repo, tmp_path, contract=contract, command=gate)
    # "Completes" has to mean the candidate ARRIVED, not merely that no call raised: a
    # verifier holding none of the agent's work would still run the gate and still be
    # classified.
    assert chain.seat.verified is True and chain.candidate.omitted == ()
    assert (chain.verifier / "src.py").read_text() == "work\n"

    v = chain.built
    assert v.baseline_surface == ("check.sh",), \
        "an empty surface would make the clean delta below a measurement of nothing"
    verify.validate_materialized(v)
    verify.run_setup(v, setup)
    fp = verify.fixed_point(v.path, gate, v.contract)
    assert fp.run.exit_code == 0, f"the gate itself failed: {fp.run.stderr}"
    cal = verify.calibrate(repo, chain.base, tmp_path / "cal", identity=IDENT,
                           contract=contract, setup=setup, command=gate)

    outcome, reason = verify.classify(fp, cal.run, v.candidate)
    assert v.candidate.gate_delta == (), "measured and clean, not unmeasured"
    assert outcome == verify.PASS, reason
    assert "rewrote no tracked path outside the generator contract" in reason, \
        "and §6.2's PASS, not the weaker one a bare Run earns"


# --- the call-site closure over `gitcmd`'s three argv presets -------------------------------
#
# A BEHAVIOURAL TEST ONLY COVERS THE PATHS ITS FIXTURE REACHES, which is how the fsmonitor
# hole survived every wave that closed part of it, each site found only after the last was
# fixed — and how `baseline.materialize`'s `write-tree` hid for two waves behind a CLEAN
# fixture, since the clean branch returns before the tree is ever built. The closures below
# read call SITES, so a site added tomorrow fails them tomorrow whatever any fixture reaches.

# THESE ARE ALLOW-LISTS, AND THE INVERSION IS THE POINT. Each was a list of subcommands that
# DO the thing, so a resolved subcommand nobody had enumerated fell through to False and was
# silently exempt from all three rules at once — while an UNRESOLVED one failed closed. The
# next call site this project adds is `git worktree add -b` into the user's own repository
# (spec §4 makes the synthesis tree a linked worktree sharing the user's `.git`, §16
# prescribes the command), and measured with the monitor armed and hooks planted it ran the
# monitor twice and fired `post-index-change` and `post-checkout` — passing all three
# enumerations without a flag. Inverted, an unmeasured verb reads as needing every preset,
# exactly as `*args` already did, and the way to clear one is to measure it.
#
# SO EVERY NAME BELOW CARRIES ITS MEASUREMENT. All three sets were measured together on git
# 2.53.0, one fresh repository per probe, in the argv form this package actually uses, under
# the environment `gitcmd.git` builds and WITHOUT the presets: a `core.fsmonitor` script and
# all 25 hook names planted at `.git/hooks`; then `diff.external`; then an in-tree
# `.gitattributes` marking `* diff=probe` with `diff.probe.command` and, separately,
# `diff.probe.textconv`.

# Measured NOT to load an index, so measured not to run `core.fsmonitor`. Everything else
# does, or has not been looked at. `ls-files` in every form ran it — `--cached` alone
# included — as did `status`, `diff`, `add`, `write-tree`, `check-attr`, `update-index` and
# `checkout -b`, which writes the index on its way to moving HEAD.
#
# ONLY VERBS THIS PACKAGE CALLS, on all three lists, and
# `test_every_verb_the_allow_lists_clear_is_one_this_package_calls` holds them to it.
# `cat-file`, `for-each-ref` and `var` were cleared on all three with no call site anywhere in
# the package: a clearance nobody could check against a caller, which is the enumeration
# problem in the inversion's clothes. `gitcmd.py` still records their measurement in prose —
# that is a measurement, and a measurement is not a clearance until a call needs one. The
# reader who adds such a call re-measures the argv form it actually uses, which is how `apply`
# came to decide by flag and `remote` to be read by its first word.
_INDEX_SAFE = frozenset({
    # Six forms measured between them: `--show-toplevel`, `HEAD`, `--absolute-git-dir`,
    # `--git-common-dir`, `--verify HEAD` and `<c>^{tree}`. None opens an index.
    "rev-parse",
    "show-ref", "symbolic-ref",
    # All four forms this package runs: `--get`, `--get-regexp`, and the two WRITES
    # (`config user.name X` and `config --local core.hooksPath /dev/null`).
    "config",
    "check-ref-format",   # measured with this inversion; the only verb in use nobody had
    "commit-tree", "update-ref",
    # `remote` and `remote remove origin` alike, the latter against a clone that HAS a fetch
    # refspec — the condition that makes it touch refs at all.
    "remote",
    # The SOURCE's index is not opened; the destination has none yet.
    "clone",
})
# `apply` is the one subcommand that decides by FLAG rather than by name: measured, `apply
# --index` runs the monitor and writes the index, while `apply --numstat` and a bare `apply`
# do neither — they never open one.
_APPLY_INDEX = "--index"

# What fires a hook out of the hooks directory the call's own repository resolves. Measured on
# git 2.53.0 with a full hook set planted at `.git/hooks` and, separately, at a `core.hooksPath`
# directory the agent wrote — the two agreed on every row:
#
#   `update-ref`      `reference-transaction`
#   writing an index  `post-index-change`
#   `checkout -b`     all three of `post-checkout`, `post-index-change`, `reference-transaction`
#   `remote remove`   `reference-transaction`, but ONLY where `remote.<n>.fetch` names refs to
#                     delete. A `--revision=` clone is given no fetch refspec (measured: `url`
#                     and `tagopt` only), so `fleet`'s own call deletes nothing and fires
#                     nothing — a property of that clone's shape, not of the subcommand.
#   `clone`           the DESTINATION's hooks, installed out of `--template`; the source
#                     repository's are never consulted.
#
# `status` is NOT here although the same measurement clears it under READONLY: with
# `GIT_OPTIONAL_LOCKS=0` it fired nothing and without it fired `post-index-change`, so a call
# added without READONLY would fire the user's hook. The closure asks for the flags rather
# than reasoning about a second call's env. `apply` decides by `--index` for the reason it
# does above, and on the same measurement: that form wrote the index and fired
# `post-index-change`, the other two fired nothing.
#
# Membership is read off the FIRST positional word, so `remote` and `remote remove` are one
# verb — and it is the DELETING form that fires `reference-transaction`, so the verb is
# absent from this set and the listing form pays a preset it cannot need. That is the
# fail-closed direction and it costs one flag pair; a second-word rule would have to be right
# about every second word anyone adds.
#
# Measured to fire something, and so absent below: `add`, `write-tree` and `apply --index`
# (`post-index-change`), `update-ref` and `remote remove` (`reference-transaction`),
# `status` without READONLY, `checkout -q -b` (`post-checkout`, `post-index-change`,
# `reference-transaction`) and `clone` (the same three, out of the DESTINATION's template).
_HOOK_SAFE = frozenset({
    "rev-parse", "show-ref", "symbolic-ref",
    "config",             # all four forms, the two writes included
    "check-ref-format", "check-attr",
    "ls-files",           # every form: `-z`, `--eol`, `-v -s -z`, `--cached --others`
    "commit-tree",        # writes an object and no ref, so no transaction to report
    "diff",               # measured in both config scopes, with and without READONLY
})

# What is measured NOT to run a DIFF DRIVER — the repository's THIRD program, after
# `core.fsmonitor` and its hooks. `diff.external` needs nothing but the config;
# `diff.<d>.command` and `diff.<d>.textconv` are selected by an in-tree `.gitattributes`,
# which a seat also writes. `diff` ran all three and is absent below; every verb here ran
# none of them. `add` and `checkout` are on the list for the driver only — they run a
# `filter.<d>.clean`/`smudge`, which is the fourth program and no argv closes it.
#
# INVERTED LIKE THE OTHER TWO, and here the fail-closed direction cannot be answered with
# flags: `NO_DIFF_DRIVERS` is a SUBCOMMAND option, so `rev-parse --no-ext-diff` is an error.
# An unmeasured verb is therefore cleared by measuring it onto this list or by an exemption
# naming what it actually runs.
#
# `worktree` and `fetch` were measured ONTO this list rather than exempted, which is the
# stronger of the two routes this comment offers. Measured on git 2.53.0 with `diff.external`,
# `diff.<d>.command` and `diff.<d>.textconv` all planted and an in-tree `.gitattributes`
# selecting the driver: `worktree add -b` and `fetch --no-tags --no-write-fetch-head <path>
# <refspec>` fired NONE of the three, while the control `git diff` over the same repository
# fired `diff.probe.command`. They are absent from `_INDEX_SAFE` and `_HOOK_SAFE` on the
# opposite measurement — `worktree add -b` fires core.fsmonitor once, `post-checkout` once,
# `post-index-change` once and `reference-transaction` FOURTEEN times, and `fetch` fires
# core.fsmonitor once and `reference-transaction` twice — so both call sites carry
# NO_DAEMON_CACHE and NO_HOOKS, which were measured necessary and sufficient for all of them.
# `--no-ext-diff` could not have been used in any case: it is rc=129 for both verbs in every
# argv position, before, after and among the subcommand's own arguments.
_DIFF_DRIVER_SAFE = frozenset({
    "rev-parse", "show-ref", "symbolic-ref",
    "config", "check-ref-format", "check-attr", "ls-files", "status",
    "add", "write-tree", "commit-tree", "update-ref", "checkout", "clone", "remote",
    "apply",              # both forms: `--numstat -z` and `--index --binary`
    # FOUR FORMS OF `worktree`, EACH MEASURED, because each is its own argv and re-measuring
    # is what clears one — exactly as `apply` came to decide by flag and `remote` by first
    # word. §15's `--gc` added the last three, measured on git 2.53.0 in the same rig as the
    # paragraph above (all three diff-driver keys planted, `.gitattributes` selecting the
    # driver, a `core.fsmonitor` script and the full hook set): `worktree list --porcelain`
    # and `worktree unlock <path>` fired NOTHING AT ALL — no driver, no monitor, no hook —
    # and `worktree remove <path>` fired the monitor and `post-index-change` and no driver.
    # The two that fire are why the verb stays off `_INDEX_SAFE` and `_HOOK_SAFE` and every
    # `worktree` call in the package carries both presets, which costs the two read-only
    # forms a flag pair they do not need. That is the fail-closed direction.
    "worktree",           # `add -b <branch> <dest> <at>`, `list --porcelain`,
                          # `unlock <path>`, `remove <path>`
    "fetch",              # `fetch --no-tags --no-write-fetch-head <path> <refspec>`
})


@dataclass(frozen=True)
class _GitCall:
    module: str
    func: str
    line: int
    sub: str            # the resolved subcommand, or "" when no literal one is reachable
    flags: frozenset    # literal option strings passed positionally
    presets: frozenset  # dotted names splatted in, e.g. "gitcmd.NO_DAEMON_CACHE"
    user_config: bool   # `user_config=True`: this call reads the USER's global/system config

    @property
    def where(self) -> str:
        return f"{self.module}:{self.func}:{self.sub or '<unresolved>'}"


def _git_calls(source: bytes, module: str) -> list:
    """Every `gitcmd.git(...)` in one module, with its subcommand and its splatted presets.

    Parsed rather than grepped, for the reason the module's `_references_the_policy` gives one
    section up: the strings this looks for appear in prose in every one of these files, and a
    text search cannot tell a call from a comment about a call.

    The SUBCOMMAND is the first positional string literal after `repo`. That holds because
    `-c` options only ever arrive as a splatted preset here, never spelled at a call site —
    which is itself the property the tests below pin, since a hand-spelled `"-c"` would take
    this slot and read as an unknown subcommand rather than as a preset.

    A call whose subcommand cannot be resolved (`*args`) yields `sub=""`, and the tests treat
    that as needing the flags: an unresolved call is exactly the one no reader can clear.

    A BARE `git(...)` counts inside `gitcmd.py` and nowhere else. That module calls its own
    function unqualified, so a `status` added to `zero_oid`'s neighbour would otherwise be the
    one call site in the package this closure cannot see — and the one holding the rule.
    """
    calls = []
    tree = ast.parse(source)
    scopes = [(n.lineno, n.end_lineno, n.name) for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        qualified = (isinstance(f, ast.Attribute) and f.attr == "git"
                     and isinstance(f.value, ast.Name) and f.value.id == "gitcmd")
        own = module == "gitcmd.py" and isinstance(f, ast.Name) and f.id == "git"
        if not (qualified or own):
            continue
        sub, flags, presets = "", set(), set()
        for arg in node.args[1:]:
            if isinstance(arg, ast.Starred):
                presets.add(ast.unparse(arg.value))
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.startswith("-"):
                    flags.add(arg.value)
                elif not sub:
                    sub = arg.value
        # A KEYWORD, not a positional: `user_config=True` is the one door out of the /dev/null
        # pin on the user's global config, and a door nothing reads is a door.
        #
        # FAIL CLOSED LIKE THE THREE SETS ABOVE, and this is the closure where that matters
        # most: it guards a DELIBERATE hole in the defence they are all built from. A literal
        # `False` is the only spelling that reads as closed, because `git()` branches on
        # `if not user_config` — so `1`, a named constant and an expression all open the pin at
        # runtime while a `value is True` reader called every one of them closed. `**kwargs`
        # counts too: the keyword set is unreadable, and unreadable is the same answer an
        # unresolved subcommand already gets.
        user_config = False
        for k in node.keywords:
            # Monotone on purpose: once something opens the pin, a later keyword cannot close
            # it again — `git(..., **kw, user_config=False)` still passes whatever `kw` holds.
            if k.arg is None or (k.arg == "user_config" and not (
                    isinstance(k.value, ast.Constant) and k.value.value is False)):
                user_config = True
        # The innermost enclosing def, so a nested helper is named rather than its parent.
        func = min((s for s in scopes if s[0] <= node.lineno <= s[1]),
                   key=lambda s: s[1] - s[0], default=(0, 0, "<module>"))[2]
        calls.append(_GitCall(module=module, func=func, line=node.lineno, sub=sub,
                              flags=frozenset(flags), presets=frozenset(presets),
                              user_config=user_config))
    return calls


def _forge_git_calls() -> list:
    out = []
    for p in sorted((ROOT / "shared" / "lib" / "forge").glob("*.py")):
        out.extend(_git_calls(p.read_bytes(), p.name))
    return out


def _loads_an_index(call: _GitCall) -> bool:
    """An unresolved subcommand ("") is in no allow-list, so it reads as needing the flags —
    the same answer the enumeration gave it, now reached by the same rule as every other
    unmeasured verb rather than by a special case."""
    if call.sub == "apply":
        return _APPLY_INDEX in call.flags
    return call.sub not in _INDEX_SAFE


def _fires_a_hook(call: _GitCall) -> bool:
    if call.sub == "apply":
        return _APPLY_INDEX in call.flags
    return call.sub not in _HOOK_SAFE


def _runs_a_diff_driver(call: _GitCall) -> bool:
    return call.sub not in _DIFF_DRIVER_SAFE


# Every call site allowed to load an index WITHOUT the flags, and the reason it is allowed.
# Keyed by module, function and subcommand rather than by line, so the entry survives an edit
# above it and still names one call. Set equality in both directions below: an entry whose
# call is gone fails just as loudly as a call with no entry, because a stale exemption is how
# an allow-list turns into the decoration it was written to replace.
#
# BOTH of these read a tree the ENGINE built, and every caller was traced to confirm it: a
# verifier or calibration clone, cloned under an empty template so it carries no hooks
# directory, with `_hooks_pin` asserted over it and `bundle.materialize` refusing a sidecar
# under `.git`. Nothing the repository supplied can name a program in that config before the
# engine itself runs the setup and gate commands there — and once it has run them, a
# `core.fsmonitor` they left behind is code the engine already granted itself by running them.
# The seat is the tree where that argument does NOT hold, which is why `harvest.artifact_set`
# and `bundle._rediff` carry the flags rather than an entry here.
#
# WHAT THE FIRST ENTRY RESTS ON, stated here because it is a claim about CALLERS and this
# file cannot check one. "Every caller was traced" was true when it was written and is not a
# property of the code: `verify.fixed_point(verifier_path, …)` is public and takes any path,
# so a caller added later that passes a SEAT path makes the exemption false with nothing here
# to say so. The rule for that day is the entry's own words — it clears a tree the ENGINE
# built — so a seat-facing caller of the verify chain is what deletes this entry and puts the
# flags on the call, not what widens the sentence.
_DAEMON_CACHE_EXEMPT = {
    "verify.py:_tracked:ls-files":
        "reads the verifier or calibration clone around the commands the engine runs in it",
    "verify.py:_checkpoint:add":
        "stages what those same commands produced, in that same clone",
}

# The hooks rule is the whole package's, not one module's, and the reason it USED to be
# `baseline.py` alone no longer holds: that scoping said a seat clone is built under an empty
# template "with no hooks directory to fire", which is true of the clone and not of the tree an
# agent then owns for its entire run. Measured — a seat that mkdirs `.git/hooks`, or points
# `core.hooksPath` at a directory it wrote, has those hooks run by an ordinary git in that tree.
# No engine call into a seat AFTER THE AGENT'S RUN fires one: `diff` and `apply --numstat` are
# not hook-firing subcommands (measured, both config scopes, with and without READONLY). The
# calls that DO fire all sit in `clone_seat`, before the agent starts — its `clone` runs the
# template's hooks and its `checkout -b` runs the seat's — and they carry the flags rather than
# an entry here. This closure is what makes the next one fail on the day it is written.
_HOOKS_EXEMPT = {
    "verify.py:_checkpoint:add":
        "the verifier is pinned by `_hooks_pin` and the pin is read back by "
        "`_assert_hooks_pinned`; a hook surviving that is one the engine's own setup or gate "
        "command installed, which is code the engine granted itself by running them",
}

# A diff of a tree whose config the engine did not write runs a program that tree names, and
# `NO_DIFF_DRIVERS` is the only thing that stops it — the other two presets do not reach it.
# The one entry is an unresolved call, not a diff; both diffs this package runs read a BUILDER
# SEAT and carry the flags.
_DIFF_DRIVER_EXEMPT = {
    "inspect.py:g:<unresolved>":
        "a local helper for `rev-parse`, `status` and `ls-files` in the user's own "
        "repository; it runs no diff producer, and the flags are not options those accept",
}

# The calls allowed to read the USER's global and system config, and what each was measured
# to run there. `gitcmd.NO_USER_CONFIG` is on every other call because the global file can
# name a PROGRAM — `core.hooksPath`, `core.fsmonitor`, `url.*.insteadOf` — so an entry here
# is a claim that this subcommand runs none of them, and it is the same measurement the three
# sets above rest on.
_USER_CONFIG_ALLOWED = {
    "gate.py:value:config":
        "`config --get user.name`/`user.email` for the identity B1 is authored with, which "
        "lives in ~/.gitconfig on an ordinary machine and is the one fact the pin hides from "
        "this engine. Measured on git 2.53.0 with a monitor armed and all 25 hooks planted: "
        "`config --get` ran neither, in any of its four forms",
}


def test_the_git_call_reader_sees_a_new_call_site():
    """The closure's own eyesight, since a closure that cannot see is not one.

    Each line below is a shape that has actually appeared in this package, and the last two are
    the ones a naive reader gets wrong: `apply` decides by flag, and a splatted `*args` hides
    the subcommand entirely.
    """
    def one(src):
        c = _git_calls(src.encode(), "x.py")
        assert len(c) == 1, c
        return c[0]

    assert one('gitcmd.git(repo, "write-tree")').sub == "write-tree"
    assert one('gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "ls-files", "-z")').presets == \
        {"gitcmd.NO_DAEMON_CACHE"}
    assert _loads_an_index(one('gitcmd.git(r, "apply", "--index", f)'))
    assert not _loads_an_index(one('gitcmd.git(r, "apply", "--numstat", f)'))
    assert _loads_an_index(one('gitcmd.git(r, *gitcmd.NO_DAEMON_CACHE, *args)')), \
        "an unresolved subcommand must read as needing the flags, not as clear"
    assert not _loads_an_index(one('gitcmd.git(r, "rev-parse", "HEAD")'))
    assert _fires_a_hook(one('gitcmd.git(r, "apply", "--index", f)')), \
        "the hooks rule reads `apply` by flag too: that form writes the index"
    assert not _fires_a_hook(one('gitcmd.git(r, "apply", "--numstat", f)'))
    assert not _fires_a_hook(one('gitcmd.git(r, "diff", "--binary", c)')), \
        "a diff fires no hook in any config scope (measured); the closure must not say it does"
    assert _fires_a_hook(one('gitcmd.git(d, "checkout", "-q", "-b", b)')), \
        "a checkout writes the index and moves HEAD: three hooks, measured"
    assert _loads_an_index(one('gitcmd.git(d, "checkout", "-q", "-b", b)')), \
        "and the same write is what runs `core.fsmonitor` (measured)"
    assert _fires_a_hook(one('gitcmd.git(d, "remote", "remove", "origin")'))
    assert _fires_a_hook(one('gitcmd.git(d, "remote")')), \
        "the first word decides, so the listing form rides on `remove`'s entry"
    assert not _loads_an_index(one('gitcmd.git(d, "remote", "remove", "origin")')), \
        "no index is opened either way; only the hooks rule reaches this one"
    assert _fires_a_hook(one('gitcmd.git(r, "clone", "--no-local", s, d)')), \
        "a clone fires the DESTINATION's template hooks, which a caller can supply"
    # A preset splatted AFTER the subcommand is still a preset, and does not take the
    # subcommand slot: `NO_DIFF_DRIVERS` can only be passed there, since a diff option before
    # `"diff"` is an error.
    assert one('gitcmd.git(r, "diff", *gitcmd.NO_DIFF_DRIVERS, "--binary", c)').sub == "diff"
    assert one('gitcmd.git(r, "diff", *gitcmd.NO_DIFF_DRIVERS, "--binary", c)').presets == \
        {"gitcmd.NO_DIFF_DRIVERS"}
    assert _runs_a_diff_driver(one('gitcmd.git(r, "log", "-p")'))
    assert not _runs_a_diff_driver(one('gitcmd.git(r, "rev-parse", "HEAD")'))
    assert _runs_a_diff_driver(one('gitcmd.git(r, *args)')), \
        "an unresolved subcommand must read as needing an exemption, not as clear"
    assert not _loads_an_index(one('gitcmd.git(r, "config", "--get", "user.name")')), \
        "a measured-safe verb is cleared by BEING on the list, not by being absent from one"
    assert _loads_an_index(one('gitcmd.git(r, "merge", "--no-ff", b)')), \
        "the inversion's whole point: a verb nobody measured needs the flags"
    assert _fires_a_hook(one('gitcmd.git(r, "merge", "--no-ff", b)')) and \
        _runs_a_diff_driver(one('gitcmd.git(r, "merge", "--no-ff", b)')), \
        "and needs them for all three rules, not for whichever one was enumerated"
    # `worktree` USED TO BE THAT EXAMPLE and is now the other half of the same point: it was
    # cleared on ONE rule by being measured on that rule, and stays unmeasured on the two it
    # was measured to trip. A verb cleared everywhere by one measurement is the enumeration
    # problem back again.
    assert _loads_an_index(one('gitcmd.git(r, "worktree", "add", "-b", b, d)')) and \
        _fires_a_hook(one('gitcmd.git(r, "worktree", "add", "-b", b, d)')), \
        "worktree add -b runs core.fsmonitor and three hooks (measured); both flags stay"
    assert not _runs_a_diff_driver(one('gitcmd.git(r, "worktree", "add", "-b", b, d)')), \
        "and runs no diff driver (measured), which is the one rule it is cleared on"
    assert not one('gitcmd.git(r, "config", "--get", "user.name")').user_config
    assert one('gitcmd.git(r, "config", "--get", "user.name", user_config=True)').user_config, \
        "the door out of the /dev/null pin is a keyword, and the reader has to see it"
    # THE FOURTH CLOSURE FAILS CLOSED LIKE THE THREE ABOVE IT, and it is the one guarding a
    # DELIBERATE hole in the defence the other three are built from, so it is the last place
    # an unrecognized spelling may read as clear. `git()` tests the parameter with
    # `if not user_config`, so every one of these opens the pin at runtime while a
    # `value is True` reader called each of them closed.
    assert one('gitcmd.git(r, "config", user_config=1)').user_config, \
        "`1` opens the door at runtime: git() branches on truth, not on identity with True"
    assert one('gitcmd.git(r, "config", user_config=OPEN_THE_PIN)').user_config, \
        "a named constant is unreadable here, and unreadable must mean open"
    assert one('gitcmd.git(r, "config", **kw)').user_config, \
        "`**kwargs` can carry the keyword, so the reader cannot say it does not"
    assert not one('gitcmd.git(r, "config", user_config=False)').user_config, \
        "the discrimination check: a literal False is the one spelling that is closed"
    assert one('def f(r):\n    gitcmd.git(r, "status")\n').func == "f"
    assert _git_calls(b'git(r, "status")\n', "gitcmd.py"), \
        "gitcmd calls its own function unqualified; a site added there must still be read"
    assert not _git_calls(b'git(r, "status")\n', "verify.py"), \
        "elsewhere a bare `git` is somebody's local helper, not this one"


def test_every_verb_the_allow_lists_clear_is_one_this_package_calls():
    """The allow-lists describe THIS package's calls, and an entry with no call site is the
    enumeration problem wearing the inversion's clothes.

    A name here is a measurement, and a measurement is only ever needed by a caller. Cleared
    without one it grows the exempt set with nothing to check it against: `cat-file`,
    `for-each-ref` and `var` sat on all three lists with no call site anywhere in the package,
    so the day one was written it would have inherited a clearance nobody re-measured against
    the argv form that call actually uses — which is how `apply` came to need a flag rule and
    `remote` to be read by its first word only.

    Both directions already run on the three EXEMPT dicts, for the same reason and in the same
    words ("a stale exemption is how an allow-list turns into the decoration it was written to
    replace"). This is that rule for the SAFE sets. The other direction — a call site with no
    entry — is the three closures below.
    """
    verbs = {c.sub for c in _forge_git_calls() if c.sub}
    assert verbs, "this package no longer runs git; the closure now guards nothing"
    for name, allowed in (("_INDEX_SAFE", _INDEX_SAFE), ("_HOOK_SAFE", _HOOK_SAFE),
                          ("_DIFF_DRIVER_SAFE", _DIFF_DRIVER_SAFE)):
        assert allowed <= verbs, (
            f"{name} clears {sorted(allowed - verbs)}, which this package never calls. Delete "
            "the name, or add the call site the measurement was taken for — an entry justified "
            "by a measurement nobody needed cannot be checked against a caller.")


def test_every_index_loading_call_carries_the_daemon_cache_flags():
    """§5 step 1 admits no repository-supplied code before authorization, and `core.fsmonitor`
    is a PROGRAM named in the repository's own config that git runs for whoever loads an index.
    GIT_INDEX_FILE does not exempt a call — the program comes from the config, not the index —
    so the only thing that suppresses it is the flags being at the call site.

    THIS IS A CLOSURE, NOT A SAMPLE. Every behavioural test in this repository covers the paths
    one fixture reaches, and every site this rule has been lost at was found only after the
    previous one was fixed. Reading the call sites is what makes the next one fail on the day
    it is written.
    """
    missing = sorted(f"{c.module}:{c.line} {c.func} git {c.sub or '*args'}"
                     for c in _forge_git_calls()
                     if _loads_an_index(c)
                     and "gitcmd.NO_DAEMON_CACHE" not in c.presets
                     and c.where not in _DAEMON_CACHE_EXEMPT)
    assert not missing, (
        "these calls load an index and would run the repository's `core.fsmonitor`: "
        f"{missing}. Splat `*gitcmd.NO_DAEMON_CACHE` into each, or add it to "
        "_DAEMON_CACHE_EXEMPT with the reason it reads a tree the engine already ran code in.")

    claimed = {c.where for c in _forge_git_calls()
               if _loads_an_index(c) and "gitcmd.NO_DAEMON_CACHE" not in c.presets}
    assert claimed == set(_DAEMON_CACHE_EXEMPT), \
        "an exemption whose call site is gone: the allow-list no longer describes the code"
    assert all(_DAEMON_CACHE_EXEMPT.values()), "an exemption must say why"


def test_every_hook_firing_call_carries_the_hooks_pin():
    """The user's decision — forge does not fire the user's hooks for its own bookkeeping —
    held as a closure for the same reason as the one above, and with a sharper need for it:
    only a DIRTY fixture reaches the two `add`s and the `write-tree`, and only a CLEAN one
    reaches the `update-ref` that returns before the tree is built. No single behavioural
    fixture covers all five sites, and the two that do exist would each go on passing if the
    branch they do not enter lost its pin.

    PACKAGE-WIDE, so it also covers the trees the user does not own. A BUILDER SEAT is the
    tree with no pin available at all: `verify._hooks_pin` works because a verifier has
    exactly one writer between the pin and `_assert_hooks_pinned`, while a seat's agent writes
    to `.git/config` continuously until harvest and can unset a clone-time pin with one
    command (measured). What defends the engine there is what the engine passes on its own
    calls, so this closure is where a seat-facing `add`, `status`, `update-ref` or
    `apply --index` is required to carry the flags — on the day it is written, not after a
    fixture happens to reach it.
    """
    calls = _forge_git_calls()
    assert calls, "this package no longer runs git; the closure now guards nothing"
    missing = sorted(f"{c.module}:{c.line} {c.func} git {c.sub or '*args'}"
                     for c in calls
                     if _fires_a_hook(c)
                     and "gitcmd.NO_HOOKS" not in c.presets
                     and c.where not in _HOOKS_EXEMPT)
    assert not missing, (
        "these calls write an index or a ref and would fire the hooks of whichever "
        f"repository they run in: {missing}. Splat `*gitcmd.NO_HOOKS` into each, or add it "
        "to _HOOKS_EXEMPT with the reason a hook there is one the engine authorized.")

    claimed = {c.where for c in calls
               if _fires_a_hook(c) and "gitcmd.NO_HOOKS" not in c.presets}
    assert claimed == set(_HOOKS_EXEMPT), \
        "an exemption whose call site is gone: the allow-list no longer describes the code"
    assert all(_HOOKS_EXEMPT.values()), "an exemption must say why"


def test_every_diff_producing_call_disables_the_repositorys_diff_drivers():
    """A diff driver is the repository's third program, and the two presets above do not reach
    it: `diff.external` alone, or `diff.<d>.command`/`diff.<d>.textconv` picked out by an
    in-tree `.gitattributes`, names a command git runs while producing the diff. Measured on
    git 2.53.0 in a builder seat, on this package's own invocation: all three ran the seat's
    program, and `core.hooksPath=/dev/null` stopped none of them.

    Both sites this binds today read a SEAT, which is the tree whose `.git/config` the agent
    owned for its whole run — the same tree, and the same two calls, where `core.fsmonitor`
    was found running the seat's program after the agent's process was gone.

    The flags are load-bearing for the PATCH as well as for the engine: the same measurement
    gave 278 bytes of textconv output and 206 of driver output where an honest seat gives 311,
    and `diff.external` gave zero bytes at exit 0 — the silent, empty-candidate loss
    `--binary` and `:(literal)` are already spent on.
    """
    calls = _forge_git_calls()
    missing = sorted(f"{c.module}:{c.line} {c.func} git {c.sub or '*args'}"
                     for c in calls
                     if _runs_a_diff_driver(c)
                     and "gitcmd.NO_DIFF_DRIVERS" not in c.presets
                     and c.where not in _DIFF_DRIVER_EXEMPT)
    assert not missing, (
        "these calls produce a diff and would run a program the repository's own config "
        f"names: {missing}. Splat `*gitcmd.NO_DIFF_DRIVERS` in AFTER the subcommand — they "
        "are diff options, not `-c` ones — or add the call to _DIFF_DRIVER_EXEMPT with the "
        "reason it reads a tree whose config the engine wrote.")

    claimed = {c.where for c in calls
               if _runs_a_diff_driver(c) and "gitcmd.NO_DIFF_DRIVERS" not in c.presets}
    assert claimed == set(_DIFF_DRIVER_EXEMPT), \
        "an exemption whose call site is gone: the allow-list no longer describes the code"
    assert all(_DIFF_DRIVER_EXEMPT.values()), "an exemption must say why"


def test_only_a_measured_call_reads_the_users_own_config():
    """`gitcmd.git(..., user_config=True)` unpins GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM, and
    the user's global file is the one place a `core.hooksPath` or a `url.*.insteadOf` reaches
    every repository at once — §4.1 names both. The pin is the package's blanket answer to
    that, so a door out of it is only as good as the list of who walked through.

    A CLOSURE for the same reason the three above are: this one has exactly one call site
    today, which is the condition under which a rule is easiest to lose. The set equality runs
    both ways, so an entry whose call is gone fails as loudly as a call with no entry.
    """
    calls = _forge_git_calls()
    opened = {c.where for c in calls if c.user_config}
    assert opened == set(_USER_CONFIG_ALLOWED), (
        f"these calls read the user's global and system config: {sorted(opened)}. Either drop "
        "`user_config=True`, or add the call to _USER_CONFIG_ALLOWED with the measurement "
        "showing that subcommand runs none of the programs that file can name.")
    assert all(_USER_CONFIG_ALLOWED.values()), "an entry must say what was measured"


def test_the_installed_plugin_paths_have_one_spelling():
    """`taskbundle.INSTALL_GLOBS` duplicates `refresh.INSTALL_GLOBS` because `shared/lib/`
    may not import `scripts/`. Two spellings of one predicate eventually disagree — and the
    disagreement here would be silent: a stale glob hashes a directory that is not the one
    `make khenrix-refresh` writes, and §20's identity rule would compare the wrong closures.

    THE VACUITY THIS GUARDS. For a CLI that is not installed BOTH enumerations answer `[]`,
    so on a machine with none installed every assertion below passes over three empty lists
    and proves nothing about a producible hash — the same "a check over an empty manifest is
    vacuous and still answers True" shape `fleet.Seat.verified` is written against. So the
    equality is asserted for every CLI, and the producibility is asserted for the installed
    ones, with an explicit skip rather than a silent pass when there are none.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts"))
    import refresh  # noqa: PLC0415

    from forge import taskbundle  # noqa: PLC0415
    assert taskbundle.INSTALL_GLOBS == refresh.INSTALL_GLOBS
    for cli in refresh.CLIS:
        assert taskbundle._install_dirs(cli) == sorted(refresh.installed_dirs(cli)), \
            f"{cli}: the two enumerations disagree about what is installed"
    installed = [c for c in refresh.CLIS if refresh.installed_dirs(c)]
    if not installed:
        pytest.skip("no CLI is installed here: the two enumerations agree over three empty "
                    "lists, which is agreement about nothing")
    for cli in installed:
        assert taskbundle.installed_closure(cli) is not None, \
            f"{cli} is installed and its closure hash must be producible"


def test_the_engine_text_strip_has_one_spelling_across_sections_8_and_11():
    """§8's rationale floor and §11's semantic hash must strip exactly the same text. They
    were two functions once; the note-before-token order is measured, and a second copy that
    reversed it would leave §11 labelling identically-prompted seats as differently-prompted
    while §8 went on measuring the right thing."""
    from council import engine as eng  # noqa: PLC0415
    from forge import fingerprint, runner  # noqa: PLC0415
    tok = eng.make_sentinel()
    for body in ("", "short", "an argued conclusion about why nothing needs to change"):
        text = eng.apply_sentinel(body, tok)
        assert runner._rationale(text, tok) == fingerprint.without_engine_text(text, tok)
    assert runner._rationale.__doc__ and "fingerprint.without_engine_text" in \
        runner._rationale.__doc__, "the delegation must be stated where the rule was measured"


def test_the_unreviewed_label_is_spelled_in_exactly_one_place():
    """SEAM: §13/§14.2 say 'independently reviewed' and §13.1 says 'independently
    re-reviewed'. Two spellings of one predicate is two things a reader has to notice are
    the same, and a grep for either finds half the run's states."""
    root = Path(__file__).resolve().parents[1] / "shared" / "lib" / "forge"
    literal = '"verified but not independently reviewed"'
    holders = [p.name for p in sorted(root.glob("*.py"))
               if literal in p.read_text(encoding="utf-8")]
    assert holders == ["review.py"], holders
    assert not any("independently re-reviewed" in p.read_text(encoding="utf-8")
                   for p in root.glob("*.py"))


def test_the_bundle_hash_the_launcher_claims_and_the_one_the_seat_gets_have_one_source():
    """Two spellings of one predicate will eventually disagree.

    `launch.make_launcher(bundle_sha256=…)` is what reaches `fingerprint.PromptIdentity`, and
    `runner._materialize_the_task` is what reaches the seat's disk. This asserts both are
    derived from `storage.task_bundle_path(run_dir)` through `taskbundle`'s own decoder and
    from nothing else — read off the SOURCE, because a behavioural test would need a real CLI.
    """
    forge_dir = ROOT / "shared" / "lib" / "forge"
    src = (forge_dir / "runner.py").read_text(encoding="utf-8")
    body = src[src.index("def _materialize_the_task"):]
    body = body[:body.index("\ndef ")]
    assert "read_task_bundle_if_recorded(run_dir)" in body
    assert "storage.task_bundle_path" not in body, (
        "the materializer opens the manifest path itself instead of going through "
        "taskbundle's decoder — that is the second spelling")

    # And the decoder really is ONE decoder: the `if_recorded` variant must delegate to
    # `read_task_bundle` rather than carry its own `json.loads`, or a field check added to one
    # would silently not apply to the path `runner` takes.
    tb = (forge_dir / "taskbundle.py").read_text(encoding="utf-8")
    ifrec = tb[tb.index("def read_task_bundle_if_recorded"):]
    ifrec = ifrec[:ifrec.index("\ndef ")] if "\ndef " in ifrec else ifrec
    assert "return read_task_bundle(run_dir)" in ifrec, ifrec
    assert "json.loads" not in ifrec and "_decode" not in ifrec, (
        "read_task_bundle_if_recorded decodes the manifest itself — that is the second decoder")

    cli = forge_dir / "cli.py"
    if cli.exists():          # a later plan lands this; the assertion arms itself when it does
        t = cli.read_text(encoding="utf-8")
        assert "read_task_bundle(" in t or "read_task_bundle_if_recorded(" in t
        assert "bundle_hash(" in t, (
            "the CLI computes a bundle hash some other way than taskbundle.bundle_hash")


def test_the_branch_name_has_one_spelling():
    """`fleet.clone_seat` computes `forge/{run_id}/{name}` for a seat and `handover.branch`
    computes it for the transport and the synthesis tree. Two spellings of one predicate will
    eventually disagree, and this one decides whether a seat's work can be found at all."""
    from forge import handover  # noqa: PLC0415
    assert handover.branch("r1", "claude") == "forge/r1/claude"
    assert handover.branch("r1", handover.SYNTHESIS) == "forge/r1/synthesis"
    src = (ROOT / "shared" / "lib" / "forge" / "fleet.py").read_text()
    assert 'f"forge/{run_id}/{name}"' in src or "handover.branch(" in src, (
        "fleet no longer spells the seat branch the way this test reads it — re-derive the "
        "pair rather than widening the pattern")


def test_both_materializers_hand_a_tree_the_same_bundle_by_the_same_route():
    """SEAM: `runner._materialize_the_task` lays §20's bundle into a SEAT and
    `handover.create_synthesis_worktree` lays it into the SYNTHESIS tree. Two call sites, one
    contract, and the parts that must not drift are the decoder, the byte source and the
    read-back — a second site that opened `storage.task_bundle_path` itself, or skipped
    `verify_materialized`, would hand one of the two trees a bundle nothing checked.

    Read off the SOURCE for `test_the_bundle_hash_the_launcher_claims…`'s reason: a
    behavioural test of the seat half needs a fleet, and what is at issue is the route rather
    than one run's bytes.
    """
    forge_dir = ROOT / "shared" / "lib" / "forge"
    runner_src = (forge_dir / "runner.py").read_text(encoding="utf-8")
    seat = runner_src[runner_src.index("def _materialize_the_task"):]
    seat = seat[:seat.index("\ndef ")]
    hand_src = (forge_dir / "handover.py").read_text(encoding="utf-8")
    synth = hand_src[hand_src.index("def create_synthesis_worktree"):]
    synth = synth[:synth.index("\ndef ")]
    for where, body in (("runner._materialize_the_task", seat),
                        ("handover.create_synthesis_worktree", synth)):
        assert "read_task_bundle_if_recorded(run_dir)" in body, where
        assert "storage.task_source_path(run_dir)" in body, where
        assert "verify_materialized(" in body, where
        assert "storage.task_bundle_path" not in body, (
            f"{where} opens the manifest path itself instead of going through taskbundle's "
            "decoder — that is the second spelling")


def test_the_synthesis_checkout_reviewers_are_sent_to_holds_the_task_it_was_given(tmp_path):
    """SEAM: `handover.create_synthesis_worktree` produces the tree and `review.run_round`
    interrogates it. §13 sets every reviewer's cwd to that tree, and `run_round` decides
    `task_bundle_present` by asking it for `taskbundle.task_dir` — so until a producer
    existed, §20's bundle reached three seats and no synthesis tree, and every reviewer was
    told "There is no task bundle in this checkout" while judging work built from one.

    ASSERTED THROUGH `run_round` RATHER THAN THROUGH THE PREDICATE, because the predicate is
    one line of `run_round` and restating it here would be this file asserting its own copy.
    What the round produces is REVIEW.md, which is what a reviewer actually reads.
    """
    from forge import handover, ledger, review, taskbundle  # noqa: PLC0415

    repo = make_repo(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    src = storage.task_source_path(run)
    src.mkdir(parents=True)
    (src / "TASK.md").write_text("Refactor the thing.\n")
    taskbundle.write_task_bundle(run, taskbundle.scan(src, entrypoint="TASK.md"))
    row = ledger.Row(id=ledger.row_id("R1", "c"), requirement_id="R1",
                     requirement_span="spec.md:1-3", requirement_sha256="0" * 64,
                     kind="architecture", component="core", semantic_claim="c",
                     status="rejected", dependencies=(), seat_evidence=(), counterevidence="",
                     acceptance_criteria=(), synthesis_evidence=None,
                     verification_receipt=None, risk="", rationale="none")
    ledger.write_ledger(run, ledger.Ledger(
        version=ledger.VERSION, rows=(row,), union_diff_bytes=10,
        degrade_threshold_bytes=ledger.DEGRADE_UNION_DIFF_BYTES, degraded=False))

    synth = tmp_path / "synth"
    handover.create_synthesis_worktree(repo, synth, run_id="r1",
                                       at=_git(repo, "rev-parse", "HEAD").stdout.strip(),
                                       run_dir=run)

    answer = ("I read the diff.\n" + "z" * 500 + '\n```json\n{"findings": []}\n```')

    def run_council(specs, *, retries, timeout, backoff, workdir, prompt=None, requested=None,
                    mode=None, read_only=None, install_signal_handler=True, env=None):
        Path(workdir).mkdir(parents=True, exist_ok=True)
        providers = []
        for s in specs:
            rf = Path(workdir) / f"{s.name}.result.txt"
            rf.write_text(answer)
            providers.append({"name": s.name, "valid": True, "reason": "ok",
                              "result_text": answer[:80], "result_file": str(rf),
                              "model": s.model})
        return {"providers": providers, "prompt_sha256": None}

    from forge import fingerprint  # noqa: PLC0415
    review.run_round(
        run, round_=1, checkout=synth, checkpoint="a" * 40, baseline_commit="b" * 40,
        baseline_tree="c" * 40, artifact_manifest=None, other_clones=(),
        log=journal.Journal(storage.journal_path(run)), run_council=run_council,
        probe=lambda **kw: fingerprint.build(
            prompt=kw["prompt"], token=kw["token"], cli=kw["cli"],
            bundle_sha256=kw.get("bundle_sha256"),
            model_requested=kw.get("model_requested"),
            model_reported=kw.get("model_reported"),
            run=lambda *a, **k: __import__("subprocess").CompletedProcess(a, 0, "1.0", ""),
            closure=lambda cli: "closure-" + cli),
        make_token=lambda: "TOK")

    text = (review.review_dir(synth, 1) / "REVIEW.md").read_text()
    assert "no task bundle" not in text, text
    assert "khenrix-forge/task" in text
    # NON-VACUITY: the same round over a checkout with no bundle still says there is none, so
    # the assertion above is about this tree and not about the sentence having been deleted.
    bare = tmp_path / "bare"
    handover.create_synthesis_worktree(repo, bare, run_id="r2",
                                       at=_git(repo, "rev-parse", "HEAD").stdout.strip(),
                                       run_dir=tmp_path / "empty-run")
    assert "no task bundle" in review.write_reviewer_inputs(
        bare, 1, checkpoint="a" * 40, baseline_commit="b" * 40, baseline_tree="c" * 40,
        artifact_manifest=None, token="TOK",
        task_bundle_present=Path(taskbundle.task_dir(bare)).is_dir()).joinpath(
            "REVIEW.md").read_text()


def test_the_task_bundle_a_run_records_and_the_bytes_it_materializes_have_one_name():
    """SEAM: `storage.task_source_path` is the ONLY name for the bundle's bytes, and
    `runner._materialize_the_task` is what reads it. A second spelling here — the operator's
    own directory, a path derived at the front end — is what §20's "persist the resolved
    instruction" exists to prevent: `--collect` would then depend on a tree that may be gone.
    """
    forge_dir = ROOT / "shared" / "lib" / "forge"
    src = (forge_dir / "runner.py").read_text(encoding="utf-8")
    body = src[src.index("def _materialize_the_task"):]
    body = body[:body.index("\ndef ")]
    assert "storage.task_source_path(run_dir)" in body, body
    holders = sorted(p.name for p in forge_dir.glob("*.py")
                     if 'Path(run_dir) / "task"' in p.read_text(encoding="utf-8"))
    assert holders == ["storage.py"], holders


def test_the_run_directory_naming_scheme_has_one_home():
    """SEAM: `gc` walks the directories `storage` writes. A second copy of the formula in
    `gc.py` was measured byte-identical to `storage.run_root`'s — which is exactly when a
    duplicate is most dangerous, because nothing fails until one of them moves and then
    `--gc all` reports an empty disk over a full one.

    THE ARITHMETIC IS ASSERTED TO BE SHARED, not just absent from `gc.py`: `run_root` writes
    the path, `run_dirs` enumerates it and `gc`'s ownership check resolves against it, so all
    three read the same two helpers rather than three expressions that agree today.
    """
    forge_dir = ROOT / "shared" / "lib" / "forge"
    gc_src = (forge_dir / "gc.py").read_text(encoding="utf-8")
    assert "sha256" not in gc_src, "gc re-derives the run-directory digest instead of asking"
    assert "XDG_STATE_HOME" not in gc_src, "gc re-derives the state root instead of asking"
    assert "storage.run_dirs(" in gc_src and "storage.forge_root(" in gc_src

    storage_src = (forge_dir / "storage.py").read_text(encoding="utf-8")
    body = storage_src[storage_src.index("def run_root("):]
    body = body[:body.index("\ndef ")]
    assert "run_digest(" in body and "forge_root(" in body, body
    assert "hashlib" not in body, "run_root spells the digest a second time"
    # ONE HOLDER OF THE STATE ROOT, across the package: the walk and the writer cannot drift
    # if neither of them owns the string. `"khenrix-forge"` is NOT asserted the same way and
    # the reason is that it is not one name — `review._FORGE_SUBDIR` and
    # `taskbundle` spell it as a directory under the GIT DIR, which is a different place from
    # the state root and would be a false positive here.
    holders = sorted(p.name for p in forge_dir.glob("*.py")
                     if '"XDG_STATE_HOME"' in p.read_text(encoding="utf-8"))
    assert holders == ["storage.py"], holders
