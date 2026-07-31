"""Read-only preflight: describe the repository, then say what makes it unsupported.

Two hard rules:

1. Describe-only. Nothing here writes an object, a ref, or the index. `git write-tree` is
   deliberately absent — it locks and rewrites the real index whenever the cache tree is
   stale, which is precisely the dirty-tree case forge exists for (spec §2.2).
2. Structural rejections are scoped to the SELECTED baseline — tracked content plus the
   untracked paths the user chose. An unscoped sweep would abort on ignored artifacts the
   user never created: this very repository carries leaked agy worktrees under gitignored
   eval workspaces, each with a `.git` FILE (spec §2.3).

Two parsing choices are load-bearing rather than stylistic, both settled against git 2.53's
actual output: `status --no-renames`, and `check-attr -z`. Their reasons sit at the code.
"""
import dataclasses
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import gitcmd

# check-attr takes its pathspec in argv and errors on an empty one, so tracked files are
# probed in batches. A fixed cap instead of batching would silently stop looking partway
# through a large repository — a fail-OPEN answer from a fail-closed check.
_ATTR_BATCH = 500


@dataclass(frozen=True)
class RepoFacts:
    root: Path
    head: str
    index_sha: str
    is_shallow: bool = False
    is_partial: bool = False
    has_submodules: bool = False
    # Both of spec §2.3's "the worktree is not the tracked tree" conditions: a sparse checkout
    # (config) AND a bare skip-worktree bit on any index entry (no config is written for that
    # one). Deliberately one field, not two — narrowing it to the config probe reopens a
    # fail-open on `git update-index --skip-worktree`.
    sparse: bool = False
    unmerged: list = field(default_factory=list)
    intent_to_add: list = field(default_factory=list)
    filtered_paths: list = field(default_factory=list)
    staged: list = field(default_factory=list)
    unstaged: list = field(default_factory=list)
    untracked: list = field(default_factory=list)


def replace(facts: RepoFacts, **kw) -> RepoFacts:
    """Test/utility helper: a copy with fields overridden."""
    return dataclasses.replace(facts, **kw)


def _z(out: str) -> list:
    return [p for p in out.split("\0") if p]


def _index_entries(out: str) -> list:
    """`(tag, mode, path)` per index entry, parsed from `ls-files -s -v -z`.

    The record is `<tag> SP <mode> SP <oid> SP <stage> TAB <path>`, so the split is on the
    FIRST tab only: under `-z` the path is raw bytes and may itself contain one, while the
    tag, mode, oid and stage never can.

    One index read answers three questions — gitlinks (mode 160000), skip-worktree state
    (tag S/s) and the tracked path list for the .gitattributes probe.
    """
    entries = []
    for rec in _z(out):
        meta, tab, path = rec.partition("\t")
        cols = meta.split(" ")
        if tab and len(cols) >= 2:
            entries.append((cols[0], cols[1], path))
    return entries


def _config(repo, key: str) -> str:
    """A config value, or "" when unset. Absent keys exit 1, so this cannot use check=True."""
    return gitcmd.git(repo, "config", "--get", key,
                      env_extra=gitcmd.READONLY, check=False).stdout.strip()


def _has_promisor_remote(repo) -> bool:
    """True when a remote is marked promisor — git 2.53's marker for a partial clone.

    `extensions.partialClone` alone is not enough: git 2.53 no longer writes it. A
    `--filter=blob:none` clone gets `core.repositoryformatversion = 1` and
    `remote.<name>.promisor = true`, with no `[extensions]` section at all, so the older
    probe reports a partial clone as ordinary — fail-OPEN on a repository whose objects live
    behind a lazy fetch. Both probes are kept: this one catches current git, the other still
    catches clones made by older git.

    Presence of the key decides, not its value. `--get-regexp` exits 1 when nothing matches,
    so this cannot use check=True.
    """
    return gitcmd.git(repo, "config", "--get-regexp", r"remote\..*\.promisor",
                      env_extra=gitcmd.READONLY, check=False).returncode == 0


def _filtered_paths(repo, tracked: list) -> list:
    """Tracked paths carrying a custom `.gitattributes` filter.

    A clean/smudge driver is *named* in .gitattributes but *defined* in .git/config, which
    `git clone` does not copy — a seat would check out different bytes than the user sees
    (spec §2.3).

    `-z` is what makes the answer parseable. check-attr's plain output is
    `<path>: <attr>: <value>` per line with core.quotePath escaping, so a path containing
    ": " or a newline is ambiguous or mangled; with -z the records are raw NUL-separated
    (path, attr, value) triples.
    """
    hits = []
    for start in range(0, len(tracked), _ATTR_BATCH):
        out = gitcmd.git(repo, "check-attr", "-z", "filter", "--",
                         *tracked[start:start + _ATTR_BATCH],
                         env_extra=gitcmd.READONLY).stdout
        fields = out.split("\0")
        for i in range(0, len(fields) - 2, 3):
            path, _attr, value = fields[i], fields[i + 1], fields[i + 2]
            if value not in ("unspecified", "unset"):
                hits.append(path)
    return hits


