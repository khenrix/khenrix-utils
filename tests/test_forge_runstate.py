"""The run's identity: the manifest written once, the refs it is judged against, where the
run had got to, and what each seat last managed to say (§9, §14, §14.1, §14.2)."""
import dataclasses
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import baseline, journal, runstate, storage  # noqa: E402
from forge import inspect as repo_inspect  # noqa: E402
from forge.verify import Step  # noqa: E402
from forge_fixtures import commit_all, git as _git, make_repo, write  # noqa: E402

AUTHOR = ("Fixture", "fixture@example.invalid")


def _manifest(repo, **kw):
    selected = kw.get("selected_paths", ())
    refs, digest = runstate.snapshot_refs(repo, selected)
    base = dict(run_id="r1", repo_path=str(repo), base_commit="a" * 40,
                baseline_ref="refs/khenrix-forge/r1/base", baseline_commit="b" * 40,
                tracked_tree_oid="c" * 40, selected_paths=(),
                generator_contract=repo_inspect.GeneratorContract(),
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
                  generator_contract=repo_inspect.GeneratorContract(
                      id="g1", relations=(("src/*", "gen/*"),)))
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
    """`protected_refs` is the remaining free-form mapping, and JSON cannot type-check it for
    the caller.

    A tuple nested inside it serializes happily and reads back a list, so the manifest on
    disk stops equalling the one in memory — and nothing would say so until a resume hours
    later compared them. Refused at the write, where the caller is still there to fix it.
    """
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    m = _manifest(repo, protected_refs={"refs/heads/main": ("a" * 40, "b" * 40)})
    with pytest.raises(runstate.ManifestError, match="protected_refs"):
        runstate.write_manifest(run, m)
    assert not storage.manifest_path(run).exists()


def test_the_contract_survives_as_pairs_and_not_as_lists_of_lists(tmp_path):
    """The field that decides what a candidate is ALLOWED to have rewritten, and the one the
    manifest's own "no conversion between what is written and what the gate runs" argument did
    not reach until now.

    Carried as a free-form dict it failed in three directions, all measured: the natural
    `dataclasses.asdict(contract)` was refused because `relations` is a tuple of tuples;
    the hand conversion callers were forced into read back as lists of lists, and rebuilding
    it the obvious way gave a tuple of LISTS that `__post_init__` accepts in silence and that
    compares UNEQUAL to the contract `verify.fixed_point` checks a candidate against.

    So the pair-ness is asserted directly rather than only through `==`: the equality is what
    a future decoder change would keep passing while handing the gate a shape it accepts.
    """
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    c = repo_inspect.GeneratorContract(
        id="render-v1", relations=(("shared/**", "marketplaces/**"), ("a/*", "b/*")))
    runstate.write_manifest(run, _manifest(repo, generator_contract=c))

    back = runstate.read_manifest(run).generator_contract
    assert isinstance(back, repo_inspect.GeneratorContract)
    assert back == c
    assert all(isinstance(r, tuple) for r in back.relations), \
        f"relations came back as {[type(r).__name__ for r in back.relations]}, not pairs"


@pytest.mark.parametrize("recorded", [
    {"totally": "unrelated"},                       # a dict that merely parses
    {},                                             # the empty dict, once a valid contract
    {"id": "g1"},                                   # a key short
    {"id": "g1", "relations": [], "extra": 1},      # a key over
    {"id": "g1", "relations": [["src/*"]]},         # a relation that is not a pair
    {"id": "g1", "relations": [["src/*", ""]]},     # an output glob matching no path
    {"id": "", "relations": [["src/*", "gen/*"]]},  # relations without an id to carry them
    {"id": "g1", "relations": "src/*"},             # a string, which unpacks into characters
])
def test_a_recorded_contract_that_is_not_one_is_refused_at_read(tmp_path, recorded):
    """`_mapping` accepted any object at all, so a manifest recording `{"totally":
    "unrelated"}` as the run's generator contract round-tripped as valid — in the field
    `verify.fixed_point` reads to decide which verify-origin rewrites a candidate may have
    made. Refused at read, because the resume is where a manifest nobody can vouch for gets
    believed."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    _tamper(run, lambda row: row.update({"generator_contract": recorded}))
    with pytest.raises(runstate.ManifestError):
        runstate.read_manifest(run)


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
    ("generator_contract", ["g1"]),   # _contract
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

    # And the whole drift check, which is what a resume actually calls: it makes git calls
    # `snapshot_refs` does not, and the pin has to hold for every one of them.
    os.utime(seed, (1893456120, 1893456120))
    before = _digest()
    runstate.drift(_manifest(repo), repo)
    assert _digest() == before


# -------------------------------------------------------------------------- run-dir layout

def test_the_run_directory_names_are_where_the_engine_looks_for_them(tmp_path):
    run = _run_dir(tmp_path)
    assert storage.manifest_path(run) == run / "manifest.json"
    assert storage.journal_path(run) == run / "events.jsonl"
    assert storage.state_path(run) == run / "state.json"
    assert storage.seat_state_path(run, "codex") == run / "seat-codex.json"


def test_the_seat_records_a_run_directory_holds_are_enumerable(tmp_path):
    """A resume enumerates seats it never launched, so the writer's own spelling is the only
    thing that says which files in the directory are seats. Read back through
    `seat_state_path`, so the glob and the name it builds cannot drift apart — and the run's
    other three files are present, or "these are the seats" would be a claim about a
    directory holding nothing else."""
    run = _run_dir(tmp_path)
    assert storage.seat_names(run) == ()
    for name in ("codex", "claude"):
        runstate.write_seat(run, name, {"phase": "building"})
    storage.manifest_path(run).write_text("{}")
    storage.state_path(run).write_text("{}")
    storage.journal_path(run).write_text("")
    assert storage.seat_names(run) == ("claude", "codex")
    assert [storage.seat_state_path(run, n) for n in storage.seat_names(run)] == \
        sorted(run.glob("seat-*.json"))


def test_a_seat_name_cannot_escape_the_run_directory(tmp_path):
    """The seat state file is named from a seat identifier, and a path separator in one
    would put a run's state outside the directory `--gc` and the quotas account for."""
    run = _run_dir(tmp_path)
    for bad in ("../escape", "a/b", "", ".", "CODEX"):
        with pytest.raises(storage.StorageError):
            storage.seat_state_path(run, bad)


