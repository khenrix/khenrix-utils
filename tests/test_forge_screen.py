"""The screen runs BEFORE any provider starts — a post-harvest scan is too late."""
import os
import signal
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import screen, storage  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402


def _within(seconds, fn, *args, **kwargs):
    """Run `fn` under a SIGALRM deadline so a blocking bug FAILS the test instead of
    hanging the suite. A signal, not a worker thread: a thread stuck in a blocking open()
    cannot be cancelled, and a non-daemon thread would then hang the interpreter at exit
    rather than the test. The handler raises, so PEP 475's EINTR retry does not resume the
    blocked call.

    Restated here rather than imported from `test_forge_snapshot`: the two suites test
    modules that guard this hazard independently, and a shared helper would make one
    suite's collection failure hide the other's.
    """
    def _fire(signum, frame):
        raise TimeoutError(f"call blocked for more than {seconds}s")
    prev = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn(*args, **kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)

# Assembled from two fragments so this FILE never holds a literal the repo's own
# scan_secrets would match. The obvious sample token (`xoxp-1234567890abcde`) cannot be
# used: its sha256 is already in checks.SECRET_ALLOW_SHA, so the screen suppresses it —
# which would make test_detects_* fail and, worse, make every "findings == []" assertion
# below pass for the wrong reason. test_detects_a_token_in_a_selected_file pins that this
# value IS detected, so the skip tests prove the skip and not the allowlist.
TOKEN = "xoxp-" + "0000000000deadbeef"
# The converse: sha256 of this string IS checks.SECRET_ALLOW_SHA's first entry, so the
# screen must suppress it. Safe to write literally here for the same reason — the repo's
# own scan_secrets allowlists it too — which is what makes it the decoy below.
ALLOWLISTED = "xoxp-1234567890abcde"


def test_detects_a_token_in_a_selected_file(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "cfg.py", f'TOKEN = "{TOKEN}"\n')
    findings, breaches = screen.screen_tree(repo, ["cfg.py"])
    assert breaches == []
    assert findings and findings[0].path == "cfg.py" and findings[0].line == 1


def test_clean_tree_is_clean(tmp_path):
    repo = make_repo(tmp_path)
    findings, breaches = screen.screen_tree(repo, ["seed.txt"])
    assert findings == [] and breaches == []


def test_binary_file_is_skipped_not_decoded(tmp_path):
    repo = make_repo(tmp_path)
    (Path(repo) / "blob.bin").write_bytes(b"\x00\x01\x02" + TOKEN.encode())
    findings, _ = screen.screen_tree(repo, ["blob.bin"])
    assert findings == [], "NUL-containing file must be skipped, not scanned"


def test_oversized_file_breaches_and_fails_closed(tmp_path):
    repo = make_repo(tmp_path)
    (Path(repo) / "big.txt").write_text("x" * 5000)
    q = storage.Quota(max_files=10, max_file_bytes=100, max_total_bytes=10_000)
    findings, breaches = screen.screen_tree(repo, ["big.txt"], quota=q)
    assert breaches and "file_bytes" in breaches[0]


def test_high_risk_names_are_flagged_by_path(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, ".env.local", "NOTHING_MATCHING_A_PATTERN=1\n")
    findings, _ = screen.screen_tree(repo, [".env.local"])
    assert any(f.pattern == "high-risk-filename" for f in findings)


def test_skips_image_suffixes(tmp_path):
    repo = make_repo(tmp_path)
    (Path(repo) / "logo.png").write_text(TOKEN)
    findings, _ = screen.screen_tree(repo, ["logo.png"])
    assert findings == []


def test_directory_selection_walks_its_files(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "pkg/a.py", "ok\n")
    write(repo, "pkg/b.py", f'K = "{TOKEN}"\n')
    findings, _ = screen.screen_tree(repo, ["pkg"])
    assert [f.path for f in findings] == ["pkg/b.py"]


def test_a_selected_symlinked_directory_is_not_followed_out_of_the_tree(tmp_path):
    """`is_dir()` follows symlinks, so a bare is_dir/is_file split walks the target.

    os.walk already refuses to descend into a symlinked subdirectory, so the escape only
    exists for a link named directly in the selection — which is exactly the case an
    attacker-authored repo controls. Not followed, but still reported.
    """
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text(f'K = "{TOKEN}"\n')
    (Path(repo) / "linkdir").symlink_to(outside, target_is_directory=True)
    findings, breaches = screen.screen_tree(repo, ["linkdir"])
    assert findings == [], "must not read through a link out of the tree"
    assert any("linkdir" in b for b in breaches), "and must not call it clean either"


def test_a_selected_symlinked_env_file_is_reported_not_silently_skipped(tmp_path):
    """The worst outcome this module can produce: a clean verdict on an unread `.env`.

    `.env` is the first entry of BLOCKED_NAMES — the one filename class the screen keeps a
    dedicated list for — so a symlink wearing that name must never vanish from the report.
    """
    repo = make_repo(tmp_path)
    write(repo, "secret_target.txt", f'K = "{TOKEN}"\n')
    (Path(repo) / ".env").symlink_to(Path(repo) / "secret_target.txt")
    findings, breaches = screen.screen_tree(repo, [".env"])
    assert findings == [], "the link itself is not read through"
    assert any(".env" in b for b in breaches), "an unread .env must fail the run closed"


