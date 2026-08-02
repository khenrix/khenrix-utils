"""Run-directory layout, quotas, and durable writes (spec §14.1, §15)."""
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import gitcmd, storage  # noqa: E402
from forge_fixtures import commit_all, make_repo, write  # noqa: E402


def test_run_root_is_under_xdg_state_and_hashed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    p = storage.run_root(Path("/home/u/git/utils"), "abc123")
    assert p.parent.parent == tmp_path / "state"
    assert p.parent.name == "khenrix-forge"
    # hashed repo path, not basename: two repos with the same basename must not collide
    q = storage.run_root(Path("/home/u/work/utils"), "abc123")
    assert p != q
    assert p.name.endswith("-abc123") and len(p.name) == 12 + 1 + 6


def test_run_root_creates_0700(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    p = storage.run_root(Path("/home/u/git/utils"), "abc123")
    assert p.is_dir()
    assert stat.S_IMODE(p.stat().st_mode) == 0o700


def test_run_root_rejects_a_colliding_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    p = storage.run_root(Path("/home/u/git/utils"), "abc123")
    # same repo, same run id: two runs must never share the directory holding their work
    with pytest.raises(FileExistsError):
        storage.run_root(Path("/home/u/git/utils"), "abc123")
    # ...unless the caller is deliberately reattaching to a run that already exists
    assert storage.run_root(Path("/home/u/git/utils"), "abc123", must_be_new=False) == p


def test_run_root_defaults_without_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    p = storage.run_root(Path("/x/y"), "r1")
    assert p.parent.parent == tmp_path / "home" / ".local" / "state"


def test_new_run_id_is_short_and_unique():
    a, b = storage.new_run_id(), storage.new_run_id()
    assert a != b and len(a) == 6 and a.isalnum()


def test_quota_default_and_breach():
    q = storage.Quota.default()
    assert q.max_files > 0 and q.max_file_bytes > 0 and q.max_total_bytes > 0
    small = storage.Quota(max_files=2, max_file_bytes=10, max_total_bytes=100)
    assert small.breach(files=1, file_bytes=5, total_bytes=50) is None
    assert "files" in small.breach(files=3, file_bytes=5, total_bytes=50)
    assert "file_bytes" in small.breach(files=1, file_bytes=99, total_bytes=50)
    assert "total_bytes" in small.breach(files=1, file_bytes=5, total_bytes=999)


def _fd_path(fd: int) -> str:
    """The path an open fd currently points at.

    Resolved through /proc/self/fd, which is Linux-only. A resolution failure RAISES instead
    of recording a placeholder, for two measured reasons and not the obvious one. Most
    assertions below are POSITIVE membership tests, which a placeholder FAILS — so the
    obvious hazard, a suite going green while measuring nothing, is not what a placeholder
    produces here; it produces a false RED accusing correct code, with a diagnostic naming
    the wrong culprit. The one assertion a placeholder does satisfy silently is the NEGATIVE
    one — "this path was not synced" — and that is the false green spec §10.1 describes,
    reached from the test side. Raising closes both.
    The forge package is POSIX-only anyway (verify._kill_group needs process groups); this
    narrows these assertions to Linux and says so out loud rather than going quiet.
    """
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError as e:
        raise AssertionError(
            f"cannot resolve fd {fd} through /proc/self/fd ({e}). These assertions identify "
            "WHICH object was fsync'd by reading that link, and none of them mean anything "
            "on a machine without it") from e


def _require_fd_resolution() -> None:
    """Resolve a fd whose path is already known, before any assertion trusts the mechanism.

    _fd_path raises on a readlink FAILURE; this pins the other direction, where the link
    resolves to something other than the file it was opened from. It also fixes the
    comparison basis: the kernel reports the path with symlinks already resolved, which is
    why every assertion below compares against realpath rather than the string it opened.
    """
    fd = os.open(__file__, os.O_RDONLY)
    try:
        resolved = _fd_path(fd)
    finally:
        os.close(fd)
    assert resolved == os.path.realpath(__file__), (
        f"/proc/self/fd resolved this suite's own file to {resolved!r}; the fsync assertions "
        "below identify their target the same way and cannot be trusted here")


class _SyscallSpy:
    """The durability syscalls in the order they were made: each fsync resolved to the path
    its fd pointed at, and each rename to its destination.

    Resolved at CALL time, because an fd is an integer and the fact that matters is which
    object it named while it was open. A spy that counted fsyncs instead reports two for a
    writer that synced the file twice and the directory never.
    """

    def __init__(self, monkeypatch):
        _require_fd_resolution()
        self.events: list[tuple[str, str]] = []
        real_fsync, real_replace = os.fsync, os.replace
        real_link, real_unlink = os.link, os.unlink

        def fsync(fd):
            self.events.append(("fsync", _fd_path(fd)))
            return real_fsync(fd)

        def replace(src, dst, *args, **kwargs):
            self.events.append(("replace", os.path.realpath(dst)))
            return real_replace(src, dst, *args, **kwargs)

        def link(src, dst, *args, **kwargs):
            # Recorded BEFORE the call: realpath of the destination is only meaningful once
            # it exists, and a link that raises still has to appear in the sequence.
            self.events.append(("link", os.path.join(os.path.realpath(os.path.dirname(dst)),
                                                     os.path.basename(dst))))
            return real_link(src, dst, *args, **kwargs)

        def unlink(target, *args, **kwargs):
            self.events.append(("unlink", os.path.join(
                os.path.realpath(os.path.dirname(target)), os.path.basename(target))))
            return real_unlink(target, *args, **kwargs)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "link", link)
        monkeypatch.setattr(os, "unlink", unlink)

    def synced(self) -> list[str]:
        return [p for kind, p in self.events if kind == "fsync"]

    def index(self, kind: str, match) -> int:
        """Position of the first `kind` event whose path is `match` (a str) or satisfies it
        (a callable). Raises naming the whole sequence, so a miss says what did happen."""
        for i, (k, p) in enumerate(self.events):
            if k == kind and (p == match if isinstance(match, str) else match(p)):
                return i
        raise AssertionError(f"no {kind} matching {match!r} in {self.events}")