# ------------------------------------------------------------------------- the state machine

def _state(phase, **kw):
    base = dict(round=0, attempt=0, verified_checkpoint=None, deliverable_checkpoint=None)
    return runstate.State(phase=phase, **{**base, **kw})


def _successors(phase):
    """The phases `advance` will actually go to from `phase`, measured rather than read.

    Asking the public call one target at a time keeps every graph property below a statement
    about what the engine DOES: a table with the right contents and a refusal that consults
    the wrong end of it would satisfy a test that read `_EDGES` directly.
    """
    s = _state(phase)
    out = set()
    for target in runstate.PHASES:
        try:
            runstate.advance(s, target)
        except runstate.TransitionError:
            continue
        out.add(target)
    return out


# The two endings §14 declares out of EVERY non-terminal phase.
_UNIVERSAL = {"failed", "source_diverged"}

# §14's five endings, by name. Restated for `_DECLARED_SPINE`'s reason and one the derivation
# makes sharper: `runstate.TERMINAL` is the phases with no successors, so it answers "which
# phases end a run" out of the very table these tests are asking about.
_DECLARED_TERMINALS = {"ready", "degraded", "review_blocked", "source_diverged", "failed"}

# §14's spine: each non-terminal phase's successors with those two taken out. Restated by
# hand rather than derived from `_EDGES`, because a table compared against itself holds for
# whatever the table happens to say — measured: derived from `_EDGES` instead, `comparing`
# pointed at `verifying` survives the widening check below, exactly as it already survives
# the reachability walk. The per-phase chain assertions catch that one anyway, so
# today the restatement is a second net rather than the only one; it is the only one for a
# phase §14 adds later, whose successors no other test here names. §14's chain moving means
# editing this copy, and that cost is the pin.
_DECLARED_SPINE = {
    "created": {"confirmed"},
    "confirmed": {"setting_up"},
    "setting_up": {"building"},
    "building": {"harvested"},
    "harvested": {"comparing"},
    "comparing": {"synthesizing"},
    "synthesizing": {"verifying"},
    "verifying": {"synthesizing", "reviewing"},
    "reviewing": {"synthesizing", "ready", "degraded", "review_blocked"},
}


def test_the_back_edge_from_reviewing_to_synthesizing_is_declared(tmp_path):
    """§14: a single enum cannot represent "fixing after review round 2". The back-edge is
    what makes the round counter mean anything."""
    s = runstate.State(phase="reviewing", round=2, attempt=1,
                       verified_checkpoint="a" * 40, deliverable_checkpoint=None)
    out = runstate.advance(s, "synthesizing")
    assert out.phase == "synthesizing"
    assert (out.round, out.verified_checkpoint) == (2, "a" * 40), \
        "a back-edge must not reset the dimensions it is orthogonal to"


def test_advance_moves_the_phase_and_leaves_the_other_four_dimensions_alone(tmp_path):
    """The whole tuple, not the two the back-edge test happens to name.

    Each of the four has its own reason for not riding along, and they are not
    interchangeable: `round` incremented here would spend a council round §14.2 says is never
    spent automatically, and `verified_checkpoint` cleared here would drop the commit §14.2
    keeps as the deliverable when a resumed fix fails to verify. A mutant that reset `attempt`
    or `deliverable_checkpoint` alone passes the back-edge test.
    """
    s = runstate.State(phase="reviewing", round=2, attempt=3,
                       verified_checkpoint="a" * 40, deliverable_checkpoint="b" * 40)
    for target in ("synthesizing", "ready", "review_blocked"):
        out = runstate.advance(s, target)
        assert dataclasses.replace(out, phase=s.phase) == s, target


def test_advance_returns_a_new_state_and_never_edits_the_one_it_was_given(tmp_path):
    """A resume compares where it thought it was against where the journal says it got to,
    and an in-place move would make those the same object."""
    s = _state("created")
    out = runstate.advance(s, "confirmed")
    assert s.phase == "created" and out is not s
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.phase = "confirmed"


def test_an_undeclared_edge_is_a_refusal(tmp_path):
    s = runstate.State(phase="created", round=0, attempt=0,
                       verified_checkpoint=None, deliverable_checkpoint=None)
    with pytest.raises(runstate.TransitionError):
        runstate.advance(s, "reviewing")


def test_a_terminal_state_admits_no_successor(tmp_path):
    for name in _DECLARED_TERMINALS:
        s = runstate.State(phase=name, round=1, attempt=1,
                           verified_checkpoint=None, deliverable_checkpoint=None)
        with pytest.raises(runstate.TransitionError):
            runstate.advance(s, "synthesizing")


def test_no_terminal_phase_admits_any_successor_at_all(tmp_path):
    """The test above names one target. A terminal that had kept an edge to anything ELSE
    would end a run that then carries on.

    Iterated over §14's names and NOT over `runstate.TERMINAL`, which is derived from the
    successor sets and so cannot hold a phase that has one — over the derived set this
    assertion is true whatever the graph says, and a `ready` given the two universal endings
    survives it. Measured, both ways round.
    """
    for name in _DECLARED_TERMINALS:
        assert _successors(name) == set(), name


def test_every_terminal_the_spec_names_exists(tmp_path):
    """A terminal nothing can reach is the defect class this project has found in every
    plan; a terminal the spec names and the code omits is the same defect one step earlier."""
    assert set(runstate.TERMINAL) == _DECLARED_TERMINALS


