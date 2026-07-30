# llm-forge Plan A: Shared-Core Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the hardened llm-council engine (`fanout.py`, ~2,300 lines) to
`shared/lib/council/engine.py` behind a module-identity façade, with every process-global
behaviour (signal handler, worktree prune/detach, seat validity, sentinel) parameterized so
the forthcoming forge engine (Plan B) can call the mechanism without inheriting council
policy — while llm-council's observable behaviour stays frozen.

**Architecture:** Characterization tests first (they must stay green through every task);
then the seams are cut *inside* `fanout.py` in place; only then does the file move, with
`fanout.py` becoming a `sys.modules`-swap façade so `import fanout` yields the *engine
module itself* (star-re-export would break monkeypatch-through-globals — spec §17). The
receipt closure, render bundling, and rendered-plugin packaging are wired in the same
sequence the spec's §17 breakage list dictates.

**Tech Stack:** Python 3.11+ stdlib only (repo constraint — no pip deps). pytest via
`uvx pytest` (the system `python3` has no pytest; this matches the Makefile's own
`RUN_PYTEST` fallback). git 2.53 on this machine.

**Spec:** `docs/superpowers/specs/2026-07-30-llm-forge-design.md` §17, §19, §23. This is
Plan A of three; Plans B (forge engine) and C (skill + evals) depend on it.

## Global Constraints

- Python is **stdlib-only**; must run on any Python 3.11+ machine with no install step.
- Never edit anything under `marketplaces/` by hand — it is generated. Run `make render`
  before every commit and include the regenerated `marketplaces/**` files in the commit,
  or `make precommit`'s render-drift check fails.
- `fanout.py --self-test` must exit 0 after **every** task — it is llm-council's gate
  (the skill is exempt from the judge harness; self-test + smoke are its gate per
  CLAUDE.md).
- The characterization suite (Task 1) must pass after every subsequent task. Never edit a
  characterization test to make a later task pass — that is the regression it exists to
  catch. The one sanctioned change is Task 2's handler-ownership test replacing 1d.
- Another agent session may be active in this repo. Before each commit: check
  `.git/index.lock` does not exist and `pgrep -af 'make |render.py'` shows nothing; if
  either fires, wait rather than force.
- Commit messages end with the repo's standard trailer (see any recent commit).

## File Structure

| Path | Role after this plan |
|---|---|
| `shared/lib/council/engine.py` | the engine — everything that was `fanout.py`, plus the Task-2 seams |
| `shared/lib/council/__init__.py` | package marker (empty) |
| `shared/skills/llm-council/scripts/fanout.py` | ~20-line façade: path bootstrap + `sys.modules` swap + `__main__` dispatch |
| `shared/skills/llm-council/tests/stub_provider.py` | unchanged, stays under the skill (wholesale skill copy carries it into plugins; `SHARED_LIBS` strips `tests/`) |
| `tests/test_council_characterization.py` | the frozen-behaviour suite |
| `scripts/render.py` | `SHARED_LIBS` gains `"council"` |
| `scripts/lib/checks.py` | closure gains the engine dir; assertion tightened |

---

### Task 1: Characterization suite

**Files:**
- Create: `tests/test_council_characterization.py`
- Test: itself

**Interfaces:**
- Consumes: current `shared/skills/llm-council/scripts/fanout.py` (`MODES`,
  `ProviderSpec`, `run_council`, `run_member`, `build_real_spec`, `make_readonly`,
  `isolate_agy_worktree`, `install_cleanup_handler`, `MIN_SUBSTANTIVE_CHARS`,
  `_LIVE_WORKTREES`), `shared/skills/llm-council/tests/stub_provider.py`.
- Produces: the invariant suite every later task runs. Helper `import_fanout()` that Tasks
  3–5 reuse in new tests.

- [ ] **Step 1: Write the suite**