def repo_facts(repo) -> RepoFacts:
    def g(*args):
        return gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *args,
                          env_extra=gitcmd.READONLY).stdout

    root = Path(g("rev-parse", "--show-toplevel").strip())
    git_dir = Path(g("rev-parse", "--absolute-git-dir").strip())

    # A freshly `git init`-ed repository is an ordinary state, not an error: HEAD points at a
    # branch that has no commit, and `rev-parse HEAD` exits 128. A module whose contract is
    # "return a rejection list" must describe that, not raise through it, so head stays "" and
    # rejections() speaks.
    head_r = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "rev-parse", "--verify", "HEAD",
                        env_extra=gitcmd.READONLY, check=False)
    head = head_r.stdout.strip() if head_r.returncode == 0 else ""

    # --no-renames is required, not tuning. With rename detection on, porcelain -z emits the
    # old path as a bare extra record after every R/C entry, which reads back as a status
    # code spliced out of a filename; and git pairs intent-to-add entries into ` R` records,
    # hiding them from the worktree-column check below. Without it a rename is a plain
    # delete + add, which is also the shape the baseline wants: paths, not pair semantics.
    staged, unstaged, untracked, intent_to_add = [], [], [], []
    for entry in _z(g("status", "--porcelain=v1", "-z",
                      "--untracked-files=all", "--no-renames")):
        if len(entry) < 4:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        if x == "?" and y == "?":
            untracked.append(path)
            continue
        if x == " " and y == "A":
            # 'A' in the WORKTREE column: the index holds an entry whose content was never
            # staged. That is what `git add -N` leaves behind and the only way to produce it.
            intent_to_add.append(path)
        if x not in (" ", "?"):
            staged.append(path)
        if y not in (" ", "?"):
            unstaged.append(path)

    # ls-files -u repeats a path once per surviving stage, so collapse it: the list is a set
    # of conflicted paths, and the count reported to the user must be one too.
    unmerged = list(dict.fromkeys(
        line.split("\t", 1)[1] for line in _z(g("ls-files", "--unmerged", "-z"))
        if "\t" in line))

    # Gitlinks are read straight out of the index rather than from `git submodule status`.
    # That command exits 128 — `no submodule mapping found in .gitmodules for path 'x'` — on
    # the commonest way a gitlink appears at all: `git add` on a directory that happens to be
    # a repository, which git accepts with a warning and no .gitmodules entry. `check=True`
    # would raise out of preflight; `check=False` would read the crash as "no submodules",
    # which is fail-OPEN on the exact condition this rejects. `ls-files` cannot fail here, and
    # its mode column is 160000 for a gitlink however the entry was created. The .gitmodules
    # probe stays because it catches the opposite direction: a mapping with no index entry.
    #
    # `-v` adds the status tag that carries skip-worktree, which spec §2.3 rejects in its own
    # right and which no config key reports: `git update-index --skip-worktree` sets the bit
    # on the entry and writes nothing to config at all. The tag is `S`, or lowercase `s` when
    # the same entry is also assume-unchanged, so both are matched.
    entries = _index_entries(g("ls-files", "-s", "-v", "-z"))
    skip_worktree = any(tag in ("S", "s") for tag, _, _ in entries)

    index = git_dir / "index"
    return RepoFacts(
        root=root, head=head,
        index_sha=hashlib.sha256(index.read_bytes()).hexdigest() if index.is_file() else "",
        is_shallow=g("rev-parse", "--is-shallow-repository").strip() == "true",
        is_partial=bool(_config(repo, "extensions.partialClone")) or _has_promisor_remote(repo),
        has_submodules=any(mode == "160000" for _, mode, _ in entries) or
        (root / ".gitmodules").is_file(),
        sparse=_config(repo, "core.sparseCheckout") == "true" or skip_worktree,
        unmerged=unmerged, intent_to_add=intent_to_add,
        filtered_paths=_filtered_paths(repo, [path for _, _, path in entries]),
        staged=staged, unstaged=unstaged, untracked=untracked)


def rejections(facts: RepoFacts, selected_untracked: list) -> list:
    """Unsupported-feature list. Empty means preflight may proceed.

    Repository-wide conditions always reject. Path-shaped conditions reject only when the
    path is SELECTED into the baseline — see the module docstring.
    """
    out = []
    if not facts.head:
        out.append("unborn HEAD: the repository has no commits yet")
    if facts.is_shallow:
        out.append("shallow repository: history is incomplete; clone semantics differ")
    if facts.is_partial:
        out.append("partial clone (promisor objects): a local clone is not self-contained")
    if facts.has_submodules:
        out.append("submodules present: nested remotes reopen the isolation problem")
    if facts.sparse:
        out.append("sparse checkout or skip-worktree entries: "
                   "the working tree is not the tracked tree")
    if facts.unmerged:
        out.append(f"unmerged index entries ({len(facts.unmerged)}): resolve the merge first")
    if facts.intent_to_add:
        out.append(f"intent-to-add entries ({len(facts.intent_to_add)}): git add or reset them")
    if facts.filtered_paths:
        out.append(f"custom .gitattributes filter on {len(facts.filtered_paths)} path(s): "
                   "the driver lives in .git/config and is not cloned")

    root = facts.root
    for rel in selected_untracked:
        p = root / rel
        if (p / ".git").exists():          # dir OR file — a linked worktree uses a FILE
            out.append(f"nested repository selected: {rel}")
        if p.is_symlink():
            target = Path(os.path.realpath(p))
            try:
                target.relative_to(root.resolve())
            except ValueError:
                out.append(f"symlink escapes the repository: {rel} -> {target}")
        elif p.exists() and not p.is_file() and not p.is_dir():
            out.append(f"special file selected: {rel}")
    return out
