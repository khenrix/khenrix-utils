"""Preflight is describe-only, and its rejections are scoped to the selected baseline."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import inspect as finspect  # noqa: E402
from forge_fixtures import _env, make_repo, write  # noqa: E402


def _git(repo, *args, check=True):
    """Fixture-setup git, run in the same hermetic environment as `forge_fixtures`.

    Not a convenience wrapper: a developer's global `commit.gpgsign` would fail every setup
    commit below, and a global `rerere.enabled` would silently auto-resolve the merge in
    `test_rejects_unmerged_index` — turning the one test that proves the unmerged rejection
    fires into a test that proves nothing.
    """
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=30, env=_env())
    if check and r.returncode != 0:
        raise AssertionError(f"fixture setup failed: git {' '.join(args)}\n{r.stderr}")
    return r


def _index_sha(repo):
    return hashlib.sha256((Path(repo) / ".git" / "index").read_bytes()).hexdigest()


def test_facts_classify_staged_unstaged_untracked(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "tracked.txt", "v1\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "add")
    write(repo, "tracked.txt", "v2\n")                 # unstaged modification
    write(repo, "staged.txt", "s\n")
    _git(repo, "add", "staged.txt")
    write(repo, "loose.txt", "u\n")                    # untracked
    f = finspect.repo_facts(repo)
    assert "tracked.txt" in f.unstaged
    assert "staged.txt" in f.staged
    assert "loose.txt" in f.untracked
    assert len(f.head) == 40


def test_preflight_does_not_touch_the_index(tmp_path):
    """The whole point of describe-only: the user's index bytes must be unchanged."""
    repo = make_repo(tmp_path)
    write(repo, "dirty.txt", "d\n")
    _git(repo, "add", "dirty.txt")
    write(repo, "dirty.txt", "d2\n")                   # stale cache-tree, the risky case
    # A second, different hazard: content that still matches the index but whose stat data
    # no longer does. Git can record that discovery, and a read-oriented command WILL rewrite
    # the index to do so unless GIT_OPTIONAL_LOCKS=0 stops it. Without this file the test
    # passes even if the module forgets gitcmd.READONLY entirely.
    write(repo, "stat_stale.txt", "s\n")
    _git(repo, "add", "stat_stale.txt")
    os.utime(Path(repo) / "stat_stale.txt", (0, 0))
    before = _index_sha(repo)
    finspect.repo_facts(repo)
    assert _index_sha(repo) == before


def test_rejects_unmerged_index(tmp_path):
    repo = make_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "other")
    write(repo, "conflict.txt", "theirs\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "theirs")
    _git(repo, "checkout", "-q", "main")
    write(repo, "conflict.txt", "ours\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ours")
    _git(repo, "merge", "other", check=False)
    f = finspect.repo_facts(repo)
    assert f.unmerged, "fixture did not produce a conflict"
    assert any("unmerged" in r for r in finspect.rejections(f, []))


def test_rejects_submodule_and_sparse_and_shallow(tmp_path):
    repo = make_repo(tmp_path)
    f = finspect.repo_facts(repo)
    assert finspect.rejections(f, []) == []
    assert any("submodule" in r for r in
               finspect.rejections(finspect.replace(f, has_submodules=True), []))
    assert any("sparse" in r for r in
               finspect.rejections(finspect.replace(f, sparse=True), []))
    assert any("shallow" in r for r in
               finspect.rejections(finspect.replace(f, is_shallow=True), []))
    assert any("partial" in r for r in
               finspect.rejections(finspect.replace(f, is_partial=True), []))


def test_rejects_embedded_gitlink_without_gitmodules(tmp_path):
    """`git add` on a directory that is itself a repository — git accepts it with a warning
    and writes a 160000 index entry that no .gitmodules maps. `git submodule status` exits
    128 on exactly that state, so detecting submodules through it turns the commonest gitlink
    into a raw GitError instead of the rejection the plan requires."""
    repo = make_repo(tmp_path)
    sub = Path(repo) / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    _git(sub, "config", "user.email", "fixture@example.invalid")
    _git(sub, "config", "user.name", "Fixture")
    write(sub, "inner.txt", "i\n")
    _git(sub, "add", "inner.txt")
    _git(sub, "commit", "-q", "-m", "inner")
    _git(repo, "add", "sub")
    assert not (Path(repo) / ".gitmodules").exists(), "fixture must be the unmapped variant"
    f = finspect.repo_facts(repo)                      # must not raise
    assert f.has_submodules
    assert any("submodule" in r for r in finspect.rejections(f, []))


def test_rejects_properly_added_submodule(tmp_path):
    """The mapped variant: .gitmodules present AND a 160000 index entry."""
    origin = make_repo(tmp_path, name="origin")
    repo = make_repo(tmp_path, name="host")
    # protocol.file.allow is set with -c, scoped to this one command: since git 2.38.1 a
    # submodule clone over `file://` is refused by default. Setting it globally would relax
    # the developer's own git.
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "sub")
    assert (Path(repo) / ".gitmodules").is_file()
    f = finspect.repo_facts(repo)
    assert f.has_submodules
    assert any("submodule" in r for r in finspect.rejections(f, []))