```python
"""Characterization of the llm-council engine's observable surface.

These tests pin behaviour that MUST survive the shared-core extraction
(spec 2026-07-30-llm-forge-design.md §17). They are written against the
pre-move fanout.py and must stay green, unmodified, through every task
of Plan A. If one goes red after a refactor, the refactor is wrong.
"""
import json
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FANOUT = ROOT / "shared" / "skills" / "llm-council" / "scripts" / "fanout.py"
STUB = ROOT / "shared" / "skills" / "llm-council" / "tests" / "stub_provider.py"


def import_fanout():
    """Import fanout exactly the way checks.model_crosscheck / eval_harness do."""
    sys.path.insert(0, str(FANOUT.parent))
    try:
        import fanout  # noqa: PLC0415
        return fanout
    finally:
        sys.path.pop(0)


def _stub_spec(fanout, tmp, mode="ok"):
    """A seat backed by tests/stub_provider.py — no network, no auth."""
    return fanout.ProviderSpec(
        "claude", [sys.executable, str(STUB), mode, "--as", "raw"],
        None, fanout.extract_raw, min_chars=0)


# --- API surface -----------------------------------------------------------

def test_api_names_and_defaults():
    f = import_fanout()
    for name in ("MODES", "MODE_TIMEOUT", "ProviderSpec", "run_council",
                 "run_member", "build_real_spec", "make_readonly",
                 "isolate_agy_worktree", "install_cleanup_handler",
                 "extract_raw", "extract_usage", "MIN_SUBSTANTIVE_CHARS"):
        assert hasattr(f, name), name
    assert {"normal", "deep"} <= set(f.MODES)
    for m in ("normal", "deep"):
        assert {"claude", "codex", "agy"} <= set(f.MODES[m])
    spec = f.ProviderSpec("claude", ["true"], None, f.extract_raw)
    assert spec.min_chars == f.MIN_SUBSTANTIVE_CHARS
    assert spec.sentinel is None and spec.cwd is None


def test_underscore_state_reachable():
    # --self-test reads _LIVE_WORKTREES directly; a facade that drops
    # underscore names kills the receipt gate (spec §17).
    f = import_fanout()
    assert isinstance(f._LIVE_WORKTREES, set)


# --- monkeypatch-through-globals (the star-import killer) ------------------

def test_monkeypatch_run_member_is_seen_by_run_provider():
    f = import_fanout()
    calls = []
    orig = f.run_member
    f.run_member = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    try:
        with tempfile.TemporaryDirectory() as td:
            m = f.run_council([_stub_spec(f, td)], retries=0, timeout=30,
                              backoff=0.05, workdir=Path(td), prompt="hi")
    finally:
        f.run_member = orig
    assert calls, ("patching fanout.run_member was NOT observed by "
                   "run_provider — module identity broke (spec §17)")
    assert m["providers"][0]["valid"] is True


# --- CLI surface -----------------------------------------------------------

def test_cli_help_flag_surface():
    r = subprocess.run([sys.executable, str(FANOUT), "--help"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    for flag in ("--prompt-file", "--mode", "--providers", "--out",
                 "--timeout", "--retries", "--allow-writes", "--self-test",
                 "--smoke"):
        assert flag in r.stdout, flag


@pytest.mark.slow
def test_self_test_exits_zero_from_repo_path():
    r = subprocess.run([sys.executable, str(FANOUT), "--self-test"],
                       capture_output=True, text=True, timeout=1200)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


# --- sentinel is per-spec, engine never injects it (forge seam, spec §13) --

def test_engine_does_not_inject_sentinel_itself():
    f = import_fanout()
    with tempfile.TemporaryDirectory() as td:
        spec = _stub_spec(f, td)
        spec.sentinel = "SENTINEL-feedbeefcafe"   # stub output will not quote it
        spec.min_chars = 0
        m = f.run_council([spec], retries=0, timeout=30, backoff=0.05,
                          workdir=Path(td), prompt="hi")
    p = m["providers"][0]
    assert p["valid"] is False and p["reason"] == "did_not_read_input"
```

- [ ] **Step 2: Run the fast subset — must pass against the CURRENT tree**

Run: `uvx pytest tests/test_council_characterization.py -v -m "not slow"`
Expected: 5 passed (if `test_monkeypatch_run_member_is_seen_by_run_provider` fails on the
stub argv, check `tests/stub_provider.py --help` for its real flag names and fix
`_stub_spec` — the stub's interface is authoritative, look at how `_stub_spec` inside
`fanout.py` builds its argv and mirror it).

- [ ] **Step 3: Run the slow self-test wrapper once**

