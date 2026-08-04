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

from forge import preflight, taskbundle  # noqa: E402
from forge_fixtures import commit_all, git as _git, make_repo, write  # noqa: E402

# A shape `scripts/lib/checks.py` recognises, built rather than pasted so this file holds no
# string a secret scanner has to be told to ignore.
_AKIA = "AKIA" + "IOSFODNN7EXAMPL"[:12] + "QRST"


def _refusals(repo, selected=()):
    return preflight.refusals(preflight.inspect_repo(repo, selected))


def _a_task_bundle(tmp_path=None):
    """A minimal real TaskBundle, built through `scan` so it carries real hashes."""
    import tempfile  # noqa: PLC0415
    root = Path(tmp_path or tempfile.mkdtemp())
    (root / "TASK.md").write_text("Do the thing.\n")
    return taskbundle.scan(root, entrypoint="TASK.md")


def test_a_clean_repository_has_no_refusals(tmp_path):
    repo = make_repo(tmp_path)
    r = preflight.inspect_repo(repo)
    assert preflight.refusals(r) == ()
    assert r.repo == Path(repo)
    assert r.facts.head, "and the report really did look at the repository"


def test_skip_worktree_is_refused_at_preflight(tmp_path):
    """§2.3 lists it, and what it costs is the BIT hiding an edit from the porcelain rather
    than any one git command. Measured on this exact fixture, git 2.53.0: the porcelain is
    empty, so `baseline.materialize` reports `dirty=False`, takes its clean early-return and
    makes B HEAD itself — B lacks the hidden edit because of that, and `add` is never invoked
    at all. A selection that makes the run dirty does reach `git add -u -- :/`, and that
    command exits ONE on such a path ("paths ... exist outside of your sparse-checkout
    definition"), so `materialize` raises `GitError`.

    What does NOT hold either way is that nothing downstream sees it. Measured: on the clean
    branch `fleet.clone_seat` raises `SeatError: seat content differs from the baseline
    manifest`, because B's manifest hashes the raw worktree bytes (which carry the hidden
    edit) while B's tree does not. That is the §4 shape a refusal exists to avoid rather than
    an argument against one — an infrastructure failure three stages later, attributed to the
    seat, in place of a sentence naming a bit the user can clear.
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
    `screen_tree(<this repository>, ["."])` returns one breach and no findings,
    `files: <n> > 5000`, several hundred past `Quota.default()`'s cap (5750 on 2026-08-02;
    a bound rather than a count, since it moves with every commit) — so a whole-tree screen
    refuses forge's first run here on the file cap alone.

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


def test_a_subdirectory_caller_screens_the_repository_the_baseline_is_built_from(tmp_path):
    """`Report` blesses a subdirectory `repo`, and a selection is worktree-ROOT-relative —
    so the two roots must not be mixed.

    Measured with the screen joined onto the caller's `repo` instead: this fixture returned
    no secrets, no breaches and NO REFUSALS, while `baseline.materialize(<repo>/sub, …,
    ["scratch"], …)` put `scratch/.env` into B's manifest. A clean screen in front of a
    baseline carrying the credential is the §3 outcome the screen exists to prevent, and it
    is worse than no screen because a clean result is what the gate shows a human.
    """
    repo = make_repo(tmp_path)
    write(repo, "scratch/.env", "AWS_SECRET_ACCESS_KEY=" + "A" * 40 + "\n")
    write(repo, "sub/scratch/notes.md", "harmless\n")

    r = preflight.inspect_repo(repo / "sub", ("scratch",))
    assert r.repo == repo / "sub" and r.facts.root == repo, \
        "the two roots really do differ here, or this fixture proves nothing"
    assert [f.path for f in r.secrets] == ["scratch/.env"]
    assert "scratch/.env: high-risk-filename" in preflight.refusals(r)


def test_a_subdirectory_caller_does_not_report_a_present_path_as_missing(tmp_path):
    """The same mixing degrades into a FALSE SENTENCE when the subdirectory has no copy of
    the selected path: `scratch: not screened — selected path does not exist`, about a path
    that exists. A breach naming the wrong reason sends the user to look for the wrong thing.
    """
    repo = make_repo(tmp_path)
    write(repo, "scratch/notes.md", "harmless\n")
    write(repo, "sub/other.md", "x\n")
    r = preflight.inspect_repo(repo / "sub", ("scratch",))
    assert r.breaches == () and preflight.refusals(r) == ()

    gone = preflight.inspect_repo(repo / "sub", ("nowhere",))
    assert gone.breaches == ("nowhere: not screened — selected path does not exist",), \
        "and a selection that really is absent still breaches, so the fix is not blanket silence"


def test_a_subdirectory_caller_sees_a_condition_set_at_the_root(tmp_path):
    """`repo_facts` is the other half of the same root question, and it was the fail-OPEN
    half: `ls-files` reports relative to the CWD and lists only what is under it, so from
    `<repo>/sub` a root-level `--skip-worktree` bit came back `sparse=False` with
    `rejections()` empty — §2.3's headline condition, silently unmet.
    """
    repo = make_repo(tmp_path)
    write(repo, "sub/inner.txt", "i\n")
    commit_all(repo, "sub")
    _git(repo, "update-index", "--skip-worktree", "seed.txt")

    from_root = preflight.inspect_repo(repo)
    from_sub = preflight.inspect_repo(repo / "sub")
    assert from_sub.facts.sparse is True
    assert preflight.refusals(from_sub) == preflight.refusals(from_root) != ()


def test_the_same_subdirectory_without_the_bit_is_admitted(tmp_path):
    """The discrimination check for the case above: reading the whole repository from a
    subdirectory must not refuse an ordinary one."""
    repo = make_repo(tmp_path)
    write(repo, "sub/inner.txt", "i\n")
    commit_all(repo, "sub")
    assert preflight.refusals(preflight.inspect_repo(repo / "sub")) == ()


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
    `git status` over stale stat data will rewrite unless every call is pinned.

    The mtimes are COMPARED BY VALUE, not merely by key. `os.utime` backdates the file so
    that a refresh is exactly what an unpinned call would do, and a rewrite that reproduced
    the same bytes — or one to any other file under `.git` — moves an mtime and nothing else.
    Key-set equality alone would answer only "created or removed" — and that half is NOT
    covered below: the index comparison reads `.git/index`'s bytes and says nothing about any
    other file under `.git`. The two assertions cover different things, which is why both are
    here.
    """
    repo = make_repo(tmp_path)
    write(repo, "dirty.txt", "untracked\n")
    _git(repo, "add", "dirty.txt")
    os.utime(Path(repo) / "dirty.txt", (0, 0))
    before = {p: p.stat().st_mtime_ns for p in (Path(repo) / ".git").rglob("*") if p.is_file()}
    index = (Path(repo) / ".git" / "index").read_bytes()

    preflight.inspect_repo(repo, ("dirty.txt",))

    after = {p: p.stat().st_mtime_ns for p in (Path(repo) / ".git").rglob("*") if p.is_file()}
    assert set(after) == set(before), "preflight created or removed a file under .git"
    assert after == before, "preflight wrote to a file under .git"
    assert (Path(repo) / ".git" / "index").read_bytes() == index


