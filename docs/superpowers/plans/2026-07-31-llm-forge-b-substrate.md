# llm-forge Plan B: Baseline + Clone-Fleet Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `shared/lib/forge` package that can take a user's repository — dirty
working tree and all — and produce an immutable composite baseline plus a fleet of
independent, remote-less clones that three write-enabled agents could safely be launched
into, without the engine ever mutating the user's index, refs, or checkout.

**Architecture:** Five focused modules under `shared/lib/forge/`, each one responsibility:
`gitcmd` (one audited way to invoke git), `storage` (run directory + quotas), `inspect`
(read-only preflight facts and the fail-closed rejection set), `baseline` (the composite B
and its plumbing), `screen` (pre-launch secret screen), `fleet` (clone construction). Every
task is hermetic — fixture git repos and temp dirs, no providers, no network, no cost.

**Tech Stack:** Python 3.11+ stdlib only. git 2.53 (`--revision` clone form available;
repository hash algorithm is sha1 here, so the all-zeros OID is 40 hex — derive it, do not
hardcode). pytest via `uvx pytest` from the repo root.

**Spec:** `docs/superpowers/specs/2026-07-30-llm-forge-design.md` §1 (threat model), §2
(baseline), §3 (secret screen), §4 (clone fleet), §15 (storage). Plan A
(`2026-07-31-llm-forge-a-shared-core.md`) shipped the shared council engine and its seams;
this plan consumes them. Harvest (§6/§7), the journal and state machine (§14), review and
ultrareview (§13), handover (§16) and the skill itself (§18/§20) are **later plans** — this
one deliberately stops at "a seat could be launched," and launches nothing.

## Global Constraints

- Python is **stdlib-only**; must run on any Python 3.11+ machine with no install step.
- **The engine never mutates the user's repository** except to create objects and refs under
  `refs/khenrix-forge/<run-id>/` after consent. Specifically: never write `.git/index`, never
  touch `HEAD`, never modify the checkout, never run a repo-wide `git worktree prune`. Every
  read-only git call carries `GIT_OPTIONAL_LOCKS=0`.
- **Never run `git write-tree` against the real index.** It takes `index.lock`
  unconditionally and rewrites the stale cache-tree extension — which is exactly the dirty
  case this code exists for. Copy `.git/index` to the run dir first and work under
  `GIT_INDEX_FILE`.
- **Seats are fallible, not adversarial** (spec §1). This code defends against accidents,
  not against a same-UID attack. Say so in docstrings rather than implying containment the
  code cannot deliver.
- Never edit anything under `marketplaces/` by hand — it is generated. Run `make render`
  before every commit and include the regenerated files, or `make precommit`'s render-drift
  check fails.
- After every task: `uvx pytest tests/ -m "not slow" -q` stays green, and the Plan A suites
  (`test_council_*.py`) are untouched.
- Another agent session may be active in this repo. Before each commit: `.git/index.lock`
  must not exist and `pgrep -af 'make |render.py'` must show nothing.
- Commit messages end with the repo's standard trailer (see any recent commit).

## File Structure

| Path | Responsibility |
|---|---|
| `shared/lib/forge/__init__.py` | package marker; exports nothing (callers import submodules) |
| `shared/lib/forge/gitcmd.py` | the only place that builds a git argv; read-only vs plumbing env |
| `shared/lib/forge/storage.py` | run directory layout, mode 0700, quotas |
| `shared/lib/forge/inspect.py` | read-only repo facts + the fail-closed rejection set |
| `shared/lib/forge/baseline.py` | composite `Baseline`, B₀/B₁, the plumbing sequence |
| `shared/lib/forge/screen.py` | bounded directory-walking secret screen |
| `shared/lib/forge/fleet.py` | clone construction, origin removal, config/env neutralization |
| `tests/test_forge_storage.py` … `tests/test_forge_fleet.py` | one suite per module |
| `tests/forge_fixtures.py` | shared fixture-repo builder (imported by the suites) |

---

### Task 1: Package skeleton, `gitcmd`, and `storage`

**Files:**
- Create: `shared/lib/forge/__init__.py`, `shared/lib/forge/gitcmd.py`,
  `shared/lib/forge/storage.py`, `tests/forge_fixtures.py`, `tests/test_forge_storage.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, relied on by every later task:
  - `gitcmd.git(repo, *args, env_extra=None, check=True, binary=False) -> subprocess.CompletedProcess`
  - `gitcmd.READONLY: dict` — `{"GIT_OPTIONAL_LOCKS": "0"}`
  - `gitcmd.NO_USER_CONFIG: dict` — global/system config disabled
  - `gitcmd.zero_oid(repo) -> str`
  - `storage.run_root(repo_path, run_id) -> Path`
  - `storage.new_run_id() -> str`
  - `storage.Quota(max_files: int, max_file_bytes: int, max_total_bytes: int)` with
    `Quota.default() -> Quota`
  - `tests/forge_fixtures.py`: `make_repo(tmp_path, name="repo") -> Path` (git repo with one
    commit, `user.name`/`user.email` set locally), `write(repo, relpath, text)`,
    `commit_all(repo, msg)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_storage.py
"""Run-directory layout and quotas (spec §15)."""
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import gitcmd, storage  # noqa: E402
from forge_fixtures import make_repo  # noqa: E402


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


def test_git_readonly_env_sets_optional_locks(tmp_path):
    repo = make_repo(tmp_path)
    r = gitcmd.git(repo, "rev-parse", "HEAD", env_extra=gitcmd.READONLY)
    assert r.returncode == 0 and len(r.stdout.strip()) == 40


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
```

```python
# tests/forge_fixtures.py
"""Fixture git repositories for the forge suites.

Local user identity only — never touches the developer's global git config.
"""
import subprocess
from pathlib import Path