def test_atomic_write_syncs_the_bytes_then_renames_then_syncs_the_directory(
        tmp_path, monkeypatch):
    """`os.replace` is atomic for READERS, not against power loss. Without the directory
    fsync a newly created file can vanish entirely; without the file fsync the name is
    published pointing at content the crash lost. Spec §10.1 names this exact omission as its
    example of a row that reads present while the property is false.

    Asserted on the syscalls and their ORDER, because the read-back is identical with no
    fsync at all, and a directory synced before the rename flushes a directory that does not
    yet hold it — leaving the rename as exactly what a crash loses.
    """
    spy = _SyscallSpy(monkeypatch)
    target = tmp_path / "sub" / "state.json"
    target.parent.mkdir()
    storage.atomic_write(target, b'{"a":1}')

    assert target.read_bytes() == b'{"a":1}'
    parent = os.path.realpath(target.parent)
    # a file IN that directory — the temp one, whose name is the writer's business
    bytes_synced = spy.index("fsync", lambda p: os.path.dirname(p) == parent)
    renamed = spy.index("replace", os.path.realpath(target))
    dir_synced = spy.index("fsync", parent)
    assert bytes_synced < renamed < dir_synced, (
        f"bytes, then the name, then the directory entry — got {spy.events}")


def test_atomic_write_leaves_no_temp_file_behind(tmp_path):
    storage.atomic_write(tmp_path / "s.json", b"x")
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


def test_atomic_write_does_not_clobber_on_failure(tmp_path, monkeypatch):
    """A write that dies mid-way must leave the previous version intact, not a truncated one
    — which is the whole reason for the temp-then-rename rather than an in-place write."""
    target = tmp_path / "s.json"
    storage.atomic_write(target, b"first")
    monkeypatch.setattr(storage.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        storage.atomic_write(target, b"second")
    assert target.read_bytes() == b"first"
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"], "the temp file was left behind"


def test_atomic_write_survives_a_temp_file_left_by_an_earlier_crash(tmp_path, monkeypatch):
    """A process killed between the create and the rename leaves its temp file behind, and
    pids are recycled. A temp name a second call can derive is one `O_EXCL` refuses, so the
    debris fails this write — on a directory whose whole purpose is to still be usable after
    a crash. Measured, that failure is ONE call and self-heals, because the failure arm
    unlinks the debris; the cost of the random suffix is the other direction, that nothing
    collects debris at all and a `--gc` walk becomes mandatory.

    The debris is the name the first write ACTUALLY used rather than a guessed one, so this
    fails for any naming scheme a later call can repeat, not only the pid-derived spelling.
    """
    target = tmp_path / "s.json"
    used = []
    real_replace = os.replace

    def record(src, dst, *args, **kwargs):
        used.append(Path(src))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", record)
    storage.atomic_write(target, b"first")
    assert used, "the write never renamed anything into place"
    used[0].write_bytes(b"debris from a crash")

    storage.atomic_write(target, b"second")
    assert target.read_bytes() == b"second"


def test_atomic_write_refuses_a_short_write(tmp_path, monkeypatch):
    """`os.write` may consume fewer bytes than it was handed. Unchecked, the rename publishes
    the truncation as the record: a file every later reader finds well-formed and short."""
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:-1]))
    with pytest.raises(storage.StorageError):
        storage.atomic_write(tmp_path / "s.json", b"0123456789")
    assert list(tmp_path.iterdir()) == [], "a partial write was left in the directory"