def test_the_declared_phases_are_section_14s_in_section_14s_order(tmp_path):
    """PHASES is the ordered tuple, so this pins the order as well as the membership. It also
    pins the count: the graph is declared as a mapping, and a name written twice there would
    collapse into one entry with only the second spelling's successors."""
    assert runstate.PHASES == (
        "created", "confirmed", "setting_up", "building", "harvested", "comparing",
        "synthesizing", "verifying", "reviewing",
        "ready", "degraded", "review_blocked", "source_diverged", "failed")


def test_every_phase_the_graph_names_can_be_reached_from_created(tmp_path):
    """A phase nothing can reach is a terminal the run can never report. `ready`, `degraded`
    and `review_blocked` hang off `reviewing` alone, so dropping `reviewing → degraded`
    strands that outcome with no route in. `failed` and `source_diverged` leave every
    non-terminal phase, so no one dropped edge can strand either and this walk says nothing
    about them; `test_every_non_terminal_phase_can_reach_failed_and_source_diverged` is what
    stands there instead.

    Reachability alone is WEAK, and measurably so: pointing `comparing` at `verifying`
    instead of `synthesizing` leaves every phase reachable, because `verifying ⇄ synthesizing`
    lets the walk arrive by the back-edge. That mutant survives this test and is caught by the
    spine below — this one is here for the phase a later graph adds with no way in.
    """
    seen, frontier = {"created"}, ["created"]
    while frontier:
        for nxt in _successors(frontier.pop()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(runstate.PHASES)


def test_the_spine_is_the_single_chain_section_14_draws(tmp_path):
    """Each phase up to `verifying` has exactly one successor that CARRIES THE RUN ON, and
    this pins which. The two universal endings are subtracted rather than listed: they are
    every phase's, so leaving them in would make each assertion here a statement about them
    as well, and the widening check below is where that belongs.

    Equality rather than membership, and per phase rather than over the whole walk: a run
    that went `comparing → verifying` would hand the verifier a synthesis nobody made, and
    every weaker property — reachability, the terminal set, the back-edge — holds while it
    does.
    """
    chain = ("created", "confirmed", "setting_up", "building", "harvested", "comparing",
             "synthesizing", "verifying")
    for phase, nxt in zip(chain, chain[1:]):
        assert _successors(phase) - _UNIVERSAL == {nxt}, phase


def test_a_failed_verify_returns_to_synthesizing_and_a_passing_one_goes_to_review(tmp_path):
    """§14's `synthesizing ⇄ verifying`, whose second arrow reachability cannot see — every
    phase is already reachable without it, so a graph that had lost it would leave a run whose
    verify failed able to reach a fix only by routing through a review of the very candidate
    that had just failed verification."""
    assert _successors("verifying") - _UNIVERSAL == {"synthesizing", "reviewing"}


def test_reviewing_is_where_the_run_chooses_its_ending(tmp_path):
    """The node that chooses BETWEEN the endings, pinned exactly so an edge nobody declared
    cannot be added to it — `reviewing → verifying` would re-verify a candidate without
    re-synthesizing the fix the review asked for. Composed from the two facts pinned above
    rather than restating the graph: the back-edge, and the five terminals. The universal
    endings are not subtracted here, because reaching them from `reviewing` is a review
    choosing one, not the amendment's edge arriving from somewhere else."""
    assert _successors("reviewing") == {"synthesizing"} | set(runstate.TERMINAL)


def test_every_non_terminal_phase_can_reach_failed_and_source_diverged(tmp_path):
    """§14 as amended. A terminal reachable from one phase cannot record where a run died,
    and §9 checks its condition continuously rather than at review.

    Both targets are spelled out here rather than taken from `_UNIVERSAL`, so the constant
    the widening check below subtracts is not also the constant that says which two endings
    §14 names — a typo in it would otherwise excuse itself.
    """
    for phase in runstate.PHASES:
        if phase in runstate.TERMINAL:
            continue
        s = _state(phase)
        assert runstate.advance(s, "failed").phase == "failed", phase
        assert runstate.advance(s, "source_diverged").phase == "source_diverged", phase


def test_the_universal_edges_did_not_widen_anything_else(tmp_path):
    """The amendment adds two targets to every non-terminal phase and nothing else — a graph
    that gained a third would be one nobody declared, and it would be reachable from every
    phase at once rather than from wherever a caller first wanted it.

    A phase added to §14 and not to `_DECLARED_SPINE` raises KeyError here rather than
    passing, which is the direction that matters: the missing entry is the spec fact nobody
    wrote down.
    """
    for phase in runstate.PHASES:
        if phase in runstate.TERMINAL:
            continue
        got = _successors(phase)
        assert _UNIVERSAL <= got, phase
        assert got - _UNIVERSAL == _DECLARED_SPINE[phase], phase


def test_a_phase_name_that_does_not_exist_is_refused_and_named_as_such(tmp_path):
    """A typo and a wrong edge are different repairs, so the refusal separates them."""
    s = _state("synthesizing")
    with pytest.raises(runstate.TransitionError) as e:
        runstate.advance(s, "synthesising")
    assert "not a declared phase" in str(e.value)


def test_a_state_carrying_a_phase_section_14_never_declared_is_refused(tmp_path):
    """A state read back from a damaged or foreign record arrives here, and a raw lookup
    would answer it with a KeyError that no resume is catching."""
    with pytest.raises(runstate.TransitionError):
        runstate.advance(_state("fixing"), "verifying")


def test_a_target_that_is_not_a_name_is_refused_rather_than_raising_from_the_lookup(tmp_path):
    with pytest.raises(runstate.TransitionError):
        runstate.advance(_state("created"), ["confirmed"])


def test_advancing_something_that_is_not_a_state_is_refused(tmp_path):
    with pytest.raises(runstate.TransitionError):
        runstate.advance(("created", 0, 0, None, None), "confirmed")


def test_outcome_unknown_is_a_seats_outcome_and_not_a_phase_of_the_run(tmp_path):
    """§14.1 gives it to an operation that started, left no receipt and has no surviving
    process. §14's graph has no such ending, so a run that reached it as a phase would be in a
    state nothing can leave and no terminal accounts for."""
    assert runstate.OUTCOME_UNKNOWN == "outcome_unknown"
    assert runstate.OUTCOME_UNKNOWN not in runstate.PHASES


# ------------------------------------------------------------------------------- seat state

def test_seat_state_survives_a_torn_write(tmp_path):
    """§14.1: a SIGTERM landing mid-rewrite must not leave truncated JSON indistinguishable
    from a seat that never wrote."""
    run = tmp_path / "run"; run.mkdir()
    runstate.write_seat(run, "claude", {"status": "building", "attempt": 1})
    p = storage.seat_state_path(run, "claude")
    p.write_bytes(p.read_bytes()[:12])          # truncate as a killed writer would
    with pytest.raises(runstate.StateError):
        runstate.read_seat(run, "claude")


def test_a_seat_that_never_wrote_is_none_not_an_error(tmp_path):
    run = tmp_path / "run"; run.mkdir()
    assert runstate.read_seat(run, "codex") is None


def test_a_seat_record_of_json_null_is_not_a_seat_that_never_wrote(tmp_path):
    """The route that collapses the two states without damaging a byte: `null` is valid JSON
    and `json.loads` hands back the very object a missing file returns. A reader that trusted
    the parse would report a seat which recorded something as one that never ran."""
    run = _run_dir(tmp_path)
    storage.seat_state_path(run, "claude").write_bytes(b"null\n")
    with pytest.raises(runstate.StateError):
        runstate.read_seat(run, "claude")


def test_a_seat_record_that_is_not_an_object_is_refused(tmp_path):
    run = _run_dir(tmp_path)
    for document in (b"[]\n", b'"building"\n', b"3\n"):
        storage.seat_state_path(run, "claude").write_bytes(document)
        with pytest.raises(runstate.StateError):
            runstate.read_seat(run, "claude")


def test_an_empty_seat_record_is_refused_at_both_ends(tmp_path):
    """`{}` is falsy exactly as `None` is, so `if read_seat(...)` — the shortest way a caller
    asks whether a seat wrote — answers the same for a seat that recorded an empty record and
    one that recorded nothing. Refused where it is produced AND where it is read, because the
    reader meets records this writer did not write."""
    run = _run_dir(tmp_path)
    with pytest.raises(runstate.StateError):
        runstate.write_seat(run, "claude", {})
    storage.seat_state_path(run, "claude").write_bytes(b"{}\n")
    with pytest.raises(runstate.StateError):
        runstate.read_seat(run, "claude")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through mode 000")
def test_a_seat_record_that_cannot_be_read_is_not_a_seat_that_never_wrote(tmp_path):
    """The other way None could be over-reported: a blanket `except OSError` would answer a
    permission failure with the same silence as an absent file."""
    run = _run_dir(tmp_path)
    runstate.write_seat(run, "claude", {"status": "building"})
    p = storage.seat_state_path(run, "claude")
    p.chmod(0o000)
    try:
        with pytest.raises(runstate.StateError):
            runstate.read_seat(run, "claude")
    finally:
        p.chmod(0o600)


def test_seat_state_round_trips_a_candidate_bundles_two_gate_fields(tmp_path):
    """Carrying `gate_delta` without `gate_surface` re-creates the half-record
    `bundle.with_gate_measurement` refuses; the classifier's fourth taint is the tripwire,
    not a silent regression."""
    run = tmp_path / "run"; run.mkdir()
    runstate.write_seat(run, "agy", {"gate_delta": ["Makefile"], "gate_surface": ["Makefile"]})
    back = runstate.read_seat(run, "agy")
    assert back["gate_delta"] == ["Makefile"] and back["gate_surface"] == ["Makefile"]


def test_a_seat_rewrites_its_own_record_as_the_run_moves(tmp_path):
    """The manifest's write-once refusal would make a seat's second status update a crash: a
    seat says `building`, then `harvested`, then what it verified, all under one name."""
    run = _run_dir(tmp_path)
    runstate.write_seat(run, "claude", {"status": "building"})
    runstate.write_seat(run, "claude", {"status": "harvested"})
    assert runstate.read_seat(run, "claude") == {"status": "harvested"}


def test_a_seat_record_is_published_by_rename_and_never_truncated_in_place(tmp_path):
    """§14.1's rule for the rewrite, measured by the only thing it leaves behind: publishing
    through a rename gives the name a NEW inode, while a writer that opened the record and
    truncated it keeps the old one — and it is that in-place window a killed writer leaves a
    reader staring at. A record replaced whole has no such window.

    The debris assertion is the same discipline one level out: a run directory that
    accumulates a temp file per status update is one `--gc` has to guess about.
    """
    run = _run_dir(tmp_path)
    runstate.write_seat(run, "claude", {"status": "building"})
    p = storage.seat_state_path(run, "claude")
    first = p.stat().st_ino
    runstate.write_seat(run, "claude", {"status": "harvested"})
    assert p.stat().st_ino != first
    assert [q.name for q in run.iterdir()] == ["seat-claude.json"]


def test_one_seat_record_has_one_spelling_on_disk(tmp_path):
    """Two callers building the same record in different key orders must produce the same
    bytes, or a `--gc` walk and a human diffing two runs both see a change that is not one."""
    run = _run_dir(tmp_path)
    runstate.write_seat(run, "claude", {"status": "building", "attempt": 1})
    runstate.write_seat(run, "codex", {"attempt": 1, "status": "building"})
    blob = storage.seat_state_path(run, "claude").read_bytes()
    assert storage.seat_state_path(run, "codex").read_bytes() == blob
    assert blob.index(b'"attempt"') < blob.index(b'"status"')


def test_a_seat_payload_json_cannot_carry_is_refused_and_the_previous_record_stands(tmp_path):
    """The refusal happens before anything is published, so the seat's last good record is
    still what a resume finds — the alternative is a seat whose status file was destroyed by
    the write that failed to replace it."""
    run = _run_dir(tmp_path)
    runstate.write_seat(run, "claude", {"status": "building"})
    with pytest.raises(runstate.StateError):
        runstate.write_seat(run, "claude", {"started_at": object()})
    assert runstate.read_seat(run, "claude") == {"status": "building"}


@pytest.mark.parametrize("payload", [
    {"gate_delta": ("Makefile",)},   # a tuple reads back a list
    {1: "building"},                 # an int key reads back a string
])
def test_a_seat_payload_that_would_not_survive_its_round_trip_is_refused(tmp_path, payload):
    """The manifest's argument, one file over: JSON has one sequence type and one key type, so
    a tuple and an int key are accepted at the write and come back as something that compares
    unequal — and nothing says so until a resume compares them hours later."""
    run = _run_dir(tmp_path)
    with pytest.raises(runstate.StateError):
        runstate.write_seat(run, "claude", payload)
    assert runstate.read_seat(run, "claude") is None


def test_writing_something_that_is_not_a_record_is_refused(tmp_path):
    run = _run_dir(tmp_path)
    for payload in (["status"], "building", None):
        with pytest.raises(runstate.StateError):
            runstate.write_seat(run, "claude", payload)


def test_a_seat_name_that_cannot_name_a_file_is_refused_at_both_ends(tmp_path):
    """StorageError rather than StateError, and the difference is the point: a damaged record
    and a caller that asked for a seat it cannot have are different repairs, and folding the
    second into the first would report a typo'd seat name as a damaged seat."""
    run = _run_dir(tmp_path)
    with pytest.raises(storage.StorageError):
        runstate.write_seat(run, "../escape", {"status": "building"})
    with pytest.raises(storage.StorageError):
        runstate.read_seat(run, "../escape")


def test_seat_state_refuses_a_value_json_can_write_but_no_reader_can_parse(tmp_path):
    """`Infinity` and `NaN` are what json.dumps emits for non-finite floats by default, and
    this module's own reader accepts them back — so the round trip closes while the record
    stops being JSON. A `--collect` written in anything else cannot read it.

    NaN was refused before this guard existed, but by accident: `nan != nan` trips the
    round-trip comparison, so it surfaced as a shape failure rather than as a value JSON
    cannot carry. Both are now one refusal with one reason.
    """
    run = tmp_path / "run"
    run.mkdir()
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(runstate.StateError, match="cannot serialize"):
            runstate.write_seat(run, "claude", {"elapsed": bad})
    assert runstate.read_seat(run, "claude") is None, \
        "a refused record must not be what creates the seat's file"


# --------------------------------------------------------------------- where the run got to

def test_the_run_state_round_trips_all_five_dimensions(tmp_path):
    """§14's tuple is five separate dimensions, so a record that carried the phase alone
    would hand a resume a round and an attempt IT chose — the manifest's failure mode one
    file over."""
    run = _run_dir(tmp_path)
    s = runstate.State(phase="verifying", round=2, attempt=3,
                       verified_checkpoint="a" * 40, deliverable_checkpoint="b" * 40)
    runstate.write_state(run, s)
    assert runstate.read_state(run) == s


def test_a_run_that_never_recorded_where_it_got_to_reads_back_none(tmp_path):
    """A run at `confirmed` has written its manifest and moved nowhere since, so no record
    is an ordinary state — and the ONE condition that reads back None, on `read_seat`'s
    rule."""
    assert runstate.read_state(_run_dir(tmp_path)) is None


def test_the_run_state_is_rewritten_as_the_run_advances(tmp_path):
    """Unlike the manifest: a phase moves the whole length of a run, so write-once here
    would make the second advance a crash."""
    run = _run_dir(tmp_path)
    runstate.write_state(run, _state("setting_up"))
    runstate.write_state(run, _state("building", attempt=1))
    assert runstate.read_state(run) == _state("building", attempt=1)


def test_a_damaged_run_state_is_not_a_run_that_never_recorded_one(tmp_path):
    """§14.1's rule for `seat-codex.json`, one file up. A killed writer's truncated JSON and
    a run that never wrote must stay different answers, or a resume starts from `confirmed`
    a run that was most of the way through."""
    run = _run_dir(tmp_path)
    runstate.write_state(run, _state("building"))
    p = storage.state_path(run)
    p.write_bytes(p.read_bytes()[:12])
    with pytest.raises(runstate.StateError):
        runstate.read_state(run)


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda row: row.pop("attempt"), id="missing-a-dimension"),
    pytest.param(lambda row: row.update(escalated=True), id="unknown-dimension"),
    pytest.param(lambda row: row.update(round="2"), id="round-is-a-string"),
    # `isinstance(True, int)` — a JSON `true` here would resume at round 1.
    pytest.param(lambda row: row.update(round=True), id="round-is-a-bool"),
    pytest.param(lambda row: row.update(phase=None), id="phase-is-not-a-name"),
    pytest.param(lambda row: row.update(verified_checkpoint=40), id="checkpoint-not-an-oid"),
])
def test_a_run_state_that_is_not_section_14s_tuple_is_refused(tmp_path, mutate):
    """Each dimension is recovered or the record is refused — never defaulted. A resume that
    accepted any of these would carry on from a position nobody recorded."""
    run = _run_dir(tmp_path)
    runstate.write_state(run, _state("verifying", round=2, attempt=1))
    row = json.loads(storage.state_path(run).read_text())
    mutate(row)
    storage.state_path(run).write_text(json.dumps(row))
    with pytest.raises(runstate.StateError):
        runstate.read_state(run)


