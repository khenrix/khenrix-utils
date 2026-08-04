"""Read-only preflight: describe the repository, say what makes it unsupported, and say
which of its tracked files a DECLARED generator owns — see `detect_generators` for why
that last answer is empty for every repository, this one included.

Three hard rules:

1. Describe-only. Nothing here writes an object, a ref, or the index. `git write-tree` is
   deliberately absent — it locks and rewrites the real index whenever the cache tree is
   stale, which is precisely the dirty-tree case forge exists for (spec §2.2).
2. Describe-only covers EXECUTION too, and `NO_DAEMON_CACHE` is what carries that half:
   `core.fsmonitor` names a program git RUNS and lives in the repository's own `.git/config`,
   while §5 step 1 admits no repository-supplied code before authorization. It goes on EVERY
   worktree-reading call, not the porcelain alone. Measured on git 2.53 against a repository
   whose `core.fsmonitor` touched a marker file: `ls-files --eol` and `check-attr` each ran
   it while they lacked the flags, and `status`/`ls-files -s` did not, because they already
   carried them.
3. Structural rejections are scoped to the SELECTED baseline — tracked content plus the
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
    # Three-valued on purpose, and read that way by `baseline.materialize`'s drift check:
    # a hash is "measured, this is it", "" is "measured, and there was no index file", and
    # None is "not measured" — the only value that disables the check. Collapsing the last
    # two would make an unmeasurable index indistinguishable from a wrongly-resolved git
    # dir, which is the failure this package has already shipped twice.
    index_sha: str | None
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
    # Both carry TRACKED paths, so both reject unconditionally: §2.3 scopes its rejections
    # to "tracked content plus the paths the user selected", and tracked content is always
    # in the baseline. The per-path scoping below applies to the SELECTED untracked set.
    eol_mismatched_paths: list = field(default_factory=list)
    escaping_symlinks: list = field(default_factory=list)
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
        out = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "check-attr", "-z", "filter", "--",
                         *tracked[start:start + _ATTR_BATCH],
                         env_extra=gitcmd.READONLY).stdout
        fields = out.split("\0")
        for i in range(0, len(fields) - 2, 3):
            path, _attr, value = fields[i], fields[i + 1], fields[i + 2]
            if value not in ("unspecified", "unset"):
                hits.append(path)
    return hits


def _eol_mismatched_paths(repo) -> list:
    """Tracked paths whose worktree line endings a fresh checkout would REWRITE.

    Spec §2.3 supports plain EOL normalization and rejects only what "does not
    round-trip", so this cannot reject on the attribute alone. `* text=auto` is the single
    most common line in any .gitattributes, and measured end to end it produces a correct
    seat — rejecting it would fail nearly every repository for an infrastructure reason,
    the outcome §4 warns against.

    What actually breaks is narrower: `baseline`'s manifest hashes the RAW WORKTREE BYTES
    while the seat's checkout re-runs the smudge, so the two disagree exactly when the
    worktree is not already in the state a checkout produces. Measured: `*.txt text
    eol=crlf` with an LF worktree (someone edited it in a Linux editor) yields
    `SeatError: seat content differs from the baseline manifest` three stages after
    preflight said `[]`.

    `ls-files --eol` is the probe rather than `check-attr` because git computes the answer:
    it reports the worktree's ACTUAL line endings next to the attributes in force, so no
    part of git's conversion rules has to be reimplemented here. Its `-z` records are
    `i/<eol> SP w/<eol> SP attr/<attrs> TAB <path>`, split on the FIRST tab for the reason
    `_index_entries` gives — under `-z` the path is raw and may contain one.
    """
    out = []
    for rec in _z(gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "ls-files", "--eol", "-z",
                             env_extra=gitcmd.READONLY).stdout):
        meta, tab, path = rec.partition("\t")
        cols = meta.split()
        if not tab or len(cols) < 3:
            continue
        worktree = cols[1].removeprefix("w/")
        attrs = " ".join(cols[2:]).removeprefix("attr/").split()
        # `-text` as an ATTRIBUTE is "never treat this as text" — no conversion at all.
        if "-text" in attrs:
            continue
        # An explicit `eol=` engages conversion on its own, so neither attribute can be the
        # sole trigger.
        eol = next((a.split("=", 1)[1] for a in attrs if a.startswith("eol=")), "")
        if not (eol or any(a == "text" or a.startswith("text=") for a in attrs)):
            continue
        # `none` is a file with no line endings at all — nothing to convert either way.
        if worktree == "none":
            continue
        # `w/-text` is git's own content-based binary detection, and the ATTRIBUTE decides
        # whether git honours it. Measured on the same PNG: under `text=auto` the seat gets
        # byte-identical content (so flagging it would reject every repository holding
        # `* text=auto` and an image), while under an explicit `text eol=crlf` git converts
        # regardless of content and the seat raised SeatError. Same `w/` column, opposite
        # answers — only `text=auto` may be skipped.
        if worktree == "-text" and "text=auto" in attrs:
            continue
        # No `eol=` means the seat's checkout decides by core.eol, and a seat has no config
        # to read: global and system are pinned to /dev/null and a clone copies no local
        # config, so it gets the built-in default — LF.
        if worktree != (eol or "lf"):
            out.append(path)
    return out


def _escaping_target(root: Path, p: Path):
    """The normalized target when `p` is a symlink leaving `root`, else None.

    `os.path.realpath` rather than `Path.resolve(strict=True)`: a symlink that escapes is
    frequently also broken from here, and the target still has to be reported.
    """
    if not p.is_symlink():
        return None
    target = Path(os.path.realpath(p))
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return target
    return None


def _escaping_links_under(root: Path, base: Path) -> list:
    """Rejection lines for escaping symlinks NESTED inside a selected DIRECTORY.

    The selected-path loop below used to test only the top-level path, but the top level is
    not the boundary the baseline respects: `baseline.materialize` selects a directory with
    `git add -f`, which sweeps its whole contents into the tree and commits a nested link AS
    A LINK. Measured, with only the top-level test: `scratch/creds -> <host>/credentials`
    under a selected `scratch` gave `rejections() == []`, `screen breaches == []`, and a
    seat that read the host's AWS credentials with `verified=True`. Nothing exotic is
    required — a selected `.venv` carries `bin/python -> /usr/bin/python3`.

    This walk changes the FIRST of those three, and `screen._walk` the second. The THIRD is
    unchanged for a chain entered below preflight — `preflight.refusals` consults this list,
    but `baseline.materialize` does not — and that is what the closing assertions of
    `test_forge_seams.py`'s
    `test_an_escaping_link_in_a_selection_is_named_by_two_refusals_and_stopped_by_neither`
    measure today.

    `.git` is pruned for `screen._walk`'s reason. os.walk under `followlinks=False` does not
    descend a linked directory and reports it in `dirnames`, never in `filenames`, so both
    lists are inspected. `_escaping_target` answers None for anything that is not a symlink,
    so the cost on an ordinary tree is one lstat per entry.

    A DIRECTORY THAT CANNOT BE LISTED IS ITS OWN REJECTION, because os.walk's default
    `onerror` swallows the error and yields nothing for that subtree — so `[]` here meant
    both "no escaping link is nested in this selection" and "a subtree could not be looked
    at", and `preflight.refusals` reads this list to decide the run may proceed. Measured,
    with the default: a selected `scratch` holding a mode-000 `scratch/locked/` whose
    `creds -> <host>/credentials` escapes gave `rejections() == []`, `screen breaches == []`
    and `refusals() == ()` — a permission error converted into a clean safety verdict by the
    one gate whose job is refusing unsafe runs.

    RECORDED RATHER THAN RAISED, unlike `snapshot.take` and `baseline._walk_selected`, which
    raise the same fact under their own error class. This module returns a rejection LIST and
    its caller acts on the sentences in it — the discipline the module docstring already sets
    for an unborn HEAD, where `rejections()` speaks instead of `repo_facts` raising through a
    describe-only pass. A line is also the stronger answer here: it names the path a run was
    refused over, alongside every other reason, where a raise would leave the operator a
    traceback out of preflight.
    """
    out = []

    def unlistable(err: OSError):
        # An UNKNOWN, spelled as a refusal — the fail-closed reading of a measurement that
        # could not be taken. Never "there are no escaping links here".
        out.append(f"cannot list {Path(err.filename).relative_to(root).as_posix()}, so it "
                   f"cannot be checked for escaping symlinks: {err.strerror}")

    for dirpath, dirnames, filenames in os.walk(base, followlinks=False,
                                                onerror=unlistable):
        d = Path(dirpath)
        dirnames[:] = sorted(n for n in dirnames if n != ".git")
        for n in [*dirnames, *sorted(filenames)]:
            q = d / n
            target = _escaping_target(root, q)
            if target is not None:
                out.append("symlink escapes the repository: "
                           f"{q.relative_to(root).as_posix()} -> {target}")
    return out


def repo_facts(repo) -> RepoFacts:
    # The root is resolved FIRST and every later read runs there, because `ls-files`,
    # `check-attr` and `ls-files --eol` all report relative to the CWD and list only what is
    # under it — while every path this returns is documented as root-relative and is joined
    # onto `facts.root` by `rejections`, `preflight` and `baseline.materialize` alike. Given a
    # subdirectory, which those callers accept, the index reads would describe that subtree
    # alone: measured, a root-level `--skip-worktree` bit came back `sparse=False` with
    # `rejections()` empty from `<repo>/sub`, a fail-OPEN on one of §2.3's own conditions. The
    # porcelain needs no such care — its paths are root-relative and root-wide from any cwd —
    # and neither does `config`, but they run at the root too so no reader has to know which.
    root = Path(gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "rev-parse", "--show-toplevel",
                           env_extra=gitcmd.READONLY).stdout.strip())

    def g(*args):
        # NO_HOOKS for the reason `runstate._status_digest` gives at its own `status`: this is
        # the user's repository, READONLY is what actually keeps the index unwritten, and the
        # helper takes any subcommand its callers add — so the pin rides on the helper rather
        # than on a reading of every present and future `*args`.
        return gitcmd.git(root, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS, *args,
                          env_extra=gitcmd.READONLY).stdout

    git_dir = Path(g("rev-parse", "--absolute-git-dir").strip())

    # A freshly `git init`-ed repository is an ordinary state, not an error: HEAD points at a
    # branch that has no commit, and `rev-parse HEAD` exits 128. A module whose contract is
    # "return a rejection list" must describe that, not raise through it, so head stays "" and
    # rejections() speaks.
    head_r = gitcmd.git(root, *gitcmd.NO_DAEMON_CACHE, "rev-parse", "--verify", "HEAD",
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
        is_partial=bool(_config(root, "extensions.partialClone")) or _has_promisor_remote(root),
        has_submodules=any(mode == "160000" for _, mode, _ in entries) or
        (root / ".gitmodules").is_file(),
        sparse=_config(root, "core.sparseCheckout") == "true" or skip_worktree,
        unmerged=unmerged, intent_to_add=intent_to_add,
        filtered_paths=_filtered_paths(root, [path for _, _, path in entries]),
        eol_mismatched_paths=_eol_mismatched_paths(root),
        # Mode 120000 is a symlink however the entry was created, read from the same index
        # pass that answers the gitlink and skip-worktree questions. §2.3 lists escaping
        # symlinks without restricting them to selected paths, and a TRACKED one ships to
        # every seat as a working path OUT of the repository — the clone reproduces the
        # link, and a permission-bypassed agent reads and writes straight through it.
        #
        # The reason recorded here was corrected once already: it used to say the manifest
        # "hashes through the link", which `baseline._entry_digest` no longer does — a link
        # is its target text everywhere. Left uncorrected, the refusal would have been
        # standing on a justification that had become an argument for deleting it.
        escaping_symlinks=[path for _, mode, path in entries if mode == "120000"
                           and _escaping_target(root, root / path) is not None],
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
    if facts.eol_mismatched_paths:
        out.append(
            f"worktree line endings do not round-trip on "
            f"{len(facts.eol_mismatched_paths)} path(s), e.g. "
            f"{facts.eol_mismatched_paths[0]}: a checkout would rewrite them, so a seat "
            "can never reproduce the bytes the baseline manifest records")
    if facts.escaping_symlinks:
        out.append(
            f"tracked symlink escapes the repository "
            f"({len(facts.escaping_symlinks)}), e.g. {facts.escaping_symlinks[0]}: every "
            "seat would get a working path out of the repository, and the candidate could "
            "not carry it back")

    root = facts.root
    for rel in selected_untracked:
        p = root / rel
        if (p / ".git").exists():          # dir OR file — a linked worktree uses a FILE
            out.append(f"nested repository selected: {rel}")
        if p.is_symlink():
            target = _escaping_target(root, p)
            if target is not None:
                out.append(f"symlink escapes the repository: {rel} -> {target}")
        elif p.is_dir():
            out += _escaping_links_under(root, p)
        elif p.exists() and not p.is_file():
            out.append(f"special file selected: {rel}")
    return out


def _not_a_relation(r) -> str:
    return (f"a generator relation is a (source glob, output glob) pair, not {r!r}; "
            'a single relation is still a tuple OF pairs: relations=(("a/*", "b/*"),)')


@dataclass(frozen=True)
class GeneratorContract:
    """Which verify-origin rewrites a run may admit (spec §7.2).

    A property of the RUN, never of a seat. A seat-declared relation would let a candidate
    write its own success criterion — "receipts are generated" is one line away from
    laundering an unearned eval receipt past a commit gate — so it is built by the engine's
    read-only preflight, confirmed at the §5 gate, and recorded in the manifest.

    Each relation is `(source glob, output glob)`, and only the OUTPUT side decides
    admission: `verify.fixed_point` never matches against the source, which is provenance
    for the manifest and for the human at the gate. Stated because a reader who assumes
    both halves are enforced would read a narrow source glob as a guarantee it is not.

    `id` is what `bundle.CandidateBundle.generator_contract_id` carries, and "" is that
    field's fail-closed sentinel — "the run declared no contract", which admits nothing.
    """
    id: str = ""
    relations: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        # ValueError rather than a forge error class: every other refusal in this module
        # describes a state of the REPOSITORY, which a caller reports to a user. These
        # describe a malformed literal in engine code or in a gate answer.
        for r in self.relations:
            # The string test comes FIRST because a two-character string unpacks into a
            # pair without complaint. It is also the likely mistake, since one relation is
            # itself a pair: `relations=("shared/**", "marketplaces/**")` iterates as two
            # strings, and `"shared/**"` would unpack into 9 characters below.
            if isinstance(r, (str, bytes)):
                raise ValueError(_not_a_relation(r))
            try:
                source, output = r
            except (TypeError, ValueError):
                raise ValueError(_not_a_relation(r)) from None
            if not (isinstance(source, str) and isinstance(output, str)):
                raise ValueError(f"a generator relation's globs must be strings: {r!r}")
            if not output:
                raise ValueError(
                    f"generator relation {r!r} has an empty output glob, which matches no "
                    "path at all — write the relation out or drop it")
        # Refused because the two halves would disagree at the two places they are read:
        # the gate would admit these paths while the manifest recorded, in the one field
        # that carries the contract forward, that the run declared none.
        if self.relations and not self.id:
            raise ValueError(
                "a generator contract that admits paths needs an id: \"\" is the manifest's "
                "fail-closed sentinel for 'this run declared no contract'")


def detect_generators(repo) -> GeneratorContract:
    """The contract a STATIC read of `repo` can justify. For this repository: none.

    `repo` is taken and unused. The signature is the one a declaration reader needs, and
    the empty contract is an answer about what is declared, not about what exists.

    WHAT WAS LOOKED FOR, in this repository, whose `make verify` runs `render` and is the
    reason §7.2 exists at all:

    - No `.gitattributes` anywhere in the tree, so git carries no `linguist-generated` (or
      any other) marking of generated paths.
    - `capabilities.toml`, the repo's own machine-readable source of truth, names no
      generator relation.
    - The relation lives in `scripts/render.py` — but as PYTHON, not as data. Its constants
      (`BUNDLED`, `BUNDLED_DIRS`, `SHARED_LIBS`, `SHARED_LIB_FILES`, `TEMPLATED_SKILLS`)
      name only SOURCES; where each lands is four different mappings expressed in the
      copy loop's control flow (root-relative file -> plugin root, `shared/lib/<n>` ->
      `<plugin>/lib/<n>` minus `tests`, `shared/skills/<n>` -> `<plugin>/skills/<n>`,
      template + `[skill_facts]` -> a generated `SKILL.md`). Reading it soundly means
      reimplementing render inside forge; running it means the engine executing
      builder-controlled code OUTSIDE the gate, which is the one thing §6 exists to prevent.

    And the coarse relation is not a safe fallback, which is the measurement that settles
    it: `shared/** + capabilities.toml -> marketplaces/**` over-admits. Of the 254 tracked
    files under `marketplaces/`, five are hand-maintained manifests render never writes —
    `marketplaces/<cli>/**/plugin.json` and the two `marketplace.json` files — so that glob
    would let a verify command rewrite the plugin manifest (which carries the version
    Claude and Codex key their plugin cache on) and have the engine STAGE it as generated
    output. An empty contract admits nothing, which is the fail-closed direction; a glob
    that is nearly right fails open on exactly the paths nobody re-reads.

    What would make detection possible is a DECLARATION the engine can read without
    executing anything and without modelling another program: source/output globs in
    `capabilities.toml`, or a generated-path marking in `.gitattributes`. Until one exists,
    §7.2's chain still works — preflight proposes nothing, the §5 gate is where a human
    states the relation, and the manifest records what they stated.
    """
    return GeneratorContract()