def test_rejects_shallow_clone(tmp_path):
    origin = make_repo(tmp_path, name="origin")
    write(origin, "second.txt", "2\n")
    _git(origin, "add", "second.txt")
    _git(origin, "commit", "-q", "-m", "second")
    # file:// rather than a plain path: a local-path clone ignores --depth and copies the
    # whole history, so the fixture would silently not be shallow.
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{origin}", "shallow")
    f = finspect.repo_facts(Path(tmp_path) / "shallow")
    assert f.is_shallow
    assert any("shallow" in r for r in finspect.rejections(f, []))


def test_rejects_partial_clone_and_not_an_ordinary_clone(tmp_path):
    """A real `--filter=blob:none` clone, because git 2.53 stopped writing the key the
    original probe reads: the clone gets `remote.origin.promisor`, no `[extensions]` section,
    and so passed preflight completely clean — objects reachable only via a lazy fetch to a
    remote a seat would not have.

    The full clone is the other half of the claim: `--get-regexp remote\\..*\\.promisor` must
    not fire on the ordinary `remote.origin.url` every clone carries.
    """
    origin = make_repo(tmp_path, name="origin")
    _git(origin, "config", "uploadpack.allowFilter", "true")
    _git(tmp_path, "clone", "-q", "--no-checkout", "--filter=blob:none",
         f"file://{origin}", "partial")
    _git(tmp_path, "clone", "-q", f"file://{origin}", "full")

    f = finspect.repo_facts(Path(tmp_path) / "partial")
    assert f.is_partial
    assert any("partial" in r for r in finspect.rejections(f, []))

    n = finspect.repo_facts(Path(tmp_path) / "full")
    assert not n.is_partial, "an ordinary remote must not read as promisor"
    assert finspect.rejections(n, []) == []


def test_rejects_sparse_checkout_cone_and_legacy_config(tmp_path):
    """Cone mode is the modern default and it puts `core.sparseCheckout` in
    `.git/config.worktree`, not `.git/config` — `git sparse-checkout` also turns on
    `extensions.worktreeConfig`. Plain `git config --get` still reads the worktree scope
    (`--local` does NOT), which is the one fact this probe depends on and the reason it
    survived git 2.53 where `extensions.partialClone` did not.
    """
    repo = make_repo(tmp_path)
    write(repo, "keep/k.txt", "k\n")
    write(repo, "drop/d.txt", "d\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "dirs")
    _git(repo, "sparse-checkout", "set", "keep")
    assert not (Path(repo) / "drop").exists(), "fixture is not actually sparse"
    f = finspect.repo_facts(repo)
    assert f.sparse
    assert any("sparse" in r for r in finspect.rejections(f, []))

    # The legacy shape, and the only thing keeping the config probe non-redundant: config
    # alone, with no skip-worktree bit set yet. This is how a sparse checkout is armed before
    # `read-tree` applies it, and what older git wrote.
    legacy = make_repo(tmp_path, name="legacy")
    _git(legacy, "config", "core.sparseCheckout", "true")
    g = finspect.repo_facts(legacy)
    assert g.sparse
    assert any("sparse" in r for r in finspect.rejections(g, []))

    plain = make_repo(tmp_path, name="plain")
    assert not finspect.repo_facts(plain).sparse


def test_rejects_skip_worktree_entry(tmp_path):
    """spec §2.3 rejects skip-worktree state in its own right, and no config key reports it:
    `git update-index --skip-worktree` sets a bit on the index entry and writes nothing to
    config, so a config-only probe reads the repo as ordinary."""
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    assert _git(repo, "config", "--get", "core.sparseCheckout",
                check=False).returncode == 1, "the config probe must not be what fires here"
    f = finspect.repo_facts(repo)
    assert f.sparse
    assert any("skip-worktree" in r for r in finspect.rejections(f, []))


def test_rejects_skip_worktree_entry_that_is_also_assume_unchanged(tmp_path):
    """`ls-files -v` lowercases the tag when the entry is ALSO assume-unchanged, so a
    skip-worktree path can be reported as `s` rather than `S`. Matching only the uppercase
    form lets exactly this repository through preflight clean."""
    repo = make_repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "seed.txt")
    _git(repo, "update-index", "--assume-unchanged", "seed.txt")
    tags = _git(repo, "ls-files", "-v").stdout.split()
    assert "s" in tags, f"fixture must produce the lowercase tag, got {tags}"
    f = finspect.repo_facts(repo)
    assert f.sparse
    assert any("skip-worktree" in r for r in finspect.rejections(f, []))


