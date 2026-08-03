"""§20: what the seat was given, as a manifest that can be re-derived from the seat."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import os  # noqa: E402
import pytest  # noqa: E402
from forge import baseline, fleet, gitcmd  # noqa: E402
from forge import bundle as bundlemod  # noqa: E402
from forge import inspect as finspect  # noqa: E402
from forge import storage, taskbundle  # noqa: E402
from forge_fixtures import make_repo  # noqa: E402


def _tree(root: Path) -> None:
    """A bundle shaped like a real skill closure: a dot directory, an executable script,
    a 0600 file and a symlink — every shape git's clone template was measured to lose."""
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / ".claude" / "skills" / "S.md").write_text("skill body\n")
    (root / "SKILL.md").write_text("entry\n")
    (root / "scripts").mkdir()
    tool = root / "scripts" / "tool.sh"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    secret = root / "config.ini"
    secret.write_text("k=v\n")
    secret.chmod(0o600)
    os.symlink("SKILL.md", root / "alias.md")


def test_the_manifest_carries_every_shape_the_clone_template_drops(tmp_path):
    _tree(tmp_path)
    b = taskbundle.scan(tmp_path, entrypoint="SKILL.md")
    by = {e.path: e for e in b.entries}
    assert set(by) == {".claude/skills/S.md", "SKILL.md", "scripts/tool.sh",
                       "config.ini", "alias.md"}
    assert by["scripts/tool.sh"].mode == 0o755
    assert by["config.ini"].mode == 0o600, \
        "0600 is the mode git's template copy was measured to rewrite to 0644"
    assert by["alias.md"].kind == "symlink"
    assert by["alias.md"].mode == 0 and by["alias.md"].size == 0, \
        "snapshot.Entry's rule: a link's mode and size are fabricated, not read"


def test_the_hash_separates_a_script_from_a_non_executable_copy(tmp_path):
    """§20's own stated failure — 'copying without modes hands it a script it cannot
    execute' — must not be invisible to the hash that proves identical materialization."""
    _tree(tmp_path)
    before = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint="SKILL.md"))
    (tmp_path / "scripts" / "tool.sh").chmod(0o644)
    after = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint="SKILL.md"))
    assert before != after


def test_the_hash_separates_two_entrypoints_over_identical_bytes(tmp_path):
    """§11 compares bundle hashes to decide 'identically prompted'. Two seats told to
    start in different places were not identically prompted."""
    _tree(tmp_path)
    a = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint="SKILL.md"))
    b = taskbundle.bundle_hash(taskbundle.scan(tmp_path, entrypoint=".claude/skills/S.md"))
    assert a != b


def test_an_empty_bundle_is_refused_rather_than_hashed(tmp_path):
    """fleet.Seat.verified's argument, one module over: a check over an empty manifest is
    vacuous and still answers True."""
    with pytest.raises(taskbundle.TaskBundleError, match="no entries"):
        taskbundle.scan(tmp_path, entrypoint="SKILL.md")


def test_an_entrypoint_no_entry_names_is_refused(tmp_path):
    _tree(tmp_path)
    with pytest.raises(taskbundle.TaskBundleError, match="entrypoint"):
        taskbundle.scan(tmp_path, entrypoint="does/not/exist.md")