def test_writing_something_that_is_not_a_state_is_refused(tmp_path):
    """`dataclasses.asdict` raises TypeError on a plain dict, which is not a failure any
    caller of this module has a name for."""
    run = _run_dir(tmp_path)
    with pytest.raises(runstate.StateError):
        runstate.write_state(run, {"phase": "building"})
    assert not storage.state_path(run).exists()


def test_a_run_state_that_would_not_survive_its_round_trip_is_refused_at_write(tmp_path):
    """JSON has one sequence type, so a checkpoint handed in as a tuple is written happily and
    comes back a list — and nothing says so until the resume that can no longer recognise its
    own position. `State` validates none of its five dimensions, so the write is where this is
    caught."""
    run = _run_dir(tmp_path)
    with pytest.raises(runstate.StateError):
        runstate.write_state(run, _state("building", verified_checkpoint=("a" * 40,)))
    assert runstate.read_state(run) is None, \
        "a refused record must not be what creates the run's state file"


def test_a_run_state_json_cannot_carry_is_refused_and_the_last_one_stands(tmp_path):
    """Nothing is published until the record is known to survive its own round trip, so a
    refused write leaves the position a resume will actually find."""
    run = _run_dir(tmp_path)
    runstate.write_state(run, _state("building"))
    with pytest.raises(runstate.StateError):
        runstate.write_state(run, _state("building", verified_checkpoint=object()))
    assert runstate.read_state(run) == _state("building")