def test_unborn_head_is_rejected_not_raised(tmp_path):
    """A freshly init-ed repository is an ordinary state; `rev-parse HEAD` exits 128 on it."""
    repo = Path(tmp_path) / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    f = finspect.repo_facts(repo)                      # must not raise
    assert f.head == ""
    assert any("unborn" in r for r in finspect.rejections(f, []))


def test_nested_repo_is_reported_only_when_selected(tmp_path):
    """This repo carries leaked agy worktrees under gitignored eval workspaces; an
    unscoped structural sweep would abort every run on artifacts nobody created."""
    repo = make_repo(tmp_path)
    write(repo, ".gitignore", "workspace/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    nested = Path(repo) / "workspace" / "inner"
    nested.mkdir(parents=True)
    (nested / ".git").write_text("gitdir: /elsewhere/worktrees/x\n")   # a .git FILE
    f = finspect.repo_facts(repo)
    assert finspect.rejections(f, []) == [], "unselected nested repo must not block"
    hits = finspect.rejections(f, ["workspace/inner"])
    assert any("nested repository" in h for h in hits)


def test_rejects_escaping_symlink_only_when_selected(tmp_path):
    repo = make_repo(tmp_path)
    (Path(repo) / "out").symlink_to("/etc/passwd")
    f = finspect.repo_facts(repo)
    assert finspect.rejections(f, []) == []
    assert any("symlink" in h for h in finspect.rejections(f, ["out"]))


def test_rejects_an_escaping_symlink_NESTED_in_a_selected_directory(tmp_path):
    """The top-level path is not the boundary the baseline respects.

    A selected DIRECTORY is swept into the tree by `git add -f`, which commits a nested
    link as a link — so it reaches every seat. The loop used to test only the top-level
    `rel`. Measured before this walk existed: `rejections() == []`, `screen` breaches
    `[]`, and a seat that read the host's AWS credentials with `verified=True`. A selected
    `.venv` is enough to produce the shape (`bin/python -> /usr/bin/python3`).

    A linked DIRECTORY is asserted alongside the linked file because os.walk reports it in
    `dirnames` only — a `filenames`-only check would miss `lib64 -> /usr/lib64` entirely.
    """
    repo = make_repo(tmp_path)
    scratch = Path(repo) / "scratch"
    (scratch / "sub").mkdir(parents=True)
    (scratch / "inside.txt").write_text("ordinary\n")
    (scratch / "creds").symlink_to("/etc/passwd")
    (scratch / "linkdir").symlink_to("/etc", target_is_directory=True)
    # Points back INTO the repository, so it must NOT be rejected: a rule that fires on
    # every link refuses every repo that uses one, which is the failure §4 warns against.
    (scratch / "sub" / "alias.txt").symlink_to("../inside.txt")

    f = finspect.repo_facts(repo)
    assert finspect.rejections(f, []) == [], "unselected: nothing to reject"
    # Whole-list, in walk order (dirnames before filenames, each sorted): a substring
    # check would pass on the in-tree alias too and certify nothing about the
    # discrimination.
    assert finspect.rejections(f, ["scratch"]) == [
        "symlink escapes the repository: scratch/linkdir -> /etc",
        "symlink escapes the repository: scratch/creds -> /etc/passwd",
    ]


# The three probes below pin the parsing that had to be adapted to git 2.53's real output
# (see the comments at each site in inspect.py). Two of them are rejections, and an unproven
# rejection is a rejection that will not fire.

def test_a_rename_does_not_smuggle_a_phantom_path(tmp_path):
    """With rename detection on, porcelain -z emits the old path as a bare extra record —
    no status code, no leading space — so a reader that assumes 'XY path' carves a phantom
    filename out of it. `--no-renames` removes the special case at the source."""
    repo = make_repo(tmp_path)
    _git(repo, "mv", "seed.txt", "renamed.txt")
    f = finspect.repo_facts(repo)
    # sorted(): the claim is that no phantom `d.txt` was carved out of the bare old-path
    # record, not that git emits the pair in a particular order.
    assert sorted(f.staged) == ["renamed.txt", "seed.txt"]   # the add and the delete, both whole
    assert f.unstaged == [] and f.untracked == []


def test_intent_to_add_is_detected_and_rejected(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "half.txt", "not really staged\n")
    _git(repo, "add", "-N", "half.txt")
    f = finspect.repo_facts(repo)
    assert f.intent_to_add == ["half.txt"]
    assert "half.txt" not in f.untracked, "an index entry is not an untracked path"
    assert any("intent-to-add" in r for r in finspect.rejections(f, []))


def test_gitattributes_filter_is_detected_and_rejected(tmp_path):
    repo = make_repo(tmp_path)
    # check-attr's plain output is one `<path>: <attr>: <value>` line per query, with
    # core.quotePath escaping — this name comes back double-quoted with a two-character
    # backslash-n in place of the newline, so a line reader reports a path that does not
    # exist. The NUL-delimited form emits the raw bytes, so the reported name is the real one.
    write(repo, "nl\nname.bin", "x\n")
    write(repo, "plain.txt", "y\n")
    write(repo, ".gitattributes", "*.bin filter=lfs\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "attrs")
    f = finspect.repo_facts(repo)
    assert f.filtered_paths == ["nl\nname.bin"]
    assert any("filter" in r for r in finspect.rejections(f, []))


def test_rejects_a_worktree_whose_line_endings_a_checkout_would_rewrite(tmp_path):
    """The gap that produced a `SeatError` three stages after preflight said `[]`.

    `baseline`'s manifest hashes raw worktree bytes while the seat's checkout re-runs the
    smudge, so they disagree whenever the worktree is not already in the state a checkout
    produces. `b.txt` is the measured case: `eol=crlf` demands CRLF, someone edited it in
    a Linux editor and left LF.

    `a.txt` is the control — attribute-conformant CRLF, which round-trips and must NOT be
    rejected, or the check would refuse every repository carrying the `*.bat text eol=crlf`
    boilerplate rather than the broken ones.
    """
    repo = make_repo(tmp_path)
    write(repo, ".gitattributes", "*.txt text eol=crlf\n")
    (Path(repo) / "a.txt").write_bytes(b"conformant\r\n")
    (Path(repo) / "b.txt").write_bytes(b"edited on linux\n")
    # seed.txt is LF and `*.txt` catches it too, so it is a second genuine hit.
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "attrs")
    f = finspect.repo_facts(repo)
    assert "b.txt" in f.eol_mismatched_paths
    assert "a.txt" not in f.eol_mismatched_paths, "a conformant worktree round-trips"
    assert any("round-trip" in r for r in finspect.rejections(f, []))


def test_plain_eol_normalization_is_supported_not_rejected(tmp_path):
    """Spec §2.3: "Plain EOL normalization *is* supported."

    `* text=auto` is the most common line in any .gitattributes, and measured end to end
    it produces a correct seat (`verified is True`). Rejecting on the attribute alone
    would fail nearly every repository for an infrastructure reason — the §4 outcome this
    check exists to avoid — so the ubiquitous case is pinned as explicitly as the broken
    one.

    The PNG is the reason this is not a one-line rule: `text=auto` reports `w/-text` for
    it, exactly as an explicit `text eol=crlf` does, and only the first of the two is
    byte-identical in the seat (both measured). The next test pins the other side.
    """
    repo = make_repo(tmp_path)
    write(repo, ".gitattributes", "* text=auto\n")
    (Path(repo) / "c.txt").write_bytes(b"one\ntwo\n")
    (Path(repo) / "e.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02")
    (Path(repo) / "oneline.txt").write_bytes(b"no trailing newline")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "attrs")
    f = finspect.repo_facts(repo)
    assert f.eol_mismatched_paths == []
    assert finspect.rejections(f, []) == []


