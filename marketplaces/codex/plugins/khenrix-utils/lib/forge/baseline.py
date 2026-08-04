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
writes the USER's index, and never fires their hooks — every call below that writes an index
or a ref carries `gitcmd.NO_HOOKS`, on the decision recorded there. `git write-tree` takes
index.lock unconditionally and rewrites a stale cache-tree extension — and "stale" is
precisely the dirty tree forge exists for — so every index-touching command here runs under
GIT_INDEX_FILE pointing at a private copy.
`gitcmd.git` applies `env_extra` LAST, after scrubbing the redirecting variables, which is
what makes that override both possible and safe.

Identity is the ORCHESTRATOR's to resolve, not this module's. `gitcmd` pins
GIT_CONFIG_GLOBAL to /dev/null, so the probe below sees repo-LOCAL config only and is blind
to an identity in ~/.gitconfig — the normal place for one. When it comes back empty this
module RAISES rather than substitute a placeholder: B1 roots the branch the user is asked to
merge, forge's own commits are authored `llm-forge`, and a fabricated third name is a FALSE
attribution that outlives the run in `git log`, `git blame` and `--author` filters with no
signal at the point it was decided. A missing author is recoverable; a wrong one is not.

The caller resolves identity once at the consent gate, and `gate.propose_identity` is that
caller: a single deliberate read outside this hardened path, whose answer `gate.confirm`
records and `gate.Confirmation` refuses to hold in an unusable shape.

DISPLAYING it is an obligation nothing in this package discharges, said here because this
paragraph asserted the opposite and a comment claiming a defence that does not exist is worse
than no comment. `gate.must_show` renders the price, the gaps and the gate surface and no
identity at all — it is handed no author to render — so the operator can consent to a commit
in their own name without having been shown the name. What would enforce it is a `must_show`
line carrying `propose_identity`'s answer, which is a SECOND `user_config=True` call site and
so a second entry in the seam suite's `_USER_CONFIG_ALLOWED` with its own measurement. Until
that exists the display is the front end's to do, not this engine's to claim.