# ------------------------------------------------------------------------------- the drift

def test_a_moved_protected_ref_is_drift(tmp_path):
    """§9: transition to `source_diverged` and do not continue to handover automatically."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    m = _manifest(repo)
    runstate.write_manifest(run, m)
    write(repo, "seed.txt", "the user kept working\n")
    commit_all(repo, "user's own commit")
    assert "HEAD" in runstate.drift(m, repo)
    # Equality as well, because this is the only case where several names drift at once: it
    # pins that the report is complete and that its order is stable, which a report a human
    # reads at handover and a resume compares between attempts both depend on.
    assert runstate.drift(m, repo) == ("HEAD", "refs/heads/main", "status")
    assert runstate.reconstruct(run, repo).diverged != ()


def test_a_protected_ref_that_disappeared_is_drift(tmp_path):
    """Measured: deleting a tag moves NO surviving ref's OID and does not move the status
    digest either — a tag is not a checkout file. So a comparison that only walks the refs
    present in both snapshots reports nothing at all, and §9's "all non-forge refs" would be
    protected only against being moved, never against being removed."""
    repo = make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/tags/v1", head)
    m = _manifest(repo)
    _git(repo, "update-ref", "-d", "refs/tags/v1")
    refs, digest = runstate.snapshot_refs(repo, ())
    assert all(refs[n] == o for n, o in m.protected_refs.items() if n in refs), \
        "the premise is that no surviving ref moved"
    assert digest == m.status_digest, "and that the checkout digest did not move either"
    assert runstate.drift(m, repo) == ("refs/tags/v1",)


def test_a_ref_the_user_created_during_the_run_is_drift(tmp_path):
    """§9 protects "all non-forge refs" and whitelists exactly two namespaces; a branch that
    appeared since t0 is in neither, so it is a write into the user's repository that nothing
    here accounted for. Measured: it moves no recorded ref's OID and no digest, so the
    new-ref case is the only thing that can see it."""
    repo = make_repo(tmp_path)
    m = _manifest(repo)
    _git(repo, "branch", "users-own-idea")
    assert runstate.snapshot_refs(repo, ())[1] == m.status_digest, \
        "the premise is that creating a branch moves nothing else this snapshot records"
    assert runstate.drift(m, repo) == ("refs/heads/users-own-idea",)


def test_a_forge_ref_created_during_the_run_is_not_drift(tmp_path):
    """Expected forge-ref movement is reported separately from unexpected protected-ref
    movement — the run creates those refs, so counting them as drift would make every run
    diverge from itself.

    The exclusion is `snapshot_refs`' own, applied to BOTH sides: the fresh snapshot never
    names a forge ref, so the new-ref case above cannot see one. A `drift` that listed refs
    from a raw `show-ref` instead would report forge's own baseline as the user's work.
    """
    repo = make_repo(tmp_path)
    m = _manifest(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/khenrix-forge/r1/base", head)
    _git(repo, "update-ref", "refs/heads/forge/r1/claude", head)
    assert runstate.drift(m, repo) == ()


def test_a_user_ref_whose_name_merely_starts_with_forge_is_drift(tmp_path):
    """The whitelist is two exact prefixes, and this is the half `snapshot_refs`' own test
    cannot reach: a `forgery-experiments` branch created DURING the run has no t0 entry to be
    compared against, so only the new-ref case speaks for it."""
    repo = make_repo(tmp_path)
    m = _manifest(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/heads/forgery-experiments", head)
    assert runstate.drift(m, repo) == ("refs/heads/forgery-experiments",)


def test_an_uncommitted_checkout_change_is_drift(tmp_path):
    repo = make_repo(tmp_path)
    m = _manifest(repo)
    write(repo, "seed.txt", "dirty\n")
    assert "status" in runstate.drift(m, repo)


@pytest.mark.parametrize("selected", [(), ("notes.txt",)])
def test_drift_is_empty_while_the_repository_stands_still(tmp_path, selected):
    """Drift has to mean drift. A report that fired on a repository nobody touched would stop
    every run at `source_diverged` and teach the user to ignore the one that matters.

    The SELECTION is parametrized because a drift check that re-derived the digest with the
    WRONG selection fails only here. Measured: with a selection recorded, such a check
    compares two digests taken over different file sets, so it reports `status` — which the
    moved-checkout cases below cannot see, since they expect `status` and get it for a reason
    nobody asked about. The failure is the always-fires direction, and it is the one that
    makes §9's report unreadable rather than absent.
    """
    repo = make_repo(tmp_path)
    write(repo, "notes.txt", "notes v1\n")
    assert runstate.drift(_manifest(repo, selected_paths=selected), repo) == ()


def test_drift_measures_the_checkout_against_the_paths_the_run_carries(tmp_path):
    """The t0 digest ranges over `selected_paths`, so the drift check must re-derive it from
    the MANIFEST's selection rather than from a default. Measured: rewriting a selected
    untracked file leaves `?? notes.txt` byte for byte what it was, so a check that passed
    `()` would compare two digests that agree and report the user's own edit as no drift —
    while the t0 digest it was compared against saw the file."""
    repo = make_repo(tmp_path)
    write(repo, "notes.txt", "notes v1\n")
    m = _manifest(repo, selected_paths=("notes.txt",))
    listing = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout
    write(repo, "notes.txt", "notes v2\n")
    assert _git(repo, "status", "--porcelain=v1",
                "--untracked-files=all").stdout == listing, \
        "the premise is that the listing cannot see the edit"
    assert runstate.drift(m, repo) == ("status",)


def test_drift_over_something_that_is_not_a_manifest_is_refused(tmp_path):
    """An attribute error out of a drift check is a failure `--collect` has no name for, on
    the one path whose whole job is to say what moved."""
    repo = make_repo(tmp_path)
    with pytest.raises(runstate.ManifestError):
        runstate.drift({"protected_refs": {}}, repo)


def test_a_forge_ref_that_existed_at_t0_and_moved_is_invisible_rather_than_whitelisted(
        tmp_path):
    """§9 asks for a whitelist by exact name AND the OID recorded at creation. This is what
    is here instead, pinned so the gap is measured rather than asserted: `snapshot_refs`
    drops forge's namespaces BEFORE recording, so a forge ref that was already there at t0
    has no recorded OID to be compared against and its movement leaves no trace at all."""
    repo = make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/khenrix-forge/r1/base", head)
    m = _manifest(repo)
    assert not [k for k in m.protected_refs if "forge" in k], \
        "the premise is that no forge ref has a recorded name or OID"
    # A commit nothing else points at, so the only thing that moves is forge's own ref.
    moved = _git(repo, "commit-tree", f"{head}^{{tree}}", "-p", head,
                 "-m", "a seat writing into forge's own namespace").stdout.strip()
    _git(repo, "update-ref", "refs/khenrix-forge/r1/base", moved)
    assert runstate.drift(m, repo) == ()


# ------------------------------------------------- the repository the manifest recorded

def test_drift_against_a_repository_the_manifest_never_recorded_is_refused(tmp_path):
    """The manifest records the repository, so a repository handed in beside it is a second
    answer to a question already settled — and the wrong one answers cleanly. Measured
    before this refusal: `drift` against a byte copy of the repo taken at t0 reported `()`
    while the recorded repository had moved `HEAD`, its branch and the checkout."""
    repo = make_repo(tmp_path)
    m = _manifest(repo)
    twin = tmp_path / "twin"
    shutil.copytree(repo, twin, symlinks=True)
    write(repo, "seed.txt", "the user kept working\n")
    commit_all(repo, "user's own commit")
    assert runstate.drift(m, repo) == ("HEAD", "refs/heads/main", "status")
    with pytest.raises(runstate.ManifestError):
        runstate.drift(m, twin)


def test_a_reconstruction_of_a_twin_of_the_recorded_repository_is_refused(tmp_path):
    """§14's "always from disk" is not only about where the FACTS come from: a resume that
    accepted any repository would report `diverged == ()` for a run whose repository moved
    on every axis §9 protects, and §9 says that run must not continue to handover."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    twin = tmp_path / "twin"
    shutil.copytree(repo, twin, symlinks=True)
    write(repo, "seed.txt", "the user kept working\n")
    commit_all(repo, "user's own commit")
    with pytest.raises(runstate.ManifestError):
        runstate.reconstruct(run, twin)
    assert runstate.reconstruct(run, repo).diverged != ()