def test_an_explicit_text_attribute_on_binary_content_is_still_a_mismatch(tmp_path):
    """The other side of `w/-text`, and the reason the skip is keyed on the attribute.

    `text=auto` lets git's binary detection suppress conversion; an explicit `text` does
    not. Measured on the SAME PNG bytes: `text=auto` gave a byte-identical seat, while
    `*.png text eol=crlf` gave `SeatError: seat content differs from the baseline
    manifest`. A rule that skipped every `w/-text` row would miss this one.
    """
    repo = make_repo(tmp_path)
    write(repo, ".gitattributes", "*.png text eol=crlf\n")
    (Path(repo) / "e.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "attrs")
    f = finspect.repo_facts(repo)
    assert f.eol_mismatched_paths == ["e.png"]
    assert any("round-trip" in r for r in finspect.rejections(f, []))


def test_rejects_a_tracked_escaping_symlink_without_being_selected(tmp_path):
    """§2.3 scopes its rejections to "tracked content plus the paths the user selected",
    and a tracked escaping symlink is the first half — it needs no selection to reach the
    baseline. `materialize`'s `ls-files` loop guards on `is_file()`, which follows the
    link, so the manifest ends up carrying a digest of content from OUTSIDE the repository.
    """
    repo = make_repo(tmp_path)
    (Path(repo) / "out").symlink_to("/etc/passwd")
    _git(repo, "add", "out")
    _git(repo, "commit", "-q", "-m", "tracked link")
    f = finspect.repo_facts(repo)
    assert f.escaping_symlinks == ["out"]
    assert any("escapes the repository" in r for r in finspect.rejections(f, []))


def test_a_tracked_symlink_inside_the_repository_is_not_an_escape(tmp_path):
    """The rejection must discriminate, or it refuses every repo that uses a symlink."""
    repo = make_repo(tmp_path)
    (Path(repo) / "alias.txt").symlink_to("seed.txt")
    _git(repo, "add", "alias.txt")
    _git(repo, "commit", "-q", "-m", "inside link")
    f = finspect.repo_facts(repo)
    assert f.escaping_symlinks == []
    assert finspect.rejections(f, []) == []


def test_a_generator_contract_refuses_a_shape_the_manifest_could_not_carry():
    """The one invariant that spans two modules: `bundle`'s `generator_contract_id` reads ""
    as "this run declared no contract, admit nothing", so a contract that admits paths under
    an empty id would be enforced one way at the gate and recorded the other way."""
    with pytest.raises(ValueError, match="fail-closed sentinel"):
        finspect.GeneratorContract(id="", relations=(("shared/**", "marketplaces/**"),))
    # A single relation IS a pair, so the outer tuple is easy to forget — and without it
    # `"shared/**"` unpacks into nine characters somewhere much later.
    with pytest.raises(ValueError, match="tuple OF pairs"):
        finspect.GeneratorContract(id="x", relations=("shared/**", "marketplaces/**"))
    # The string test is not subsumed by the arity one, and this is the case that shows it:
    # a TWO-character string unpacks into a pair of one-character strings and would be
    # accepted as the relation `("a", "b")` — a glob nobody wrote, admitting a file named b.
    with pytest.raises(ValueError, match="tuple OF pairs"):
        finspect.GeneratorContract(id="x", relations=("ab",))
    for bad in (("a", "b", "c"), ("a",), 42):
        with pytest.raises(ValueError, match="tuple OF pairs"):
            finspect.GeneratorContract(id="x", relations=(bad,))
    with pytest.raises(ValueError, match="must be strings"):
        finspect.GeneratorContract(id="x", relations=((1, "b"),))
    # An empty output glob matches no path at all, so it reads as a relation and admits
    # nothing — the shape that looks like a contract and is not one.
    with pytest.raises(ValueError, match="empty output glob"):
        finspect.GeneratorContract(id="x", relations=(("a/*", ""),))
    assert finspect.GeneratorContract(id="x", relations=()).relations == ()


def test_generator_detection_proposes_nothing_it_cannot_read_from_a_declaration(tmp_path):
    """Fail-closed by default, and on THIS repository too — the §7.2 counterexample, whose
    own `make verify` runs `render`.

    ROOT is asserted next to a fixture repo because it is the one tree where a detector is
    tempted to guess. The relation there is real, but it is expressed in `scripts/render.py`
    as Python rather than as data, and the coarse `marketplaces/**` glob that looks like it
    would over-admit five hand-maintained manifests render never writes. If a machine-
    readable declaration is ever added, this is the assertion that has to be re-earned
    rather than quietly widened.
    """
    for repo in (make_repo(tmp_path), ROOT):
        c = finspect.detect_generators(repo)
        assert (c.id, c.relations) == ("", ())
