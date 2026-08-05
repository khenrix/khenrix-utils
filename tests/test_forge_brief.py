"""§16's fusion brief. Reads the run directory and spends nothing."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from forge import brief, cli, gitcmd, runstate  # noqa: E402
from forge_fixtures import commit_all, git, make_repo, write  # noqa: E402


def _seat(run_dir, name, *, paths, outcome="PASS"):
    """One seat record in `runner._record`'s own shape, one attempt deep."""
    attempt = {
        "attempt": 1,
        "path": str(run_dir / "seats" / name / "attempt-1"),
        "branch": f"forge/aaaaaa/{name}",
        "sentinel": "SENTINEL-000000000000",
        "status": {"process": "ok", "artifacts": "usable", "proven_read": "proven",
                   "forge": "completed", "setup": "ok", "verify": "pass"},
        "verification": None if outcome is None else {"outcome": outcome, "reason": "r"},
        "verification_refused": None,
        "setup_run": None,
        "verifier_setup": None,
        "artifacts": ({"paths": list(paths), "origin": {}, "setup_overlap": [],
                       "verify_overlap": []}
                      if paths is not None else None),
        "candidate": {"baseline_ref": "refs/khenrix-forge/aaaaaa/base",
                      "baseline_commit": "a" * 40, "tracked_patch_bytes": 10,
                      "sidecars": [], "omitted": [], "generator_contract_id": None,
                      "gate_delta": None, "gate_surface": None},
        "launch": None,
        "prompt_identity": None,
    }
    runstate.write_seat(run_dir, name, {"name": name, "attempts": [attempt]})


def test_seat_paths_reads_the_last_attempts_path_set(tmp_path):
    _seat(tmp_path, "claude", paths=["a.py", "b.py"])
    assert brief.seat_paths(tmp_path) == {"claude": frozenset({"a.py", "b.py"})}


def test_an_unreadable_path_set_is_UNKNOWN_and_never_the_empty_set(tmp_path):
    """`nothing` and `nobody` must not leave the same record: an empty frozenset is the true
    claim "this seat changed nothing", which makes every other seat's path sole-touched.

    `is brief.UNKNOWN`, NEVER `is not frozenset()`. Measured: `frozenset() is frozenset()` is
    False on this build, so the identity form can never fail and would pass over a seat
    reported as having touched nothing. The equality form below is what distinguishes them."""
    _seat(tmp_path, "claude", paths=None)
    got = brief.seat_paths(tmp_path)["claude"]
    assert got is brief.UNKNOWN
    assert got != frozenset(), "an unreadable path set decoded as 'this seat changed nothing'"


def test_a_seat_that_really_touched_nothing_is_the_empty_set_not_UNKNOWN(tmp_path):
    _seat(tmp_path, "claude", paths=[])
    assert brief.seat_paths(tmp_path) == {"claude": frozenset()}


def test_overlap_of_two_unknown_seats_is_None_and_never_zero(tmp_path):
    """Two different failures must not compare equal to a measured disjointness."""
    _seat(tmp_path, "claude", paths=None)
    _seat(tmp_path, "codex", paths=None)
    assert brief.overlap(brief.seat_paths(tmp_path)) == {("claude", "codex"): None}


def test_overlap_counts_shared_paths_for_two_known_seats(tmp_path):
    _seat(tmp_path, "claude", paths=["a.py", "b.py"])
    _seat(tmp_path, "codex", paths=["b.py", "c.py"])
    assert brief.overlap(brief.seat_paths(tmp_path)) == {("claude", "codex"): 1}


def test_sole_is_empty_when_any_seat_is_unknown(tmp_path):
    """"Only seat X touched db.py" is a claim about ALL seats, so it cannot be made from
    two of three — the unreadable seat is exactly the one that might also have touched it."""
    _seat(tmp_path, "claude", paths=["a.py"])
    _seat(tmp_path, "codex", paths=None)
    assert brief.sole(brief.seat_paths(tmp_path)) == {}


def test_sole_names_the_paths_exactly_one_seat_touched(tmp_path):
    _seat(tmp_path, "claude", paths=["a.py", "shared.py"])
    _seat(tmp_path, "codex", paths=["b.py", "shared.py"])
    assert brief.sole(brief.seat_paths(tmp_path)) == {"claude": ["a.py"], "codex": ["b.py"]}


def test_an_unrecorded_verify_outcome_is_None_and_not_a_failure(tmp_path):
    """§6.2 has no outcome for "nobody measured", so the brief may not invent one."""
    _seat(tmp_path, "claude", paths=["a.py"], outcome=None)
    assert brief.seat_verify(tmp_path) == {"claude": None}


def test_text_over_no_seat_is_refused(tmp_path):
    with pytest.raises(brief.BriefError, match="records no seat"):
        brief.text(tmp_path)


def test_text_says_in_words_that_an_unknown_seat_was_not_compared(tmp_path):
    """The assertions quote the rendered text EXACTLY, including its case. The first draft
    asserted `"no seat can be named the only one"` against prose that renders
    `**No seat can be named the only one to touch a path**` and failed on the capital N —
    a test that cannot pass over correct output is not a test of the output."""
    _seat(tmp_path, "claude", paths=["a.py"])
    _seat(tmp_path, "codex", paths=None)
    body = brief.text(tmp_path)
    assert "not recorded" in body
    assert "No seat can be named the only one to touch a path" in body
    assert "codex" in body, "the refusal must name the seat whose absence caused it"


def _a_synthesis_worktree(tmp_path):
    """A real linked worktree, because the brief's location is a claim about git's answer.

    `brief_path` asks `rev-parse --absolute-git-dir` rather than joining `.git`, and in a
    linked worktree those two are a directory and a FILE. A tmp_path stand-in would make the
    test pass over a join that is right by luck and wrong on the one tree this ever runs in.
    """
    repo = make_repo(tmp_path)
    write(repo, "a.py", "x = 1\n")
    commit_all(repo, "base")
    co = tmp_path / "synthesis"
    git(repo, "worktree", "add", "-q", "-b", "forge/aaaaaa/synthesis", str(co), "HEAD")
    return repo, co


def test_write_puts_the_brief_where_git_status_cannot_see_it(tmp_path):
    """THE DEFECT THIS TEST EXISTS FOR. A brief in the worktree is a `??` record, which
    `cli._sidecars_of` keeps and `handover.mergeability` reads as an out-of-band artifact —
    so every fusion would report `PATCH_ONLY` and the handover would tell the user to copy
    the engine's own scaffolding into their repository."""
    _seat(tmp_path, "claude", paths=["a.py"])
    repo, co = _a_synthesis_worktree(tmp_path)
    p = brief.write(tmp_path, co)
    assert p == brief.brief_path(co)
    assert p.read_text(encoding="utf-8").startswith("# Fusion brief")
    assert not p.is_relative_to(co), "the brief is inside the tree being handed over"
    assert [s.path for s in cli._sidecars_of(co)] == []


def test_the_brief_lands_where_gc_reclaims_it(tmp_path):
    """Under the WORKTREE's own git directory and not the repository's shared one. §15 removes
    the synthesis worktree, which takes `.git/worktrees/<name>/` with it; a brief written into
    the shared git dir instead would outlive every run that wrote one."""
    _seat(tmp_path, "claude", paths=["a.py"])
    repo, co = _a_synthesis_worktree(tmp_path)
    p = brief.write(tmp_path, co)
    assert p.is_relative_to(repo.resolve() / ".git" / "worktrees"), p
    git(repo, "worktree", "remove", "--force", str(co))
    assert not p.exists(), "the brief survived the worktree it described"