def test_exclusive_write_syncs_the_bytes_then_links_then_unlinks_then_syncs_the_directory(
        tmp_path, monkeypatch):
    """FOUR steps, where `atomic_write` has three, and the difference is the one substitution.
    `os.replace` publishes the name and removes the staging one in a single syscall; `os.link`
    only ADDS a name, so the staging name is still there and has to go separately. The
    durability order is otherwise identical, with the directory entry synced last.
    """
    spy = _SyscallSpy(monkeypatch)
    target = tmp_path / "sub" / "manifest.json"
    target.parent.mkdir()
    storage.exclusive_write(target, b'{"run_id":"r1"}')

    assert target.read_bytes() == b'{"run_id":"r1"}'
    parent = os.path.realpath(target.parent)
    bytes_synced = spy.index("fsync", lambda p: os.path.dirname(p) == parent)
    linked = spy.index("link", os.path.join(parent, "manifest.json"))
    staging_gone = spy.index("unlink", lambda p: os.path.dirname(p) == parent
                             and os.path.basename(p) != "manifest.json")
    dir_synced = spy.index("fsync", parent)
    assert bytes_synced < linked < staging_gone < dir_synced, (
        f"bytes, then the name, then the staging name, then the directory — got {spy.events}")


def test_exclusive_write_refuses_to_overwrite_and_raises_file_exists(tmp_path):
    """The documented contract, asserted directly rather than through a caller that wraps it.
    `FileExistsError` and not `StorageError`: the refusal is the KERNEL's, and a caller that
    means to translate it into its own vocabulary — `runstate` does — needs the class the
    kernel raises, not one this module invented on top of it."""
    target = tmp_path / "manifest.json"
    storage.exclusive_write(target, b"first")
    with pytest.raises(FileExistsError):
        storage.exclusive_write(target, b"second")
    assert target.read_bytes() == b"first"
    assert [p.name for p in tmp_path.iterdir()] == ["manifest.json"], \
        "the refusal left its staging file behind"


def test_exclusive_write_leaves_no_temp_file_behind(tmp_path):
    storage.exclusive_write(tmp_path / "m.json", b"x")
    assert [p.name for p in tmp_path.iterdir()] == ["m.json"]


def test_exclusive_write_refuses_a_short_write(tmp_path, monkeypatch):
    """A record that must never be rewritten is the last one that may be published truncated:
    nothing can correct it afterwards, because the name it would need is now taken."""
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:-1]))
    with pytest.raises(storage.StorageError):
        storage.exclusive_write(tmp_path / "m.json", b"0123456789")
    assert list(tmp_path.iterdir()) == [], "a partial write was left in the directory"


def test_append_line_syncs_the_record_and_the_directory_on_creation(tmp_path, monkeypatch):
    spy = _SyscallSpy(monkeypatch)
    log = tmp_path / "events.jsonl"
    storage.append_line(log, b'{"event":"a"}')
    on_creation = spy.synced()
    storage.append_line(log, b'{"event":"b"}')

    assert log.read_bytes() == b'{"event":"a"}\n{"event":"b"}\n'
    assert os.path.realpath(log) in on_creation, "the record was never fsync'd"
    assert os.path.realpath(tmp_path) in on_creation, \
        "the directory was never fsync'd, so the whole log can vanish with its entry"
    # and not again afterwards: the entry is already durable, and a journal that syncs its
    # directory per record pays two fsyncs for every one it needs
    assert os.path.realpath(tmp_path) not in spy.synced()[len(on_creation):]


