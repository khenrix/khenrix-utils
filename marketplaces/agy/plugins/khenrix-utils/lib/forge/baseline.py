"""The composite baseline B (spec §2).

B is not one git OID. A tree cannot represent empty directories, full POSIX modes, or
ignored sidecars, and every downstream consumer (clone, worktree) needs a COMMIT. So B
carries: the base commit, the tracked tree, the synthetic commit B1 that anchors execution,
the sidecar list, and a filesystem manifest to validate materialization against.

B1 and the "synthetic anchor" are ONE commit. On a clean tree no commit is created at all
and B1 == base_commit. When the tree is dirty, B1 is authored by the USER with a message
saying exactly what it is — because the synthesis branch is rooted here, and merging the
deliverable would otherwise commit their scratch work as forge's (spec §2.1).

This is the first module allowed to write: it creates objects and one ref. It still never
writes the USER's index. `git write-tree` takes index.lock unconditionally and rewrites a
stale cache-tree extension — and "stale" is precisely the dirty tree forge exists for — so
every index-touching command here runs under GIT_INDEX_FILE pointing at a private copy.
`gitcmd.git` applies `env_extra` LAST, after scrubbing the redirecting variables, which is
what makes that override both possible and safe.

Identity is the ORCHESTRATOR's to resolve, not this module's. `gitcmd` pins
GIT_CONFIG_GLOBAL to /dev/null, so the probe below sees repo-LOCAL config only and is blind
to an identity in ~/.gitconfig — the normal place for one. When it comes back empty this
module RAISES rather than substitute a placeholder: B1 roots the branch the user is asked to
merge, forge's own commits are authored `llm-forge`, and a fabricated third name is a FALSE
attribution that outlives the run in `git log`, `git blame` and `--author` filters with no
signal at the point it was decided. A missing author is recoverable; a wrong one is not.

The caller should resolve identity once at the consent gate — a single deliberate
`git var GIT_AUTHOR_IDENT` outside this hardened path — and DISPLAY it as part of what the
user consents to, because its output is possibly-a-guess that must never be trusted
silently. Measured on git 2.53: with `user.name` set and no `user.email` it returns
`Configured <khenrix@Surface-Book-2.localdomain>` at rc=0, the email invented from
user@hostname with nothing marking which half was guessed. It is not infallible in the
other direction either — with no name and an empty gecos field it exits 128
(`empty ident name`), so a caller cannot assume the call always yields an answer.
"""
import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import gitcmd


class BaselineError(RuntimeError):
    """B cannot be built honestly from what the caller supplied."""


@dataclass(frozen=True)
class Baseline:
    base_commit: str
    tracked_tree_oid: str
    commit: str                      # B1; == base_commit when the tree is clean
    ref: str
    dirty: bool
    # None, never []. Spec §2's sidecar manifest — declared ignored inputs, empty
    # directories, special files — has no producer yet, and a later plan consumes this field
    # as authoritative: an empty list would tell it "there are none" when the truth is
    # "nobody looked", which is the fail-OPEN reading of the two.
    sidecars: list | None = None
    filesystem_manifest: dict = field(default_factory=dict)


def _resolve_author(repo, author):
    """(name, email) for B1, or raise. Never guesses — see the module docstring."""
    if author is not None:
        return author
    name = gitcmd.git(repo, "config", "--get", "user.name",
                      env_extra=gitcmd.READONLY, check=False).stdout.strip()
    email = gitcmd.git(repo, "config", "--get", "user.email",
                       env_extra=gitcmd.READONLY, check=False).stdout.strip()
    if not (name and email):
        raise BaselineError(
            "cannot author B1: this repository has no local user.name/user.email, and "
            "global config is disabled on every call this package makes. Resolve the "
            "user's identity at the consent gate and pass author=(name, email). "
            "Refusing to substitute a placeholder — B1 is history the user is asked to "
            "merge, so a fabricated author would be a permanent false attribution.")
    return name, email