`propose_identity` reads `config --get` on
both halves rather than `git var GIT_AUTHOR_IDENT`, which this file recommended until the
guess was measured against what it costs here: with `user.name` set and no `user.email`,
git 2.53.0 returns `Configured <khenrix@Surface-Book-2.localdomain>` at rc=0, the email
invented from user@hostname with nothing marking which half was guessed — and a guess that
survives to B1 is exactly the permanent false attribution the paragraph above refuses. `git
var` is not infallible in the other direction either: with no name and an empty gecos field
it exits 128 (`empty ident name`), so a caller cannot assume it always yields an answer.
"""
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import gitcmd, snapshot, storage


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
    # directories, special files — has no producer yet: nothing in this package walks those
    # three categories and fills the field. Whatever eventually consumes it must treat it as
    # authoritative rather than as evidence: an empty list would say "there are none" when
    # the truth is "nobody looked", which is the fail-OPEN reading of the two.
    sidecars: list | None = None
    filesystem_manifest: dict = field(default_factory=dict)


_FILESYSTEM_MANIFEST = "baseline.filesystem.json"


def filesystem_manifest_path(run_dir) -> Path:
    """Where the manifest `materialize` measured is kept, beside `baseline.index`."""
    return Path(run_dir) / _FILESYSTEM_MANIFEST


def read_filesystem_manifest(run_dir) -> dict:
    """The manifest `materialize` recorded, or a refusal.

    IT IS NOT IN `runstate.Manifest`, and it cannot be: §14.2 writes that record once at
    `confirmed` and `_decode` refuses a field it does not know, so adding one is a schema
    change. It is not derivable from B1 either — a repository with `text=auto` or a clean
    filter has worktree bytes that are not its blob bytes, so a manifest re-derived from the
    tree would REFUSE a correct seat.

    So it is written out here, and read back here, because the alternative is what a caller
    reading the run directory would otherwise have to do: build a `Baseline` with
    `filesystem_manifest={}` and hand it to `fleet.clone_seat`, whose per-path check then
    ranges over nothing and returns `Seat.verified is True` on the HEAD assertion alone.
    `verify.build_verifier` reads that as "clone_seat has already verified the checkout
    against its manifest" before it takes the baseline gate surface, so an empty one is a
    premise asserted and not measured.

    Absent is a REFUSAL rather than an empty dict, for that same reason: `{}` is exactly
    what the disarmed check looks like, and the two must not be one value.

    AND AN EMPTY ONE ON DISK IS THE SAME REFUSAL, which this paragraph used to leave open by
    speaking only of absence. `{}` was unreachable from a missing FILE and perfectly
    reachable from a present one — a truncated write, a hand-edited run directory, a
    `_record_filesystem_manifest` over a baseline that inventoried nothing — and it returned
    the exact value the sentence above says must never be produced, with every downstream
    check ranging over zero paths and reporting verified. A baseline with genuinely no files
    is indistinguishable from that here, and it is not a repository forge has anything to do:
    there is no tracked content for a seat to change and none for a gate to run over.
    """
    path = filesystem_manifest_path(run_dir)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise BaselineError(
            f"{path} does not hold the baseline's filesystem manifest: {e}. It is what "
            "`fleet.clone_seat` checks a seat's content against, and an empty one is not a "
            "weaker check but no check at all.") from e
    try:
        row = json.loads(raw)
    except ValueError as e:
        raise BaselineError(f"{path} is not readable as JSON: {e}") from e
    if not isinstance(row, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in row.items()):
        raise BaselineError(
            f"{path} is not a path-to-digest mapping, so nothing can be checked against it")
    if not row:
        raise BaselineError(
            f"{path} describes no paths at all, so `fleet.clone_seat`'s per-path check would "
            "range over nothing and return verified on the HEAD assertion alone. That is the "
            "disarmed check this file exists to make impossible, and an empty manifest is "
            "indistinguishable from it.")
    return row


def _record_filesystem_manifest(run_dir, manifest: dict) -> None:
    """Write the manifest where a later process can find it. See `read_filesystem_manifest`."""
    blob = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    storage.atomic_write(filesystem_manifest_path(run_dir), blob)


def restore(run_dir, *, base_commit, tracked_tree_oid, commit, ref) -> Baseline:
    """B as `run_dir` recorded it — the object a later process rebuilds when the one
    `materialize` returned is gone.

    The four OIDs come from the caller because §14.2's manifest is where they were recorded
    and this module does not read that record; the filesystem manifest comes from disk
    because nothing else holds it.

    `dirty` is DERIVED rather than taken: `commit == base_commit` is exactly what a clean
    tree produces, so a caller passing it separately would be a second answer to a settled
    question. `sidecars` stays `None` — its own field note calls `[]` the fail-OPEN reading,
    because it would say "there are none" where the truth is that nothing recorded them.
    """
    return Baseline(base_commit=base_commit, tracked_tree_oid=tracked_tree_oid,
                    commit=commit, ref=ref, dirty=commit != base_commit,
                    filesystem_manifest=read_filesystem_manifest(run_dir))


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
            "global config is disabled on every call this package makes. This is the "
            "ORDINARY state of a repository whose identity lives in ~/.gitconfig, not a "
            "fault — the identity is an ANSWER at the §5 gate, so pass author=(name, email); "
            "`gate.propose_identity(repo)` is what reads the one git would use, and "
            "`gate.confirm` records what the operator confirmed of it. Refusing to "
            "substitute a placeholder — B1 is history the user is asked to merge, so a "
            "fabricated author would be a permanent false attribution.")
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


# `snapshot.walk_error`'s rule under this module's class, and the argument for RAISING here
# rather than recording — which is what the two other walks over a selection do — is what
# `read_filesystem_manifest` already argues about an empty manifest: it "is not a weaker check
# but no check at all", because `fleet.clone_seat`'s per-path check ranges over exactly what
# the manifest names. A SHORT one is the same disarmed check with fewer paths, and nothing
# downstream can tell it from a selection that genuinely held less. Measured on a selected
# `scratch` with an unreadable `scratch/locked/`: `git add -f` warns and exits 0, so the tree
# drops the subtree too and `materialize` returned a `Baseline` that silently carried neither,
# as success. `BaselineError` is this module's documented failure class, and the caller that
# meets it is the one that would otherwise have started three seats on that baseline.
_walk_error = snapshot.walk_error(BaselineError)


def _walk_selected(base: Path, repo: Path) -> list:
    """Repo-relative leaves under a selected DIRECTORY, in `screen._walk`'s terms.

    Same rules, for the same reasons: `.git` is pruned rather than post-filtered (it is the
    object store the baseline was built from), and symlinks are never FOLLOWED — os.walk
    under `followlinks=False` does not descend into a linked directory.

    Order is DETERMINISTIC but no longer sorted: each level emits its linked directories
    (sorted) before its files (sorted), because the two come from different os.walk lists.
    Determinism is the property that was ever wanted — the caller builds a dict — and a
    single sorted stream would need the two lists merged for no gain.

    A link is reported rather than dropped, and a linked DIRECTORY too: it arrives in
    `dirnames`, never in `filenames`, so both lists are inspected exactly as `screen._walk`
    and `inspect._escaping_links_under` inspect both. `git add -f` commits either AS A LINK,
    so a dropped one is in the tree with nothing in the manifest describing it — the
    third-outcome gap `tests/test_forge_seams.py` exists to rule out. `_entry_digest` reads
    the link's target TEXT, so reporting it here still never opens what it points at.

    A directory that cannot be LISTED raises rather than contributing nothing — see
    `_walk_error` above, and `snapshot.walk_error` for the rule it is one spelling of.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False,
                                                onerror=_walk_error):
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

    Byte-for-byte `snapshot._symlink_entry`'s digest — including the surrogateescape
    encode, which is what makes the two comparable — and mirrored again in
    `fleet._sha256_link`, which checks this manifest against a seat. A link target is a
    filesystem NAME: a plain `.encode()` raises UnicodeEncodeError on a non-UTF-8 one, and
    since the loop below stopped guarding on `is_file()` this function meets those.
    """
    return hashlib.sha256(os.readlink(p).encode("utf-8", "surrogateescape")).hexdigest()


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

    `author` is the (name, email) recorded on B1, and `gate.open_run` always supplies one —
    the None branch is what a direct caller gets, and it probes the repository's own
    user.name/user.email and raises `BaselineError` if either is missing. That refusal is
    reachable on an ORDINARY repository, since the probe is blind to `~/.gitconfig`, which is
    why the gate settles the identity before a run directory exists rather than letting this
    branch decide it three writes in. See the module docstring for why neither guesses.
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
    # NO_DAEMON_CACHE on a CACHED-ONLY `ls-files`: loading the index is what runs the
    # repository's `core.fsmonitor` program, and `--others` has nothing to do with it. See
    # the constant's own note for the measured list.
    for rel in gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "ls-files", "-z",
                          env_extra=gitcmd.READONLY).stdout.split("\0"):
        # `is_file()` FOLLOWS a link, so this loop used to give a tracked symlink the digest
        # of its target's CONTENT — the one thing `_walk_selected` refuses to do for a
        # selected directory, in the same manifest, for the reason that it "must not
        # describe content from outside the tree it claims to describe". `screen` breached
        # on the entry and `snapshot` digested the target TEXT, so B, F0 and the screen held
        # three different opinions about one path and `fleet` skipped it rather than choose.
        # `_entry_digest` settles it: a link is its target text, everywhere — `baseline`,
        # `snapshot`, `fleet` and `bundle` all give one shape one identity, and `fleet` now
        # VERIFIES the entry rather than skipping it (see its own comment on that check).
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

    # Written on BOTH branches, and here rather than beside each return: it is what
    # `fleet.clone_seat` checks a seat's content against, and a run loop that rebuilds B from
    # the run directory has no other source for it. See `read_filesystem_manifest`.
    _record_filesystem_manifest(run_dir, manifest)

    if not dirty:
        ref = f"refs/khenrix-forge/{run_id}/base"
        # NO_HOOKS on every call below that writes an index or a ref: forge does not fire the
        # user's hooks for its own bookkeeping. The constant carries the argument, including
        # the half of it a later reader may want to reverse.
        gitcmd.git(repo, *gitcmd.NO_HOOKS, "update-ref", ref, base_commit,
                   gitcmd.zero_oid(repo))
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
    gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS, "add", "-u", "--", ":/",
               env_extra=env)

    if selected_untracked:
        # Literal pathspecs from a NUL file: globs, leading dashes and newlines in names
        # are all taken as themselves, and never reach the option parser. GIT_LITERAL_PATHSPECS
        # is load-bearing, not belt-and-braces: without it `weird[1].txt` is a character class
        # that also matches `weird1.txt`, sweeping an UNSELECTED file into the baseline.
        spec = run_dir / "selected.pathspec"
        spec.write_bytes(b"\0".join(p.encode() for p in selected_untracked) + b"\0")
        gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS, "add", "-f",
                   f"--pathspec-from-file={spec}", "--pathspec-file-nul",
                   env_extra={**env, "GIT_LITERAL_PATHSPECS": "1"})

    # Both tuples here for the reason they are on the two `add` calls above, and pointing
    # GIT_INDEX_FILE at a private copy stands in for neither: `core.fsmonitor` and
    # `core.hooksPath` are read from the REPOSITORY's config, so `write-tree` over the copy
    # still ran the user's monitor and still fired their `post-index-change`. Measured: the
    # tree OID is identical with and without.
    tree = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS, "write-tree",
                      env_extra=env).stdout.strip()

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
    gitcmd.git(repo, *gitcmd.NO_HOOKS, "update-ref", ref, commit, gitcmd.zero_oid(repo))

    return Baseline(base_commit=base_commit, tracked_tree_oid=tree, commit=commit,
                    ref=ref, dirty=True, filesystem_manifest=manifest)