Run: `uvx pytest tests/test_council_characterization.py -v -m slow`
Expected: 1 passed (takes a few minutes — it runs the full engine self-test).

- [ ] **Step 4: Commit**

```bash
git add tests/test_council_characterization.py
git commit -m "test(llm-council): characterization suite ahead of the engine move"
```

---

### Task 2: Cut the process-global seams inside fanout.py (no move yet)

**Files:**
- Modify: `shared/skills/llm-council/scripts/fanout.py`
- Test: `tests/test_council_seams.py` (create)

**Interfaces:**
- Consumes: Task 1's `import_fanout()` pattern.
- Produces (Plan B builds against these exact signatures):
  - `_STATE: dict` with key `"handler_fired"` (replaces module bool `_HANDLER_FIRED`)
  - `install_cleanup_handler(force: bool = False) -> bool` — returns False and installs
    nothing when a foreign SIGTERM handler exists (unless `force=True`)
  - `run_council(..., install_signal_handler: bool = True)` keyword
  - `isolate_agy_worktree(spec, workdir, repo_dir=None, *, prune: bool = True,
    branch: str | None = None)` — `prune=False` skips the repo-wide prune;
    `branch="x"` uses `worktree add -b x` instead of `--detach`
  - `ProviderSpec.validator: Callable | None = None` — signature
    `(exit_code, stdout, stderr, spec) -> tuple[bool, str, str, bool]`; when set,
    `run_provider` calls it INSTEAD of `evaluate`

- [ ] **Step 1: Write the failing tests**

```python
"""Seam tests for the caller-parameterized process-global behaviour (spec §17)."""
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from test_council_characterization import import_fanout, _stub_spec


def test_handler_state_is_a_container():
    f = import_fanout()
    assert not hasattr(f, "_HANDLER_FIRED"), "rebindable bool must be gone"
    assert f._STATE["handler_fired"] is False


def test_install_cleanup_handler_respects_foreign_handler():
    f = import_fanout()
    prev = signal.getsignal(signal.SIGTERM)
    marker = lambda s, fr: None
    signal.signal(signal.SIGTERM, marker)
    try:
        installed = f.install_cleanup_handler()
        assert installed is False
        assert signal.getsignal(signal.SIGTERM) is marker
        assert f.install_cleanup_handler(force=True) is True
        assert signal.getsignal(signal.SIGTERM) is not marker
    finally:
        signal.signal(signal.SIGTERM, prev)


def test_run_council_can_skip_handler_install():
    f = import_fanout()
    prev = signal.getsignal(signal.SIGTERM)
    marker = lambda s, fr: None
    signal.signal(signal.SIGTERM, marker)
    try:
        with tempfile.TemporaryDirectory() as td:
            f.run_council([_stub_spec(f, td)], retries=0, timeout=30,
                          backoff=0.05, workdir=Path(td), prompt="hi",
                          install_signal_handler=False)
        assert signal.getsignal(signal.SIGTERM) is marker
    finally:
        signal.signal(signal.SIGTERM, prev)


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=30)


def test_isolate_worktree_branch_and_no_prune(tmp_path):
    f = import_fanout()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "seed")
    spec = f.ProviderSpec("agy", ["true"], None, f.extract_raw)
    handle = f.isolate_agy_worktree(spec, tmp_path / "wd", repo_dir=str(repo),
                                    prune=False, branch="forge/test/seat")
    assert handle is not None
    r = _git(repo, "branch", "--list", "forge/test/seat")
    assert "forge/test/seat" in r.stdout
    f.remove_agy_worktree(handle)


def test_injectable_validator_replaces_evaluate():
    f = import_fanout()
    with tempfile.TemporaryDirectory() as td:
        spec = _stub_spec(f, td)
        spec.min_chars = f.MIN_SUBSTANTIVE_CHARS  # would normally fail short output
        spec.validator = lambda rc, out, err, s: (True, "ok", "forced", False)
        m = f.run_council([spec], retries=0, timeout=30, backoff=0.05,
                          workdir=Path(td), prompt="hi")
    assert m["providers"][0]["valid"] is True
    assert m["providers"][0]["reason"] == "ok"
```

Note the import: `from test_council_characterization import …` requires
`tests/` on `sys.path`; run pytest from the repo root (as below) and it is.