def test_append_line_writes_the_record_and_its_terminator_in_one_call(tmp_path, monkeypatch):
    """Two writes can interleave with another process's O_APPEND write between them, and the
    reader then frames one record as two halves around a stranger. The bytes on disk are the
    same either way, so the assertion is on the call."""
    writes = []
    real_write = os.write

    def record(fd, data):
        writes.append(bytes(data))
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", record)
    storage.append_line(tmp_path / "events.jsonl", b'{"event":"a"}')
    assert writes == [b'{"event":"a"}\n']


def test_append_line_refuses_an_embedded_newline(tmp_path):
    """One record per line is the reader's whole framing, and a record carrying a newline
    lands as two lines, neither of which is the record. Both are newline-terminated, so
    §14.1's torn-tail rule — which discards an unterminated LAST line — does not reach
    them: the reader takes both as authoritative."""
    log = tmp_path / "e.jsonl"
    with pytest.raises(storage.StorageError):
        storage.append_line(log, b'{"a":"x\ny"}')
    assert not log.exists(), "the refused record created the log it was refused from"


def test_append_line_reports_a_torn_record_rather_than_completing_it(tmp_path, monkeypatch):
    """A half-written record cannot be finished by a second write — another process's append
    can already have landed behind it. The tail is torn either way; what a caller must not
    get back is a return value saying the record is durable."""
    log = tmp_path / "events.jsonl"
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:-3]))
    with pytest.raises(storage.StorageError):
        storage.append_line(log, b'{"event":"a"}')
    assert not log.read_bytes().endswith(b"\n"), "the fixture did not tear anything"


def test_append_line_syncs_the_directory_when_the_creating_call_FAILS(tmp_path, monkeypatch):
    """Creating the file is what obliges this call to sync the directory, and a torn record
    does not discharge it: the file is on disk either way, and the entry pointing at it is
    what a crash would otherwise lose.

    Skipping it is not one missed sync but a PERMANENT one — every later `append_line` finds
    the file present, takes the `FileExistsError` branch, and skips the directory for the rest
    of the run. Measured: after a torn creating call, two further `journal.Journal.record`
    calls produced three fsyncs on `events.jsonl` and none on its directory — a journal whose
    bytes are durable and whose NAME is not, so a power loss drops the whole log and turns
    every partly-ran operation into never-started.

    Asserted on the syscall, because the file reads back byte-identical with no directory
    fsync at all — spec §10.1's example, reached from the test side.
    """
    spy = _SyscallSpy(monkeypatch)
    log = tmp_path / "events.jsonl"
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:-3]))
    with pytest.raises(storage.StorageError):
        storage.append_line(log, b'{"event":"a"}')
    monkeypatch.setattr(os, "write", real_write)

    assert log.exists(), "the fixture created no file, so there is no unsynced entry to find"
    on_failure = spy.synced()
    assert os.path.realpath(tmp_path) in on_failure, (
        "the creating call failed and left the log's directory entry unsynced")
    # and the obligation is discharged, not deferred: the next call finds the file present and
    # must not pay a second fsync for a name that is already durable
    storage.append_line(log, b'{"event":"b"}')
    assert os.path.realpath(tmp_path) not in spy.synced()[len(on_failure):]


def test_append_line_does_not_sync_a_directory_it_never_created_a_file_in(tmp_path):
    """An `os.open` that fails for any reason OTHER than the file already existing created
    nothing, so there is no entry owed a sync.

    Pinned against one measured spelling of the fix above and not against the idea of it:
    hoisting `created = True` before the open AND widening the same `try/finally` to cover the
    open makes a missing run directory raise out of `_fsync_dir` instead, replacing the error
    that names the LOG with one naming its parent — and sending the reader after the wrong
    missing thing. That mutant is caught here and survives every other `append_line` test.
    """
    missing = tmp_path / "no-such-run-dir" / "events.jsonl"
    with pytest.raises(FileNotFoundError) as e:
        storage.append_line(missing, b'{"event":"a"}')
    assert str(missing) in str(e.value), \
        f"the refusal named {e.value!r} rather than the log it could not open"


def test_git_readonly_preset_reaches_the_child(tmp_path):
    # A `!`-alias is the only way to see what git's own child process was handed; asserting
    # that rev-parse merely succeeded would pass just as well with env_extra ignored.
    repo = make_repo(tmp_path)
    gitcmd.git(repo, "config", "alias.envprobe", "!printenv GIT_OPTIONAL_LOCKS")
    assert gitcmd.git(repo, "envprobe", env_extra=gitcmd.READONLY).stdout.strip() == "0"
    # not vacuous: printenv exits 1 and prints nothing when the variable is unset
    bare = gitcmd.git(repo, "envprobe", check=False)
    assert bare.returncode != 0 and bare.stdout.strip() == ""


