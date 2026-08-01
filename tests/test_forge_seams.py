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
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import (baseline, bundle, fleet, gitcmd, harvest,  # noqa: E402
                   inspect as finspect, screen, snapshot, verify)
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
    edit reopen the whole path. Neither STOPS anything today — Task 5 measured that
    `rejections` and `screen_tree` each have zero consumers, so both return a list into a
    void. What is pinned here is that they still NAME it; the closing assertions
    CHARACTERIZE what the tree does anyway, and the distance between the two halves is the
    finding, not a gap in the test.
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

    Nothing in here consults `inspect.rejections`; that is the property under test, and
    `test_nothing_in_the_chain_consults_either_refusal` is where it is measured rather
    than assumed.
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

    TODAY THIS CHANGES NOTHING, and that is measured next door: nothing under `shared/` or
    `scripts/` so much as names `rejections` in code, so the chain never consults the policy
    whether it is patched or not. The patch is here so that the day a consumer appears —
    spec §5 lists unsupported-feature rejections at step 1 of its confirmation chronology,
    so that is where one belongs — these cases keep bypassing exactly the policy and not
    the defence underneath it.
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

    BOTH refusals are watched. `screen_tree` has no consumer either, and the escaping-link
    seam's docstring says so — a claim that would go false in silence if only `rejections`
    were pinned here.

    The cost of matching a bare name over the whole scanned tree: an unrelated local called
    `rejections` or `screen_tree` anywhere under `shared/` or `scripts/` reds this gate with
    a message about the forge policy. Accepted rather than narrowed — the tripwire exists to fire on a day
    nobody is expecting it, so a false alarm someone must read beats a filter that quietly
    excludes the file the consumer actually lands in.
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


def test_nothing_in_the_chain_consults_either_refusal():
    """SEAM: `inspect.rejections`, `screen.screen_tree`, and every module that would obey.

    This is the premise the whole section below rests on, so it is measured here instead of
    asserted in a docstring. Each returns a list and no module reads it: the refusals are
    enforced by whoever calls preflight, and spec §5's chronology — the caller that would —
    is not in this plan.

    SCANNED OVER `shared/` AND `scripts/`, not just `shared/lib/forge/*.py`. The consumer
    this waits for is that chronology, whose likeliest home is an orchestrator under
    `shared/skills/llm-forge/scripts/` which does not exist yet — outside the package, and a
    glob that cannot see it stays green on the one day it should fire.

    WHEN THIS GOES RED, a consumer has appeared. That is the intended direction and nothing
    here should be deleted for it: `_refusals_disabled` has just become load-bearing, so
    check that it still bypasses only the policy, and that each case below still reaches the
    mechanism it is measuring.
    """
    scanned = sorted(p for root in (ROOT / "shared", ROOT / "scripts")
                     for p in root.glob("**/*.py"))
    # Both roots must really have been reached, or `callers == []` is a statement about an
    # empty sweep — the failure a widened glob is meant to remove, arriving as a pass.
    assert ROOT / "shared" / "lib" / "forge" / "inspect.py" in scanned
    assert ROOT / "scripts" / "render.py" in scanned
    callers = [str(p.relative_to(ROOT)) for p in scanned
               if _references_the_policy(p.read_bytes())]
    assert callers == [], (
        f"{callers} now enforce {' or '.join(_REFUSALS)} — see this test's docstring "
        "before changing anything below it")


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