- [ ] **Step 2: Run to verify they fail**

Run: `uvx pytest tests/test_council_seams.py -v`
Expected: 5 failures (`_STATE` missing, `install_cleanup_handler() takes 0 args`,
unexpected keyword `install_signal_handler`, unexpected keyword `prune`, `ProviderSpec`
has no field `validator`).

- [ ] **Step 3: Implement the seams in fanout.py**

Five mechanical edits (find each site by the quoted anchor text, not by line number):

**(a) State container.** Replace `_HANDLER_FIRED = False` (near the
`_LIVE_WORKTREES` definition) with:

```python
_STATE = {"handler_fired": False}   # mutable container: a facade re-export of a
                                    # rebindable bool would go permanently stale (spec §17)
```

In `_signal_cleanup`, replace the `global _HANDLER_FIRED` / `if not _HANDLER_FIRED:` /
`_HANDLER_FIRED = True` block with:

```python
    if not _STATE["handler_fired"]:   # re-entry guard: a second signal skips straight to exit
        _STATE["handler_fired"] = True
```

In `self_test()`, replace both `globals()["_HANDLER_FIRED"] = False` occurrences (search
for that exact string; there are two) with `_STATE["handler_fired"] = False`.

**(b) Handler ownership.** Replace the `def install_cleanup_handler() -> None:` definition
with:

```python
def install_cleanup_handler(force: bool = False) -> bool:
    """Install _signal_cleanup for SIGTERM/SIGINT — unless the caller already owns the
    handler. run_council once installed unconditionally, which silently replaced an
    embedding orchestrator's own handler (spec §17: 'calling run_council does not
    replace a pre-existing SIGTERM handler'). Returns True iff installed."""
    current = signal.getsignal(signal.SIGTERM)
    foreign = current not in (signal.SIG_DFL, signal.SIG_IGN, None, _signal_cleanup)
    if foreign and not force:
        return False
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _signal_cleanup)
    return True
```

(Keep whatever the current body registers — if it also handles `SIGHUP` or restores
semantics differently, preserve that list verbatim and only add the foreign-handler
check + return value.)

**(c) run_council opt-out.** Change the signature
`def run_council(specs: list[ProviderSpec], *, retries: int, timeout: int,` to add a
trailing keyword `install_signal_handler: bool = True`, and change the bare
`install_cleanup_handler()` call inside it to:

```python
    if install_signal_handler:
        install_cleanup_handler()
```

**(d) Worktree parameters.** Change
`def isolate_agy_worktree(spec: ProviderSpec, workdir: Path, repo_dir: Optional[str] = None)`
to
`def isolate_agy_worktree(spec, workdir, repo_dir=None, *, prune=True, branch=None)`
(keep the existing type annotations on the first three). Gate the prune call:

```python
        if prune:
            subprocess.run(["git", "-C", repo, "worktree", "prune"],
                           capture_output=True, text=True, timeout=10)
```

and build the add command conditionally:

```python
        if branch:
            add_cmd = ["git", "-C", repo, "worktree", "add", "-b", branch, wt, "HEAD"]
        else:
            add_cmd = ["git", "-C", repo, "worktree", "add", "--detach", wt, "HEAD"]
        add = subprocess.run(add_cmd, capture_output=True, text=True, timeout=30)
```

**(e) Injectable validator.** Add to the `ProviderSpec` dataclass, after `min_chars`:

```python
    # When set, run_provider calls this INSTEAD of evaluate() — council seat policy
    # (length floor, sentinel) is not a property of running a provider (spec §8.1).
    # Signature: (exit_code, stdout, stderr, spec) -> (valid, reason, result_text, structured)
    validator: Optional[Callable] = None
```

In `run_provider`, find the call site `evaluate(exit_code, stdout, stderr, spec)` and
replace with `(spec.validator or evaluate)(exit_code, stdout, stderr, spec)`.

- [ ] **Step 4: Run the seam tests and both gates**

Run: `uvx pytest tests/test_council_seams.py tests/test_council_characterization.py -v -m "not slow"`
Expected: all pass.

Run: `python3 shared/skills/llm-council/scripts/fanout.py --self-test`
Expected: exit 0 (echo $? → 0). If the D-series teardown checks fail, revisit (a): the
self-test resets handler state between cases and now does so via `_STATE`.