@pytest.mark.parametrize("spelling", ["trailing slash", "dot segments", "symlink",
                                      "subdirectory"])
def test_the_recorded_repository_answers_to_every_spelling_of_itself(tmp_path, spelling):
    """The identity is git's, not the string's. A refusal that compared the argument as
    written would fail an ordinary resume — the same directory reached through a symlinked
    home, or named from inside itself, which `_status_digest` already answers for because
    the porcelain's paths are repository-root-relative wherever git ran."""
    repo = make_repo(tmp_path)
    (repo / "sub").mkdir()
    write(repo, "sub/a.txt", "a\n")
    commit_all(repo, "sub")
    m = _manifest(repo)
    link = tmp_path / "link-to-repo"
    os.symlink(repo, link)
    named = {"trailing slash": Path(str(repo) + os.sep),
             "dot segments": repo.parent / "." / repo.name,
             "symlink": link,
             "subdirectory": repo / "sub"}[spelling]
    assert runstate.drift(m, named) == ()
    write(repo, "seed.txt", "the user kept working\n")
    assert runstate.drift(m, named) == ("status",), \
        "and it must still be the same repository's drift, not a silent ()"


def test_a_repository_recorded_through_a_symlink_resumes_from_either_path(tmp_path):
    """The RECORDED side is a string nobody resolved. A run confirmed under a symlinked path
    — a home directory reached through a link, a `/tmp` that is one — records the link, while
    git answers with the target: resolving only the argument would refuse the repository the
    manifest was written for."""
    repo = make_repo(tmp_path)
    link = tmp_path / "link-to-repo"
    os.symlink(repo, link)
    m = _manifest(repo, repo_path=str(link))
    assert runstate.drift(m, repo) == ()
    assert runstate.drift(m, link) == ()