def test_a_symlink_inside_a_selected_directory_is_a_breach_not_a_silent_skip(tmp_path):
    """The rule the top-level branch already applies, one level down, where it was absent.

    `_walk` used to `continue` on a link leaf in silence while `screen_tree`'s top-level
    branch breached on the identical shape. That gap is not academic: `baseline` selects a
    directory with `git add -f`, which commits a nested link AS A LINK, so it ships to
    every seat. Measured with the `continue` in place — preflight `[]`, breaches `[]`, and
    a seat that read the host's credentials through `scratch/creds` with `verified=True`.

    The target carries a token, so `findings == []` is evidence the link was never
    followed rather than evidence there was nothing behind it.
    """
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "credentials").write_text(f'K = "{TOKEN}"\n')
    write(repo, "scratch/a.py", "ok\n")
    (Path(repo) / "scratch" / "creds").symlink_to(outside / "credentials")
    findings, breaches = screen.screen_tree(repo, ["scratch"])
    assert findings == [], "the link was followed and its target scanned"
    # Whole-string, and identical to the top-level branch's wording: the point of the fix
    # is that a nested path breaks the SAME rule, stated the same way.
    assert breaches == ["scratch/creds: not screened — symlink; links are never followed"]


def test_an_in_tree_link_to_an_unselected_path_is_still_a_breach(tmp_path):
    """D-2, pinned: the screen does NOT discriminate escaping links from in-tree ones.

    The tempting rule — "a link whose normalized target stays under the repository root is
    not a breach" — is unsound in exactly this shape, and this fixture is the counterexample
    it is unsound for. `scratch/creds -> ../.env` normalizes to `.env`, which IS under the
    root, so that rule reports no breach; but `.env` is not in the selection, this pass
    never opens it, and it holds a live token. A clean verdict on an unread `.env` is the
    single worst thing this module can produce.

    The sound rule is "the target is itself something this pass screened", which needs the
    completed target set and the caller's skip config — so it lives in the caller, and
    `screen` stays conservative. This test fails the moment someone implements the tempting
    rule here.
    """
    repo = make_repo(tmp_path)
    write(repo, ".env", f'K = "{TOKEN}"\n')
    write(repo, "scratch/a.py", "ok\n")
    (Path(repo) / "scratch" / "creds").symlink_to(Path("..") / ".env")
    findings, breaches = screen.screen_tree(repo, ["scratch"])
    assert findings == [], "the link was followed and an unselected file scanned through it"
    assert breaches == ["scratch/creds: not screened — symlink; links are never followed"]
    # The target really is inside the root, or the counterexample is not one.
    assert (Path(repo) / "scratch" / "creds").resolve() == (Path(repo) / ".env").resolve()


def test_a_symlinked_directory_inside_a_selection_is_reported_too(tmp_path):
    """A linked DIRECTORY arrives in os.walk's `dirnames`, never in `filenames`, so a
    leaf-only check leaves it neither walked nor reported — while git still commits it as
    a link the seat can read straight through. `.venv/lib64 -> lib` is this shape."""
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text(f'K = "{TOKEN}"\n')
    write(repo, "scratch/a.py", "ok\n")
    (Path(repo) / "scratch" / "linkdir").symlink_to(outside, target_is_directory=True)
    findings, breaches = screen.screen_tree(repo, ["scratch"])
    assert findings == [], "must not read through a link out of the tree"
    assert breaches == ["scratch/linkdir: not screened — symlink; links are never followed"]


def test_a_fifo_under_a_selected_directory_breaches_instead_of_blocking_forever(tmp_path):
    """`open("rb")` on a FIFO blocks until a writer appears, and nothing in this call path
    has a timeout — so `screen_tree` never returned at all. Measured before the guard: a
    5s alarm fired inside the call, and without an alarm the suite wedges.

    The deadline is what makes this a FAILING test rather than a hung one. A socket is
    checked in the same fixture because it fails the other way — `open` raises ENXIO, a
    class the `(findings, breaches)` contract has no room for — and one `S_ISREG` test
    closes both. `snapshot._special_entry` already documents the identical hazard.

    The readable sibling carries a token so the walk is proved to have continued past the
    two refusals rather than aborted at the first.
    """
    repo = make_repo(tmp_path)
    write(repo, "run/live.py", f'K = "{TOKEN}"\n')
    os.mkfifo(Path(repo) / "run" / "pipe")
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(Path(repo) / "run" / "app.sock"))
        findings, breaches = _within(10, screen.screen_tree, repo, ["run"])
    finally:
        sock.close()
    assert [f.path for f in findings] == ["run/live.py"], "the readable file was not screened"
    assert sorted(breaches) == [
        "run/app.sock: not screened — not a regular file or directory",
        "run/pipe: not screened — not a regular file or directory",
    ]


