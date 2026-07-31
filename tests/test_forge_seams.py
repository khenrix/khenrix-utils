"""Properties that must hold ACROSS module boundaries.

Plan B's suites each exercised one module, with others present only as fixtures. Three
shipped defects lived in those seams: the manifest missed a selected directory's
contents, the seat's exclude replay silently no-opped in a linked worktree, and the
seat's environment re-admitted the redirectors gitcmd strips. Each is one assertion here.

Every assertion below names TWO modules and holds only if both agree. A test that would
still pass with either side broken belongs in that side's own suite, not this file.
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import (baseline, bundle, fleet, gitcmd, harvest,  # noqa: E402
                   inspect as finspect, screen, snapshot)
from forge_fixtures import git as _git, make_repo, write  # noqa: E402

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


def test_an_escaping_link_inside_a_selected_directory_never_reaches_a_seat(tmp_path):
    """SEAM: four modules individually consistent and jointly wrong, measured end to end.

    `inspect.rejections` tested only the top-level selected path; `screen._walk` dropped a
    nested link in silence; `baseline` kept it out of the manifest while `git add -f` put
    it in the TREE; `fleet`'s verification skipped symlinks. Result, measured on this exact
    fixture: rejections `[]`, breaches `[]`, `verified=True`, and a seat whose
    `scratch/creds` read a file outside the repository. Every module's own suite was green.

    The manifest and `fleet` now describe the link by its target text (Plan D, D-1), which
    changes nothing about the containment story: describing a link is not refusing one, so
    the two refusals asserted below remain the whole defence.

    BOTH gates are asserted rather than one, because either alone would let a single edit
    reopen the whole path.

    The last two assertions CHARACTERIZE what the tree still does — the link is committed
    and a seat can follow it — rather than endorsing it. That behaviour is the tracked-
    symlink design question routed to Plan D, and it is what makes the two refusals above
    load-bearing instead of decorative.
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
