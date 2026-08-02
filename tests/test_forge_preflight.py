"""What preflight refuses, what it admits, and what it declines to claim.

Every refusal case is paired with a discrimination check — a repository or a selection that
differs in exactly the one property the refusal names and is ADMITTED. Without the pair, a
fixture strange enough to trip its own refusal is also strange enough to trip a different
one, and `refusals()` being non-empty says nothing about which.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from forge import preflight  # noqa: E402
from forge_fixtures import commit_all, git as _git, make_repo, write  # noqa: E402

# A shape `scripts/lib/checks.py` recognises, built rather than pasted so this file holds no
# string a secret scanner has to be told to ignore.
_AKIA = "AKIA" + "IOSFODNN7EXAMPL"[:12] + "QRST"


def _refusals(repo, selected=()):
    return preflight.refusals(preflight.inspect_repo(repo, selected))


def test_a_clean_repository_has_no_refusals(tmp_path):
    repo = make_repo(tmp_path)
    r = preflight.inspect_repo(repo)
    assert preflight.refusals(r) == ()
    assert r.repo == Path(repo)
    assert r.facts.head, "and the report really did look at the repository"


def test_skip_worktree_is_refused_at_preflight(tmp_path):
    """§2.3 lists it, and `git add -u -- :/` exits 0 while SILENTLY SKIPPING such a path, so
    `baseline.materialize` builds B without the user's hidden edit and reports `dirty=False`
    — measured in this repository's `runstate` docstring and re-measured for this task.

    What does NOT hold is that nothing downstream sees it. Measured on this exact fixture:
    `fleet.clone_seat` raises `SeatError: seat content differs from the baseline manifest`,
    because B's manifest hashes the raw worktree bytes (which carry the hidden edit) while
    B's tree does not. That is the §4 shape a refusal exists to avoid rather than an argument
    against one — an infrastructure failure three stages later, attributed to the seat, in
    place of a sentence naming a bit the user can clear.
    """
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    got = _refusals(repo)
    assert any("skip-worktree" in line for line in got), got


def test_the_same_repository_without_the_bit_is_admitted(tmp_path):
    """The discrimination check for the case above: `make_repo` on its own must be clean, or
    the refusal there could have come from anything a fixture repository carries."""
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    _git(repo, "update-index", "--no-skip-worktree", "seed.txt")
    assert _refusals(repo) == ()


def test_a_shallow_repository_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "second.txt", "x\n")
    commit_all(repo, "second")
    shallow = tmp_path / "shallow"
    # `file://` rather than a path: git takes a plain local path as the LOCAL transport and
    # ignores `--depth` there, producing a clone that quietly is not shallow.
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow))
    got = _refusals(shallow)
    assert any("shallow" in line for line in got), got

    whole = tmp_path / "whole"
    _git(tmp_path, "clone", "-q", f"file://{repo}", str(whole))
    assert _refusals(whole) == (), "and a full clone of the same source is admitted"


def test_a_selected_path_that_escapes_the_repository_is_refused(tmp_path):
    """The containment check the selection has never had.

    Measured: `inspect.rejections(facts, ["../outside.txt"])` is `[]`, and the escape is not
    merely unrefused — `runstate.snapshot_refs`'s carried digest MOVED when the host file
    changed under that selection, so an unguarded selection hashes content outside the
    repository into the run's own identity.

    The containment line is asserted BY NAME rather than through "some refusal fired".
    `screen_tree` applies the same lexical rule and would breach on this path anyway, so an
    assertion satisfied by any non-empty answer would stay green with this check deleted —
    which is the whole property under test.
    """
    repo = make_repo(tmp_path)
    (tmp_path / "outside.txt").write_text("host\n")
    r = preflight.inspect_repo(repo, ("../outside.txt",))
    assert r.escaping == ("selected path escapes the repository: '../outside.txt'",)
    assert preflight.refusals(r)[0] == r.escaping[0]
    assert r.breaches == (), \
        "and the escaping path never reached the screen, so it was refused rather than read"
    assert r.selected == ("../outside.txt",), "the report still says what was asked for"


def test_an_absolute_selection_is_refused(tmp_path):
    """The other half of the same lexical rule, and the half `Path` hides: `root / "/etc"` IS
    `/etc`, so an unguarded absolute selection replaces the repository root in silence."""
    repo = make_repo(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "note.txt").write_text("host\n")
    r = preflight.inspect_repo(repo, (str(outside),))
    assert r.escaping == (f"selected path escapes the repository: {str(outside)!r}",)
    assert r.breaches == () and r.rejections == ()


def test_an_ordinary_relative_selection_is_not_refused_as_an_escape(tmp_path):
    """The discrimination check for both cases above: the containment rule must admit the
    selection forge exists to carry, or it would refuse every run."""
    repo = make_repo(tmp_path)
    write(repo, "scratch/notes.txt", "notes\n")
    r = preflight.inspect_repo(repo, ("scratch",))
    assert r.escaping == () and preflight.refusals(r) == ()


def test_a_secret_in_a_selected_path_is_a_refusal_not_a_note(tmp_path):
    """§3: the screen runs BEFORE any provider starts. A finding that only informs is a
    finding that ships the credential to three providers.

    BOTH of the screen's routes are exercised, because the obvious fixture only reaches one.
    Measured: `AWS_SECRET_ACCESS_KEY=<40 chars>` matches no pattern in `checks.SECRET_FAIL`
    at all — that file is caught by its NAME. A file whose name is unremarkable and whose
    contents match a pattern is the other route, and nothing else here would cover it.
    """
    repo = make_repo(tmp_path)
    write(repo, "scratch/.env", "AWS_SECRET_ACCESS_KEY=" + "A" * 40 + "\n")
    write(repo, "scratch/deploy.md", f"aws key {_AKIA}\n")
    r = preflight.inspect_repo(repo, ("scratch",))
    assert {f.path for f in r.secrets} == {"scratch/.env", "scratch/deploy.md"}
    got = preflight.refusals(r)
    assert "scratch/.env: high-risk-filename" in got, got
    assert any(line.startswith("scratch/deploy.md:1:") for line in got), got


def test_a_selection_with_nothing_in_it_is_admitted(tmp_path):
    """The discrimination check for the case above: an ordinary scratch directory must come
    through clean, or "a selection was refused" would carry no information."""
    repo = make_repo(tmp_path)
    write(repo, "scratch/notes.md", "aws key AKIA-not-a-key\n")
    write(repo, "scratch/env.sample", "AWS_SECRET_ACCESS_KEY=\n")
    r = preflight.inspect_repo(repo, ("scratch",))
    assert r.secrets == () and preflight.refusals(r) == ()


def test_a_selected_path_the_screen_could_not_read_is_a_refusal(tmp_path):
    """A breach is not a finding — it is the ABSENCE of one — and it still stops the run.
    A screen that certifies what it never opened is worth no more than one that finds
    nothing.

    The link points INSIDE the tree so that `rejections` stays silent: an escaping one is
    refused by two categories at once, and this case is about the breach on its own.
    """
    repo = make_repo(tmp_path)
    (Path(repo) / "scratch").mkdir()
    (Path(repo) / "scratch" / "creds").symlink_to("../seed.txt")
    r = preflight.inspect_repo(repo, ("scratch",))
    assert r.secrets == () and r.rejections == (), \
        "nothing was found and nothing was rejected, which is exactly the danger"
    assert preflight.refusals(r) == (
        "scratch/creds: not screened — symlink; links are never followed",)


def test_an_unselected_nested_repository_does_not_abort_the_run(tmp_path):
    """§2.3's scoping paragraph, which this repository is the worked example for: two agy
    worktrees leaked by crashed eval runs sit under `evals/*/workspace/` right now, each
    carrying a `.git` FILE, and an unscoped structural sweep would abort forge's first run on
    artifacts the user never created.

    Selecting the same directory DOES refuse it, which is what makes this a scoping property
    rather than a hole: the condition is detected, and it applies to what the run carries.
    """
    repo = make_repo(tmp_path)
    write(repo, "workspace/leaked/.git", "gitdir: /elsewhere/.git/worktrees/leaked\n")
    assert _refusals(repo) == ()
    assert any("nested repository" in line
               for line in _refusals(repo, ("workspace/leaked",)))


def test_the_screen_reads_the_selection_and_not_the_tree_around_it(tmp_path):
    """The scoping half of §2.3's paragraph, on §3's side of the fence.

    Measured, and it is why an unscoped sweep is not the safer default:
    `screen_tree(<this repository>, ["."])` returns `files: 5740 > 5000` — one breach, no
    findings — so a whole-tree screen refuses forge's first run here on the file cap alone.

    This test asserts a BOUNDARY, and the module docstring names what falls outside it: a
    credential in a tracked file, including an uncommitted edit to one, is not screened here.
    Selecting the same directory does screen it, which is what makes this scoping rather than
    blindness.
    """
    repo = make_repo(tmp_path)
    write(repo, "elsewhere/.env", "TOKEN=whatever\n")
    write(repo, "scratch/notes.md", "nothing sensitive\n")
    r = preflight.inspect_repo(repo, ("scratch",))
    assert r.secrets == () and r.breaches == () and preflight.refusals(r) == ()

    picked = preflight.inspect_repo(repo, ("scratch", "elsewhere"))
    assert [f.path for f in picked.secrets] == ["elsewhere/.env"]


def test_preflight_executes_nothing_the_repository_supplies(tmp_path):
    """§5 step 1 is STATIC and READ-ONLY: no arbitrary project setup code runs before
    authorization.

    `core.fsmonitor` is the one that is easy to miss, because git runs it on the caller's
    behalf rather than the caller running it, and it is set in the repository's own
    `.git/config`. Measured before `inspect` carried `NO_DAEMON_CACHE` on every worktree
    read: `repo_facts` executed it, through `ls-files --eol` and `check-attr`. The Makefile,
    `setup.py` and `conftest.py` beside it are the shapes a reader expects and none of them
    was ever the live one.
    """
    repo = make_repo(tmp_path)
    write(repo, "Makefile", "all:\n\t@exit 42\n")
    write(repo, "setup.py", "raise SystemExit('preflight executed setup.py')\n")
    write(repo, "conftest.py", "raise SystemExit('preflight imported conftest')\n")
    commit_all(repo, "hostile")

    ran = tmp_path / "FSMONITOR-RAN"
    program = tmp_path / "fsmonitor.sh"
    program.write_text(f"#!/bin/sh\ntouch {ran}\nexit 1\n")
    program.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(program))
    # The fixture must be one git would really run, or its silence below is meaningless.
    _git(repo, "-c", "core.untrackedCache=false", "status", "--porcelain")
    assert ran.exists(), "the fixture program does not run under a plain git; re-check it"
    ran.unlink()

    r = preflight.inspect_repo(repo)
    assert preflight.refusals(r) == ()
    assert not ran.exists(), "preflight ran a program the repository named"


def test_the_report_does_not_claim_a_gate_surface_it_could_not_measure(tmp_path):
    """`gate_surface` needs a confirmed verify command, which preflight does not have yet.
    None is "nobody looked"; () would say "this repository defines no gate", which is a
    different and much stronger claim."""
    repo = make_repo(tmp_path)
    assert preflight.inspect_repo(repo).gate_surface is None


def test_the_report_proposes_no_generator_contract(tmp_path):
    """`detect_generators` answers the empty contract for every repository, and "" is the
    manifest's fail-closed sentinel for "this run declared no contract". A report that
    proposed one would be preflight writing a rule the §5 gate exists to have a human state.
    """
    r = preflight.inspect_repo(make_repo(tmp_path))
    assert r.contract.id == "" and r.contract.relations == ()


def test_a_string_selection_is_refused_rather_than_iterated(tmp_path):
    """A string iterates into its characters, so `"scratch"` would arrive downstream as seven
    single-character selections — each one a breach about a path nobody named, and the real
    selection screened by nothing."""
    repo = make_repo(tmp_path)
    with pytest.raises(preflight.PreflightError, match="not 'scratch'"):
        preflight.inspect_repo(repo, "scratch")


def test_refusals_will_not_answer_for_something_that_is_not_a_report():
    with pytest.raises(preflight.PreflightError, match="a Report is required"):
        preflight.refusals({"rejections": []})


def test_preflight_writes_nothing_in_the_repository(tmp_path):
    """Read-only is also a claim about the INDEX, which §9 protects and which an ordinary
    `git status` over stale stat data will rewrite unless every call is pinned."""
    repo = make_repo(tmp_path)
    write(repo, "dirty.txt", "untracked\n")
    _git(repo, "add", "dirty.txt")
    os.utime(Path(repo) / "dirty.txt", (0, 0))
    before = {p: p.stat().st_mtime_ns for p in (Path(repo) / ".git").rglob("*") if p.is_file()}
    index = (Path(repo) / ".git" / "index").read_bytes()

    preflight.inspect_repo(repo, ("dirty.txt",))

    after = {p: p.stat().st_mtime_ns for p in (Path(repo) / ".git").rglob("*") if p.is_file()}
    assert set(after) == set(before), "preflight created or removed a file under .git"
    assert (Path(repo) / ".git" / "index").read_bytes() == index