def test_a_linked_worktree_of_the_recorded_repository_is_refused(tmp_path):
    """A second checkout of one repository is a different working tree with its own HEAD,
    its own index and its own status — so the snapshot cannot be judged against it, even
    though every object and ref is shared."""
    repo = make_repo(tmp_path)
    m = _manifest(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "wt-branch")
    with pytest.raises(runstate.ManifestError):
        runstate.drift(m, wt)


def test_a_manifest_that_records_no_repository_is_refused_rather_than_met_by_the_cwd(
        tmp_path, monkeypatch):
    """`os.path.realpath("")` is the PROCESS's working directory, so a manifest whose
    `repo_path` is empty is satisfied by whatever directory forge happens to be run from —
    a fact nobody recorded answering for itself, which is the fail-OPEN direction. Measured
    with the cwd inside the repository, which is where a resume is normally launched."""
    repo = make_repo(tmp_path)
    m = _manifest(repo, repo_path="")
    monkeypatch.chdir(repo)
    with pytest.raises(runstate.ManifestError):
        runstate.drift(m, repo)


def test_a_path_that_is_not_a_repository_is_refused_for_the_reason_it_actually_failed(
        tmp_path):
    """Fail closed on the question actually being asked, and SAY which way it failed.

    The message is asserted because the other branch answers too, wrongly: with the git
    failure unhandled, the empty stdout resolves to the PROCESS's working directory, and the
    mismatch branch then reports a run "handed" a path nobody named. Measured as a surviving
    mutant, which is what this assertion now kills.
    """
    repo = make_repo(tmp_path)
    m = _manifest(repo)
    stranger = tmp_path / "not-a-repo"
    stranger.mkdir()
    with pytest.raises(runstate.ManifestError) as e:
        runstate.drift(m, stranger)
    assert str(stranger) in str(e.value) and "not a git repository" in str(e.value)