- [ ] **Step 5: Render and commit**

```bash
make render
git add -A shared/skills/llm-council marketplaces tests/test_council_seams.py
git commit -m "refactor(llm-council): caller-parameterized process-global seams

_STATE container, handler ownership with foreign-handler respect,
run_council install_signal_handler=, worktree prune=/branch=,
ProviderSpec.validator — the spec §17 seams, cut before the move."
```

---

### Task 3: Move the engine; fanout.py becomes a module-identity façade

**Files:**
- Create: `shared/lib/council/__init__.py`, `shared/lib/council/engine.py` (via `git mv`)
- Modify: `shared/skills/llm-council/scripts/fanout.py` (new façade content),
  `scripts/render.py` (one line)
- Test: existing suites + `tests/test_council_facade.py` (create)

**Interfaces:**
- Consumes: Task 2's seams.
- Produces: `import fanout` and `import council.engine` are the **same module object**;
  `shared/lib/council/engine.py` is the editable source of truth. Plan B imports
  `council.engine`.

- [ ] **Step 1: Write the failing façade test**

```python
"""The facade must preserve module IDENTITY, not just names (spec §17)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FANOUT = ROOT / "shared" / "skills" / "llm-council" / "scripts" / "fanout.py"


def test_facade_is_the_engine_module():
    sys.path.insert(0, str(FANOUT.parent))
    sys.path.insert(0, str(ROOT / "shared" / "lib"))
    try:
        import fanout
        import council.engine
        assert fanout is council.engine, (
            "facade must sys.modules-swap to the engine; a star-import copy "
            "breaks monkeypatch-through-globals")
    finally:
        sys.path.pop(0); sys.path.pop(0)


def test_engine_lives_under_shared_lib():
    import_path = ROOT / "shared" / "lib" / "council" / "engine.py"
    assert import_path.is_file()


def test_stub_resolves_from_new_engine_location():
    sys.path.insert(0, str(ROOT / "shared" / "lib"))
    try:
        import council.engine as e
        assert Path(e.STUB).is_file(), e.STUB
    finally:
        sys.path.pop(0)
```

Run: `uvx pytest tests/test_council_facade.py -v`
Expected: 3 failures (no `shared/lib/council`).

- [ ] **Step 2: Move the file and write the façade**

```bash
mkdir -p shared/lib/council
git mv shared/skills/llm-council/scripts/fanout.py shared/lib/council/engine.py
touch shared/lib/council/__init__.py
git add shared/lib/council/__init__.py
```

In `shared/lib/council/engine.py`, replace the `STUB = ...` line (anchor: search for
`stub_provider.py`) with a layout-independent resolver — the old
`Path(__file__).parent.parent / "tests"` now points at `shared/lib/tests`, which does not
exist:

```python
def _find_stub() -> Path:
    """tests/ stays under the SKILL (the wholesale skill copy carries it into rendered
    plugins; the SHARED_LIBS bundle strips tests/). From this file the skill sits at
    <root>/skills/llm-council in a plugin and <shared>/skills/llm-council in the repo —
    the same relative expression either way (spec §17.3)."""
    here = Path(__file__).resolve()
    for cand in (here.parent.parent / "tests" / "stub_provider.py",          # legacy layout
                 here.parents[2] / "skills" / "llm-council" / "tests" / "stub_provider.py"):
        if cand.is_file():
            return cand
    return here.parent.parent / "tests" / "stub_provider.py"   # best effort; self-test will say so

STUB = _find_stub()
```

New `shared/skills/llm-council/scripts/fanout.py`, in full:

```python
#!/usr/bin/env python3
"""Facade: the llm-council engine now lives at <root>/lib/council/engine.py (repo:
shared/lib/council/engine.py; rendered plugin: <plugin>/lib/council/engine.py — the
same relative expression from this file either way).

sys.modules-swap, NOT `from ... import *`: consumers monkeypatch `fanout.run_member`
and expect `fanout.run_provider` to see it, `--self-test` reads underscore state, and
`_write_receipt` shells this file as a program. Only module identity preserves all
three (spec 2026-07-30-llm-forge-design.md §17).
"""
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parents[3] / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import council.engine as _engine          # noqa: E402

sys.modules[__name__] = _engine

if __name__ == "__main__":
    sys.exit(_engine.main())
```