def _run(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r


def make_repo(tmp_path, name="repo") -> Path:
    repo = Path(tmp_path) / name
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "fixture@example.invalid")
    _run(repo, "config", "user.name", "Fixture")
    (repo / "seed.txt").write_text("seed\n")
    _run(repo, "add", "seed.txt")
    _run(repo, "commit", "-q", "-m", "seed")
    return repo


def write(repo: Path, relpath: str, text: str) -> Path:
    p = Path(repo) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def commit_all(repo: Path, msg: str) -> str:
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", msg)
    return _run(repo, "rev-parse", "HEAD").stdout.strip()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_storage.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'forge'`.

- [ ] **Step 3: Implement**

```python
# shared/lib/forge/__init__.py
"""llm-forge engine substrate. Import submodules directly (forge.baseline, ...)."""
```

```python
# shared/lib/forge/gitcmd.py
"""The one audited way this package invokes git.

Every call is an argv list with an explicit environment — never a shell string, so a
path containing a metacharacter cannot become a command. Two env presets:

  READONLY       describe-only calls; GIT_OPTIONAL_LOCKS=0 stops read-oriented commands
                 opportunistically refreshing the USER's real index (spec §2.2).
  NO_USER_CONFIG global/system config disabled, for clone and for anything running inside
                 a seat clone: an empty template dir does NOT neutralise a global
                 core.hooksPath or url.*.insteadOf (spec §4.1).

Neither preset can make `git write-tree` safe against the real index — that command takes
index.lock unconditionally. Callers must supply GIT_INDEX_FILE instead.
"""
import os
import subprocess
from pathlib import Path

READONLY = {"GIT_OPTIONAL_LOCKS": "0"}
NO_USER_CONFIG = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
# fsmonitor/untracked-cache are daemon state; a baseline must not depend on them.
NO_DAEMON_CACHE = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")


class GitError(RuntimeError):
    """A git invocation exited non-zero and the caller asked for check=True."""


def git(repo, *args, env_extra=None, check=True, binary=False, timeout=60):
    env = dict(os.environ)
    env.update(env_extra or {})
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=not binary, timeout=timeout, env=env)
    if check and r.returncode != 0:
        err = r.stderr if not binary else r.stderr.decode("utf-8", "replace")
        raise GitError(f"git {' '.join(str(a) for a in args)} -> {r.returncode}: {err.strip()}")
    return r


def zero_oid(repo) -> str:
    """All-zeros OID at THIS repository's hash width — 40 for sha1, 64 for sha256.
    Used as update-ref's <expected-old> when creating a ref that must not already exist."""
    head = git(repo, "rev-parse", "HEAD", env_extra=READONLY).stdout.strip()
    return "0" * len(head)
```

```python
# shared/lib/forge/storage.py
"""Run directory layout and quotas (spec §15).

Under XDG_STATE_HOME, not XDG_CACHE_HOME: the run directory holds the only copy of the
seats' work, and XDG defines the cache as data that can be deleted without loss — it is
what every cleanup tool targets first. The repo path is hashed rather than basenamed so
~/git/a/utils and ~/work/b/utils cannot collide.
"""
import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def new_run_id() -> str:
    return secrets.token_hex(3)   # 6 hex chars; collision-checked by the caller's mkdir


def run_root(repo_path, run_id: str) -> Path:
    state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    digest = hashlib.sha256(str(Path(repo_path).resolve()).encode()).hexdigest()[:12]
    p = Path(state) / "khenrix-forge" / f"{digest}-{run_id}"
    p.mkdir(parents=True, exist_ok=True)
    p.chmod(0o700)
    return p


@dataclass(frozen=True)
class Quota:
    """Caps that FAIL CLOSED with a report line — never a silent truncation."""
    max_files: int
    max_file_bytes: int
    max_total_bytes: int

    @classmethod
    def default(cls) -> "Quota":
        return cls(max_files=5000, max_file_bytes=32 * 1024 * 1024,
                   max_total_bytes=512 * 1024 * 1024)

    def breach(self, *, files: int, file_bytes: int, total_bytes: int):
        """Return a human-readable breach description, or None when within limits."""
        if files > self.max_files:
            return f"files: {files} > {self.max_files}"
        if file_bytes > self.max_file_bytes:
            return f"file_bytes: {file_bytes} > {self.max_file_bytes}"
        if total_bytes > self.max_total_bytes:
            return f"total_bytes: {total_bytes} > {self.max_total_bytes}"
        return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_storage.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge tests/forge_fixtures.py tests/test_forge_storage.py