# --------------------------------------------------------------- reconstruction from disk

def test_a_run_reconstructs_from_disk_after_the_writer_is_gone(tmp_path):
    """The property `--collect` rests on: nothing in memory, and the same answer whether the
    process was compacted or killed."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    j = journal.Journal(storage.journal_path(run))
    j.record(journal.intent("setup"), operation_id="op1", seat="claude")
    j.record(journal.done("setup"), operation_id="op1", exit_code=0)
    j.record(journal.intent("build"), operation_id="op2", seat="claude")
    runstate.write_seat(run, "claude", {"phase": "building"})
    runstate.write_state(run, _state("building"))
    del j                                    # the writer is gone; only the directory remains

    r = runstate.reconstruct(run, repo)
    assert r.manifest.run_id == "r1"
    assert [e.operation_id for e in r.orphans] == ["op2"], \
        "the build started and never finished — outcome_unknown, not a retry"
    assert r.seats["claude"]["phase"] == "building"
    assert r.state == _state("building")
    assert r.diverged == ()


def test_a_run_that_recorded_nothing_but_its_manifest_still_reconstructs(tmp_path):
    """A journal with no records and a seat that never wrote are STATES, not errors: a run
    killed between `confirmed` and its first operation has exactly this directory, and a
    reconstruction that raised on it would leave the one run `--collect` most needs to read
    unreadable."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    assert not storage.journal_path(run).exists(), "the premise is that nothing was recorded"
    r = runstate.reconstruct(run, repo)
    assert (r.orphans, r.seats, r.state) == ((), {}, None)


def test_a_reconstruction_reads_every_seat_that_wrote_and_invents_none_that_did_not(tmp_path):
    """A seat absent from the mapping never wrote; that is `read_seat`'s None carried up
    without a placeholder, because a seat entry the reader supplied is a fact no seat
    recorded."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    runstate.write_seat(run, "claude", {"phase": "building"})
    runstate.write_seat(run, "agy", {"phase": "harvested"})
    r = runstate.reconstruct(run, repo)
    assert sorted(r.seats) == ["agy", "claude"]
    assert "codex" not in r.seats


def test_a_damaged_seat_is_not_dropped_from_a_reconstruction(tmp_path):
    """§14.1's whole point, at the one call that reads every seat at once: a resume that
    skipped the torn record would report the other two seats and carry on as though the
    third had never run.

    Three seats, because that sentence is the failure: with one seat on disk a skipping
    reconstruction returns an empty mapping, which a caller may still notice, while with two
    healthy neighbours it returns a full-looking fleet that is quietly missing a member."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    for name in ("claude", "codex", "agy"):
        runstate.write_seat(run, name, {"phase": "building"})
    p = storage.seat_state_path(run, "claude")
    p.write_bytes(p.read_bytes()[:12])
    with pytest.raises(runstate.StateError):
        runstate.reconstruct(run, repo)


def test_a_seat_record_this_engine_could_not_have_written_is_refused(tmp_path):
    """`seat-CODEX.json` is a name `write_seat` refuses to produce, so a run directory
    holding one holds a record from somewhere else. Skipping it would make the reconstruction
    quietly narrower than the directory it read."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    (run / "seat-CODEX.json").write_text('{"phase": "building"}')
    with pytest.raises(storage.StorageError):
        runstate.reconstruct(run, repo)


def test_a_reconstruction_refuses_a_journal_it_cannot_read(tmp_path):
    """JournalError unwrapped, on this package's precedent: a resume that read past a
    damaged record would proceed on a history missing whatever came after it."""
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    runstate.write_manifest(run, _manifest(repo))
    storage.journal_path(run).write_bytes(b'{"seq": 1}\nnot json at all\n')
    with pytest.raises(journal.JournalError):
        runstate.reconstruct(run, repo)


def test_a_reconstruction_without_a_manifest_stops_before_it_reads_anything_else(tmp_path):
    """`--collect` on a run that never reached `confirmed` has no record of what the run
    agreed to do, and everything below the manifest is read in terms of it.

    The ORDER is asserted with a damaged seat present, because `read_seat`'s own docstring
    rests on it: "a run directory that does not exist at all is `read_manifest`'s refusal,
    which every resume passes through before it asks a seat anything". A reconstruction that
    read the seats first would answer a missing run with a torn-record message, and send
    whoever is repairing it to the wrong file.
    """
    repo = make_repo(tmp_path)
    run = _run_dir(tmp_path)
    with pytest.raises(runstate.ManifestError):
        runstate.reconstruct(run, repo)
    (run / "seat-claude.json").write_bytes(b'{"phase": "buil')
    with pytest.raises(runstate.ManifestError):
        runstate.reconstruct(run, repo)