Add `"council"` to `SHARED_LIBS` in `scripts/render.py`:

```python
SHARED_LIBS = ["wikisync", "council"]
```

- [ ] **Step 3: Run everything**

Run: `uvx pytest tests/ -v -m "not slow" -k "council"`
Expected: characterization + seams + façade all pass. The monkeypatch test is the one
that proves the swap worked.

Run: `python3 shared/skills/llm-council/scripts/fanout.py --self-test`
Expected: exit 0 — this exercises the façade's `__main__` dispatch AND `_find_stub()`.

- [ ] **Step 4: Render and check the bundle**

```bash
make render
ls marketplaces/claude/plugins/khenrix-utils/lib/council/engine.py   # must exist
ls marketplaces/claude/plugins/khenrix-utils/skills/llm-council/tests/stub_provider.py  # must exist
```

- [ ] **Step 5: Commit**

```bash
git add -A shared marketplaces scripts/render.py tests/test_council_facade.py
git commit -m "refactor(llm-council): engine -> shared/lib/council; fanout.py becomes a module-identity facade"
```

---

### Task 4: Receipt closure follows the engine

**Files:**
- Modify: `scripts/lib/checks.py` (two sites)
- Test: `tests/test_council_facade.py` (extend)

**Interfaces:**
- Consumes: `checks.SKILL_EXTRA_DIRS` (the existing wikisync pattern),
  `checks.source_manifest(ROOT, skill)`.
