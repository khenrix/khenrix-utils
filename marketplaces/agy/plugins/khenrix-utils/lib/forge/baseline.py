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
    sidecars: list = field(default_factory=list)
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


def _walk_selected(base: Path, repo: Path) -> list:
    """Repo-relative regular files under a selected DIRECTORY, in `screen._walk`'s terms.

    Same three rules, for the same reasons: `.git` is pruned rather than post-filtered
    (it is the object store the baseline was built from), symlinks are never followed —
    os.walk does not descend into linked directories, so leaves are all that must be
    dropped — and names are sorted so the manifest is deterministic.

    A symlink therefore reaches the tree (git commits it as a link) without reaching the
    manifest. That asymmetry is deliberate: hashing it means `open()` following it, which
    is a read of whatever it points at, and the manifest must not describe content from
    outside the tree it claims to describe.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        d = Path(dirpath)
        dirnames[:] = sorted(n for n in dirnames if n != ".git")
        for n in sorted(filenames):
            q = d / n
            if not q.is_symlink():
                out.append(q.relative_to(repo).as_posix())
    return out


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


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

    base_commit = facts.head
    dirty = bool(facts.staged or facts.unstaged or selected_untracked)

    # Resolved BEFORE anything is written, though it reads more naturally next to
    # commit-tree. Probing after write-tree leaves a refused run's tree and blobs loose in
    # the user's object store, unreachable but present until git's two-week gc grace expires.
    # Fail-closed has to mean nothing was written, not merely nothing was reachable.
    name, email = _resolve_author(repo, author) if dirty else (None, None)

    manifest = {}
    for rel in gitcmd.git(repo, "ls-files", "-z", env_extra=gitcmd.READONLY).stdout.split("\0"):
        if rel and (repo / rel).is_file():
            manifest[rel] = _sha256_file(repo / rel)
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
            continue
        if p.is_dir():
            for sub in _walk_selected(p, repo):
                manifest[sub] = _sha256_file(repo / sub)
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
                    ref=ref, dirty=True, sidecars=[], filesystem_manifest=manifest)
