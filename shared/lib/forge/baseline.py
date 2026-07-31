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
"""
import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import gitcmd


@dataclass(frozen=True)
class Baseline:
    base_commit: str
    tracked_tree_oid: str
    commit: str                      # B1; == base_commit when the tree is clean
    ref: str
    dirty: bool
    sidecars: list = field(default_factory=list)
    filesystem_manifest: dict = field(default_factory=dict)


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def materialize(repo, run_dir, facts, selected_untracked: list, run_id: str,
                author=None) -> Baseline:
    """Build B. Creates objects and a ref in the user's repo; touches nothing else.

    `author` overrides the (name, email) recorded on B1. The default probes the repository's
    own user.name/user.email, which sees repo-LOCAL config only: `gitcmd.git` pins
    GIT_CONFIG_GLOBAL to /dev/null, so an identity that lives in ~/.gitconfig does not
    resolve and B1 falls back to an explicit `unknown` sentinel. A caller that knows who the
    user is should pass `author=` rather than rely on the probe.
    """
    repo, run_dir = Path(repo), Path(run_dir)
    base_commit = facts.head
    dirty = bool(facts.staged or facts.unstaged or selected_untracked)

    manifest = {}
    for rel in gitcmd.git(repo, "ls-files", "-z", env_extra=gitcmd.READONLY).stdout.split("\0"):
        if rel and (repo / rel).is_file():
            manifest[rel] = _sha256_file(repo / rel)
    for rel in selected_untracked:
        if (repo / rel).is_file():
            manifest[rel] = _sha256_file(repo / rel)

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
    env = {**gitcmd.READONLY, "GIT_INDEX_FILE": str(idx)}

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
        gitcmd.git(repo, "add", "-f", f"--pathspec-from-file={spec}", "--pathspec-file-nul",
                   env_extra={**env, "GIT_LITERAL_PATHSPECS": "1"})

    tree = gitcmd.git(repo, "write-tree", env_extra=env).stdout.strip()

    if author is None:
        name = gitcmd.git(repo, "config", "--get", "user.name",
                          env_extra=gitcmd.READONLY, check=False).stdout.strip() or "unknown"
        email = gitcmd.git(repo, "config", "--get", "user.email",
                           env_extra=gitcmd.READONLY,
                           check=False).stdout.strip() or "unknown@invalid"
    else:
        name, email = author
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