- Produces: editing `shared/lib/council/engine.py` stales the llm-council receipt again
  (and, in Plan C, llm-forge's).

- [ ] **Step 1: Write the failing test (append to `tests/test_council_facade.py`)**

```python
def test_closure_includes_engine_at_new_path():
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    try:
        import checks
        rels = [r for r, _ in checks.source_manifest(checks.ROOT, "llm-council")]
    finally:
        sys.path.pop(0)
    assert any(r == "shared/lib/council/engine.py" for r in rels), (
        "engine left the closure — make precommit would silently stop "
        "protecting llm-council (spec §17 breakage 1)")
    assert any(r.endswith("scripts/fanout.py") for r in rels)   # facade still counted
```

Run: `uvx pytest tests/test_council_facade.py -v`
Expected: the new test FAILS (engine path not in closure).

- [ ] **Step 2: Wire the closure**

In `scripts/lib/checks.py`, extend `SKILL_EXTRA_DIRS` (anchor: the wikisync entries):

```python
SKILL_EXTRA_DIRS = {
    "khenrix-wiki-add":  ["shared/lib/wikisync"],
    "khenrix-wiki-sync": ["shared/lib/wikisync"],
    # the council engine moved out of the skill dir; without this line, engine edits
    # no longer move llm-council's source_hash and precommit stops gating them.
    "llm-council":       ["shared/lib/council"],
}
```

Tighten the self-check (anchor: `"llm-council closure includes fanout.py"`) from a
filename-substring match to the full path, plus the façade:

```python
    ok.append(("llm-council closure includes the moved engine",
               any(r == "shared/lib/council/engine.py"
                   for r, _ in source_manifest(ROOT, "llm-council"))))
```

- [ ] **Step 3: Run**

Run: `uvx pytest tests/test_council_facade.py -v` → all pass.
Run: `make verify`
Expected: exits 0. The receipt gate will WARN that llm-council's receipt is stale — that
is correct and expected (advisory in `verify`); Task 6 reseeds.

- [ ] **Step 4: Commit**

```bash
make render
git add -A scripts/lib/checks.py marketplaces tests/test_council_facade.py
git commit -m "fix(checks): llm-council closure follows the engine to shared/lib/council"
```

---

### Task 5: Rendered-plugin packaging test

**Files:**
- Test: `tests/test_council_facade.py` (extend)

**Interfaces:**
- Consumes: `make render` output under `marketplaces/claude/`.
- Produces: the assertion nobody previously made — self-test green **from the plugin
  layout** (spec §17.3: the repo run staying green while the plugin run dies is the worst
  available break).

- [ ] **Step 1: Write the failing-or-passing test (append)**

```python
import pytest

@pytest.mark.slow
def test_self_test_exits_zero_from_rendered_plugin():
    plugin_fanout = (ROOT / "marketplaces" / "claude" / "plugins" / "khenrix-utils"
                     / "skills" / "llm-council" / "scripts" / "fanout.py")
    assert plugin_fanout.is_file(), "run `make render` first"
    r = subprocess.run([sys.executable, str(plugin_fanout), "--self-test"],
                       capture_output=True, text=True, timeout=1200)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
```

(Add `import subprocess` to the file's imports if not present.)

- [ ] **Step 2: Render, then run it**

Run: `make render && uvx pytest tests/test_council_facade.py -v -m slow`
Expected: PASS. If it fails on stub resolution, `_find_stub()`'s plugin branch is wrong —
verify `marketplaces/claude/plugins/khenrix-utils/skills/llm-council/tests/` exists and
recount `parents[]` from the *plugin's* `lib/council/engine.py`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_council_facade.py
git commit -m "test(llm-council): self-test must pass from the rendered plugin layout"
```

---

### Task 6: Reseed receipts, full gates, refresh installed CLIs

**Files:**
- Modify: `evals/*/receipt.json` (regenerated, all skills — `render.py` is in
  `GLOBAL_INPUTS`, so the bundling change stales every receipt)

**Interfaces:**
- Consumes: `scripts/eval_harness.py --seed-receipt` (global flag; `--skill` optional).
- Produces: `make precommit` green; installed CLIs running the new façade.

- [ ] **Step 1: See what precommit says**

Run: `make precommit`
Expected: FAILS listing stale receipts (llm-council at minimum; likely all ten, because
`render.py` changed). This failure is the gate working.

- [ ] **Step 2: Reseed**

Run: `python3 scripts/eval_harness.py --seed-receipt`
Expected: receipts rewritten for the current source state.

- [ ] **Step 3: Full gates**

Run: `make verify` → exit 0, no receipt warnings.
Run: `make test` → exit 0 (runs the engine self-test).
Run: `make precommit` → `✅ precommit clean`.

- [ ] **Step 4: Commit with the mandated rationale**

```bash
git add -A evals
git commit -m "chore(evals): reseed receipts after mechanical engine relocation

render.py is in GLOBAL_INPUTS, so bundling the council engine into
SHARED_LIBS staled every receipt. No skill behaviour changed: the facade
preserves module identity and the CLI surface byte-for-byte, gated by the
characterization suite + fanout --self-test from both repo and rendered
layouts. Reseeding per CLAUDE.md's mechanical-render allowance."
```

- [ ] **Step 5: Refresh installed CLIs and smoke live**

Run: `make khenrix-refresh`
Expected: plugin pushed into all three CLIs (they cache by version).

Run: `make smoke-llm-council`
Expected: exit 0 — one real provider answers "pong" through the new façade. Costs one
cheap provider call; needs auth. If it fails on auth/quota, report the reason and stop —
do not mark the task complete on a red smoke.

---

## Self-review

- **Spec coverage (§17):** façade + module identity (T3), `SHARED_LIBS` not `LIB_SCRIPTS`
  (T3), closure + tightened positive assertion (T4), rendered-plugin self-test (T5),
  receipts reseed with rationale (T6), handler ownership / `_STATE` / prune / `--detach` /
  validator seams (T2), consumers exercised (T1: import-path, `__main__`, monkeypatch;
  `make verify` in T4 covers `checks.model_crosscheck` end-to-end since the façade
  self-bootstraps its path). §19's `MODE_TIMEOUT["forge"]` and the agy timeout-reason
  mapping are **deliberately Plan B** — they are forge behaviour, not extraction.
- **Placeholders:** one soft spot acknowledged in-line — Task 2(b) says "preserve the
  current signal list verbatim"; that is an instruction to read the anchor site, not
  invented code. No TBDs.
- **Type consistency:** `install_cleanup_handler(force=False) -> bool`,
  `install_signal_handler: bool = True`, `prune: bool = True`, `branch: str | None`,
  `ProviderSpec.validator` — names match across T2 tests, T2 implementation, and the
  Interfaces blocks Plan B will consume.