def test_a_task_naming_provider_specific_machinery_is_refused():
    """§20: fail preflight for irreducibly provider-specific workflows, and ask for a
    portable task bundle instead. Each string below names a thing that exists on ONE of the
    three CLIs and cannot be carried by a file."""
    for text, referent in (
            ("Dispatch a subagent with subagent_type: Explore and merge its findings.",
             "subagent_type"),
            ("Read ${CLAUDE_PLUGIN_ROOT}/skills/x/SKILL.md and follow it.",
             "CLAUDE_PLUGIN_ROOT"),
            ("Run codex exec --json over the diff.", "codex exec"),
            ("Call mcp__chrome-devtools__take_snapshot and describe the page.",
             "mcp__chrome-devtools__take_snapshot"),
            ("Look at ~/.codex/config.toml first.", "~/.codex")):
        out = preflight.task_refusals(text)
        assert out, f"not refused: {text!r}"
        assert any(referent in line for line in out), (referent, out)
        assert any("portable task bundle" in line for line in out), out


def test_an_mcp_tool_name_is_reported_whole_and_not_truncated_at_its_first_hyphen():
    """The referent is the WHOLE point of naming it. `[A-Za-z0-9_]+` stopped at the hyphen in
    the server name, so the refusal said `mcp__chrome` about a task containing no such string
    — an operator who greps their own instruction for it finds nothing and reads the refusal
    as a detector bug, which is the same as not having a detector."""
    (line,) = preflight.task_refusals("Call mcp__chrome-devtools__take_snapshot on the page.")
    assert "'mcp__chrome-devtools__take_snapshot'" in line, line


def test_a_bundle_does_not_make_a_provider_specific_referent_portable():
    """§20 forbids automatic translation. A bundle carries FILES; it cannot turn a named
    subagent type into something codex or agy has."""
    b = _a_task_bundle()
    out = preflight.task_refusals("Use subagent_type: Explore.", bundle=b)
    assert out, "a supplied bundle cleared a refusal it cannot answer"