git commit -m "feat(forge): package skeleton, audited git invocation, run storage"
```

---

### Task 2: Read-only preflight facts and the fail-closed rejection set

**Files:**
- Create: `shared/lib/forge/inspect.py`, `tests/test_forge_inspect.py`

**Interfaces:**
- Consumes: `gitcmd.git`, `gitcmd.READONLY`, `gitcmd.NO_DAEMON_CACHE`.
- Produces:
  - `inspect.RepoFacts` dataclass: `root: Path`, `head: str`, `is_shallow: bool`,
    `is_partial: bool`, `has_submodules: bool`, `sparse: bool`, `unmerged: list[str]`,
    `intent_to_add: list[str]`, `filtered_paths: list[str]`, `staged: list[str]`,
    `unstaged: list[str]`, `untracked: list[str]`, `index_sha: str`
  - `inspect.repo_facts(repo) -> RepoFacts` — read-only
  - `inspect.rejections(facts, selected_untracked: list[str]) -> list[str]` — the §2.3
    fail-closed set, **scoped to tracked content plus the selected paths**

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_inspect.py
"""Preflight is describe-only, and its rejections are scoped to the selected baseline."""
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import inspect as finspect  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402


def _index_sha(repo):
    return hashlib.sha256((Path(repo) / ".git" / "index").read_bytes()).hexdigest()


def test_facts_classify_staged_unstaged_untracked(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "tracked.txt", "v1\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add"], check=True)
    write(repo, "tracked.txt", "v2\n")                 # unstaged modification
    write(repo, "staged.txt", "s\n")
    subprocess.run(["git", "-C", str(repo), "add", "staged.txt"], check=True)
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
    subprocess.run(["git", "-C", str(repo), "add", "dirty.txt"], check=True)
    write(repo, "dirty.txt", "d2\n")                   # stale cache-tree, the risky case
    before = _index_sha(repo)
    finspect.repo_facts(repo)
    assert _index_sha(repo) == before


def test_rejects_unmerged_index(tmp_path):
    repo = make_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "other"], check=True)
    write(repo, "conflict.txt", "theirs\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "theirs"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    write(repo, "conflict.txt", "ours\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "ours"], check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True)
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


def test_nested_repo_is_reported_only_when_selected(tmp_path):
    """This repo carries leaked agy worktrees under gitignored eval workspaces; an
    unscoped structural sweep would abort every run on artifacts nobody created."""
    repo = make_repo(tmp_path)
    write(repo, ".gitignore", "workspace/\n")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "ignore"], check=True)
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_inspect.py -q`
Expected: `ImportError: cannot import name 'inspect'` (or `ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# shared/lib/forge/inspect.py
"""Read-only preflight: describe the repository, then say what makes it unsupported.

Two hard rules:

1. Describe-only. Nothing here writes an object, a ref, or the index. `git write-tree` is
   deliberately absent — it locks and rewrites the real index whenever the cache tree is
   stale, which is precisely the dirty-tree case forge exists for (spec §2.2).
2. Structural rejections are scoped to the SELECTED baseline — tracked content plus the
   untracked paths the user chose. An unscoped sweep would abort on ignored artifacts the
   user never created: this very repository carries leaked agy worktrees under gitignored
   eval workspaces, each with a `.git` FILE (spec §2.3).
"""
import dataclasses
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import gitcmd


@dataclass(frozen=True)
class RepoFacts:
    root: Path
    head: str
    index_sha: str
    is_shallow: bool = False
    is_partial: bool = False
    has_submodules: bool = False
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


def repo_facts(repo) -> RepoFacts:
    g = lambda *a: gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *a,
                              env_extra=gitcmd.READONLY).stdout
    root = Path(g("rev-parse", "--show-toplevel").strip())
    head = g("rev-parse", "HEAD").strip()

    staged, unstaged, untracked = [], [], []
    for entry in _z(g("status", "--porcelain=v1", "-z", "--untracked-files=all")):
        # porcelain -z: "XY path"; renames emit a second NUL-separated path we ignore here
        if len(entry) < 4:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        if x == "?" and y == "?":
            untracked.append(path)
            continue
        if x not in (" ", "?"):
            staged.append(path)
        if y not in (" ", "?"):
            unstaged.append(path)

    unmerged = [ln.split("\t", 1)[1] for ln in _z(g("ls-files", "--unmerged", "-z"))
                if "\t" in ln]
    intent_to_add = _z(g("diff", "--name-only", "--diff-filter=A", "--cached", "-z"))
    intent_to_add = [p for p in intent_to_add if p in untracked]

    # .gitattributes filters: a clean/smudge driver lives in .git/config, which `git clone`
    # does not copy — a seat would silently check out different bytes (spec §2.3).
    filtered = []
    tracked = _z(g("ls-files", "-z"))
    if tracked:
        attrs = gitcmd.git(repo, "check-attr", "--stdin", "-z", "filter", "text",
                           env_extra=gitcmd.READONLY, check=False)
        # check-attr --stdin needs input; fall back to a per-batch scan when unsupported
        probe = gitcmd.git(repo, "check-attr", "filter", "--", *tracked[:2000],
                           env_extra=gitcmd.READONLY, check=False)
        for line in (probe.stdout or "").splitlines():
            path, _, value = line.rpartition(": filter: ")
            if value and value not in ("unspecified", "unset"):
                filtered.append(path.rstrip(": "))
        del attrs

    return RepoFacts(
        root=root, head=head,
        index_sha=hashlib.sha256((root / ".git" / "index").read_bytes()).hexdigest()
        if (root / ".git" / "index").is_file() else "",
        is_shallow=g("rev-parse", "--is-shallow-repository").strip() == "true",
        is_partial=bool(gitcmd.git(repo, "config", "--get", "extensions.partialClone",
                                   env_extra=gitcmd.READONLY, check=False).stdout.strip()),
        has_submodules=bool(_z(g("submodule", "status", "--recursive")) or
                            (root / ".gitmodules").is_file()),
        sparse=gitcmd.git(repo, "config", "--get", "core.sparseCheckout",
                          env_extra=gitcmd.READONLY, check=False).stdout.strip() == "true",
        unmerged=unmerged, intent_to_add=intent_to_add, filtered_paths=filtered,
        staged=staged, unstaged=unstaged, untracked=untracked)


def rejections(facts: RepoFacts, selected_untracked: list) -> list:
    """Unsupported-feature list. Empty means preflight may proceed.

    Repository-wide conditions always reject. Path-shaped conditions reject only when the
    path is SELECTED into the baseline — see the module docstring.
    """
    out = []
    if facts.is_shallow:
        out.append("shallow repository: history is incomplete; clone semantics differ")
    if facts.is_partial:
        out.append("partial clone (promisor objects): a local clone is not self-contained")
    if facts.has_submodules:
        out.append("submodules present: nested remotes reopen the isolation problem")
    if facts.sparse:
        out.append("sparse checkout: the working tree is not the tracked tree")
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_inspect.py -q`
Expected: 6 passed. If `check-attr` behaves differently on this git build, adjust only the
`filtered` probe — the assertions stay.

- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/inspect.py tests/test_forge_inspect.py
git commit -m "feat(forge): read-only preflight facts + scoped fail-closed rejections"
```

---

### Task 3: The composite baseline

**Files:**
- Create: `shared/lib/forge/baseline.py`, `tests/test_forge_baseline.py`

**Interfaces:**
- Consumes: `gitcmd.*`, `inspect.RepoFacts`, `storage.run_root`.
- Produces:
  - `baseline.Baseline` dataclass: `base_commit: str`, `tracked_tree_oid: str`,
    `commit: str` (B₁, equal to `base_commit` on a clean tree), `ref: str`,
    `sidecars: list[str]`, `filesystem_manifest: dict[str, str]`, `dirty: bool`
  - `baseline.materialize(repo, run_dir, facts, selected_untracked, run_id, author=None) -> Baseline`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_baseline.py
"""B is composite; construction never touches the user's index (spec §2)."""
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import baseline, inspect as finspect  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402


def _idx(repo):
    return hashlib.sha256((Path(repo) / ".git" / "index").read_bytes()).hexdigest()


def _tree_paths(repo, tree):
    out = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", tree],
                         capture_output=True, text=True, check=True).stdout
    return set(out.split())


def _mk(repo, run, selected=()):
    f = finspect.repo_facts(repo)
    return baseline.materialize(repo, run, f, list(selected), "r1")


def test_clean_tree_creates_no_commit(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run)
    assert b.dirty is False
    assert b.commit == b.base_commit, "a clean baseline must not invent history"


def test_dirty_tree_captures_staged_unstaged_and_selected_untracked(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "s.txt", "staged\n")
    subprocess.run(["git", "-C", str(repo), "add", "s.txt"], check=True)
    write(repo, "seed.txt", "modified\n")          # unstaged change to a tracked file
    write(repo, "chosen.txt", "picked\n")          # untracked, selected
    write(repo, "ignored_by_user.txt", "no\n")     # untracked, NOT selected
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["chosen.txt"])
    assert b.dirty is True and b.commit != b.base_commit
    paths = _tree_paths(repo, b.tracked_tree_oid)
    assert {"s.txt", "seed.txt", "chosen.txt"} <= paths
    assert "ignored_by_user.txt" not in paths, "add -A leaked an unselected path"
    blob = subprocess.run(["git", "-C", str(repo), "show", f"{b.tracked_tree_oid}:seed.txt"],
                          capture_output=True, text=True, check=True).stdout
    assert blob == "modified\n", "unstaged content missing from the baseline"


def test_materialize_never_writes_the_user_index(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "s.txt", "x\n")
    subprocess.run(["git", "-C", str(repo), "add", "s.txt"], check=True)
    write(repo, "s.txt", "y\n")                    # stale cache-tree
    run = tmp_path / "run"; run.mkdir()
    before = _idx(repo)
    _mk(repo, run)
    assert _idx(repo) == before, "write-tree ran against the real index"


def test_ref_is_created_and_reachable(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["d.txt"])
    assert b.ref == "refs/khenrix-forge/r1/base"
    got = subprocess.run(["git", "-C", str(repo), "rev-parse", b.ref],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert got == b.commit


def test_b1_carries_user_authorship(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["d.txt"])
    who = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>%n%s",
                          b.commit], capture_output=True, text=True, check=True).stdout
    assert "Fixture <fixture@example.invalid>" in who
    assert "uncommitted working tree" in who


def test_path_with_glob_characters_survives_literally(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "weird[1].txt", "w\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["weird[1].txt"])
    assert "weird[1].txt" in _tree_paths(repo, b.tracked_tree_oid)


def test_filesystem_manifest_covers_selected_and_tracked(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "chosen.txt", "c\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["chosen.txt"])
    assert "seed.txt" in b.filesystem_manifest and "chosen.txt" in b.filesystem_manifest
    assert len(b.filesystem_manifest["chosen.txt"]) == 64
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_baseline.py -q`
Expected: `ModuleNotFoundError: No module named 'forge.baseline'`.

- [ ] **Step 3: Implement**

```python
# shared/lib/forge/baseline.py
"""The composite baseline B (spec §2).

B is not one git OID. A tree cannot represent empty directories, full POSIX modes, or
ignored sidecars, and every downstream consumer (clone, worktree) needs a COMMIT. So B
carries: the base commit, the tracked tree, the synthetic commit B1 that anchors execution,
the sidecar list, and a filesystem manifest to validate materialization against.

B1 and the "synthetic anchor" are ONE commit. On a clean tree no commit is created at all
and B1 == base_commit. When the tree is dirty, B1 is authored by the USER with a message
saying exactly what it is — because the synthesis branch is rooted here, and merging the
deliverable would otherwise commit their scratch work as forge's (spec §2.1).
"""
import hashlib
import os
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

    `author` overrides the (name, email) recorded on B1; default reads the repo's own
    user.name/user.email so the commit is honestly the user's.
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
    # A byte copy of .git/index is a consistent snapshot: git writes the index by atomic
    # rename, so a plain copy is never torn. Absent-or-copied, never an empty file.
    idx = run_dir / "baseline.index"
    src_idx = repo / ".git" / "index"
    if src_idx.is_file():
        shutil.copy2(src_idx, idx)
    env = {**gitcmd.READONLY, "GIT_INDEX_FILE": str(idx)}

    # `:/` is pathspec MAGIC (repo-root-relative) — it must not fall inside the literal
    # scope below, or `add -u` would look for a directory named ":/".
    gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "add", "-u", "--", ":/", env_extra=env)

    if selected_untracked:
        # Literal pathspecs from a NUL file: globs, leading dashes and newlines in names
        # are all taken as themselves, and never reach the option parser.
        spec = run_dir / "selected.pathspec"
        spec.write_bytes(b"\0".join(p.encode() for p in selected_untracked) + b"\0")
        gitcmd.git(repo, "add", "-f", f"--pathspec-from-file={spec}", "--pathspec-file-nul",
                   env_extra={**env, "GIT_LITERAL_PATHSPECS": "1"})

    tree = gitcmd.git(repo, "write-tree", env_extra=env).stdout.strip()

    if author is None:
        name = gitcmd.git(repo, "config", "--get", "user.name",
                          env_extra=gitcmd.READONLY, check=False).stdout.strip() or "unknown"
        email = gitcmd.git(repo, "config", "--get", "user.email",
                           env_extra=gitcmd.READONLY, check=False).stdout.strip() or "unknown@invalid"
    else:
        name, email = author
    msg = ("forge: snapshot of your uncommitted working tree\n\n"
           "This commit is yours, not forge's. It exists so every seat starts from the "
           "same tree you were looking at. Forge's own work stacks on top of it.")
    commit = gitcmd.git(
        repo, "commit-tree", tree, "-p", base_commit, "-m", msg,
        env_extra={**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                   "GIT_COMMITTER_NAME": "llm-forge",
                   "GIT_COMMITTER_EMAIL": "forge@khenrix.invalid"}).stdout.strip()

    ref = f"refs/khenrix-forge/{run_id}/base"
    # update-ref immediately: until the ref exists the commit is unreachable and a
    # concurrent `git gc --prune=now` can drop it.
    gitcmd.git(repo, "update-ref", ref, commit, gitcmd.zero_oid(repo))

    return Baseline(base_commit=base_commit, tracked_tree_oid=tree, commit=commit,
                    ref=ref, dirty=True, sidecars=[], filesystem_manifest=manifest)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_baseline.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/baseline.py tests/test_forge_baseline.py
git commit -m "feat(forge): composite baseline B via an alternate index"
```

---

### Task 4: Pre-launch secret screen

**Files:**
- Create: `shared/lib/forge/screen.py`, `tests/test_forge_screen.py`

**Interfaces:**
- Consumes: `storage.Quota`, and `scripts/lib/checks.py`'s `SECRET_FAIL`,
  `SECRET_ALLOW_SHA`, `SCAN_SKIP_SUFFIX` (imported, never forked).
- Produces:
  - `screen.Finding` dataclass: `path: str`, `line: int`, `pattern: str`
  - `screen.screen_tree(root, rel_paths, quota=None) -> tuple[list[Finding], list[str]]`
    — returns `(findings, breaches)`; a breach fails the run closed
  - `screen.BLOCKED_NAMES: tuple[str, ...]` — path-shaped high-risk names

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_screen.py
"""The screen runs BEFORE any provider starts — a post-harvest scan is too late."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import screen, storage  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402


def test_detects_a_token_in_a_selected_file(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "cfg.py", 'TOKEN = "xoxp-1234567890abcde"\n')
    findings, breaches = screen.screen_tree(repo, ["cfg.py"])
    assert breaches == []
    assert findings and findings[0].path == "cfg.py" and findings[0].line == 1


def test_clean_tree_is_clean(tmp_path):
    repo = make_repo(tmp_path)
    findings, breaches = screen.screen_tree(repo, ["seed.txt"])
    assert findings == [] and breaches == []


def test_binary_file_is_skipped_not_decoded(tmp_path):
    repo = make_repo(tmp_path)
    (Path(repo) / "blob.bin").write_bytes(b"\x00\x01\x02xoxp-1234567890abcde")
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
    (Path(repo) / "logo.png").write_text('xoxp-1234567890abcde')
    findings, _ = screen.screen_tree(repo, ["logo.png"])
    assert findings == []


def test_directory_selection_walks_its_files(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "pkg/a.py", "ok\n")
    write(repo, "pkg/b.py", 'K = "xoxp-1234567890abcde"\n')
    findings, _ = screen.screen_tree(repo, ["pkg"])
    assert [f.path for f in findings] == ["pkg/b.py"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_screen.py -q`
Expected: `ModuleNotFoundError: No module named 'forge.screen'`.

- [ ] **Step 3: Implement**

```python
# shared/lib/forge/screen.py
"""Bounded secret screen over the selected baseline, run BEFORE any provider starts.

Spec §3: whatever the baseline contains is what N cloud-backed full-permission agents
read. Scanning the OUTPUT is too late — the exposure already happened. This is a
high-confidence screen, NOT proof the baseline is secret-free, and it does not contain
agents that keep access to the real $HOME.

The patterns are IMPORTED from scripts/lib/checks.py, never copied: one definition of what
a secret looks like. checks.scan_path is deliberately not reused — it is a single-file
reader with no size cap, no binary guard and no walk, so pointing it at a tree would decode
a 400 MB build log into memory.
"""
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

from .storage import Quota

# High-risk by NAME regardless of content: a credential file whose value shape we do not
# recognise is still a credential file.
BLOCKED_NAMES = (".env", ".envrc", ".netrc", ".pgpass", "credentials", "id_rsa",
                 "id_ed25519", ".npmrc", ".pypirc")


def _checks():
    """Import scripts/lib/checks.py by path — it is not an installed package."""
    if "khenrix_checks" in sys.modules:
        return sys.modules["khenrix_checks"]
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "scripts" / "lib" / "checks.py",      # repo layout
                 here.parents[2] / "lib" / "checks.py"):
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("khenrix_checks", cand)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["khenrix_checks"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("checks.py not found; forge cannot screen without the patterns")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    pattern: str


def _is_high_risk_name(rel: str) -> bool:
    base = Path(rel).name
    return any(base == n or base.startswith(n + ".") for n in BLOCKED_NAMES)


def screen_tree(root, rel_paths, quota: Quota = None):
    """Scan the given repo-relative paths (files or directories).

    Returns (findings, breaches). A non-empty `breaches` means the caller must FAIL the
    run closed with that message — never silently scan less than it claimed to.
    """
    import hashlib
    c = _checks()
    quota = quota or Quota.default()
    root = Path(root)

    targets = []
    for rel in rel_paths:
        p = root / rel
        if p.is_dir():
            targets += [q for q in sorted(p.rglob("*")) if q.is_file() and not q.is_symlink()]
        elif p.is_file() and not p.is_symlink():
            targets.append(p)

    breaches, findings, total = [], [], 0
    if (b := quota.breach(files=len(targets), file_bytes=0, total_bytes=0)):
        return [], [b]

    for p in targets:
        rel = str(p.relative_to(root))
        size = p.stat().st_size
        total += size
        if (b := quota.breach(files=0, file_bytes=size, total_bytes=total)):
            breaches.append(f"{rel}: {b}")
            continue
        if _is_high_risk_name(rel):
            findings.append(Finding(rel, 0, "high-risk-filename"))
        if rel.endswith(c.SCAN_SKIP_SUFFIX):
            continue
        raw = p.read_bytes()
        if b"\x00" in raw[:8192]:          # binary guard: do not decode, do not scan
            continue
        for i, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            for rx in c.SECRET_FAIL:
                m = rx.search(line)
                if m and hashlib.sha256(m.group(0).encode()).hexdigest() not in c.SECRET_ALLOW_SHA:
                    findings.append(Finding(rel, i, rx.pattern))
                    break
    return findings, breaches
```

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_screen.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/screen.py tests/test_forge_screen.py
git commit -m "feat(forge): bounded pre-launch secret screen reusing the repo patterns"
```

---

### Task 5: The clone fleet

**Files:**
- Create: `shared/lib/forge/fleet.py`, `tests/test_forge_fleet.py`

**Interfaces:**
- Consumes: `gitcmd.*`, `baseline.Baseline`.
- Produces:
  - `fleet.clone_seat(repo, baseline, dest, *, template_dir=None) -> Path`
  - `fleet.scrub_env(env: dict, repo_path) -> dict`
  - `fleet.forge_child_env(repo_path, env=None) -> dict` — scrubbed + `LLM_FORGE_DEPTH`
    incremented (the council engine's `child_env` increments `LLM_COUNCIL_DEPTH` only, so
    without this a seat reaching for `/llm-forge` spawns three more write-enabled seats,
    recursively)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_fleet.py
"""Independent clones, not worktrees — and the push vector actually closed (spec §4)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import baseline, fleet, inspect as finspect  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402


def _mk_baseline(repo, run, selected=()):
    f = finspect.repo_facts(repo)
    return baseline.materialize(repo, run, f, list(selected), "r1")


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True).stdout.strip()