def test_a_git_directory_inside_a_bundle_is_refused(tmp_path):
    _tree(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    with pytest.raises(taskbundle.TaskBundleError, match="git"):
        taskbundle.scan(tmp_path, entrypoint="SKILL.md")


def test_an_escaping_symlink_is_refused_not_carried(tmp_path):
    """The escaping-link lesson, carried forward: a link whose target leaves the bundle
    describes content the bundle does not claim to hold."""
    root = tmp_path / "b"
    root.mkdir()
    (root / "SKILL.md").write_text("entry\n")
    os.symlink("../outside.txt", root / "esc.md")
    with pytest.raises(taskbundle.TaskBundleError, match="escapes"):
        taskbundle.scan(root, entrypoint="SKILL.md")


def test_a_special_file_is_refused_rather_than_given_a_payload(tmp_path):
    root = tmp_path / "b"
    root.mkdir()
    (root / "SKILL.md").write_text("entry\n")
    os.mkfifo(root / "pipe")
    with pytest.raises(taskbundle.TaskBundleError, match="special"):
        taskbundle.scan(root, entrypoint="SKILL.md")


def test_a_breached_cap_is_a_refusal_and_names_the_cap_it_applied(tmp_path):
    _tree(tmp_path)
    tiny = storage.Quota(max_files=2, max_file_bytes=1 << 20, max_total_bytes=1 << 20)
    with pytest.raises(taskbundle.TaskBundleError, match="files: 5 > 2"):
        taskbundle.scan(tmp_path, entrypoint="SKILL.md", quota=tiny)


def test_an_oversized_file_is_refused_before_it_is_read(tmp_path, monkeypatch):
    """F1: the per-file cap must stop a runaway file BEFORE it is hashed, not merely
    refuse the bundle after. A test that only checks for the raise passes even on the old
    code, which walked and SHA-256'd every regular file first and only compared sizes
    against the cap afterwards — so this one also proves `snapshot._digest` was never
    called on the oversized file, not just that `scan()` eventually raised."""
    root = tmp_path / "b"
    root.mkdir()
    (root / "SKILL.md").write_text("entry\n")
    big = root / "big.bin"
    big.write_bytes(b"x" * 4096)

    real_digest = taskbundle.snapshot._digest

    def _digest_or_fail(p):
        if Path(p) == big:
            pytest.fail(f"{p} was hashed even though it exceeds the per-file cap — the "
                        "cap must be checked before the read, not after it")
        return real_digest(p)

    monkeypatch.setattr(taskbundle.snapshot, "_digest", _digest_or_fail)

    tiny = storage.Quota(max_files=100, max_file_bytes=1024, max_total_bytes=1 << 20)
    with pytest.raises(taskbundle.TaskBundleError, match="file_bytes: 4096 > 1024"):
        taskbundle.scan(root, entrypoint="SKILL.md", quota=tiny)


def test__rel_wraps_a_bundle_error_as_a_task_bundle_error(monkeypatch, tmp_path):
    """F2: `_assert_contained` raises `bundle.BundleError`, a type this module's own
    contract does not promise. Genuinely unreachable through `_rel`'s real call sites —
    `p` always comes from `os.walk`, which never yields a literal `..` component for
    `relative_to` to leave behind — so this forces the branch directly to pin the wrap
    that wants to hold if that ever stops being true."""
    def _boom(rel, what):
        raise bundlemod.BundleError("forced for the test")
    monkeypatch.setattr(taskbundle.bundlemod, "_assert_contained", _boom)
    with pytest.raises(taskbundle.TaskBundleError, match="forced for the test"):
        taskbundle._rel(tmp_path, tmp_path / "x")


def test_the_caps_actually_applied_are_recorded_not_assumed(tmp_path):
    _tree(tmp_path)
    b = taskbundle.scan(tmp_path, entrypoint="SKILL.md")
    q = storage.Quota.for_task_bundle()
    assert (b.max_files, b.max_file_bytes, b.max_total_bytes) == \
        (q.max_files, q.max_file_bytes, q.max_total_bytes), \
        "a bundle that fit and a bundle nobody measured must be different records"


def test_the_bundle_round_trips_through_the_run_directory(tmp_path):
    _tree(tmp_path / "src")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    b = taskbundle.scan(tmp_path / "src", entrypoint="SKILL.md")
    taskbundle.write_task_bundle(run_dir, b)
    assert taskbundle.read_task_bundle(run_dir) == b
    assert taskbundle.bundle_hash(taskbundle.read_task_bundle(run_dir)) == \
        taskbundle.bundle_hash(b)


def test_a_field_the_decoder_does_not_know_is_refused(tmp_path):
    _tree(tmp_path / "src")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    taskbundle.write_task_bundle(run_dir, taskbundle.scan(tmp_path / "src",
                                                          entrypoint="SKILL.md"))
    p = storage.task_bundle_path(run_dir)
    p.write_text(p.read_text().replace('"version": 1', '"version": 1, "extra": 2'))
    with pytest.raises(taskbundle.TaskBundleError, match="does not know"):
        taskbundle.read_task_bundle(run_dir)


def test_a_missing_bundle_raises_rather_than_reading_as_empty(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(taskbundle.TaskBundleError, match="does not exist"):
        taskbundle.read_task_bundle(run_dir)


def _seat(tmp_path):
    """A real clone, built the way §4 builds one, so the git-dir question is a real one.

    `clone_seat` takes a `Baseline`, NOT a rev string: it reads `baseline.ref` (and requires
    the `refs/khenrix-forge/<run-id>/base` shape — `fleet.py:157` splits on `/` and takes
    element 2), `baseline.commit` (`fleet.py:288`) and `baseline.filesystem_manifest`
    (`fleet.py:290`). Passing `"HEAD"` raises `AttributeError` before any assertion in this
    file. This is `tests/test_forge_fleet.py:22`'s `_mk_baseline` spelling, restated here
    rather than imported because a test module importing another test module's private
    helper is a coupling neither file declares.
    """
    repo = make_repo(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude",
                         identity=("Forge", "forge@example.invalid"))
    return repo, s


def test_the_task_directory_is_asked_of_git_not_joined_onto_the_seat(tmp_path):
    _, s = _seat(tmp_path)
    d = taskbundle.task_dir(s.path)
    real = gitcmd.git(s.path, "rev-parse", "--absolute-git-dir",
                      env_extra=gitcmd.READONLY).stdout.strip()
    assert d == Path(real) / "khenrix-forge" / "task"


def test_materialize_then_reverify_carries_every_shape_the_template_drops(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    dest = taskbundle.materialize(b, src, s.path)
    taskbundle.verify_materialized(b, s.path)          # must not raise
    assert (dest / ".claude" / "skills" / "S.md").is_file(), \
        "the dot directory git's clone template was measured to drop silently"
    assert (dest / "config.ini").stat().st_mode & 0o777 == 0o600, \
        "the 0600 git's clone template was measured to rewrite to 0644"
    assert (dest / "scripts" / "tool.sh").stat().st_mode & 0o777 == 0o755
    assert (dest / "alias.md").is_symlink()


def test_the_bundle_is_invisible_to_the_seats_worktree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    taskbundle.materialize(taskbundle.scan(src, entrypoint="SKILL.md"), src, s.path)
    # THE PRESETS GO BEFORE THE SUBCOMMAND. `NO_DAEMON_CACHE` is `("-c", "core.fsmonitor=false",
    # "-c", "core.untrackedCache=false")` (`gitcmd.py:50`) — git's TOP-LEVEL options, which must
    # precede the verb. Measured: `git -C <repo> status --porcelain -c core.fsmonitor=false`
    # answers `error: unknown switch 'c'`, rc 129, and `gitcmd.git` defaults `check=True`, so the
    # splat-last form is a `GitError` rather than a `""` that would satisfy this assertion by
    # accident. Every real call site splats first (`bundle.py:553`, `fleet.py:271`); the closure
    # seam tests scan `shared/lib/forge/*.py` only, so a test-file call site is not covered there.
    porcelain = gitcmd.git(s.path, *gitcmd.NO_DAEMON_CACHE, "status", "--porcelain",
                           env_extra=gitcmd.READONLY).stdout
    assert porcelain == "", \
        "a bundle the agent can commit is a bundle that can be harvested as its work"


def test_a_lost_file_is_a_refusal_not_a_silent_pass(tmp_path):
    """The whole reason materialization is re-derived: a copier that returns is not
    evidence the bytes arrived."""
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    dest = taskbundle.materialize(b, src, s.path)
    (dest / ".claude" / "skills" / "S.md").unlink()
    with pytest.raises(taskbundle.TaskBundleError, match="does not match the authored"):
        taskbundle.verify_materialized(b, s.path)


def test_a_rewritten_mode_is_a_refusal(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    dest = taskbundle.materialize(b, src, s.path)
    (dest / "config.ini").chmod(0o644)
    with pytest.raises(taskbundle.TaskBundleError, match="does not match the authored"):
        taskbundle.verify_materialized(b, s.path)


def test_re_verification_reads_the_seat_not_the_source(tmp_path):
    """Non-vacuity: mutating the SOURCE after materialization must not move the verdict."""
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    taskbundle.materialize(b, src, s.path)
    (src / "SKILL.md").write_text("something else entirely\n")
    taskbundle.verify_materialized(b, s.path)          # still clean: the seat is intact


def test_materializing_twice_is_a_refusal_not_an_overwrite(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    taskbundle.materialize(b, src, s.path)
    with pytest.raises(taskbundle.TaskBundleError, match="already"):
        taskbundle.materialize(b, src, s.path)


def test_a_manifest_path_that_escapes_is_refused_before_anything_is_written(tmp_path):
    """WHAT THE FIXTURES CANNOT REACH. `scan` routes every path through `_rel`, so a
    scan-built bundle can carry neither a `..` component nor a `.git` one — which is exactly
    why materializing one proves nothing about the paths `materialize` is willing to WRITE.
    `_decode` type-checks entry fields and never validates `path`, so `read_task_bundle` —
    the recorded bundle §8.1's fresh-clone retry re-materializes from — is a route to this
    function that `_rel` does not stand in front of. `dest / "../../x"` escapes without Path
    complaining, which is `bundle._assert_contained`'s own argument, and here it would write
    outside the seat's git directory entirely.

    Refused BEFORE the directory is created, so a rejected bundle leaves nothing behind —
    `clone_seat`'s rule, and the reason `dest.exists()` would otherwise wedge the retry.
    """
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _, s = _seat(tmp_path)
    good = taskbundle.scan(src, entrypoint="SKILL.md")
    for bad, why in (("../escaped.md", "escapes"), (".git/config", "git")):
        forged = taskbundle.TaskBundle(
            good.version, good.entrypoint,
            good.entries + (taskbundle.BundleEntry(bad, "file", 0o644, "0" * 64, 0),),
            good.max_files, good.max_file_bytes, good.max_total_bytes)
        with pytest.raises(taskbundle.TaskBundleError, match=why):
            taskbundle.materialize(forged, src, s.path)
        assert not taskbundle.task_dir(s.path).exists(), \
            "a refused bundle must leave no directory behind, or the retry cannot run"
    # Non-vacuity: the same seat still accepts the honest bundle, so the refusals above
    # were about the forged path and not about this seat being unusable.
    taskbundle.materialize(good, src, s.path)
    taskbundle.verify_materialized(good, s.path)


def test_a_symlink_to_a_directory_survives_the_round_trip(tmp_path):
    """`_walk` treats a symlink TO a directory as an entry it does not descend, and that
    branch is the one shape `_tree` has no instance of: its `alias.md` points at a FILE.
    Materializing one exercises the ordering hazard a file-only fixture cannot — the link is
    laid down before the directory it names exists — and re-derivation is what says so.
    """
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    os.symlink("scripts", src / "aaa_link_to_dir")
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md")
    assert {e.path: e.kind for e in b.entries}["aaa_link_to_dir"] == "symlink"
    order = [e.path for e in b.entries]
    assert order.index("aaa_link_to_dir") < order.index("scripts/tool.sh"), \
        "non-vacuity: the link is laid down before the directory it names is created, which " \
        "is the ordering a fixture whose only link points at an existing file cannot produce"
    dest = taskbundle.materialize(b, src, s.path)
    assert (dest / "aaa_link_to_dir").is_symlink()
    assert os.readlink(dest / "aaa_link_to_dir") == "scripts"
    taskbundle.verify_materialized(b, s.path)          # must not raise


def test_an_uninstalled_cli_is_none_and_none_never_agrees_with_none(monkeypatch):
    """§20's licence to use an ambient skill requires all three to hash IDENTICALLY.
    Two CLIs that were never looked at must not satisfy it.

    THE FIRST HALF IS THE HALF THAT WAS MISSING, and mutation testing is what said so:
    replacing `installed_closure`'s `return None` with a hash of the empty manifest SURVIVED
    a suite that asserted `ambient_verdict(...)` over three literal `None`s. Hand-written
    `None`s prove the verdict rejects them; they prove nothing about whether an uninstalled
    CLI ever PRODUCES one. Without this line the exact fail-open this function's docstring is
    written against — three uninstalled CLIs hashing identically to the empty manifest, and
    §20's rule licensing an ambient skill none of them have — is caught by nothing.
    """
    monkeypatch.setattr(taskbundle, "_install_dirs", lambda cli: [])
    assert taskbundle.installed_closure("claude") is None
    assert taskbundle.ambient_verdict({c: taskbundle.installed_closure(c)
                                       for c in ("claude", "codex", "agy")}) is False
    assert taskbundle.ambient_verdict({"claude": None, "codex": None, "agy": None}) is False
    assert taskbundle.ambient_verdict({"claude": "a", "codex": "a", "agy": None}) is False
    assert taskbundle.ambient_verdict({"claude": "a", "codex": "a", "agy": "b"}) is False
    assert taskbundle.ambient_verdict({"claude": "a", "codex": "a", "agy": "a"}) is True


def test_the_closure_hash_is_stable_and_path_independent(tmp_path, monkeypatch):
    """Three CLIs install to three different absolute paths by construction. A hash that
    included the path would make §20's identity rule unsatisfiable for a reason that is
    not about the closures."""
    a, b2 = tmp_path / "one", tmp_path / "two"
    for d in (a, b2):
        (d / "skills").mkdir(parents=True)
        (d / "skills" / "S.md").write_text("same bytes\n")
    monkeypatch.setattr(taskbundle, "_install_dirs", lambda cli: [a] if cli == "claude" else [b2])
    assert taskbundle.installed_closure("claude") == taskbundle.installed_closure("codex")


def test_re_verification_applies_the_caps_the_bundle_recorded_not_todays(tmp_path):
    """THE REASON THE CAPS ARE ON THE VALUE. A bundle authored under a more permissive quota
    must re-derive under THAT quota: re-reading `Quota.for_task_bundle()` here would refuse a
    bundle that legitimately fit when it was written, and the refusal would name the wrong
    failure — a cap change reinterpreting an old bundle."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text("entry\n")
    # Over `for_task_bundle`'s 4 MiB per-file cap, under the quota this bundle records.
    (src / "big.bin").write_bytes(b"\0" * (5 * 1024 * 1024))
    roomy = storage.Quota(max_files=2000, max_file_bytes=8 * 1024 * 1024,
                          max_total_bytes=64 * 1024 * 1024)
    _, s = _seat(tmp_path)
    b = taskbundle.scan(src, entrypoint="SKILL.md", quota=roomy)
    assert b.max_file_bytes > storage.Quota.for_task_bundle().max_file_bytes, \
        "non-vacuity: the two cap sets must actually differ for this file"
    taskbundle.materialize(b, src, s.path)
    taskbundle.verify_materialized(b, s.path)          # must not raise


def test_an_unhashable_installed_closure_is_none_not_an_exception(tmp_path, monkeypatch):
    """The declared type is `str | None` and `_walk` refuses a `.git` component, an escaping
    symlink, a special file and a cap breach. Those refusals are right for an AUTHORED bundle;
    the installed plugin cache is not authored — `refresh.sync` is an additive copytree over
    whatever is on disk — so a stale `.git` there would turn a provenance hash into a
    run-ending exception. `None` is the value this function already defines for 'could not be
    described', and `ambient_verdict` fails on it exactly as it does on 'not installed'."""
    d = tmp_path / "installed"
    (d / ".git").mkdir(parents=True)
    (d / ".git" / "config").write_text("[core]\n")
    (d / "SKILL.md").write_text("body\n")
    monkeypatch.setattr(taskbundle, "_install_dirs", lambda cli: [d])
    assert taskbundle.installed_closure("claude") is None
    assert taskbundle.ambient_verdict({"claude": None, "codex": "a", "agy": "a"}) is False


def test_an_unreadable_installed_file_is_none_too_not_a_permission_error(tmp_path, monkeypatch):
    """The OTHER way out of a `str | None`, and the one a `.git` fixture cannot reach.
    `_walk` reaches `snapshot._digest`, which OPENS every regular file; a plugin cache is a
    tree this engine did not write, so one unreadable file there raises `PermissionError` —
    an `OSError`, not a `TaskBundleError` — straight past a handler that names only the
    latter. `installed_closure`'s docstring says "NEVER A RAISE", and a docstring asserting
    something the code does not do is the defect, so the claim is pinned here.
    """
    d = tmp_path / "installed"
    d.mkdir()
    (d / "SKILL.md").write_text("body\n")
    locked = d / "locked.md"
    locked.write_text("secret\n")
    locked.chmod(0o000)
    if os.access(locked, os.R_OK):          # running as root reads it anyway
        pytest.skip("this user can read a 0000 file, so there is no unreadable file to test")
    monkeypatch.setattr(taskbundle, "_install_dirs", lambda cli: [d])
    assert taskbundle.installed_closure("claude") is None


def test_the_ambient_note_is_an_instruction_never_a_bar():
    note = taskbundle.ambient_note("chunk-map")
    assert "chunk-map" in note
    assert taskbundle.ambient_note.__doc__ and "not a mechanical bar" in \
        taskbundle.ambient_note.__doc__.lower(), \
        "§20's 'bar ambient invocation' has no mechanism in reach; the docstring must say so"