def test_an_ambient_skill_is_refused_unless_all_three_closures_hash_identically():
    """§20: use a named skill only when all three hash identically. `installed_closure`
    answers None for a CLI that is not installed, and `ambient_verdict` reads None as False —
    so three ABSENCES must not read as agreement."""
    text = "Follow the /markitdown skill to convert the file."
    assert preflight.task_refusals(text, closures={"claude": "a", "codex": "a", "agy": "a"}) == ()
    out = preflight.task_refusals(text, closures={"claude": "a", "codex": "b", "agy": "a"})
    assert out and any("markitdown" in line for line in out), out
    three_absences = {"claude": None, "codex": None, "agy": None}
    out2 = preflight.task_refusals(text, closures=three_absences)
    assert out2, "three uninstalled CLIs hashed identically and licensed an ambient skill"
    # The refusal names the mismatching hashes readably. `"not installed"[:12]` is
    # `"not installe"`, which is the truncation reading as a hash prefix.
    assert "not installed" in " ".join(out2), out2


def test_the_second_form_an_operator_writes_a_skill_in_is_read_the_same_way():
    """`/markitdown` and "use the markitdown skill" are one reliance, and only the first was
    covered. A pattern nothing exercises is a pattern that can be deleted without a test
    noticing, which is the same as not having it."""
    text = "Use the markitdown skill on this PDF."
    out = preflight.task_refusals(text, closures={"claude": "a", "codex": "b", "agy": "a"})
    assert out and any("`markitdown`" in line for line in out), out
    # The discrimination: the same sentence with the bar CLEARED is admitted, so the refusal
    # above is about the closures and not about the sentence.
    assert preflight.task_refusals(
        text, closures={"claude": "a", "codex": "a", "agy": "a"}) == ()


def test_a_closure_nobody_measured_is_not_reported_as_one_that_is_not_installed():
    """Two different failures must not compare equal in the sentence an operator acts on. A
    CLI absent from the mapping is one this caller never asked about; a CLI mapped to None is
    one `installed_closure` looked for and could not describe. Both refuse — and the operator
    fixes a different thing in each case."""
    text = "Follow the /markitdown skill to convert the file."
    (only_two,) = preflight.task_refusals(text, closures={"claude": "a", "codex": "b"})
    assert "agy=not measured" in only_two, only_two
    (absent,) = preflight.task_refusals(
        text, closures={"claude": "a", "codex": "b", "agy": None})
    assert "agy=not installed" in absent, absent
    assert only_two != absent, "two different facts produced one sentence"


def test_closures_this_engine_cannot_read_are_refused_rather_than_asked_for_a_get():
    """`ambient_verdict` calls `.get`, so a list or a string left this gate as an
    `AttributeError` — out of a function whose entire contract is that it answers in refusal
    lines a caller can print."""
    for bad in (["a", "a", "a"], "aaa", 5):
        with pytest.raises(preflight.PreflightError):
            preflight.task_refusals("Follow the /markitdown skill.", closures=bad)
        with pytest.raises(preflight.PreflightError):
            preflight.ambient_notes("Follow the /markitdown skill.", closures=bad)


def test_a_bundle_does_not_clear_the_ambient_skill_bar_either(tmp_path):
    """The bar must not consult `bundle`. The front end ALWAYS builds one and passes it, so a
    `bundle is None` condition would make §20's three-closure check dead in the only
    production caller — and a directory of files does not make `/markitdown` the same skill on
    claude, codex and agy, which was the entire argument for the check."""
    b = _a_task_bundle(tmp_path)
    text = "Follow the /markitdown skill to convert the file."
    out = preflight.task_refusals(text, bundle=b,
                                  closures={"claude": "a", "codex": "b", "agy": "a"})
    assert out and any("markitdown" in line for line in out), (
        "a supplied bundle cleared the ambient-skill bar, which is the shape that made the "
        "check unreachable from the CLI")


def test_a_cleared_ambient_skill_produces_the_note_the_prompt_carries():
    """`taskbundle.ambient_note` is what §20 asks the caller to add to the prompt, and until
    this function existed it had no caller anywhere. A cleared skill yields a note; a skill
    that did NOT clear yields none, because the refusal is the answer in that case."""
    text = "Follow the /markitdown skill to convert the file."
    agreed = {"claude": "a", "codex": "a", "agy": "a"}
    notes = preflight.ambient_notes(text, closures=agreed)
    assert len(notes) == 1 and "markitdown" in notes[0], notes
    assert preflight.ambient_notes(text,
                                   closures={"claude": "a", "codex": "b", "agy": "a"}) == ()
    assert preflight.ambient_notes("no skills named here.", closures=agreed) == ()