def _index_sha(repo) -> str:
    """sha256 of the index git would use for `repo`, or "" when there is no index file.

    The git dir is ASKED FOR, never joined onto `.git`: in a linked worktree that is a
    FILE, the joined path does not exist, `is_file()` answers False without raising, and
    this returns "" for a repository that has a perfectly good index. `inspect.repo_facts`
    computes the value being compared the same way, so the two must resolve the location
    the same way too — and the guard below compares "" rather than tolerating it, so a slip
    here fails the run instead of quietly disarming the check.

    `check=False` and a BaselineError, because this module's documented failure mode is
    BaselineError: a raw GitError out of a helper the caller cannot see is not one.
    """
    r = gitcmd.git(repo, "rev-parse", "--absolute-git-dir",
                   env_extra=gitcmd.READONLY, check=False)
    if r.returncode != 0:
        raise BaselineError(
            "cannot locate the git directory, so the mid-snapshot drift check cannot run: "
            f"{r.stderr.strip()}")
    idx = Path(r.stdout.strip()) / "index"
    return hashlib.sha256(idx.read_bytes()).hexdigest() if idx.is_file() else ""


def _walk_selected(base: Path, repo: Path) -> list:
    """Repo-relative leaves under a selected DIRECTORY, in `screen._walk`'s terms.

    Same rules, for the same reasons: `.git` is pruned rather than post-filtered (it is the
    object store the baseline was built from), symlinks are never FOLLOWED — os.walk under
    `followlinks=False` does not descend into a linked directory — and names are sorted so
    the manifest is deterministic.

    A link is reported rather than dropped, and a linked DIRECTORY too: it arrives in
    `dirnames`, never in `filenames`, so both lists are inspected exactly as `screen._walk`
    and `inspect._escaping_links_under` inspect both. `git add -f` commits either AS A LINK,
    so a dropped one is in the tree with nothing in the manifest describing it — the
    third-outcome gap `tests/test_forge_seams.py` exists to rule out. `_entry_digest` reads
    the link's target TEXT, so reporting it here still never opens what it points at.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        d = Path(dirpath)
        dirnames[:] = sorted(n for n in dirnames if n != ".git")
        for n in list(dirnames):
            q = d / n
            if q.is_symlink():
                dirnames.remove(n)
                out.append(q.relative_to(repo).as_posix())
        for n in sorted(filenames):
            out.append((d / n).relative_to(repo).as_posix())
    return out


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_link(p: Path) -> str:
    """A symlink's identity: the sha256 of its TARGET TEXT, never of the target's content.

    Byte-for-byte `snapshot._symlink_entry`'s digest — including the strict `.encode()`,
    which is what makes the two comparable — and mirrored again in `fleet._sha256_link`,
    which checks this manifest against a seat.
    """
    return hashlib.sha256(os.readlink(p).encode()).hexdigest()


def _entry_digest(p: Path) -> str:
    """The manifest value for one path. The link test comes FIRST because `_sha256_file`
    opens THROUGH a link, which would describe content from outside the tree the manifest
    claims to describe."""
    return _sha256_link(p) if p.is_symlink() else _sha256_file(p)


def materialize(repo, run_dir, facts, selected_untracked: list, run_id: str,
                author=None) -> Baseline:
    """Build B. Creates objects and a ref in the user's repo; touches nothing else.

    `facts` is AUTHORITATIVE for location: work happens at `facts.root`, and `repo` only has
    to name the same repository — a subdirectory of it is fine. Naming a different one
    raises rather than quietly building A's baseline because the caller passed B.

    `author` is the (name, email) recorded on B1. When it is None the repository's own
    user.name/user.email are probed, and `BaselineError` is raised if either is missing —
    see the module docstring for why this refuses to guess.
    """
    run_dir = Path(run_dir)
    # Every path here is worktree-ROOT-relative, so the root is where commands must run.
    # `ls-files` reports relative to cwd while `add -u -- :/` is root-relative magic: given a
    # SUBDIRECTORY those two disagree, and the result is a root-scoped tree paired with a
    # subdirectory-scoped, wrongly-keyed manifest — returned as success. Since the manifest
    # is what downstream validates materialization against, that is silent corruption, so
    # `facts.root` wins over the argument. Deferring to it that way would also swallow a
    # caller who paired the wrong facts with the wrong repo, so the two are checked to agree
    # first: same repository, any directory within it.
    top = Path(gitcmd.git(repo, "rev-parse", "--show-toplevel",
                          env_extra=gitcmd.READONLY).stdout.strip()).resolve()
    repo = Path(facts.root).resolve()
    if top != repo:
        raise BaselineError(
            f"repo and facts describe different repositories: {top} vs facts.root {repo}. "
            "B would be built from one and attributed to the other.")

    # §2.2: "abort if the source moved mid-snapshot". The describe pass recorded the index
    # hash; if it moved between then and now, a concurrent editor or IDE wrote the index and
    # B would describe a tree nobody asked for. index_sha had a producer and no consumer
    # until this check.
    #
    # FIRST, before the manifest and long before `add` — the abort has to mean nothing was
    # written, not merely that nothing became reachable, for the same reason the identity
    # probe was hoisted above write-tree: a refused run's blobs and tree otherwise sit loose
    # in the user's object store until git's two-week gc grace expires.
    # STRICT: `is not None` is the only opt-out, and "" compares like any other value.
    # Tolerating an empty hash on either side would look like defensiveness and be the
    # opposite — `idx_now == ""` is the exact signature of a git dir resolved wrongly, the
    # bug this package has shipped twice, so the tolerant form turns a path slip into a
    # silently unguarded baseline that only a bespoke regression test would ever catch.
    # None means the caller did not measure; "" means it measured and there was no index.
    idx_now = _index_sha(repo)
    if facts.index_sha is not None and facts.index_sha != idx_now:
        raise BaselineError(
            "the repository index moved between preflight and baseline construction "
            f"({facts.index_sha[:12] or '<no index>'} -> {idx_now[:12] or '<no index>'}); "
            "re-run preflight")

    base_commit = facts.head
    dirty = bool(facts.staged or facts.unstaged or selected_untracked)

    # Resolved BEFORE anything is written, though it reads more naturally next to
    # commit-tree. Probing after write-tree leaves a refused run's tree and blobs loose in
    # the user's object store, unreachable but present until git's two-week gc grace expires.
    # Fail-closed has to mean nothing was written, not merely nothing was reachable.
    name, email = _resolve_author(repo, author) if dirty else (None, None)

    manifest = {}
    for rel in gitcmd.git(repo, "ls-files", "-z", env_extra=gitcmd.READONLY).stdout.split("\0"):
        # `is_file()` FOLLOWS a link, so this loop used to give a tracked symlink the digest
        # of its target's CONTENT — the one thing `_walk_selected` refuses to do for a
        # selected directory, in the same manifest, for the reason that it "must not
        # describe content from outside the tree it claims to describe". `screen` breached
        # on the entry and `snapshot` digested the target TEXT, so B, F0 and the screen held
        # three different opinions about one path and `fleet` skipped it rather than choose.
        # `_entry_digest` settles it: a link is its target text, everywhere (Plan D, D-1).
        # A DANGLING tracked link now enters the manifest too, where `is_file()` dropped it.
        p = repo / rel
        if rel and (p.is_symlink() or p.is_file()):
            manifest[rel] = _entry_digest(p)
    # A selected path may be a DIRECTORY — spec §2.2 contemplates one explicitly, and the
    # literal pathspec below sweeps its whole contents into the tree. An `is_file()`-only
    # guard therefore returns a manifest that describes none of that content while the
    # tree carries all of it, and returns it as success: §2.2's validation of the
    # materialized tree and §4's full-manifest assertion both pass over it vacuously.
    # `is_dir()` follows links, so the symlink test comes first — a selected
    # `linkdir -> ~/.ssh` must not be walked (screen_tree refuses it outright).
    for rel in selected_untracked:
        p = repo / rel
        if p.is_symlink():
            # Recorded, not skipped, and by its target text — `git add -f` puts a selected
            # link in the tree, so dropping it left the tree describing a path the manifest
            # did not. `_entry_digest` never walks it: `is_dir()` follows links, which is
            # why this branch still has to come first.
            manifest[rel] = _sha256_link(p)
        elif p.is_dir():
            for sub in _walk_selected(p, repo):
                manifest[sub] = _entry_digest(repo / sub)
        elif p.is_file():
            manifest[rel] = _sha256_file(p)

    if not dirty:
        ref = f"refs/khenrix-forge/{run_id}/base"
        gitcmd.git(repo, "update-ref", ref, base_commit, gitcmd.zero_oid(repo))
        tree = gitcmd.git(repo, "rev-parse", f"{base_commit}^{{tree}}",
                          env_extra=gitcmd.READONLY).stdout.strip()
        return Baseline(base_commit=base_commit, tracked_tree_oid=tree,
                        commit=base_commit, ref=ref, dirty=False,
                        filesystem_manifest=manifest)

    # --- Phase 2: create objects, under an ALTERNATE index only. -------------------
    # A byte copy of the index is a consistent snapshot: git writes the index by atomic
    # rename, so a plain copy is never torn. Absent-or-copied, never an empty file.
    #
    # The git dir is ASKED FOR, not assumed to be `<repo>/.git`. In a linked worktree that
    # is a FILE and the real index lives under `.git/worktrees/<name>/`; assuming the layout
    # leaves the alternate index empty, and `add -u` then finds no tracked entry to update
    # and aborts with a pathspec error that names nothing relevant. `inspect.repo_facts`
    # already resolves the git dir this way — the two modules must agree on which index is
    # the user's, since one hashes it as a tripwire and the other copies it.
    idx = run_dir / "baseline.index"
    git_dir = Path(gitcmd.git(repo, "rev-parse", "--absolute-git-dir",
                              env_extra=gitcmd.READONLY).stdout.strip())
    src_idx = git_dir / "index"
    if src_idx.is_file():
        shutil.copy2(src_idx, idx)
    # GIT_LITERAL_PATHSPECS is pinned OFF, not merely left unset: an ambient `1` (a caller
    # already defending its own pathspecs) turns `:/` below from magic into a directory name
    # and `add -u` dies with a pathspec error that names the symptom, not the cause.
    env = {**gitcmd.READONLY, "GIT_INDEX_FILE": str(idx), "GIT_LITERAL_PATHSPECS": "0"}

    # `:/` is pathspec MAGIC (repo-root-relative) — it must not fall inside the literal
    # scope below, or `add -u` would look for a directory named ":/".
    gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "add", "-u", "--", ":/", env_extra=env)

    if selected_untracked:
        # Literal pathspecs from a NUL file: globs, leading dashes and newlines in names
        # are all taken as themselves, and never reach the option parser. GIT_LITERAL_PATHSPECS
        # is load-bearing, not belt-and-braces: without it `weird[1].txt` is a character class
        # that also matches `weird1.txt`, sweeping an UNSELECTED file into the baseline.
        spec = run_dir / "selected.pathspec"
        spec.write_bytes(b"\0".join(p.encode() for p in selected_untracked) + b"\0")
        gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "add", "-f",
                   f"--pathspec-from-file={spec}", "--pathspec-file-nul",
                   env_extra={**env, "GIT_LITERAL_PATHSPECS": "1"})

    tree = gitcmd.git(repo, "write-tree", env_extra=env).stdout.strip()

    msg = ("forge: snapshot of your uncommitted working tree\n\n"
           "This commit is yours, not forge's. It exists so every seat starts from the "
           "same tree you were looking at. Forge's own work stacks on top of it.")
    # ONLY the four identity variables go in env_extra. Splatting os.environ here would
    # re-inject every redirector gitcmd just scrubbed — with an ambient GIT_DIR the commit
    # is written into ANOTHER repository and the returned OID does not exist in this one.
    commit = gitcmd.git(
        repo, "commit-tree", tree, "-p", base_commit, "-m", msg,
        env_extra={"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                   "GIT_COMMITTER_NAME": "llm-forge",
                   "GIT_COMMITTER_EMAIL": "forge@khenrix.invalid"}).stdout.strip()

    ref = f"refs/khenrix-forge/{run_id}/base"
    # update-ref immediately: until the ref exists the commit is unreachable and a
    # concurrent `git gc --prune=now` can drop it.
    gitcmd.git(repo, "update-ref", ref, commit, gitcmd.zero_oid(repo))

    return Baseline(base_commit=base_commit, tracked_tree_oid=tree, commit=commit,
                    ref=ref, dirty=True, filesystem_manifest=manifest)