def test_a_selected_path_that_does_not_exist_is_reported(tmp_path):
    repo = make_repo(tmp_path)
    findings, breaches = screen.screen_tree(repo, ["does/not/exist"])
    assert findings == []
    assert any("does/not/exist" in b for b in breaches), \
        "a path that was never opened may not come back clean"


def test_max_files_breach_returns_no_findings_only_a_breach(tmp_path):
    """A partial scan is never handed back as if it were complete."""
    repo = make_repo(tmp_path)
    write(repo, "pkg/a.py", "ok\n")
    write(repo, "pkg/b.py", f'K = "{TOKEN}"\n')
    q = storage.Quota(max_files=1, max_file_bytes=10_000, max_total_bytes=10_000)
    findings, breaches = screen.screen_tree(repo, ["pkg"], quota=q)
    assert findings == [], "no findings may be reported from a scan that never ran"
    assert any("files:" in b for b in breaches)


def test_an_allowlisted_match_does_not_mask_a_live_one_on_the_same_line(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "cfg.py", f'DEMO = "{ALLOWLISTED}"  # real: {TOKEN}\n')
    findings, breaches = screen.screen_tree(repo, ["cfg.py"])
    assert breaches == []
    assert [(f.path, f.line) for f in findings] == [("cfg.py", 1)]


def test_bare_high_risk_names_are_flagged(tmp_path):
    """The exact-match branch of _is_high_risk_name; `.env.local` covers only startswith."""
    repo = make_repo(tmp_path)
    write(repo, ".env", "NOTHING_MATCHING_A_PATTERN=1\n")
    write(repo, "id_rsa", "not actually a key\n")
    findings, _ = screen.screen_tree(repo, [".env", "id_rsa"])
    assert sorted(f.path for f in findings if f.pattern == "high-risk-filename") == \
        [".env", "id_rsa"]


def test_the_git_object_store_is_not_walked(tmp_path):
    repo = make_repo(tmp_path)
    (Path(repo) / ".git" / "leak.txt").write_text(f'K = "{TOKEN}"\n')
    findings, breaches = screen.screen_tree(repo, ["."])
    assert breaches == []
    assert findings == [], "the object store the baseline came from is not worth decoding"


def test_absolute_selected_path_is_a_breach_not_a_crash(tmp_path):
    """`root / "/etc/hostname"` IS `/etc/hostname`: an absolute right-hand side REPLACES
    the root rather than joining to it, silently and without raising. The selection then
    reaches a host file the baseline never contained — which this module opens and scans —
    and only `relative_to(root)` notices, by raising a bare ValueError out of a function
    whose whole contract is to return (findings, breaches).

    The target carries a token, so `findings == []` is evidence it was never opened, not
    merely evidence that nothing interesting was there.
    """
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(f'K = "{TOKEN}"\n')
    findings, breaches = screen.screen_tree(repo, [str(outside)])
    assert findings == [], "an absolute selection must never be opened"
    # Matched WHOLE, not by substring. `any("absolute" in b …)` passed here for a reason
    # that had nothing to do with the code: pytest derives tmp_path's basename from the test
    # function's own name, so the word arrived inside the path being reported back. The
    # breach must state the rule that was broken.
    assert breaches == [f"{outside}: not screened — an absolute path is not a "
                        "repo-relative selection"]


def test_a_dotdot_selection_cannot_escape_the_root(tmp_path):
    """The escape nothing downstream notices.

    `../outside.txt` is not absolute, so an isabs-only guard passes it through; `root / rel`
    then lands outside the tree, the file is opened and scanned, and `relative_to(root)`
    — purely LEXICAL — accepts it without raising. The result is a host file read, reported
    with an EMPTY breach list, by the module whose contract is never to scan less than it
    claimed to. The absolute case at least crashed.
    """
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(f'K = "{TOKEN}"\n')
    findings, breaches = screen.screen_tree(repo, ["../outside.txt"])
    assert findings == [], "a selection escaping the root was opened and scanned"
    assert breaches == ["../outside.txt: not screened — a '..' component escapes the "
                        "repository root; selections must be repo-relative"]


def test_the_skipped_path_is_named_so_coverage_is_not_overstated(tmp_path):
    """A mixed selection must say which half it did not read.

    The screened path holds a token and the skipped one is a link, so the two halves are
    distinguishable in the result: the findings prove the readable file WAS read, and the
    breach names the exact path that was not — rather than a bare "some paths were skipped"
    a caller cannot act on.
    """
    repo = make_repo(tmp_path)
    write(repo, "cfg.py", f'K = "{TOKEN}"\n')
    (Path(repo) / "link").symlink_to("cfg.py")
    findings, breaches = screen.screen_tree(repo, ["cfg.py", "link"])
    assert [f.path for f in findings] == ["cfg.py"], "the readable half was not screened"
    assert [b for b in breaches if b.startswith("link: not screened")] == breaches, \
        "the skipped path must be named individually"