def test_a_skill_name_inside_a_url_is_not_read_as_an_ambient_skill_reference():
    """The detector reports what it matched so an operator can see it — and a detector that
    fires on a URL path segment teaches the operator to write around it, which is the same as
    not having one."""
    text = "See https://example.com/docs/markitdown for background; write your own converter."
    assert preflight.task_refusals(text, closures={}) == ()


def test_an_ordinary_absolute_path_is_not_read_as_an_ambient_skill_reference():
    """The same hazard as the URL case by the same mechanism, one boundary over: `/home` in
    `read /home/user/notes.md` IS preceded by whitespace, so the leading rule alone admitted
    it and the refusal named an ambient `home` skill nobody has. A `/name` followed by another
    path separator is a path segment, not an invocation.

    Paired with its discrimination, because a rule that refused BOTH would pass the first
    assertion for the wrong reason."""
    mismatched = {"claude": "a", "codex": "b", "agy": "a"}
    assert preflight.task_refusals(
        "Read /home/user/notes.md and summarise it.", closures=mismatched) == ()
    assert preflight.task_refusals(
        "Read the file, then run /markitdown on it.", closures=mismatched), \
        "the narrowing swallowed a real invocation as well"


def test_a_skill_named_only_inside_a_fenced_block_is_still_a_reliance_on_it():
    """The fail-open direction of the URL fix, named so it is a choice rather than an
    oversight: a task whose fenced block runs `/markitdown` relies on that skill exactly as
    much as one whose prose does, so fences are NOT stripped."""
    text = "Run this:\n\n```\n/markitdown report.pdf\n```\n"
    out = preflight.task_refusals(text, closures={"claude": "a", "codex": "b", "agy": "a"})
    assert out and any("markitdown" in line for line in out), out


def test_an_instruction_this_engine_could_not_read_is_refused_rather_than_cleared():
    """() means 'examined and nothing stands in the way'. An unexamined task must not
    borrow that sentence."""
    for bad in ("", "   ", None, 5, b"bytes"):
        try:
            preflight.task_refusals(bad)
        except preflight.PreflightError:
            pass
        else:
            raise AssertionError(f"{bad!r} was not refused")
        try:
            preflight.ambient_notes(bad, closures={"claude": "a", "codex": "a", "agy": "a"})
        except preflight.PreflightError:
            pass
        else:
            raise AssertionError(f"{bad!r} produced a note for a task nobody could read")


def _repo_with_tracked_secret(tmp_path):
    """A tracked file holding a token this repo's own allow-list does NOT know.

    Deliberately not AKIAIOSFODNN7EXAMPLE: that is one of three entries in
    `SECRET_ALLOW_SHA`, so a fixture using it comes back clean even when the scanner works —
    a fixture too well known to fail. This one matches `AKIA[0-9A-Z]{16}` and is not
    allowlisted.
    """
    repo = make_repo(tmp_path)
    write(repo, "config/settings.py", 'AWS_KEY = "AKIA' + 'Q7ZB3KXJ2M9WLPRT"\n')
    commit_all(repo, "add config")
    return repo


def test_a_run_that_screened_nothing_does_not_report_what_a_clean_repo_reports(tmp_path):
    """The external question: over how many files is this emptiness a claim?

    `cli.py` passes `args.select or ()`, so the EMPTY selection is the default path — and
    `screen_tree(root, [])` builds an empty target list, never enters the scan loop, and
    returns `([], [])`: byte-for-byte what a fully screened clean repository returns. No
    field on `Report` could tell an operator which of the two they were looking at.
    """
    repo = _repo_with_tracked_secret(tmp_path)
    rep = preflight.inspect_repo(repo, ())
    assert rep.screened > 0, \
        "a report that opened no files must not be indistinguishable from a clean one"


def test_the_screen_covers_what_B1_actually_carries(tmp_path):
    """§3's stakes, in its own words: whatever the baseline contains is what N cloud-backed
    full-permission agents read, and scanning the OUTPUT is too late.

    `baseline.materialize` puts every TRACKED file into the filesystem manifest and B1, so a
    screen over the selection alone certifies a set it does not cover.
    """
    repo = _repo_with_tracked_secret(tmp_path)
    rep = preflight.inspect_repo(repo, ())          # nothing selected: the default path
    assert rep.secrets, "a tracked credential reached B1 without entering the scanner"
    assert preflight.refusals(rep), "and nothing refused the run over it"


def test_a_genuinely_clean_repository_still_passes(tmp_path):
    """The guard against over-tightening: widening the screen must not refuse ordinary repos."""
    repo = make_repo(tmp_path)
    write(repo, "app.py", "KEY = os.environ['KEY']\n")
    commit_all(repo, "add app")
    rep = preflight.inspect_repo(repo, ())
    assert rep.secrets == ()
    assert rep.screened > 0