def test_clone_checks_out_the_dirty_baseline_not_head(tmp_path):
    """--single-branch alone follows the source's HEAD and would silently drop the
    user's uncommitted work from every seat."""
    repo = make_repo(tmp_path)
    write(repo, "seed.txt", "modified\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk_baseline(repo, run)
    seat = fleet.clone_seat(repo, b, tmp_path / "seat1")
    assert (seat / "seed.txt").read_text() == "modified\n"
    assert _git(seat, "rev-parse", "HEAD") == b.commit


def test_clone_has_no_origin(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    seat = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "seat1")
    assert _git(seat, "remote") == "", "git clone always sets origin; it must be removed"


def test_clone_is_not_hardlinked_to_the_source_objects(tmp_path):
    """A rogue non-git write through a shared inode corrupts the USER's repository."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    seat = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "seat1")
    src = {p.stat().st_ino for p in (Path(repo) / ".git" / "objects").rglob("*") if p.is_file()}
    dst = {p.stat().st_ino for p in (seat / ".git" / "objects").rglob("*") if p.is_file()}
    assert not (src & dst), "clone shares object inodes with the source"


def test_clone_carries_no_hooks_from_a_global_template(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    tmpl = tmp_path / "tmpl" / "hooks"; tmpl.mkdir(parents=True)
    (tmpl / "post-checkout").write_text("#!/bin/sh\ntouch /tmp/forge-hook-ran\n")
    (tmpl / "post-checkout").chmod(0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f"[init]\n\ttemplateDir = {tmpl.parent}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    run = tmp_path / "run"; run.mkdir()
    seat = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "seat1")
    assert not (seat / ".git" / "hooks" / "post-checkout").exists()


def test_scrub_env_removes_only_values_pointing_at_the_repo(tmp_path):
    repo = make_repo(tmp_path)
    env = {"VIRTUAL_ENV": f"{repo}/.venv", "PYTHONPATH": f"{repo}/src",
           "PATH": "/usr/bin:/home/u/.local/share/mise/shims", "HOME": "/home/u",
           "EDITOR": "vim"}
    out = fleet.scrub_env(env, repo)
    assert "VIRTUAL_ENV" not in out and "PYTHONPATH" not in out
    assert out["PATH"] == env["PATH"], "PATH must survive: mise shims live outside the repo"
    assert out["HOME"] == "/home/u" and out["EDITOR"] == "vim"


def test_forge_depth_guard_increments(tmp_path):
    repo = make_repo(tmp_path)
    e1 = fleet.forge_child_env(repo, {"PATH": "/usr/bin"})
    assert e1["LLM_FORGE_DEPTH"] == "1"
    assert fleet.forge_child_env(repo, e1)["LLM_FORGE_DEPTH"] == "2"


def test_seats_are_independent_of_each_other(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = _mk_baseline(repo, run)
    s1 = fleet.clone_seat(repo, b, tmp_path / "s1")
    s2 = fleet.clone_seat(repo, b, tmp_path / "s2")
    (s1 / "seed.txt").write_text("seat one only\n")
    subprocess.run(["git", "-C", str(s1), "commit", "-aqm", "s1"], check=True)
    assert (s2 / "seed.txt").read_text() == "seed\n"
    assert _git(repo, "rev-parse", "HEAD") == b.base_commit, "user repo HEAD moved"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_fleet.py -q`
Expected: `ModuleNotFoundError: No module named 'forge.fleet'`.

- [ ] **Step 3: Implement**

```python
# shared/lib/forge/fleet.py
"""Independent seat clones (spec §4).

Linked worktrees share the parent's .git — refs, objects, config, hooks — so a
permission-bypassed agent can rewrite the user's branches or push, without leaving its cwd.
Seats therefore get real clones. Two details do the actual work:

  * the clone is made from the BASELINE REF, not from HEAD. `--single-branch` follows the
    source's HEAD and would hand every seat a tree without the user's uncommitted work.
  * `origin` is REMOVED. `git clone` always writes remote.origin.url, so a clone that
    merely exists still ships a working push target aimed at the user's repository;
    receive.denyCurrentBranch blocks only the checked-out branch.

Never `--local`/hardlinks: against git's own operations hardlinked objects are safe
(content-addressed, mode 444), but forge's whole premise is a process that may write
outside git's rules, and a truncate through a shared inode corrupts the user's repository.
"""
import os
import shutil
from pathlib import Path

from . import gitcmd


def clone_seat(repo, baseline, dest, *, template_dir=None) -> Path:
    """Clone `baseline.ref` into `dest` and hand back a checked-out, remote-less seat."""
    repo, dest = Path(repo), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # An EMPTY template dir, so a globally configured init.templateDir cannot install
    # hooks into the seat; global/system config off for the same reason (a global
    # core.hooksPath or url.*.insteadOf survives an empty template otherwise).
    tmpl = Path(template_dir) if template_dir else (dest.parent / f".{dest.name}.tmpl")
    tmpl.mkdir(parents=True, exist_ok=True)
    env = {**gitcmd.NO_USER_CONFIG, "GIT_OPTIONAL_LOCKS": "0"}

    gitcmd.git(repo, "clone", "--no-local", "--no-hardlinks", "--no-tags",
               f"--template={tmpl}", f"--revision={baseline.ref}",
               str(repo), str(dest), env_extra=env, timeout=600)

    # Close the push vector the clone just opened. Do this BEFORE any setup or agent runs.
    gitcmd.git(dest, "remote", "remove", "origin", env_extra=env, check=False)

    # Ignore semantics that live in the source repo but are NOT cloned.
    src_exclude = repo / ".git" / "info" / "exclude"
    if src_exclude.is_file():
        dst_exclude = dest / ".git" / "info" / "exclude"
        dst_exclude.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_exclude, dst_exclude)

    if not template_dir:
        shutil.rmtree(tmpl, ignore_errors=True)
    return dest


def scrub_env(env: dict, repo_path) -> dict:
    """Drop variables whose VALUE resolves into the original checkout.

    By predicate, not by name-list. "Sanitize PATH" is exactly wrong on a shim-based
    machine: uvx and friends reach PATH via mise shims outside the repo, and a blanket
    scrub kills the toolchain, failing every candidate for an infrastructure reason
    (spec §4).
    """
    root = str(Path(repo_path).resolve())
    return {k: v for k, v in env.items()
            if not (isinstance(v, str) and root in v)}


def forge_child_env(repo_path, env=None) -> dict:
    """Scrubbed environment plus the forge recursion guard.

    The council engine's child_env increments LLM_COUNCIL_DEPTH only; without a forge
    guard a seat that reaches for /llm-forge spawns three more write-enabled seats, each
    of which can spawn three more.
    """
    base = dict(env if env is not None else os.environ)
    out = scrub_env(base, repo_path)
    cur = int(out.get("LLM_FORGE_DEPTH", "0") or "0")
    out["LLM_FORGE_DEPTH"] = str(cur + 1)
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `uvx pytest tests/test_forge_fleet.py -q`
Expected: 7 passed. If `--revision` is rejected, this git is older than 2.53 — report it
rather than silently substituting `--branch`, which cannot name a non-branch ref.

- [ ] **Step 5: Commit**

```bash
git add shared/lib/forge/fleet.py tests/test_forge_fleet.py
git commit -m "feat(forge): independent seat clones from the baseline ref, origin removed"
```

---

### Task 6: Packaging, closure, and gate wiring

**Files:**
- Modify: `scripts/render.py` (one line), `scripts/lib/checks.py` (one entry),
  `Makefile` (one variable), `docs/superpowers/plans/2026-07-31-llm-forge-b-substrate.md`
  (mark tasks done — optional)
- Test: `tests/test_forge_packaging.py` (create)

**Interfaces:**
- Consumes: everything above.
- Produces: `forge` bundled into every rendered plugin at `<plugin>/lib/forge/`; the
  future `llm-forge` skill's receipt closure covering both `shared/lib/forge` and
  `shared/lib/council`; the forge suites inside `make verify`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forge_packaging.py
"""forge must ship in the rendered plugins and be inside the receipt closure."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_forge_is_bundled_into_the_claude_plugin():
    p = ROOT / "marketplaces" / "claude" / "plugins" / "khenrix-utils" / "lib" / "forge"
    assert (p / "baseline.py").is_file(), "run `make render`"
    assert (p / "fleet.py").is_file()
    assert not (p / "tests").exists(), "SHARED_LIBS must strip tests/"


def test_forge_is_bundled_into_every_cli():
    for cli in ("claude", "codex", "agy"):
        p = ROOT / "marketplaces" / cli / "plugins" / "khenrix-utils" / "lib" / "forge"
        assert (p / "gitcmd.py").is_file(), cli


def test_llm_forge_closure_covers_both_libs():
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    try:
        import checks
    finally:
        sys.path.pop(0)
    dirs = checks.SKILL_EXTRA_DIRS.get("llm-forge", [])
    assert "shared/lib/forge" in dirs
    assert "shared/lib/council" in dirs, "forge imports the council engine"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uvx pytest tests/test_forge_packaging.py -q`
Expected: 3 failures (nothing bundled, no closure entry).

- [ ] **Step 3: Implement**

In `scripts/render.py`, extend `SHARED_LIBS` (anchor: `SHARED_LIBS = ["wikisync", "council"]`):

```python
SHARED_LIBS = ["wikisync", "council", "forge"]
```

In `scripts/lib/checks.py`, extend `SKILL_EXTRA_DIRS` (anchor: the `"llm-council"` entry
added in Plan A):

```python
    # llm-forge drives BOTH shared engines; editing either must stale its receipt. The
    # skill itself arrives in a later plan — the entry is harmless until then.
    "llm-forge":         ["shared/lib/forge", "shared/lib/council"],
```

In `Makefile`, extend the council/forge test variable (anchor: `COUNCIL_TESTS :=`). Add a
sibling variable and include it in the same target that runs `COUNCIL_TESTS`:

```make
FORGE_TESTS := tests/test_forge_storage.py tests/test_forge_inspect.py \
               tests/test_forge_baseline.py tests/test_forge_screen.py \
               tests/test_forge_fleet.py tests/test_forge_packaging.py
```

and in the `council-test` recipe, append `$(FORGE_TESTS)` to the file list passed to
`RUN_PYTEST` (it already carries `-m "not slow"`; keep that). Read the recipe before
editing and preserve its exact `$(call ...)` form.

- [ ] **Step 4: Run to verify it passes**

Run: `make render`
Run: `uvx pytest tests/test_forge_packaging.py -q` → 3 passed.
Run: `make council-test` → exit 0, output shows the forge files collected.
Run: `make verify` → exit 0 (10-minute timeout; advisory receipt warnings are fine).
Run: `make precommit` → exit 0. If it fails on stale receipts, run
`python3 scripts/eval_harness.py --seed-receipt --skill <name>` for each skill it names —
**scoped, never global**: an unscoped reseed overwrites real `provenance: "eval"` receipts,
and `skill-tuneup` currently carries one.

- [ ] **Step 5: Commit**

```bash
git add scripts/render.py scripts/lib/checks.py Makefile marketplaces \
        tests/test_forge_packaging.py
git commit -m "build(forge): bundle the forge lib, wire its closure and gate"
```

---

## Self-review

**Spec coverage.** §1 threat model → stated in `screen.py`/`fleet.py` docstrings and
enforced by scope (nothing here trusts seat-written state, because nothing here reads any).
§2 baseline → Tasks 2–3, including the composite shape, the alternate-index rule, B₀/B₁
identity, literal pathspecs, `update-ref` immediacy, and the scoped rejection set. §3 screen
→ Task 4. §4 clone fleet → Task 5, including the exact-ref transport, origin removal,
global-config neutralization, the predicate-based env scrub, and `LLM_FORGE_DEPTH`. §15
storage → Task 1 (XDG_STATE, hashed dir, 0700, quotas).

**Deliberately out of scope**, each with a later home: harvest and the four inventories
(§6/§7), verifier clones and the generator fixed point (§6.2/§7.2), the journal and state
machine (§14), review/ultrareview (§13), handover (§16), the skill and its evals (§18/§20),
and §9's protected-ref tripwire — which belongs with the state machine, since its
consequence is the `source_diverged` state. No task in this plan launches a provider or
spends a token.

**Placeholder scan.** None. Every step carries runnable code or an exact command. Two
adaptation points are named explicitly rather than left vague: the `check-attr` probe in
Task 2 (adjust the probe, never the assertions) and the `Makefile` recipe form in Task 6
(read before editing).

**Type consistency.** `Baseline.commit` is used as B₁ everywhere including
`fleet.clone_seat`'s `--revision={baseline.ref}`; `RepoFacts` field names match between
`repo_facts`, `rejections`, `replace`, and `baseline.materialize`'s use of
`facts.staged/unstaged/head`; `Quota.breach` keyword arguments are identical in
`storage.py` and `screen.py`; `screen_tree` returns `(findings, breaches)` in both its
docstring and every test.

**One risk worth naming.** Task 5's hardlink test asserts on inode disjointness, which is
the honest check but is filesystem-dependent; on a filesystem without hardlink support the
assertion passes vacuously. It still fails loudly if `--no-hardlinks` is ever dropped on
ext4/WSL2, which is where this runs.