def test_git_ignores_an_ambient_git_dir(tmp_path, monkeypatch):
    # Under a hook, `git rebase --exec` or `git bisect run`, GIT_DIR is exported and beats
    # `-C`; inheriting it would aim the engine at the user's repository.
    a = make_repo(tmp_path, "a")
    b = make_repo(tmp_path, "b")
    write(b, "b-only.txt", "b\n")
    head_b = commit_all(b, "b-only")          # distinct history, so the OIDs differ
    head_a = gitcmd.git(a, "rev-parse", "HEAD").stdout.strip()
    assert head_a != head_b

    monkeypatch.setenv("GIT_DIR", str(b / ".git"))
    assert gitcmd.git(a, "rev-parse", "HEAD").stdout.strip() == head_a


def test_git_env_extra_wins_over_the_scrub(tmp_path):
    # The scrub runs BEFORE env_extra, so a deliberate GIT_INDEX_FILE still takes effect —
    # baseline construction depends on exactly this ordering.
    repo = make_repo(tmp_path)
    assert gitcmd.git(repo, "ls-files").stdout.split() == ["seed.txt"]
    alt = tmp_path / "alt.index"
    r = gitcmd.git(repo, "ls-files", env_extra={"GIT_INDEX_FILE": str(alt)})
    assert r.stdout.strip() == ""


def test_git_never_reads_the_users_global_config(tmp_path, monkeypatch):
    # Config discovery may only ever NARROW: scrubbing GIT_CONFIG_GLOBAL would restore
    # ~/.gitconfig (and its core.hooksPath), so git() pins it to /dev/null instead.
    repo = make_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[forge]\n\tcanary = leaked\n")
    monkeypatch.setenv("HOME", str(home))

    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    assert gitcmd.git(repo, "config", "--get", "forge.canary", check=False).returncode != 0
    # and an outer harness that already hardened the variable must not be widened by us
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    assert gitcmd.git(repo, "config", "--get", "forge.canary", check=False).returncode != 0
    # a caller pointing GIT_CONFIG_GLOBAL somewhere deliberately still wins (env_extra last)
    r = gitcmd.git(repo, "config", "--get", "forge.canary",
                   env_extra={"GIT_CONFIG_GLOBAL": str(home / ".gitconfig")})
    assert r.stdout.strip() == "leaked"


def test_fixture_survives_a_hostile_global_config(tmp_path, monkeypatch):
    # A developer with commit.gpgsign = true could not build a fixture repo at all.
    hostile = tmp_path / "hostile.gitconfig"
    hostile.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /bin/false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    repo = make_repo(tmp_path, "hostile")
    write(repo, "more.txt", "more\n")
    assert len(commit_all(repo, "more")) in (40, 64)


def test_fixture_ignores_an_ambient_git_dir(tmp_path, monkeypatch):
    # Without the scrub, `git add -A` under an exported GIT_DIR + GIT_WORK_TREE commits the
    # developer's real working tree — the cardinal invariant breached from a test.
    victim = make_repo(tmp_path, "victim")
    monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(victim))

    repo = make_repo(tmp_path, "fresh")
    write(repo, "only-here.txt", "x\n")
    commit_all(repo, "only-here")
    assert gitcmd.git(repo, "log", "--format=%s").stdout.split() == ["only-here", "seed"]
    assert gitcmd.git(victim, "log", "--format=%s").stdout.split() == ["seed"]


def test_git_check_raises_on_failure(tmp_path):
    repo = make_repo(tmp_path)
    try:
        gitcmd.git(repo, "rev-parse", "--verify", "refs/heads/nope")
    except gitcmd.GitError as e:
        assert "nope" in str(e) or "fatal" in str(e).lower()
    else:
        raise AssertionError("expected GitError")
    r = gitcmd.git(repo, "rev-parse", "--verify", "refs/heads/nope", check=False)
    assert r.returncode != 0


def test_zero_oid_matches_repo_hash_width(tmp_path):
    repo = make_repo(tmp_path)
    z = gitcmd.zero_oid(repo)
    assert set(z) == {"0"}
    head = gitcmd.git(repo, "rev-parse", "HEAD").stdout.strip()
    assert len(z) == len(head)
