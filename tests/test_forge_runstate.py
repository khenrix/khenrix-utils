"""The run's identity: the manifest written once, and the refs it is judged against (§9, §14.2)."""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import baseline, runstate, storage  # noqa: E402
from forge import inspect as repo_inspect  # noqa: E402
from forge.verify import Step  # noqa: E402
from forge_fixtures import commit_all, git as _git, make_repo, write  # noqa: E402

AUTHOR = ("Fixture", "fixture@example.invalid")


def _manifest(repo, **kw):
    selected = kw.get("selected_paths", ())
    refs, digest = runstate.snapshot_refs(repo, selected)
    base = dict(run_id="r1", repo_path=str(repo), base_commit="a" * 40,
                baseline_ref="refs/khenrix-forge/r1/base", baseline_commit="b" * 40,
                tracked_tree_oid="c" * 40, selected_paths=(), generator_contract={},
                setup=(Step(argv=("true",)),), verify=(Step(argv=("./check.sh",)),),
                protected_refs=refs, status_digest=digest, created_at="2026-08-01T00:00:00Z")
    return runstate.Manifest(**{**base, **kw})


def _run_dir(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    return d


# --------------------------------------------------------------------------- written once

def test_a_manifest_is_written_once_and_never_rewritten(tmp_path):
    """§14.2: written once at `confirmed`, so commands are never re-detected. A resume that
    could rewrite it could silently change what the run agreed to do."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    with pytest.raises(runstate.ManifestError):
        runstate.write_manifest(run, _manifest(repo, run_id="r2"))
    assert runstate.read_manifest(run).run_id == "r1"


def test_a_refused_second_write_leaves_the_first_manifest_byte_identical(tmp_path):
    """The refusal has to be structural, not a check followed by a write.

    A `read_manifest` that still answers "r1" would also pass if the second write had
    replaced the file and then been reported as an error, and the fact this file exists to
    protect is on the platter, not in the return value.
    """
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    before = storage.manifest_path(run).read_bytes()
    with pytest.raises(runstate.ManifestError):
        runstate.write_manifest(run, _manifest(repo, run_id="r2", base_commit="d" * 40))
    assert storage.manifest_path(run).read_bytes() == before


def test_a_refused_second_write_leaves_no_debris_in_the_run_directory(tmp_path):
    """A staging file left behind is a name a `--gc` walk has to guess about, and a run
    directory that grows one per refusal is one nobody can inventory."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    with pytest.raises(runstate.ManifestError):
        runstate.write_manifest(run, _manifest(repo, run_id="r2"))
    assert sorted(p.name for p in run.iterdir()) == ["manifest.json"]


# ------------------------------------------------------------------------------ round trip

def test_a_manifest_round_trips_every_field(tmp_path):
    """A field that does not survive the round trip is a fact `--collect` cannot recover,
    and the resume would proceed on a default instead."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    m = _manifest(repo, selected_paths=("scratch",),
                  generator_contract={"id": "g1", "relations": [["src/*", "gen/*"]]})
    runstate.write_manifest(run, m)
    assert runstate.read_manifest(run) == m


def test_the_confirmed_commands_survive_as_argv_not_as_a_string(tmp_path):
    """§5.1: shell metacharacter syntax is rejected, not reinterpreted. A manifest that
    stored `"cd frontend && npm ci"` would hand a resume a command it must re-parse."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    m = _manifest(repo, setup=(Step(argv=("npm", "ci")), Step(argv=("./gen.sh",))))
    runstate.write_manifest(run, m)
    assert [s.argv for s in runstate.read_manifest(run).setup] == [("npm", "ci"),
                                                                  ("./gen.sh",)]


def test_a_confirmed_step_records_its_cwd_env_and_timeout_as_well_as_its_argv(tmp_path):
    """§5.1's step is `{argv, cwd, env, timeout}`, and its motivating sentence is that real
    monorepos need several steps with DIFFERENT CWDS — `cd frontend && npm ci` must not be
    approximated. A manifest that recorded argv alone would hand a `--collect` resume
    `cwd=""`, `env={}` and `timeout=600` back, supplied by the reader rather than agreed by
    the user: this file's own failure mode, three fields per step instead of one per manifest.
    """
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    steps = (Step(argv=("npm", "ci"), cwd="frontend", env={"CI": "1"}, timeout=45),
             Step(argv=("make", "gen"), cwd="backend/svc", env={}, timeout=900))
    runstate.write_manifest(run, _manifest(repo, setup=steps))
    assert runstate.read_manifest(run).setup == steps


def test_a_recovered_argv_is_a_tuple_and_not_a_list(tmp_path):
    """Equality is how a resume checks it recovered what was confirmed, and `["npm","ci"]`
    compares unequal to `("npm","ci")` while printing almost the same. The declared type is
    restored explicitly rather than the field being declared a list, so the manifest a
    resume holds is the same object the run agreed to."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo, selected_paths=("a", "b"),
                                           setup=(Step(argv=("npm", "ci")),)))
    got = runstate.read_manifest(run)
    assert isinstance(got.selected_paths, tuple) and isinstance(got.setup, tuple)
    assert all(isinstance(s, Step) and isinstance(s.argv, tuple) for s in got.setup)


def test_a_step_recorded_as_a_bare_argv_list_is_refused_at_write(tmp_path):
    """The shape §5.1 rejects, caught where the caller is still there to fix it. A manifest
    holding `[["npm","ci"]]` reads back as a step whose other three fields the reader chose."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    with pytest.raises(runstate.ManifestError, match="step record"):
        runstate.write_manifest(run, _manifest(repo, setup=(("npm", "ci"),)))
    assert not storage.manifest_path(run).exists()


def test_a_payload_that_would_not_survive_the_round_trip_is_refused_at_write(tmp_path):
    """The one field JSON cannot type-check for the caller is the free-form contract dict.

    A tuple nested inside it serializes happily and reads back a list, so the manifest on
    disk stops equalling the one in memory — and nothing would say so until a resume hours
    later compared them. Refused at the write, where the caller is still there to fix it.
    """
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    m = _manifest(repo, generator_contract={"relations": (("src/*", "gen/*"),)})
    with pytest.raises(runstate.ManifestError):
        runstate.write_manifest(run, m)
    assert not storage.manifest_path(run).exists()


def test_a_declared_tuple_handed_in_as_a_list_is_refused_and_the_field_is_named(tmp_path):
    """Equality is the whole mechanism, so a field that would come back a different type than
    it went in is refused rather than quietly normalised — and the refusal names it, because
    "does not round trip" over a record this wide is not a lead."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    with pytest.raises(runstate.ManifestError, match="selected_paths"):
        runstate.write_manifest(run, _manifest(repo, selected_paths=["scratch"]))


def test_the_manifest_bytes_are_canonical_so_one_fact_has_one_spelling(tmp_path):
    """Sorted keys, so the file can be compared or content-addressed without re-parsing it
    — a manifest whose byte order follows a dataclass's declaration order changes shape the
    next time a field is inserted, for a run that agreed to exactly the same thing."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    keys = list(json.loads(storage.manifest_path(run).read_text()))
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------- fail closed

def test_a_missing_manifest_is_an_error_not_an_empty_one(tmp_path):
    """`--collect` resuming a run whose manifest never landed must stop, not proceed on
    defaults it invented."""
    with pytest.raises(runstate.ManifestError):
        runstate.read_manifest(_run_dir(tmp_path))


def test_a_manifest_missing_a_field_is_refused_rather_than_defaulted(tmp_path):
    """A field the reader supplies is a fact the run never agreed to."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    row = json.loads(storage.manifest_path(run).read_text())
    del row["verify"]
    storage.manifest_path(run).write_text(json.dumps(row))
    with pytest.raises(runstate.ManifestError):
        runstate.read_manifest(run)


def test_a_manifest_carrying_an_unknown_field_is_refused(tmp_path):
    """An engine that once recorded a fact this reader drops silently answers questions
    about the run from a manifest it only partly understands."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    row = json.loads(storage.manifest_path(run).read_text())
    row["strategy"] = "from-scratch"
    storage.manifest_path(run).write_text(json.dumps(row))
    with pytest.raises(runstate.ManifestError):
        runstate.read_manifest(run)


def test_a_damaged_manifest_is_refused_rather_than_half_read(tmp_path):
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    storage.manifest_path(run).write_text('{"run_id": "r1"')
    with pytest.raises(runstate.ManifestError):
        runstate.read_manifest(run)


def test_a_manifest_whose_argv_is_a_bare_string_is_refused(tmp_path):
    """`"npm ci"` iterates into characters, so a reader that just calls `tuple()` on it
    recovers `('n','p','m',...)` and a resume runs a command named `n`."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    row = json.loads(storage.manifest_path(run).read_text())
    row["setup"][0]["argv"] = "npm ci"
    storage.manifest_path(run).write_text(json.dumps(row))
    with pytest.raises(runstate.ManifestError):
        runstate.read_manifest(run)


def _tamper(run, mutate):
    row = json.loads(storage.manifest_path(run).read_text())
    mutate(row)
    storage.manifest_path(run).write_text(json.dumps(row))


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda row: row["setup"][0].pop("timeout"), id="missing-a-field"),
    pytest.param(lambda row: row["setup"][0].update(retryable=True), id="unknown-field"),
    # `isinstance(True, int)` is True, so an unguarded read runs the step for one second.
    pytest.param(lambda row: row["setup"][0].update(timeout=True), id="timeout-is-a-bool"),
    pytest.param(lambda row: row["setup"][0].update(env={"CI": 1}), id="env-value-not-a-str"),
    pytest.param(lambda row: row["setup"][0].update(cwd=None), id="cwd-not-a-str"),
    pytest.param(lambda row: row["setup"][0].update(argv=[]), id="argv-names-no-program"),
    pytest.param(lambda row: row["setup"][0].update(argv=["make verify"]), id="argv0-shellish"),
])
def test_a_step_record_that_is_not_section_5_1s_shape_is_refused(tmp_path, mutate):
    """Each of §5.1's four fields is recovered or the manifest is refused — never defaulted.
    A resume that accepted any of these would run a command on terms nobody confirmed, which
    is what recording all four fields rather than the argv alone exists to prevent."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    _tamper(run, mutate)
    with pytest.raises(runstate.ManifestError):
        runstate.read_manifest(run)


def test_writing_something_that_is_not_a_manifest_is_refused(tmp_path):
    """`dataclasses.asdict` raises TypeError on a plain dict, and `getattr` on the round-trip
    comparison would raise AttributeError — neither is a failure a caller of this module has
    a name for, and neither says which argument was wrong."""
    run = _run_dir(tmp_path)
    with pytest.raises(runstate.ManifestError):
        runstate.write_manifest(run, {"run_id": "r1"})
    assert not storage.manifest_path(run).exists()


@pytest.mark.parametrize("document", ["[1, 2]", '"a manifest"', "5", "null"])
def test_a_manifest_that_is_not_an_object_at_all_is_refused(tmp_path, document):
    """A JSON document is not necessarily a mapping, and the two halves of that are not
    equally covered by what follows. Over a list or a string the field loop's `n not in row`
    reports every field missing, which is a ManifestError by luck; over a number or `null` it
    raises TypeError from inside the decoder — a class `--collect` has no name for, out of the
    one path whose whole contract is a named refusal."""
    run = _run_dir(tmp_path)
    storage.manifest_path(run).write_text(document)
    with pytest.raises(runstate.ManifestError):
        runstate.read_manifest(run)


@pytest.mark.parametrize("field, value", [
    ("run_id", 5),                    # _text
    ("base_commit", None),            # _text
    ("generator_contract", ["g1"]),   # _mapping
    ("protected_refs", "HEAD"),       # _mapping
])
def test_a_manifest_field_of_the_wrong_type_is_refused(tmp_path, field, value):
    """Every field is checked, not only the two sequence decoders. A `run_id` that reads back
    as a number names a run directory that does not exist; a `protected_refs` that is a string
    makes §9's ref comparison iterate characters and find no ref moved."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    _tamper(run, lambda row: row.update({field: value}))
    with pytest.raises(runstate.ManifestError):
        runstate.read_manifest(run)


def test_a_manifest_whose_selected_paths_is_a_bare_string_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    row = json.loads(storage.manifest_path(run).read_text())
    row["selected_paths"] = "scratch"
    storage.manifest_path(run).write_text(json.dumps(row))
    with pytest.raises(runstate.ManifestError):
        runstate.read_manifest(run)


# ------------------------------------------------------------------------- the refs snapshot

def test_the_refs_snapshot_records_protected_refs_by_name_and_oid(tmp_path):
    """§9 whitelists by exact name AND the exact OID recorded at creation, because a
    namespace whitelist would let a seat write into forge's own namespace invisibly."""
    repo = make_repo(tmp_path)
    refs, digest = runstate.snapshot_refs(repo, ())
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert refs["HEAD"] == head
    assert any(k.startswith("refs/heads/") and v == head for k, v in refs.items())
    assert digest


def test_a_forge_ref_is_not_recorded_as_protected(tmp_path):
    """§9 allows `refs/khenrix-forge/<run>/*` and `refs/heads/forge/<run>/*` explicitly;
    recording them as protected would make forge's own baseline commit look like drift."""
    repo = make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/khenrix-forge/r1/base", head)
    _git(repo, "update-ref", "refs/heads/forge/r1/claude", head)
    refs, _ = runstate.snapshot_refs(repo, ())
    assert not [k for k in refs if "forge" in k]


def test_a_user_ref_whose_name_merely_starts_with_forge_stays_protected(tmp_path):
    """The exclusion is two exact prefixes, not the substring "forge". A user's
    `forgery-experiments` branch is theirs, and dropping it from the snapshot would let a
    seat move it with nothing to compare against."""
    repo = make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/heads/forgery-experiments", head)
    _git(repo, "update-ref", "refs/tags/forge-v1", head)
    refs, _ = runstate.snapshot_refs(repo, ())
    assert refs["refs/heads/forgery-experiments"] == head
    assert refs["refs/tags/forge-v1"] == head


def test_the_refs_snapshot_survives_a_repository_with_no_refs_at_all(tmp_path):
    """`git show-ref` exits 1 on a freshly initialised repository, and `rev-parse HEAD`
    exits 128. Preflight rejects an unborn HEAD with a sentence the user can act on; a
    snapshot that raised first would replace it with a git stderr dump."""
    repo = Path(tmp_path) / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    refs, digest = runstate.snapshot_refs(repo, ())
    assert refs == {} and digest


# ------------------------------------------------------------------------- the status digest

def test_the_status_digest_moves_when_the_checkout_does(tmp_path):
    repo = make_repo(tmp_path)
    before = runstate.snapshot_refs(repo, ())[1]
    write(repo, "seed.txt", "changed\n")
    assert runstate.snapshot_refs(repo, ())[1] != before


def test_the_status_digest_is_stable_while_nothing_changes(tmp_path):
    """Drift has to mean drift. A digest that moved on its own would make `source_diverged`
    fire on every run, and a warning that always fires is one nobody reads."""
    repo = make_repo(tmp_path)
    write(repo, "untracked.txt", "u\n")
    assert runstate.snapshot_refs(repo, ())[1] == runstate.snapshot_refs(repo, ())[1]


def test_the_status_digest_moves_when_the_user_commits_their_own_work(tmp_path):
    """Measured: a clean tree, an edit, and a commit leaves `status --porcelain` byte for
    byte what it was at t0 — empty. So the porcelain alone reports "no drift" across a
    whole commit of the user's work, which is exactly the work §9 refuses to let a clean
    merge silently revert. HEAD is what separates them."""
    repo = make_repo(tmp_path)
    before = runstate.snapshot_refs(repo, ())[1]
    write(repo, "seed.txt", "user work\n")
    commit_all(repo, "the user's own commit")
    porcelain = _git(repo, "status", "--porcelain=v1").stdout
    assert porcelain == "", "the premise of this test is that the porcelain went back to clean"
    assert runstate.snapshot_refs(repo, ())[1] != before


def test_the_status_digest_moves_when_a_file_appears_inside_an_untracked_directory(tmp_path):
    """Measured: git's default `-unormal` collapses an untracked directory to `?? dir/`, so
    adding a second file inside one leaves the porcelain identical. Untracked paths are
    selectable into the baseline, which makes that a file the run may be carrying and the
    user may be editing underneath it. `--untracked-files=all` is what sees it."""
    repo = make_repo(tmp_path)
    write(repo, "scratch/a.txt", "one\n")
    before = runstate.snapshot_refs(repo, ())[1]
    collapsed = _git(repo, "status", "--porcelain=v1").stdout
    assert collapsed.strip() == "?? scratch/", "the premise is git's default collapse"
    write(repo, "scratch/b.txt", "two\n")
    assert _git(repo, "status", "--porcelain=v1").stdout == collapsed
    assert runstate.snapshot_refs(repo, ())[1] != before


def test_the_status_digest_moves_when_the_user_switches_to_a_branch_at_the_same_commit(tmp_path):
    """§9 protects the user's CURRENT BRANCH REF, and that is a third fact: measured, a
    switch between two branches at one commit moves neither the porcelain, nor `HEAD`'s
    OID, nor any ref. Handover targets the branch that was current, so a run that cannot
    see the switch offers to merge into a branch the user left."""
    repo = make_repo(tmp_path)
    _git(repo, "branch", "other")
    before_refs, before = runstate.snapshot_refs(repo, ())
    _git(repo, "switch", "-q", "other")
    after_refs, after = runstate.snapshot_refs(repo, ())
    assert after_refs == before_refs, "the premise is that no ref moved"
    assert after != before


def test_the_status_digest_moves_when_the_head_detaches_at_the_same_commit(tmp_path):
    repo = make_repo(tmp_path)
    before = runstate.snapshot_refs(repo, ())[1]
    _git(repo, "switch", "-q", "--detach", "HEAD")
    assert runstate.snapshot_refs(repo, ())[1] != before


# ------------------------------------------------ the content of what the run is carrying

def _carrying(tmp_path):
    """A repository in forge's NORMAL shape: a dirty tracked file and a selected untracked
    one, both of which B1 carries. Built through `baseline.materialize` rather than asserted,
    so "the run is carrying both" is measured here and not assumed by the tests below."""
    repo = make_repo(tmp_path)
    run = tmp_path / "b"
    run.mkdir()
    write(repo, "seed.txt", "user v1\n")
    write(repo, "notes.txt", "notes v1\n")
    b = baseline.materialize(repo, run, repo_inspect.repo_facts(repo), ["notes.txt"], "r1",
                             author=AUTHOR)
    assert b.dirty and {"seed.txt", "notes.txt"} <= set(b.filesystem_manifest), \
        "the premise is that B1 carries the content of both paths"
    return repo


def test_the_status_digest_moves_when_the_user_rewrites_the_work_the_run_is_carrying(tmp_path):
    """§9's worked example, on the tree shape forge treats as normal.

    The porcelain is a PATH/STATUS LISTING and carries no content, so an edit to a path it
    ALREADY lists moves it not at all — measured, both files rewritten, ` M seed.txt` and
    `?? notes.txt` byte for byte either way. A dirty tree is forge's ordinary case and "the
    user keeps editing the files they were already editing" is what a human waiting on a run
    does, so this is the likeliest way for §9's clean merge to silently revert their work.
    """
    repo = _carrying(tmp_path)
    listing = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout
    before = runstate.snapshot_refs(repo, ("notes.txt",))[1]
    write(repo, "seed.txt", "user v2\n")
    write(repo, "notes.txt", "notes v2\n")
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout == listing, \
        "the premise is that the listing cannot see either edit"
    assert runstate.snapshot_refs(repo, ("notes.txt",))[1] != before


def test_the_status_digest_moves_when_only_the_dirty_tracked_file_is_rewritten(tmp_path):
    """Half of the case above, on its own — and the half a content hash over `selected_paths`
    alone would still be blind to. A tracked file is carried by B1 whether or not it was
    selected; selection is only how an UNTRACKED path gets in."""
    repo = _carrying(tmp_path)
    before = runstate.snapshot_refs(repo, ("notes.txt",))[1]
    write(repo, "seed.txt", "user v2\n")
    assert runstate.snapshot_refs(repo, ("notes.txt",))[1] != before


def test_the_status_digest_moves_when_only_the_selected_untracked_file_is_rewritten(tmp_path):
    repo = _carrying(tmp_path)
    before = runstate.snapshot_refs(repo, ("notes.txt",))[1]
    write(repo, "notes.txt", "notes v2\n")
    assert runstate.snapshot_refs(repo, ("notes.txt",))[1] != before


def test_the_status_digest_moves_when_a_selected_directorys_content_changes(tmp_path):
    """§2.2 contemplates selecting a DIRECTORY, and `baseline.materialize`'s literal pathspec
    sweeps its whole contents into B — so a digest that stopped at the directory itself would
    be blind to every file the run is carrying inside it. Measured against a recomputed tree
    OID, which sees this and was the reason the walk exists."""
    repo = make_repo(tmp_path)
    write(repo, "scratch/deep/a.txt", "one\n")
    before = runstate.snapshot_refs(repo, ("scratch",))[1]
    write(repo, "scratch/deep/a.txt", "one v2\n")
    assert runstate.snapshot_refs(repo, ("scratch",))[1] != before


def test_the_status_digest_reads_content_from_the_repository_root_not_the_cwd(tmp_path):
    """Measured: `status --porcelain` reports REPOSITORY-ROOT-relative paths even when git
    runs in a subdirectory, while `repo` only has to name the repository. Joining those paths
    onto the argument would look for every carried file in the wrong directory, find none of
    them, and give a checkout that changed and one that did not the same digest — the same
    slip `baseline.materialize` resolves `facts.root` to avoid."""
    repo = make_repo(tmp_path)
    write(repo, "pkg/app.py", "v1\n")
    commit_all(repo, "pkg")
    write(repo, "pkg/app.py", "v2\n")
    sub = repo / "pkg"
    before = runstate.snapshot_refs(sub, ())[1]
    assert before == runstate.snapshot_refs(repo, ())[1], \
        "the snapshot must not depend on which directory of the repository it was handed"
    write(repo, "pkg/app.py", "v3\n")
    assert runstate.snapshot_refs(sub, ())[1] != before


def test_a_selected_directory_does_not_carry_a_nested_repositorys_git_dir(tmp_path):
    """`.git` is pruned rather than post-filtered, on `baseline._walk_selected`'s argument.
    Digesting an object store would make every commit, fetch or gc inside a nested repository
    read as drift in the user's own work, and would walk that store on every drift check.
    `inspect.rejections` does not cover this: it rejects a nested repository AT the selected
    path, and here the selected path is its parent."""
    repo = make_repo(tmp_path)
    write(repo, "vendor/lib/src.py", "v1\n")
    _git(repo / "vendor" / "lib", "init", "-q", "-b", "main", ".")
    before = runstate.snapshot_refs(repo, ("vendor",))[1]
    (repo / "vendor" / "lib" / ".git" / "description").write_text("a nested repo moved\n")
    assert runstate.snapshot_refs(repo, ("vendor",))[1] == before
    write(repo, "vendor/lib/src.py", "v2\n")
    assert runstate.snapshot_refs(repo, ("vendor",))[1] != before


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 directory regardless")
def test_a_selected_directory_that_cannot_be_listed_does_not_read_as_empty(tmp_path):
    """`os.walk` swallows a listing error and yields nothing, so an unreadable directory and
    an empty one produce the same walk. Folding the errno in is what keeps those two apart —
    otherwise a directory the run is carrying could be filled while the digest reports it as
    it was."""
    repo = make_repo(tmp_path)
    (repo / "scratch" / "sub").mkdir(parents=True)
    empty = runstate.snapshot_refs(repo, ("scratch",))[1]
    write(repo, "scratch/sub/x.txt", "hidden\n")
    (repo / "scratch" / "sub").chmod(0o000)
    try:
        assert runstate.snapshot_refs(repo, ("scratch",))[1] != empty
    finally:
        (repo / "scratch" / "sub").chmod(0o755)


def test_two_different_carried_sets_cannot_be_made_to_digest_the_same(tmp_path):
    """The length framing inside the carried set is load-bearing, and this is the witness.

    Concatenated unframed, `"a" + digest(a) + "b" + digest(b)` is byte for byte what the
    single path literally named `a<digest(a)>b` produces when its own content digests to
    `digest(b)` — constructed below, not hypothesised. The outer framing over the four parts
    cannot stand in for it: it frames the carried digest as ONE value, so a collision inside
    that value is already committed by the time it is applied. Asserted on the helper, because
    a repository can only reach this through paths the porcelain does not list at all — which
    would make the porcelain part, not the framing, what separates them.
    """
    (tmp_path / "a").write_text("X")
    (tmp_path / "b").write_text("Y")
    root = os.fsencode(tmp_path)
    imitation = "a" + runstate._path_digest(os.fsencode(tmp_path / "a")).decode() + "b"
    (tmp_path / imitation).write_text("Y")
    assert runstate._carried_digest(root, b"", ["a", "b"]) \
        != runstate._carried_digest(root, b"", [imitation])


def test_the_status_digest_moves_when_a_carried_script_gains_its_executable_bit(tmp_path):
    """The exec bit is the only part of the mode git records, so it is part of what B1
    carries and part of what a merge could revert. On an ALREADY-DIRTY path the listing does
    not move for it, which is this file's whole subject."""
    repo = make_repo(tmp_path)
    write(repo, "run.sh", "#!/bin/sh\n")
    commit_all(repo, "script")
    write(repo, "run.sh", "#!/bin/sh\necho hi\n")
    before = runstate.snapshot_refs(repo, ())[1]
    (repo / "run.sh").chmod(0o755)
    assert runstate.snapshot_refs(repo, ())[1] != before


def test_the_status_digest_ignores_an_untracked_file_the_run_is_not_carrying(tmp_path):
    """The scope, pinned from the other side. An untracked path nobody selected is in no tree
    forge writes, so no merge of forge's work can revert it — and hashing every untracked
    file's content would put each stray editor swap file and test log into a drift report
    whose only value is that it means something."""
    repo = make_repo(tmp_path)
    write(repo, "stray.log", "a\n")
    before = runstate.snapshot_refs(repo, ())[1]
    write(repo, "stray.log", "b\n")
    assert runstate.snapshot_refs(repo, ())[1] == before


def test_the_status_digest_never_reads_through_a_selected_symlink(tmp_path):
    """A selected link is its TARGET TEXT, on `baseline._sha256_link`'s argument: `git add -f`
    commits it AS A LINK, so reading through it would put content from outside the tree into
    a measurement that claims to describe the tree — and would then report an edit to a file
    the run never carried as drift in the user's own work."""
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("v1\n")
    (repo / "link").symlink_to(outside)
    before = runstate.snapshot_refs(repo, ("link",))[1]
    outside.write_text("v2 — content the run never carried\n")
    assert runstate.snapshot_refs(repo, ("link",))[1] == before
    (repo / "link").unlink()
    (repo / "link").symlink_to(tmp_path / "elsewhere.txt")
    assert runstate.snapshot_refs(repo, ("link",))[1] != before


def _ignored_repo(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, ".gitignore", "secret.env\n")
    commit_all(repo, "ignore")
    write(repo, "secret.env", "v1\n")
    return repo


def test_the_status_digest_does_not_see_an_ignored_file_the_run_is_not_carrying(tmp_path):
    """DELIBERATE, and the reason is the other direction. `--ignored` stays off because a
    repository with a dev server or a watcher rewrites ignored build output continuously, so
    the digest would move during every run and `source_diverged` would fire on all of them —
    the fail-noisy direction that trains a user to ignore the one report that matters. An
    ignored path nobody selected is in no tree forge writes, so no merge of forge's work can
    revert it, and there is nothing here for a drift report to be about."""
    repo = _ignored_repo(tmp_path)
    before = runstate.snapshot_refs(repo, ())[1]
    write(repo, "secret.env", "v2\n")
    assert runstate.snapshot_refs(repo, ())[1] == before


def test_the_status_digest_sees_an_ignored_file_the_run_selected(tmp_path):
    """The other half of the same rule, and what closes the gap the exclusion above used to
    leave. Nothing validates `selected_untracked ⊆ facts.untracked` — `inspect.rejections`
    iterates the caller's list and `baseline.materialize` consumes it directly — so a caller
    CAN hand-select an ignored path, and `git add -f` then sweeps it into B. Selection is
    what puts a path in the content set, so the run carries it and the digest can speak for
    it, without `--ignored` and without the churn that ruled `--ignored` out."""
    repo = _ignored_repo(tmp_path)
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout == "", \
        "the premise is that the porcelain never mentions this path at all"
    before = runstate.snapshot_refs(repo, ("secret.env",))[1]
    write(repo, "secret.env", "v2\n")
    assert runstate.snapshot_refs(repo, ("secret.env",))[1] != before


def test_the_status_digest_ignores_the_users_rename_display_preference(tmp_path):
    """`status.renames` is repo-local, so the /dev/null pins on the global and system config
    files do not reach it. Measured: flipping it rewrites the porcelain for a checkout that
    did not change. A digest that inherited it would report the user's display preference as
    drift in their work, and §9's drift report is only worth reading if it means work."""
    repo = make_repo(tmp_path)
    write(repo, "big.txt", "".join(f"line {i}\n" for i in range(200)))
    commit_all(repo, "big")
    _git(repo, "mv", "big.txt", "renamed.txt")
    plain = _git(repo, "status", "--porcelain=v1").stdout
    before = runstate.snapshot_refs(repo, ())[1]
    _git(repo, "config", "status.renames", "false")
    assert _git(repo, "status", "--porcelain=v1").stdout != plain, \
        "premise: the knob rewrites the porcelain for an unchanged checkout"
    assert runstate.snapshot_refs(repo, ())[1] == before


def test_the_status_digest_survives_a_path_that_is_not_valid_utf_8(tmp_path):
    """Under `-z` a path is git's raw bytes with no quoting, and a repository is allowed to
    hold one that is not valid UTF-8. Measured: decoding that output raises
    UnicodeDecodeError before any digest exists, and a drift check that raises is a drift
    check that does not run — on the repository least able to spare it."""
    repo = make_repo(tmp_path)
    before = runstate.snapshot_refs(repo, ())[1]
    with open(os.path.join(os.fsencode(repo), b"bad\xffname.txt"), "wb") as fh:
        fh.write(b"x")
    assert runstate.snapshot_refs(repo, ())[1] != before


def test_the_snapshot_does_not_rewrite_the_users_index(tmp_path):
    """§9 protects the index hash and §2.2 names the mechanism: read-oriented git commands
    opportunistically refresh the index they read. Measured on git 2.53 — a plain
    `git status` over a tracked file whose stat data went stale rewrote `.git/index`, and
    the same command under `GIT_OPTIONAL_LOCKS=0` left it byte for byte alone. The snapshot
    runs against the USER's repository, so a refresh here is forge mutating the thing it
    exists to protect, and it would do it once per drift check."""
    repo = make_repo(tmp_path)
    index = Path(repo) / ".git" / "index"
    seed = Path(repo) / "seed.txt"

    def _digest():
        return hashlib.sha256(index.read_bytes()).hexdigest()

    # The premise, checked rather than assumed: an index whose stat data is stale is one an
    # ordinary status DOES rewrite. Without this the assertion below passes on a repository
    # where nothing would have refreshed the index anyway.
    os.utime(seed, (1893456000, 1893456000))
    stale = _digest()
    _git(repo, "status", "--porcelain=v1")   # the fixture git carries no GIT_OPTIONAL_LOCKS
    assert _digest() != stale, "premise: an ordinary status refreshes a stale index"

    os.utime(seed, (1893456060, 1893456060))
    before = _digest()
    runstate.snapshot_refs(repo, ())
    assert _digest() == before


# -------------------------------------------------------------------------- run-dir layout

def test_the_run_directory_names_are_where_the_engine_looks_for_them(tmp_path):
    run = _run_dir(tmp_path)
    assert storage.manifest_path(run) == run / "manifest.json"
    assert storage.journal_path(run) == run / "events.jsonl"
    assert storage.seat_state_path(run, "codex") == run / "seat-codex.json"


def test_a_seat_name_cannot_escape_the_run_directory(tmp_path):
    """The seat state file is named from a seat identifier, and a path separator in one
    would put a run's state outside the directory `--gc` and the quotas account for."""
    run = _run_dir(tmp_path)
    for bad in ("../escape", "a/b", "", ".", "CODEX"):
        with pytest.raises(storage.StorageError):
            storage.seat_state_path(run, bad)
