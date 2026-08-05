# llm-forge M — the load-bearing residuals

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the seven deferred findings that something already shipped *rests on* — the change predicates Plan L's brackets are built from, the parser a deferred fix names as its mechanism, the identity check coverage takes on unverified rows, and the ranked outcome L1.2 had to renumber around.

**Architecture:** Plan L's deferral list marked seven entries ⚠ — "load-bearing for something this plan schedules" — and shipped anyway, on the argument that a plan claiming otherwise would be a verdict reading cleaner than its evidence. This is the plan that pays that down. Ordering is by *what depends on what*, not by severity: **M1** and **M2** repair the two change predicates every bracket in the package is written in terms of, because a bracket over a blind predicate is a measurement of nothing and both L1.4 and L1.5 sit on top of one. **M3** repairs the pytest parser that Plan L's own deferral list names as the mechanism of a *later* fix, so that fix is not built on a parser which manufactures measured sets. **M4** makes coverage's row identity real. **M5** decides the ranked outcome L1.2 deliberately did not renumber. **M6** wires §12.3's oscillation, which Plan I₂ specified and no plan scheduled — sequenced here because K4 has now edited the exact call site. **M7** closes the TOCTOU half of the B₁ screen.

**Tech Stack:** Python 3.11+, stdlib only. `pytest` via `uvx --with pytest pytest` (this repo's `python3` cannot import it). `make verify` is the gate; `make precommit` is the commit boundary.

## Global Constraints

- Python is **stdlib-only**. No pip dependencies. It must run on any Python 3.11+ machine with no install step.
- `make verify` is the gate; `make precommit` is the commit boundary. A skill change needs an eval receipt (CLAUDE.md, "Skill changes require evals").
- Commit directly to `main`. Do not push without asking.
- Reconcile is non-destructive by design: it only adds missing entries or updates ones tagged `khenrix-managed`, and never removes machine-specific config. Preserve this invariant.
- Every `tests/test_forge_*.py` must be named in `FORGE_TESTS` or `FORGE_SLOW_TESTS`; `test_forge_packaging.py` fails on one in neither.
- The two binding rules: **FAIL CLOSED**, and **A VERDICT MUST NEVER READ CLEANER THAN ITS EVIDENCE**. The founding premise: **a check the builder could have rigged is not a check.**

## Test discipline — the one rule this codebase keeps breaking

Every hole closed in this package so far was closed **at the spelling it was found in**, and the suite then tested that it had been. The named instances: a completeness test that restates `_read_report`'s own gap predicate; `test_forge_runner.py:951` proving a candidate can rewrite the setup entrypoint and then asserting exactly ONE resulting mutation; `test_forge_verify.py:1525` repointing a symlink but never modifying its referent; `kind` added to review's inventory digest and never carried to harvest's `snapshot.diff`; an agreement test between two copies of a predicate that passes because they agree **and are both wrong**.

Every task below must assert the **external question** — "can this inventory distinguish these two trees?", "does anything call this?", "what ELSE can this proven capability do?" — never a restatement of the implementation. Where a proposed test would only confirm the code agrees with itself, the task says so and gives a different one.

## Naming: Plan K's "Plan L" is not the Plan L that exists

Plan K's Order-of-work section defines a follow-on it calls **Plan L**, whose L0 is "close Task 4's reviewer-root residual". No such plan was ever written. The document at `docs/superpowers/plans/2026-08-04-llm-forge-l-closing-the-council-review.md` is a **different** Plan L: it closes the seven-slice council review and has been executed and pushed. **Plan K's "Plan L" work is not in this plan either** — it is the review-verb band, and it goes to **Plan N** with the rest, listed at the end. Read "Plan L" in Plan K's prose as "the review-verb band, unscheduled".

---

## Task 1: The two change predicates every bracket is written in terms of

**Why first:** `snapshot.py` is the inventory the whole package's before/after brackets are taken with — `review.loop`'s round bracket, `harvest`'s artifact set, `verify`'s worktree identity. Two of its fields are blind, and both blindnesses were found *inside* the path of a fix Plan L shipped:

- **`snapshot.py:206`** records `st.st_mode & 0o777`, which drops setuid, setgid and sticky. **Measured end to end, not at the leaf:** `review.worktree_identity` — the round bracket three unattended bypass-permissions reviewers are measured by, and the one K4 built them a private clone for — returns the **identical digest** before and after `chmod u+s` on a tracked file. So a reviewer can make a binary setuid inside its own review tree and the round reports `after_digest == before_digest`: the tree undisturbed, findings admissible, and the fix budget spent on them. `_special_entry` (`snapshot.py:132`) masks the same way, so the FIFO and device branch is blind to it too.
- **`snapshot.diff` (`snapshot.py:210`)** compares content, mode and size and **not `kind`**, while `review._inventory_digest` (`review.py:1337`) DOES include it and says so in a comment. That is **two definitions of "did this path change", maintained separately** — this project's recurring shape, and the one that produced "an agreement test between two copies of a predicate that passes because they agree and are both wrong".

  **MEASURED, AND THE FINDING IS NARROWER THAN PLAN L RECORDED IT.** The plan's first draft asserted that a file replaced by a FIFO is a silent drop in `harvest`. It is not: `_special_entry` folds the file type into the DIGEST (`sha256(f"special:{S_IFMT}")`) and `_symlink_entry` digests the target text, so every constructible type change already moves the digest. Both proposed FIFO tests **passed before any fix** — exactly the "test that confirms the code agrees with itself" failure this plan exists to prevent. The reproduction attempts are in the commit that corrected this task.

  So the `kind` change is **structural, not a reproducible miss**: it collapses two predicates into one so that a future change to `_special_entry`'s digest cannot silently separate them. It ships with a test that asserts the structural property, never a fabricated miss.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/snapshot.py` (the `Entry` mode mask ~`:206`; `diff` ~`:210-240`)
- Test: `/home/khenrix/git/khenrix-utils/tests/test_forge_snapshot.py`

**Interfaces:**
- Consumes: `snapshot.Entry(rel, digest, mode, size, kind)`; `stat.S_ISREG`, `stat.S_IMODE`.
- Produces: no signature change. `Entry.mode` widens from the permission bits to the full **file-type-stripped** mode (`stat.S_IMODE`, which is `& 0o7777` — permission bits plus the three special bits), and `diff` gains `kind` as a compared field.

**The fail-open this task must not have:** widening the mask must not make the inventory record the file **type** bits as well, because `kind` already carries that and two fields answering one question is how "two spellings of one predicate" gets built for the eighth time. `stat.S_IMODE` is the correct call and `& 0o7777` spelled by hand is the same value — use `S_IMODE`, so the intent is in the name.

- [ ] **Step 1: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_snapshot.py`:

```python
def test_the_review_round_bracket_sees_a_setuid_bit(tmp_path):
    """THE EXTERNAL QUESTION, asked at the level the answer matters: not "does Entry hold a
    mode" but "can the round bracket distinguish these two trees". Measured before the fix:
    `worktree_identity` returned the SAME digest either side of `chmod u+s`, so a reviewer
    could make a binary setuid in its own tree and the round read `after == before` — tree
    undisturbed, findings admissible, fix budget spent on them.

    This test lives in `test_forge_snapshot.py` beside the predicate it is about, and it
    reaches through `review.worktree_identity` on purpose: a test at `snapshot.take` alone
    would leave the composition unasserted, which is how `kind` came to be carried by review's
    digest and not by `snapshot.diff`.
    """
    from forge import review, storage as st_mod
    root = tmp_path / "t"; root.mkdir()
    p = root / "run.sh"; p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(0o755)
    quota = st_mod.Quota(max_files=1000, max_file_bytes=10 ** 7, max_total_bytes=10 ** 8)
    before, _ = review.worktree_identity(root, quota)
    p.chmod(0o4755)
    after, _ = review.worktree_identity(root, quota)
    assert before != after, "the round bracket read a setuid binary as no change at all"


def test_the_inventory_sees_a_setuid_bit(tmp_path):
    """The same claim at the leaf, so a failure says WHICH layer lost the bit."""
    root = tmp_path / "t"; root.mkdir()
    p = root / "run.sh"; p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(0o755)
    before, breaches = snapshot.take(root)
    assert breaches == []
    p.chmod(0o4755)
    after, breaches = snapshot.take(root)
    assert breaches == []
    assert before != after, "chmod u+s left a byte-identical inventory"
    assert snapshot.diff(before, after) == {"run.sh": "modified"}


def test_the_inventory_sees_setgid_and_sticky_too(tmp_path):
    """The discrimination check for the bit above. One bit fixed and two left blind is the
    same defect with a smaller surface."""
    root = tmp_path / "t"; root.mkdir()
    p = root / "run.sh"; p.write_text("#!/bin/sh\n", encoding="utf-8")
    for mode in (0o755, 0o2755, 0o1755):
        p.chmod(mode)
        inv, _ = snapshot.take(root)
        yield_mode = inv["run.sh"].mode
        assert yield_mode == mode, (mode, yield_mode)


def test_diff_compares_kind_even_when_every_other_field_matches():
    """ONE PREDICATE, NOT TWO. `review._inventory_digest` includes `kind` and says so;
    `snapshot.diff` did not. Today they agree on every tree that can be built, because
    `_special_entry` folds the file type into the digest and `_symlink_entry` digests the
    target text — so this is not a reproducible miss and no test here should pretend it is
    (two drafts of this task asserted a FIFO miss that PASSED before any fix).

    What it is, is two definitions of "did this path change" maintained separately, one field
    apart. This states the predicate directly over hand-built entries, which is the only way
    to assert it: a filesystem cannot produce two entries that agree on digest, mode and size
    and differ in kind, and that is precisely why the gap is invisible until the day
    `_special_entry`'s digest changes.
    """
    a = {"x": snapshot.Entry("x", "d", 0o644, 0, "file")}
    b = {"x": snapshot.Entry("x", "d", 0o644, 0, "special")}
    assert snapshot.diff(a, b) == {"x": "modified"}


def test_the_two_inventory_predicates_read_the_same_fields():
    """The structural claim, asserted where it can actually fail: `review._inventory_digest`
    builds its row from five fields and `snapshot.diff` must compare the same five. A future
    field added to `Entry` and wired into one of them is the next instance of this defect."""
    import dataclasses
    fields = {f.name for f in dataclasses.fields(snapshot.Entry)}
    assert fields == {"path", "digest", "mode", "size", "kind"}, (
        "Entry gained or lost a field — check that BOTH snapshot.diff and "
        "review._inventory_digest were updated, not just one")
```

> `os` is no longer needed by these tests — the FIFO cases were removed as unreproducible. `test_the_inventory_sees_setgid_and_sticky_too` is written with a `for` loop and a plain `assert`; the `yield_mode` name is a local, not a generator — do not turn this into a `pytest.mark.parametrize` that hides which bit failed.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_snapshot.py -k "setuid or setgid or kind or predicates"`
Expected: FAIL — the first two on `before != after` / mode inequality, and
`test_diff_compares_kind_even_when_every_other_field_matches` on `diff` returning `{}`.
`test_the_two_inventory_predicates_read_the_same_fields` should PASS already: it is the
invariant the change must not break, written first so a break is visible.

- [ ] **Step 3: Widen the mode mask**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/snapshot.py`, change:

```python
            entries[rel] = Entry(rel, _digest(p), st.st_mode & 0o777, st.st_size, "file")
```

to:

```python
            # `S_IMODE`, NOT `& 0o777`. The permission mask dropped setuid, setgid and sticky,
            # so `chmod u+s` on a tracked file left a byte-identical inventory and every
            # bracket written in terms of this predicate — the review round's, harvest's
            # artifact set, verify's worktree identity — reported the tree undisturbed. The
            # FILE TYPE bits stay out: `kind` already carries that answer, and two fields
            # answering one question is how this package builds two spellings of a predicate.
            entries[rel] = Entry(rel, _digest(p), stat.S_IMODE(st.st_mode), st.st_size, "file")
```

**`_special_entry` masks the same way and must change too** — verified, `snapshot.py:132` reads `st.st_mode & 0o777`. A special entry whose mode is recorded differently from a regular one is the same defect wearing the other branch, and a FIFO with the setgid bit would stay invisible while a regular file no longer was.

- [ ] **Step 4: Compare `kind` in `diff`**

In `diff`, add `kind` to the compared tuple and replace the docstring paragraph that concedes the gap:

```python
def diff(before: dict, after: dict) -> dict:
    """path -> added | removed | modified. Compares content, mode, size and KIND.

    `kind` USED TO BE LEFT OUT, with a docstring saying a file replaced by a symlink was
    "caught only incidentally — via the digest, and via the mode 0 / size 0 the symlink branch
    records", and calling that "reliable in practice but a side effect, not a rule". It was
    neither: `test_forge_review.py:1852` demonstrated the FIFO/symlink collision against
    review's own inventory digest, and the fix was never carried here — so `harvest`, which
    reads this function, drops a builder's artifact replaced by a FIFO of the same size
    without a word. An incidental rule is not a rule; this compares the field.
    """
```

The comparison itself is whichever expression the existing body uses, extended by one field — read it and extend it rather than rewriting the loop.

- [ ] **Step 5: Run the tests to verify they pass, then the suites that read this predicate**

Run: `uvx --with pytest pytest -q tests/test_forge_snapshot.py tests/test_forge_harvest.py tests/test_forge_review.py tests/test_forge_verify.py`
Expected: PASS. If an existing test breaks, read it before changing it: a test asserting that two trees differing only in a special bit compare EQUAL is the defect asserted, and it is corrected rather than accommodated.

- [ ] **Step 6: Commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/snapshot.py tests/ marketplaces
make verify
git commit -m "fix(forge): the bracket every measurement rests on could not see a setuid bit or a FIFO"
```

---

## Task 2: `GIT_LITERAL_PATHSPECS` is not in `HOSTILE_ENV`

**Why second:** `GIT_LITERAL_PATHSPECS=1` ambient makes the `git diff` L1.5 guards **exit 0 with an empty patch**. `baseline.py:429` pins it OFF for its own index build and `baseline.py:426` explains why in full — so the reason is already written down in this repository, one module over, and the variable is simply absent from the tuple every other call site scrubs.

**THE MECHANISM IS NOT GLOBBING, AND THE FIRST DRAFT OF THIS TASK HAD IT BACKWARDS.** Its test used a filename containing `[1]` on the theory that `LITERAL=1` stops a glob matching. Measured: that test **passes before any fix** — with `LITERAL=1` the literal name `a[1].txt` matches the file `a[1].txt` perfectly well; it is the *unset* case that treats the name as a glob.

What `LITERAL=1` actually disables is **pathspec magic**, and this package passes magic almost everywhere:

| site | pathspec | what it becomes under `LITERAL=1` |
|---|---|---|
| `bundle.py:287` | `:(literal)<path>` | a literal filename `":(literal)<path>"` — matches nothing |
| `harvest.py:105` | `:(literal)<path>` | same |
| `verify.py:1232` | `:(literal)<path>` on `git add -f` | same — **the verifier stages nothing** |
| `runstate.py:543` | `:/` on `ls-files` | a literal path named `:/` — matches nothing |
| `baseline.py:433` | `:/` on `add -u` | **already pinned OFF**, with a comment saying `add -u` "would look for a directory named `:/`" |

Measured on git 2.53 in a repo holding one modified tracked file: `diff --name-only -- :(literal)a.txt` answered `a.txt` clean and **`''` with `LITERAL=1`, exit 0 both times**; `ls-files --full-name -- :/` did the same. So the blast radius is wider than "the diff L1.5 guards": `verify.py:1232` is a `git add -f` that stages **nothing** and reports success, which is a candidate verified over an empty change. `baseline.py` already knows this exact mechanism and pins against it; nowhere else does.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/gitcmd.py` (`HOSTILE_ENV`, ~`:149-164`)
- Test: `/home/khenrix/git/khenrix-utils/tests/test_forge_seams.py` (`_KNOWN_REDIRECTORS`'s neighbourhood)

**Interfaces:**
- Consumes: `gitcmd.HOSTILE_ENV`, `gitcmd.git(repo, *args, env_extra=)`.
- Produces: `HOSTILE_ENV` gains one entry. No signature change.

**The fail-open this task must not have:** the test must not be an enumeration of what the tuple contains. This repository already learned that lesson — `test_forge_seams.py`'s `_KNOWN_REDIRECTORS` restates a literal set **on purpose**, with a comment saying "a test that only reads the live list cannot notice the list shrinking". An enumeration fails open on any member nobody enumerated. Assert the **behaviour**: with the variable ambient, a `git` call this package makes must still see the path.

- [ ] **Step 1: Write the failing test**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_seams.py`:

```python
def test_an_ambient_literal_pathspecs_cannot_blind_a_git_this_package_runs(tmp_path,
                                                                           monkeypatch):
    """THE EXTERNAL QUESTION, and it is not "is the name in the tuple".

    `GIT_LITERAL_PATHSPECS=1` disables pathspec MAGIC, and this package passes magic at four
    sites — `:(literal)<path>` in `bundle`, `harvest` and `verify`, and `:/` in `runstate`.
    Under an ambient `1` each becomes a literal filename that matches nothing, and git EXITS 0
    over it: an empty patch read as "this seat changed nothing", and at `verify.py:1232` a
    `git add -f` that stages nothing and reports success.

    Asserted through `gitcmd.git` rather than by reading HOSTILE_ENV, because a test that
    reads the tuple passes over exactly the member nobody added to it. And asserted on MAGIC
    rather than on a glob: a draft of this test used a `[1]` filename on the theory that
    LITERAL stops globbing, and it passed before any fix — under LITERAL a literal name
    matches its file perfectly well.
    """
    repo = make_repo(tmp_path)
    write(repo, "a.txt", "one\n")
    commit_all(repo, "seed")
    write(repo, "a.txt", "two\n")
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")
    out = _git_through_package(repo, "diff", "--name-only", "--", ":(literal)a.txt")
    assert out.strip() == "a.txt", (
        "an ambient GIT_LITERAL_PATHSPECS blinded a pathspec this package actually passes")


def test_the_top_level_magic_runstate_passes_survives_it_too(tmp_path, monkeypatch):
    """The discrimination check on the OTHER magic in use. `runstate.py:543` passes `:/`, and
    `baseline.py:433` already pins LITERAL off for exactly this — its comment says `add -u`
    "would look for a directory named `:/`". One of the two was defended and the other was
    not, which is the same predicate protected in one place and not the next."""
    repo = make_repo(tmp_path)
    write(repo, "a.txt", "one\n")
    commit_all(repo, "seed")
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")
    out = _git_through_package(repo, "ls-files", "--full-name", "--", ":/")
    assert out.strip() == "a.txt"


def _git_through_package(repo, *args):
    return gitcmd.git(repo, *args, env_extra=gitcmd.READONLY).stdout
```

> Read the existing `test_the_seat_environment_admits_no_git_redirector` before writing this: it is the precedent for *why* the assertion is behavioural, and its `_KNOWN_REDIRECTORS` comment is the argument. Place `_git_through_package` with the module's other helpers rather than after its first use.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uvx --with pytest pytest -q tests/test_forge_seams.py -k "literal_pathspecs"`
Expected: FAIL — both, on `assert '' == 'a.txt'`. If either PASSES on your machine, do not proceed: measure why (`git --version`, and whether the shell exported the variable) before concluding the hole is closed.

- [ ] **Step 3: Add the entry**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/gitcmd.py`, inside `HOSTILE_ENV`, after `GIT_TEMPLATE_DIR`:

```python
    # What turns every pathspec into a literal string, DISABLING PATHSPEC MAGIC — which this
    # package passes at four sites: `:(literal)<path>` in `bundle`, `harvest` and `verify`,
    # and `:/` in `runstate`. Under an ambient `1` each becomes a filename that matches
    # nothing and git EXITS 0 over it, so `harvest`'s diff reports an empty patch (read as
    # "this seat changed nothing") and `verify.py:1232`'s `git add -f` stages nothing and
    # reports success — a candidate verified over an empty change. `baseline.py:426` already
    # pins this off for its own `:/` and says why in full; the variable was simply never added
    # to the tuple every OTHER call site scrubs. A silent, exit-0 blinding.
    "GIT_LITERAL_PATHSPECS",
```

- [ ] **Step 4: Run the test to verify it passes, then the seam and harvest suites**

Run: `uvx --with pytest pytest -q tests/test_forge_seams.py tests/test_forge_harvest.py tests/test_forge_baseline.py`
Expected: PASS. `baseline.py` pins the variable to `"0"` in `env_extra`, which is applied AFTER the scrub — so its explicit pin still wins and nothing there changes.

- [ ] **Step 5: Commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/gitcmd.py tests/test_forge_seams.py marketplaces
make verify
git commit -m "fix(forge): one ambient variable made every unpinned git diff exit 0 with an empty patch"
```

---

## Task 3: `progress.pytest_fingerprints` — the parser a deferred fix is built on

**Why third:** Plan L's deferral list records that s2 H2's deferred fix names this parser as its mechanism — *"a parser already exists"*. Deferring both would build that fix on a parser that manufactures measured sets. Three defects, all in `shared/lib/forge/progress.py:42-117`:

- **`_PYTEST_BANNERS` holds bare substrings** — `" passed"` and `" failed"`. Any runner whose output contains the word " passed" is read as pytest, and this module's whole domain claim is "pytest, recognised by its own banner".
- **`_FAILED = re.compile(r"^FAILED (\S+)")`** stops at the first whitespace, so `FAILED tests/t.py::test_x[a b]` captures `tests/t.py::test_x[a`. **Two different parametrized failures compare equal** — the exact defect shape this project keeps re-finding.
- **A nonzero exit with one `FAILED` line returns that set as complete** (`return ids or None`). pytest's exits 2, 3 and 4 are collection error, internal error and usage error; a run that collected half the suite, printed one `FAILED`, then died reports a one-element *complete* failure set.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/progress.py:42-45,107-117`
- Test: `/home/khenrix/git/khenrix-utils/tests/test_forge_progress.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `progress.pytest_fingerprints(stdout, stderr, exit_code) -> frozenset | None` — unchanged signature, strictly narrower domain and strictly more `None`.

**The fail-open this task must not have:** narrowing the banner set must not make an ordinary green pytest run return `None`, which would turn every §12.3 progress question `unresolved` and make the whole mechanism decline by construction. The banner must be one pytest actually prints on every run — verify against real output rather than reasoning about it.

- [ ] **Step 1: Confirm the three defects still reproduce**

Run the three inputs through the live parser and read the answers:

```
progress.pytest_fingerprints("ok  \t3 passed\n", "", 0)
progress.pytest_fingerprints("short test summary info\nFAILED t.py::test_x[a b]\nFAILED t.py::test_x[a c]\n", "", 1)
progress.pytest_fingerprints("short test summary info\nFAILED t.py::test_x - E\n!!! Interrupted !!!\n", "", 4)
```

Measured 2026-08-05, all three reproduce:

| input | answer | what it means |
|---|---|---|
| go test output, exit 0 | `frozenset()` | another runner read as a green pytest run |
| two parametrized failures | `frozenset({'t.py::test_x[a'})` | **two different failures, one id** |
| collection interrupted, exit 4 | `frozenset({'t.py::test_x'})` | a partial set reported as complete |

- [ ] **Step 2: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_progress.py`:

```python
def test_two_parametrized_failures_do_not_compare_equal():
    """`\\S+` truncated at the first space, so `test_x[a b]` and `test_x[a c]` both captured
    `test_x[a` — two DIFFERENT failures with one id. That is this project's recurring defect
    shape and it is in the parser §12.3's progress question is answered from."""
    out = ("=== short test summary info ===\n"
           "FAILED tests/t.py::test_x[a b] - AssertionError\n"
           "FAILED tests/t.py::test_x[a c] - AssertionError\n")
    ids = progress.pytest_fingerprints(out, "", 1)
    assert ids is not None
    assert len(ids) == 2, ids
    assert "tests/t.py::test_x[a b]" in ids


def test_another_runners_output_is_not_read_as_pytest():
    """THE DOMAIN CLAIM IS "pytest, recognised by its own banner", and `" passed"` is not a
    banner — it is two words that appear in almost every test runner's summary. A parser
    outside its declared domain answering anything at all is the fail-open."""
    assert progress.pytest_fingerprints("go test ./...\nok  \t3 passed\n", "", 0) is None
    assert progress.pytest_fingerprints("cargo test\n7 passed; 0 failed\n", "", 0) is None


def test_a_real_green_pytest_run_is_still_an_honest_empty_set():
    """THE DISCRIMINATION CHECK FOR THE NARROWING, and it caught a wrong first draft: a green
    `pytest -q` run prints NO section rule and NO session header — its whole output is
    `35 passed in 0.08s` — so narrowing to literal section rules would have returned `None`
    for every clean gate run and made §12.3 decline on its most common case. Measured output,
    verbatim, not a guess."""
    assert progress.pytest_fingerprints(
        "...................................    [100%]\n35 passed in 0.08s\n", "", 0
    ) == frozenset()
    # ...and the full-output form, which is a different shape entirely.
    assert progress.pytest_fingerprints(
        "============================= test session starts =====================\n"
        "platform linux -- Python 3.14.6, pytest-9.1.1\n\n35 passed in 0.08s\n", "", 0
    ) == frozenset()


def test_a_make_recipe_that_merely_says_passed_is_not_pytest():
    """The other side of the same discrimination. `" passed"` as a bare substring matched a
    Makefile echoing "all tests passed" — and returned an HONEST-LOOKING empty failure set
    for a gate this parser never read."""
    assert progress.pytest_fingerprints("all tests passed\n", "", 0) is None
    assert progress.pytest_fingerprints(
        "test result: ok. 7 passed; 0 failed; finished in 0.01s\n", "", 0) is None


def test_a_collection_error_is_not_a_complete_failure_set():
    """pytest exit 4. The run printed one FAILED line and then died, so the set is a LOWER
    BOUND and `ids or None` returned it as the answer. A subset reported as the whole set is
    the fail-open this module's own docstring says it exists to close — closed on the
    no-FAILED-line branch and left open on the one-FAILED-line branch."""
    out = ("=== short test summary info ===\n"
           "FAILED tests/t.py::test_x - AssertionError\n"
           "!!! Interrupted: 1 error during collection !!!\n")
    assert progress.pytest_fingerprints(out, "", 4) is None


def test_an_ordinary_test_failure_still_reads(tmp_path):
    """The discrimination check for the exit-code narrowing: exit 1 is pytest's ordinary
    "tests failed" and must still produce a set, or the mechanism declines on its main case."""
    out = ("=== short test summary info ===\n"
           "FAILED tests/t.py::test_x - AssertionError\n"
           "=== 1 failed, 11 passed in 0.4s ===\n")
    assert progress.pytest_fingerprints(out, "", 1) == frozenset({"tests/t.py::test_x"})
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_progress.py -k "parametrized or another_runner or collection_error"`
Expected: FAIL — three failures.

- [ ] **Step 4: Fix the three defects**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/progress.py`:

```python
# The id runs to END OF LINE, minus pytest's own ` - <reason>` suffix. `(\S+)` stopped at the
# first space, so `FAILED t.py::test_x[a b]` captured `t.py::test_x[a` and two DIFFERENT
# parametrized failures compared equal — this project's recurring defect shape, in the parser
# §12.3's progress question is answered from. Parametrized ids routinely contain spaces.
_FAILED = re.compile(r"^FAILED (.+?)(?: - .*)?$", re.MULTILINE)

# The evidence that this output came from pytest AT ALL. `" passed"` and `" failed"` used to
# be in here as bare substrings and are not banners — they are two words go test, cargo test
# and jest all print, so this parser answered outside its own declared domain.
#
# A SUBSTRING TUPLE CANNOT EXPRESS THIS, AND THE FIRST DRAFT OF THIS FIX WAS WRONG. It
# narrowed to three literal section rules, and MEASURED, a green `pytest -q` run prints none
# of them — its entire output is `35 passed in 0.08s`. That narrowing would have returned
# `None` for every clean gate run and made §12.3 decline on its most common case: the
# fail-open's mirror image, and a verdict reading dirtier than its evidence.
#
# So the discriminator is pytest's own SHAPE, anchored at line start: its full-output header,
# its section rules, or a summary line that is a count followed by `in <float>s`. Verified
# against seven real outputs — pytest -q green, pytest -q red, pytest full green, go test,
# cargo test, jest, and a make recipe echoing "all tests passed" — the first three match and
# the last four do not.
_PYTEST_BANNER = re.compile(
    r"^=+ test session starts =+$"
    r"|^=+ (FAILURES|short test summary info) =+"
    r"|^\d+ (passed|failed|error|skipped)\b.*\bin \d+\.\d+s",
    re.MULTILINE)
```

and change the guard in `pytest_fingerprints` from the `any(b in text for b in
_PYTEST_BANNERS)` membership test to `if not _PYTEST_BANNER.search(text): return None`.

and in `pytest_fingerprints`, replace the final `return ids or None` with:

```python
    if exit_code not in (0, 1):
        # pytest's 2, 3, 4 and 5 are interrupted, internal error, usage error and no-tests.
        # A run that printed one `FAILED` and then died gives a LOWER BOUND, and the docstring
        # above already says an empty set there is "the subset-of-everything fail-open this
        # module exists to close". A one-element set is the same fail-open with one element.
        return None
    return ids or None
```

Update the docstring's "A NONZERO exit with a banner and no `FAILED` line is `None`" paragraph to say what is now true: any exit outside 0 and 1 is `None` whatever it printed.

- [ ] **Step 5: Run the tests to verify they pass, then the whole progress and review suites**

Run: `uvx --with pytest pytest -q tests/test_forge_progress.py tests/test_forge_review.py tests/test_forge_coverage.py`
Expected: PASS. `coverage._test` reads a per-test receipt rather than this parser (L2.4) and should not move; if it does, read why before changing either.

- [ ] **Step 6: Commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/progress.py tests/test_forge_progress.py marketplaces
make verify
git commit -m "fix(forge): the parser a deferred fix was to be built on manufactured measured sets"
```

---

## Task 4: Coverage's row identity, and the rows that produce no `Result`

**Why fourth:** two findings, one visit, because both are `coverage`'s treatment of a ledger row's identity and fixing one without the other leaves the pair disagreeing.

- **s3 M3** — `coverage.check` (`coverage.py:752`) calls `ledger._check_rows(l.rows)`, not `ledger._check`. `_check_rows` validates the rows; `_check` validates the ledger, which is where duplicate and stale row ids are caught. So `coverage.check` measures over identities nothing verified, and L2.1/L2.3 made it measure **more carefully** on them.
- **s3 H2** — a row whose status is not `accepted` produces **no `Result` at all**. Forty unsettled claims render a clean report. L2.3's accepted-row filter (`coverage.check` now filters `r.status == "accepted"`) makes this strictly more consequential: the filter is correct, and the rows it drops now leave no trace anywhere.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/coverage.py` (`check`, ~`:744-760`)
- Test: `/home/khenrix/git/khenrix-utils/tests/test_forge_coverage.py`

**Interfaces:**
- Consumes: `ledger._check(l)`, `ledger._check_rows(rows)`, `coverage.Result`, `coverage.unmeasured(results)`.
- Produces: `coverage.check` raises on a ledger `_check` refuses; every non-`accepted` row produces a `Result` whose status is `unresolved` with a reason naming the row's actual status.

**The fail-open this task must not have:** an `unresolved` `Result` for a rejected row must not be counted as a *gap in coverage* the way an accepted-but-uncriterioned row is — those are two different facts and §12.4's fallback fires on one of them. Read `NO_CRITERION`'s index and give the new state its own, so the roll-up can tell them apart.

- [ ] **Step 1: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_coverage.py`:

```python
def test_check_refuses_a_ledger_with_duplicate_row_ids():
    """THE EXTERNAL QUESTION: does `check` measure over identities anything verified? It
    called `_check_rows`, which validates rows; `_check` validates the LEDGER, and duplicate
    and stale ids are a ledger-level property. So every measurement below was taken over ids
    that may not be unique."""
    l = _a_ledger_with_duplicate_ids()
    with pytest.raises(Exception) as e:
        coverage.check(l, _no_results())
    assert "duplicate" in str(e.value).lower()


def test_a_rejected_row_leaves_a_result_rather_than_nothing():
    """Forty unsettled claims used to render a clean report: a row that is not `accepted`
    produced no `Result` at all, so the roll-up counted zero of them and the reader saw a
    coverage report with nothing missing from it. `nothing` and `nobody` must not leave the
    same record."""
    l = _a_ledger(rows=[_row("c1", status="rejected"), _row("c2", status="accepted")])
    report = coverage.check(l, _no_results())
    ids = {r.claim_id for r in report.results}
    assert ids == {"c1", "c2"}, ids
    c1 = next(r for r in report.results if r.claim_id == "c1")
    assert c1.status == "unresolved" and "rejected" in c1.reason


def test_a_rejected_rows_result_is_not_counted_as_a_missing_criterion():
    """The discrimination check. §12.4's fallback fires on an accepted claim nobody wrote a
    criterion for; a REJECTED claim with no criterion is not that, and folding the two would
    make the fallback fire on every ledger that rejected anything."""
    l = _a_ledger(rows=[_row("c1", status="rejected")])
    report = coverage.check(l, _no_results())
    c1 = report.results[0]
    assert c1.index != coverage.NO_CRITERION
    assert c1.index == coverage.NOT_ACCEPTED
```

> `_a_ledger`, `_row` and `_no_results` follow the existing conventions in
> `tests/test_forge_coverage.py` — read its helpers and extend them. `_a_ledger_with_duplicate_ids`
> must build a ledger `ledger._check` refuses and `ledger._check_rows` accepts; if no such
> ledger can be built, that is the finding — report it, because it would mean the two functions
> are the same check under two names and s3 M3 is not a defect.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_coverage.py -k "duplicate_row_ids or rejected_row"`
Expected: FAIL — three failures, the third on `AttributeError: module 'forge.coverage' has no attribute 'NOT_ACCEPTED'`.

- [ ] **Step 3: Add the index and the two fixes**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/coverage.py`, beside `NO_CRITERION`:

```python
# A row that is not `accepted`. DISTINCT FROM `NO_CRITERION`, which is an accepted claim nobody
# wrote a criterion for and is what §12.4's fallback fires on. A rejected claim with no
# criterion is not a coverage gap — it is a claim the ledger settled the other way — and
# folding the two would fire the fallback on every ledger that rejected anything.
NOT_ACCEPTED = "not-accepted"
```

In `check`, change `ledger._check_rows(l.rows)` to `ledger._check(l)` with the reason in a comment:

```python
        # `_check`, NOT `_check_rows`. `_check_rows` validates the ROWS; duplicate and stale
        # row ids are a LEDGER-level property, and every measurement below is keyed by row id
        # — so the narrower call measured carefully over identities nothing had verified.
        ledger._check(l)
```

and give the non-accepted rows a `Result` rather than dropping them. The filter L2.3 added is correct and stays; what changes is that the dropped rows leave a record:

```python
        if r.status != "accepted":
            out.append(Result(claim_id=r.id, status="unresolved", index=NOT_ACCEPTED,
                              reason=f"the ledger records this claim as {r.status!r}, so it "
                                     "is not a claim coverage measures — recorded rather "
                                     "than dropped, because a report that omitted it would "
                                     "read as complete"))
            continue
```

Match `Result`'s actual field names and order — read the dataclass rather than trusting this block's keywords.

- [ ] **Step 4: Run the tests to verify they pass, then everything that reads a coverage report**

Run: `uvx --with pytest pytest -q tests/test_forge_coverage.py tests/test_forge_rubric.py tests/test_forge_strategy.py tests/test_forge_ledger.py`
Expected: PASS. `coverage.unmeasured` is the single exported predicate L2.1 introduced and both `rubric` and `strategy` import it — confirm neither gained a second copy as a side effect of this change.

- [ ] **Step 5: Commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/coverage.py tests/ marketplaces
make verify
git commit -m "fix(forge): forty unsettled claims rendered a clean report, measured on unverified ids"
```

---

## Task 5: `GATE_RANK` ranks `GATE_CHANGED` second of six

**Why fifth, and why it is a decision rather than an edit:** `rubric.GATE_RANK` is contiguous 0–5 with no free integer, and `GATE_CHANGED` sits at rank 2 — **above `FLAKY` and `FAIL`**. So a candidate that rewrote the gate ranks better than one whose tests merely failed. `runner.py:773-775` records that L1.2 deliberately did **not** renumber, because renumbering around a known defect re-blesses it as a side effect of an unrelated fix. This task is where the decision is made deliberately.

**The decision, and it is the plan's, not the implementer's.** The map is currently:

```
0 PASS   1 BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE   2 GATE_CHANGED   3 FLAKY   4 FAIL   5 HARVEST_INCOMPLETE
```

`GATE_CHANGED` moves **below `FAIL`** and `FLAKY` and `FAIL` shift up one:

```
0 PASS   1 BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE   2 FLAKY   3 FAIL   4 GATE_CHANGED   5 HARVEST_INCOMPLETE
```

§6.2's own disposition is that a candidate which changed the gate cannot be compared on the gate's evidence at all; ranking it second says the opposite. A candidate that honestly failed is strictly more useful than one whose PASS cannot be trusted.

**It does NOT move to last, and the first draft of this task said it should.** That would put `GATE_CHANGED` below `HARVEST_INCOMPLETE`, which is a *different* claim and one nothing here has argued: `HARVEST_INCOMPLETE` means the engine could not read the candidate's artifacts, so there is nothing to merge at all, while a gate-rewriting candidate still has reviewable code behind an untrustworthy verdict. Ranking "unusable" above "untrustworthy" is a second decision, and this task takes only the one the finding names.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/rubric.py:29-40`
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/runner.py:773-775` (the comment that recorded the deferral)
- Test: `/home/khenrix/git/khenrix-utils/tests/test_forge_rubric.py`

**Interfaces:**
- Consumes: `verify.PASS`, `verify.FAIL`, `verify.GATE_CHANGED`, `verify.FLAKY`, and the other §6.2 outcomes.
- Produces: `rubric.GATE_RANK` — same keys, new values. `rubric.dimensions_from` and `rubric._unmeasured` are unchanged.

**The fail-open this task must not have:** the rank map must stay **total over §6.2's outcomes**. `runner.py:773` already notes that "§6.2 gaining a row is meant to fail loudly"; a renumber that silently drops a key turns a missing outcome into a `KeyError` at rank time rather than at import time. Assert totality against `verify.OUTCOMES`, not against a literal list.

- [ ] **Step 1: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_rubric.py`:

```python
def test_a_candidate_that_rewrote_the_gate_ranks_below_one_that_honestly_failed():
    """§6.2's disposition is that a candidate which changed the gate cannot be compared on the
    gate's evidence at all. Ranking it 2nd of 6 — above FLAKY and above FAIL — said the
    opposite: rewrite the gate and outrank the seat whose tests merely failed."""
    assert rubric.GATE_RANK[verify.GATE_CHANGED] > rubric.GATE_RANK[verify.FAIL]
    assert rubric.GATE_RANK[verify.GATE_CHANGED] > rubric.GATE_RANK[verify.FLAKY]
    # ...and NOT last. A candidate whose artifacts could not be harvested has nothing to
    # merge at all; a gate-rewriting one has reviewable code behind an untrustworthy verdict.
    # That is a second decision and this task does not take it.
    assert rubric.GATE_RANK[verify.GATE_CHANGED] < rubric.GATE_RANK[verify.HARVEST_INCOMPLETE]


def test_the_rank_map_is_total_over_every_outcome_verify_can_produce():
    """THE EXTERNAL QUESTION, and the one a renumber can silently break: not "are these six
    keys present" but "is every outcome verify can produce rankable". An enumeration here
    fails open on the seventh outcome nobody enumerated — so the set comes from `verify`."""
    assert set(rubric.GATE_RANK) == set(verify.OUTCOMES), (
        set(verify.OUTCOMES) ^ set(rubric.GATE_RANK))


def test_the_ranks_are_a_total_order_with_no_ties():
    """Two outcomes at one rank is two different verdicts comparing equal — this project's
    recurring shape, and the one that would make `_unmeasured` return an arbitrary winner."""
    ranks = list(rubric.GATE_RANK.values())
    assert len(set(ranks)) == len(ranks), rubric.GATE_RANK
    assert sorted(ranks) == list(range(len(ranks))), rubric.GATE_RANK
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_rubric.py -k "rewrote_the_gate or total_over or total_order"`
Expected: FAIL on the first (2 is not > 4/5). The second and third should PASS already — they are the invariants the renumber must not break, written first so a break is visible.

- [ ] **Step 3: Renumber**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/rubric.py`, move `verify.GATE_CHANGED` to rank 4 and shift `FLAKY` and `FAIL` up one, leaving `HARVEST_INCOMPLETE` last. Write the reason above the map:

```python
# `GATE_CHANGED` SITS BELOW `FAIL`, DELIBERATELY, AND IT USED TO SIT SECOND. §6.2's disposition
# is that a candidate which rewrote the gate cannot be compared on the gate's evidence at all,
# and rank 2 — above FLAKY and above FAIL — said the opposite: rewrite the gate and outrank the
# seat whose tests honestly failed. `runner.py` recorded the deferral rather than renumbering as
# a side effect of an unrelated fix; this is the deliberate decision, taken on its own.
#
# IT IS NOT LAST. `HARVEST_INCOMPLETE` stays worst: the engine could not read that candidate's
# artifacts, so there is nothing to merge at all, where a gate-rewriting candidate still has
# reviewable code behind a verdict nobody can trust. Ranking "unusable" above "untrustworthy"
# is a SECOND decision and nothing here has argued it.
```

Then delete the paragraph at `runner.py:773-775` that records the deferral and replace it with one sentence saying it was taken — a comment describing a decision that has since been made is a stale docstring, which is a finding this project has already logged four times.

- [ ] **Step 4: Run the tests to verify they pass, then everything that ranks**

Run: `uvx --with pytest pytest -q tests/test_forge_rubric.py tests/test_forge_runner.py tests/test_forge_strategy.py tests/test_forge_cli.py`
Expected: PASS. A test asserting a specific integer for an outcome is asserting the old numbering — correct it to assert the ORDER, which is the property that matters, rather than re-pinning a literal.

- [ ] **Step 5: Commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/rubric.py shared/lib/forge/runner.py tests/ marketplaces
make verify
git commit -m "fix(forge): rewriting the gate outranked failing the tests"
```

---

## Task 6: §12.3's oscillation, specified by Plan I₂ and scheduled by nothing

**Why sixth, and why now:** Plan I₂ Task 2 states verbatim *"Task 5 calls `cap_remaining` and `oscillation`; Task 6 calls `cap_remaining`."* Measured: `oscillation`, `from_runs` and `compare` have **zero** production callers, while `cap_remaining` has three and both writers are wired. `review.py` hard-codes `prog=progress.Progress(None, None)` at the `record_fix_done` call, so every sighting is unmeasured by construction. Plan L recorded the deferral and named the real dependency: **K Task 4 changes `loop`'s signature and edits that exact call site**. K4 has now landed, so this is the one visit.

The root cause is the injected `fix` contract: `fix(findings, checkpoint) -> (new_checkpoint | None, verified: bool)` returns a boolean where `from_runs` needs the candidate and baseline `Run`s.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/review.py` (`loop`'s `fix` contract and its `record_fix_done` call)
- Test: `/home/khenrix/git/khenrix-utils/tests/test_forge_review.py`
- Test: `/home/khenrix/git/khenrix-utils/tests/test_forge_progress.py`

**Interfaces:**
- Consumes, all three measured rather than assumed:
  - `progress.from_runs(candidate_run, baseline_run, *, parse=pytest_fingerprints) -> Progress` (`progress.py:120`). Both arguments need `.stdout`, `.stderr` and `.exit_code`; a baseline whose output cannot be read makes "new" unanswerable and the whole tuple unknown, which is why the baseline is required rather than optional.
  - `progress.oscillation(events) -> tuple` (`progress.py:257`) — **it takes the journal events, not a pair of Progresses**, so `loop` calls it with `log.read()` and nothing has to be threaded to it. Its rule is that the second sighting of the same `(tree_oid, fingerprints)` pair is the stop signal, and a sighting with unmeasured fingerprints forms **no pair** — which is exactly why the hard-coded `Progress(None, None)` made it unable to fire.
  - `progress.record_fix_done(log, *, operation_id, tree_oid, prog)`.
- Produces: `fix`'s contract widens to `fix(findings, checkpoint) -> (new_checkpoint | None, verified: bool, candidate_run, baseline_run)`. **`candidate_run` and `baseline_run` may be `None`**, and `None` means "this fix implementation did not measure a run" — which produces `Progress(None, None)` *explicitly*, from a fix that said so, rather than by a hard-coded literal nobody can distinguish from a measurement.

**The fail-open this task must not have:** a `fix` that returns the old 2-tuple must **refuse**, not be padded with `None`s. Padding makes "this fix does not measure runs" and "this fix predates the contract" the same record, which is the shape of every defect in this file. And `oscillation` firing must not be read as a *verdict* — it is a stop signal for §12.3's loop, and Plan M does not give it one.

- [ ] **Step 1: Write the failing tests**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_review.py`:

```python
def test_the_loop_refuses_a_fix_that_answers_the_old_contract(tmp_path):
    """PADDING WOULD BE THE FAIL-OPEN. A 2-tuple padded to 4 with `None`s makes "this fix does
    not measure runs" and "this fix predates the contract" the same record — and the second is
    a bug in the caller while the first is a legitimate answer."""
    repo, run_dir, run_id, head = _a_run_with_a_checkpoint(tmp_path)
    co = run_dir / "synthesis"
    with pytest.raises(review.ReviewError, match="four"):
        review.loop(run_dir, state=_reviewing(), checkout=co, checkpoint=head,
                    baseline_commit=head,
                    baseline_tree=_git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(),
                    artifact_manifest=(), log=_a_log(run_dir),
                    manifest=_a_manifest(review_rounds=2, cap=3),
                    fix=lambda f, c: (c[:-1] + "f", True),
                    other_clones=(), repo=repo, run_id=run_id, identity=("F", "f@e.x"),
                    make_tree=_not_a_clone(co), run=_a_blocking_round)


def test_a_fix_that_measured_two_runs_reaches_the_journal_as_a_measurement(tmp_path):
    """THE EXTERNAL QUESTION: does anything call `from_runs`? `progress.Progress(None, None)`
    was hard-coded at the one call site, so every sighting was unmeasured BY CONSTRUCTION and
    `oscillation` could not fire whatever happened. This asserts a measured pair arrives."""
    repo, run_dir, run_id, head = _a_run_with_a_checkpoint(tmp_path)
    co = run_dir / "synthesis"
    cand, base = _a_run_with_failures({"t.py::a"}), _a_run_with_failures({"t.py::a", "t.py::b"})
    review.loop(run_dir, state=_reviewing(), checkout=co, checkpoint=head,
                baseline_commit=head,
                baseline_tree=_git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(),
                artifact_manifest=(), log=_a_log(run_dir),
                manifest=_a_manifest(review_rounds=2, cap=3),
                fix=lambda f, c: (c[:-1] + "f", True, cand, base),
                other_clones=(), repo=repo, run_id=run_id, identity=("F", "f@e.x"),
                make_tree=_not_a_clone(co), run=_a_blocking_round)
    rows = [e for e in _a_log(run_dir).read() if e.get("op", "").startswith("review-fix")]
    prog = [r for r in rows if r.get("fixed") is not None or r.get("progress")]
    assert prog, rows
    assert any(r != {"fixed": None, "remaining": None} for r in
               (r.get("progress") or {} for r in rows)), "still Progress(None, None)"


def test_a_fix_that_measured_nothing_says_so_rather_than_looking_measured(tmp_path):
    """`None` for both runs is a legitimate answer — a fix implementation that does not run a
    gate cannot produce one — and it must be distinguishable from the hard-coded literal it
    replaces. It is, because the fix RETURNED it."""
    repo, run_dir, run_id, head = _a_run_with_a_checkpoint(tmp_path)
    co = run_dir / "synthesis"
    review.loop(run_dir, state=_reviewing(), checkout=co, checkpoint=head,
                baseline_commit=head,
                baseline_tree=_git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(),
                artifact_manifest=(), log=_a_log(run_dir),
                manifest=_a_manifest(review_rounds=2, cap=3),
                fix=lambda f, c: (c[:-1] + "f", True, None, None),
                other_clones=(), repo=repo, run_id=run_id, identity=("F", "f@e.x"),
                make_tree=_not_a_clone(co), run=_a_blocking_round)
    # No exception, and the journal carries the unmeasured pair as itself.
```

> `_a_blocking_round` is a `run` callback returning a `Round` carrying one blocker, so the loop
> actually reaches its `fix` call — the existing `_clean_round` does not. `_a_run_with_failures`
> builds whatever shape `progress.from_runs` consumes; read `progress.from_runs`'s signature and
> `tests/test_forge_progress.py`'s existing fixtures before writing it, and if `from_runs` takes
> a `Run` this suite has no builder for, put the builder in `test_forge_progress.py` and import it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_review.py -k "old_contract or measured_two_runs or measured_nothing"`
Expected: FAIL — the first because the 2-tuple is accepted, the others on the unpacking.

- [ ] **Step 3: Widen the contract**

In `review.loop`, replace the `fix` call and the `record_fix_done` that follows it:

```python
        answer = fix(tuple(blockers), current)
        if not isinstance(answer, tuple) or len(answer) != 4:
            # NOT PADDED. A 2-tuple padded with `None`s makes "this fix did not measure runs"
            # and "this fix predates the contract" the same record, and the second is a caller
            # bug while the first is a legitimate answer. §12.3's progress question cannot be
            # answered from a value nobody supplied, and it must not LOOK answered.
            raise ReviewError(
                f"§12.3's fix contract returns four values — (checkpoint|None, verified, "
                f"candidate_run|None, baseline_run|None) — and this one returned "
                f"{len(answer) if isinstance(answer, tuple) else type(answer).__name__}. "
                "The last two are what `progress.from_runs` needs; `None` for both is a fix "
                "saying it measured no run, which is an answer, and a missing pair is not.")
        new_checkpoint, verified, cand_run, base_run = answer
        # MEASURED WHEN THE FIX MEASURED, AND `Progress(None, None)` ONLY WHEN IT DID NOT.
        # This call site used to hard-code the unmeasured pair, so every sighting was
        # unmeasured by construction and §12.3's oscillation stop could not fire whatever
        # happened — the mechanism Plan I2 specified, present in the module and reachable
        # from nothing.
        prog = (progress.from_runs(cand_run, base_run)
                if cand_run is not None and base_run is not None
                else progress.Progress(None, None))
        progress.record_fix_done(
            log, operation_id=op,
            tree_oid=(_tree_of(checkout, new_checkpoint) if new_checkpoint else None),
            prog=prog)
```

Then consult `oscillation` per round, after the resolutions are written and before `current` advances:

```python
        stop, why_stop, _ = progress.oscillation(log.read())
        if stop:
            # §12.3's stop signal, not a verdict. The run has returned to a state it has
            # already been in — fix A traded failure X for Y and fix B traded back — so
            # another round buys nothing. `_stop` records the position; this plan gives
            # `oscillation` no say in the terminal CLASSIFICATION, which is `settle`'s.
            return _stop(run_dir, state, rounds_run=n, events=log.read(), note=why_stop)
```

`oscillation` returns three values; unpack all three and use the two this call needs, rather than indexing. If the third turns out to be load-bearing here, that is a finding — say so rather than dropping it silently.

Update `loop`'s docstring paragraph describing the `fix` contract: it currently states the 2-tuple, and a docstring describing the old contract is exactly the stale-docstring finding this project has logged four times.

- [ ] **Step 4: Run the tests to verify they pass, then the review and progress suites**

Run: `uvx --with pytest pytest -q tests/test_forge_review.py tests/test_forge_progress.py`
Expected: PASS. Every existing `fix=` in the suite returns a 2-tuple and must be widened — **do not add a compatibility branch**; the refusal above is the point.

- [ ] **Step 5: Commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/review.py tests/ marketplaces
make verify
git commit -m "fix(forge): the oscillation stop could not fire, because its input was a literal"
```

---

## Task 7: The B₁ screen's TOCTOU half

**Why last:** it is the only task here that adds a pass over the tree rather than repairing one, and it is the one whose cost is proportional to the repository. `gate.SCREEN_RACE` (`gate.py:986`) is a **declared gap**, not a closed hole: the §3 screen runs at preflight and the gate reuses its report, so anything written between the two — by the operator, an editor or a watcher — enters B₁ unscreened, and nothing after that point reads those bytes before three providers do. L0.4 closed the *coverage* half (`git ls-files -z`, index blobs and working tree as separate namespaces) and recorded this one in prose.

**The fix:** screen the **content-addressed B₁ path set after B₁ is built**, binding the scan to the per-path hashes seat verification already consumes. B₁ is immutable once built, so a screen taken over it has no race left to lose.

**Files:**
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/baseline.py` (`materialize`, immediately after `_record_filesystem_manifest(run_dir, manifest)` at ~`:395` and **before** the `update-ref` below it)
- Modify: `/home/khenrix/git/khenrix-utils/shared/lib/forge/gate.py` (`SCREEN_RACE`'s gap line and `ACCEPTABLE_GAPS`)
- Test: `/home/khenrix/git/khenrix-utils/tests/test_forge_baseline.py`

**Interfaces:**
- Consumes: `screen.screen_tree(root, rel_paths, quota=None) -> (findings, breaches)`; `baseline.Baseline.filesystem_manifest`; `baseline.read_filesystem_manifest(run_dir)`.
- Produces: `baseline.materialize` raises `BaselineError` when the post-B₁ screen finds a secret or cannot read what it claimed to. `gate.SCREEN_RACE` is **removed** from `ACCEPTABLE_GAPS` and its line is deleted from `must_show`, because a gap that has been closed and is still announced is a verdict reading dirtier than its evidence — the same defect in the other direction.

**The fail-open this task must not have:** a breach (`"we did not read this"`) must fail the run closed, exactly as `screen_tree`'s docstring requires — **never** be folded into "no findings". And the screen must run over the paths **B₁ actually holds**, read back from the built baseline, not over the selection the operator asked for: the whole finding is that those two sets differ.

- [ ] **Step 1: Write the failing test**

Append to `/home/khenrix/git/khenrix-utils/tests/test_forge_baseline.py`:

```python
def test_a_secret_written_after_preflight_does_not_reach_b1(tmp_path):
    """§3's screen runs at preflight and the gate reuses its report, so a file written between
    the two entered B1 unscreened and three cloud CLIs read it. THE EXTERNAL QUESTION is not
    "does preflight screen" — it does — but "can a secret reach B1", and the answer was yes
    through a window nothing narrowed.
    """
    repo = make_repo(tmp_path)
    write(repo, "app.py", "print('hi')\n")
    commit_all(repo, "seed")
    facts = preflight.inspect_repo(repo).facts       # the screen runs HERE
    write(repo, "creds.env", "AWS_SECRET_ACCESS_KEY=" + "Q7ZB3KXJ2M9WLPRT" * 2 + "\n")
    _git(repo, "add", "creds.env")
    _git(repo, "commit", "-q", "-m", "oops")         # ...and the tree moved AFTER it
    with pytest.raises(baseline.BaselineError, match="secret"):
        baseline.materialize(repo, _a_run_dir(tmp_path), facts, [], "aaaaaa",
                             author=("A", "a@e.x"))


def test_an_unreadable_path_in_b1_fails_the_run_closed(tmp_path):
    """`screen_tree`'s own contract: "a non-empty `breaches` means the caller must FAIL the
    run closed with that message — never silently scan less than it claimed to". A breach
    folded into "no findings" is the whole design's fail-open, and the one place it would be
    invisible is a screen nobody previously ran."""
    ...


def test_the_gate_no_longer_announces_a_gap_it_has_closed(tmp_path):
    """The other direction of the same rule. A verdict must never read cleaner than its
    evidence AND never dirtier: a gap line for a race that has been closed tells an operator
    to accept a risk that is not there, and trains them to skim the gap lines that matter."""
    r = _report(tmp_path)
    q = gate.quote(r, seats=3, attempts=3, review_rounds=2, seat_timeout_sec=3600)
    lines = gate.must_show(r, q, verify.Command.parse([["true"]]),
                           setup=verify.Command.parse([["true"]]))
    assert not any(gate.SCREEN_RACE in l for l in lines)
```

> The second test's body is deliberately left for the implementer to write against
> `screen_tree`'s actual breach mechanism — read `screen.screen_tree`'s docstring and
> `tests/test_forge_screen.py` for how the existing suite provokes one (an unreadable mode, a
> symlink it refuses to follow). Write it before Step 3; a task whose fail-closed half is
> untested is the task most likely to ship the fail-open. The third test belongs in
> `tests/test_forge_gate.py`, not this file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uvx --with pytest pytest -q tests/test_forge_baseline.py -k "after_preflight or unreadable_path_in_b1"`
Expected: FAIL — `DID NOT RAISE`.

- [ ] **Step 3: Screen B₁ after it is built**

In `baseline.materialize`, immediately after `_record_filesystem_manifest(run_dir, manifest)` (~`:395`) and **before** the `update-ref` below it, screen the path set B₁ actually holds.

**That placement is the point, and it is not "after B₁ exists".** `manifest` is complete at line 395 — it is `git ls-files -z` plus the selection, walked and digested — and the ref that puts B₁ in the **user's own repository** is written after it. Screening here means a refusal leaves nothing behind at all: no ref, no object, nothing in a repository the operator did not ask forge to write to. That is `open_run`'s rule — "every refusal comes before every write" — applied one module over, and its docstring records that the same rule has been broken three times for the same structural reason. The local is `manifest`; there is no `Baseline` object yet.

```python
    # THE SCREEN §3 ASKS FOR, TAKEN WHERE IT CANNOT LOSE A RACE. `preflight`'s screen runs
    # before the gate and the gate reuses its report, so anything written in between entered
    # B1 unscreened and three cloud CLIs read it — `gate.SCREEN_RACE` was that gap, declared
    # rather than closed. B1 is immutable once built, so a screen over ITS OWN path set has no
    # window left. The set is `manifest` — `git ls-files -z` plus the selection, which is what
    # B1 actually carries — and not the operator's `selected_untracked`, because the whole
    # finding is that those two sets differ. Measured: `manifest` is complete here and the ref
    # is written below, so a refusal leaves nothing in the user's repository.
    findings, breaches = screen.screen_tree(facts.root, sorted(manifest))
    if breaches:
        # `screen_tree`'s contract, verbatim: "a non-empty `breaches` means the caller must
        # FAIL the run closed with that message — never silently scan less than it claimed to".
        raise BaselineError(
            f"B1 holds {len(breaches)} path(s) this screen could not read: {breaches[:4]}. "
            "§3 fails closed on what it could not scan, because 'we did not read this' and "
            "'this is clean' are two answers and only one of them is safe to build on.")
    if findings:
        raise BaselineError(
            f"B1 carries {len(findings)} secret finding(s): {findings[:4]}. This screen runs "
            "AFTER B1 is built, so it covers what preflight's could not — anything written "
            "between the two. B1 is what three cloud CLIs receive.")
```

Import `screen` in `baseline.py` if it is not already imported. **Confirm `manifest` is non-empty before trusting the screen**: a check that passes because it looked at nothing is the failure this task is most exposed to, and `read_filesystem_manifest`'s own docstring (`baseline.py:113`) already argues the same point about an empty manifest. Assert it in the test, not only in review.

- [ ] **Step 4: Retire the gap**

In `/home/khenrix/git/khenrix-utils/shared/lib/forge/gate.py`, remove `SCREEN_RACE` from `ACCEPTABLE_GAPS` and delete its line from `must_show`. Keep the constant itself only if something else names it; if nothing does, delete it and its comment.

- [ ] **Step 5: Run the tests to verify they pass, then the full forge suite**

Run: `uvx --with pytest pytest -q tests/test_forge_baseline.py tests/test_forge_gate.py tests/test_forge_screen.py tests/test_forge_cli.py`
Expected: PASS. Existing gate tests asserting `SCREEN_RACE` appears are asserting the gap's presence — they are corrected, and the correction is the third test above.

- [ ] **Step 6: Commit**

```bash
cd /home/khenrix/git/khenrix-utils
make render
git add shared/lib/forge/baseline.py shared/lib/forge/gate.py tests/ marketplaces
make verify
make precommit
git commit -m "feat(forge): the screen runs where B1 cannot move under it"
```

---

## Deferred to Plan N — explicit, so nothing is silently dropped

Plan L deferred nineteen entries; seven were marked ⚠ *load-bearing* and are the seven tasks above. **Item 13's label was wrong in Plan L and is corrected here:** it read "s5 — C4 and all six Highs" while describing C1's and C2's content. s5's C3 (`assert_ledger_is_out_of_reach` checking one direction) **was closed** by L1.4; C1, C2 and C4 remain. The corrected list:

1. **The review-verb band — what Plan K's Order-of-work calls "Plan L".** The `--review` verb driving one `run_round`; then the unattended `loop`; then §12.5's rank in `cli._strongest`; then the priced synthesis verifier pass, which must ship *with* the review verb rather than before it. **Blocking prerequisite, restated:** K Task 4 gave the panel its own `.git`, and its residual is unclosed — a round convened over `run_dir/review/round-<n>/checkout` is one relative path from the ledger, the panel's in-flight answers and every candidate clone. The review tree must move outside `run_dir` and join `other_clones` before anything convenes a real panel.
2. **Reprice §13's rounds, or refuse `--review-rounds` while nothing convenes one.** L4's test proves `review` and `review_fixes` — 9 of the quoted 19 calls — have no production caller, and SKILL.md says so. The quote is *not* corrected: zeroing the terms stops `--review-rounds` moving `provider_calls` and makes `confirm` refuse every run (`synthesis_fix_cap` derives from the same term) — measured, 13 failures. The ceiling and the price are two numbers; separating them is the work. **M's Task 4 (K6) added the review clone to `peak_disk_gb` on the opposite argument** — the quote already prices the unbuilt review stage in calls and in `review_fixes`' verifier clones, so omitting only the clone was inconsistent. Whichever way this is settled, it must be settled for all four terms at once.
3. **`Status.setup` → `Status.builder_setup`.** L1.2 closed the verdict half and corrected `seat.py`'s docstring; the rename is an on-disk schema change plus a required keyword across ~42 `classify_seat` call sites, 30 of them in `test_forge_seat.py`, colliding with the existing top-level `verifier_setup` key. It needs a reviewer looking at a rename, not a rider.
4. **The blind-review boundary, really closed** — OS-level isolation for reviewers and seats, or holding the ledger in memory for the round. L1.4 fixed the direction and wrote the residual down; this is the closure, and it is the same object as item 1's prerequisite.
5. **s4 remainder** — H3 oscillation's *consumers* (M's Task 6 wires the producer); M2 `snapshot.take`'s undeclared `FileNotFoundError`; M3 `Size` accepting `(0,0)`, negatives and bools; M4 `_dir_digest`; L1 the lost-journal/no-fixes collapse.
6. **s5 — C1, C2, C4 and all six Highs** *(label corrected)*: council result files written outside the bracket with no integrity re-check (C1); the review bundle inside `.git` with its path in argv and no digest ever taken (C2); repo-local diff drivers and `git replace` refs unmeasured for reviewers' own `git diff` (C4); **a fully-silent 0/3 panel classifying `degraded` — which ships — rather than `review_blocked`**; ultrareview's absent journal and durable receipt; plus its seven Mediums.
7. **s6** — `gc` deleting refs by namespace prefix rather than exact name+OID; the `PATCH_ONLY` handover citing a patch nothing generates; `--collect` discarding §9 drift and §14.1 orphans; the cloud review's missing idempotency guard **whose own refusal text tells the operator to re-trigger it**; the seat-count denominator taken from a disk glob rather than `manifest.seats`; the verify command truncated to step 0; **the handover asserting the synthesis branch from the run id while measuring HEAD, with `--gc` then reclaiming the difference**; plus its five Mediums.
8. **s7** — **"fusion, not selection" is unenforced** (the collector rejects only a tree identical to B₁; `--strategy` is reported and `cli.py:772` says it cannot be checked); the eval-baseline contamination; `--collect`'s re-payable review; `mutate.py`'s bytecode purge and missing path containment; `eval_trigger`'s type coercions (`"false"` → true; `null` → `"None"` → the abstention label); `reconcile`'s orphaned-marker destruction and `backup()` overwrite; `--seats 1` vs "all three CLIs".
9. **s1 remainder** — the symlink gate referent; the make memo key **(fix together with `_scan_make`'s `--directory=`/`-C` parser gap — two holes in one detector)**; the calibration aggregate; the control-plane integrity tripwire; **Fwork byte-binding**; durable-state reconstruction; `Seat.verified` over `sidecars is None`; two index definitions; `_gate_taints`' `isinstance` gate; `_command_paths`' silent `continue`; `_AMBIENT_SKILL`'s short-path refusals; `screen.py` carrying this repo's allow-list into foreign repos; `fleet.clone_seat`'s bare `IndexError`; `Quote`'s unvalidated fields.
10. **s2 remainder** — `no_change` with `proven_read=False`; **the executed-and-refuted check recorded as `not-run`**; §8.1's missing input half; `RunnerError`-as-retry; the empty fleet reaching `comparing`; `FLAKY` unreachable; `_clip`'s evidence truncation; `_verify_dim`'s collapse.
11. **s3 remainder** — `installed_closure`'s permutation collision; `verify_materialized`'s copied fields **and** the size/cap-blind `bundle_hash`; the criterion-to-claim binding; seat provenance; the journal creation race; hash criteria not distinguishing a file from a symlink.
12. **`--start` resume after an interrupted run, and parallel builders** — G6's other half, from Plan K's own order-of-work.

**Closed since Plan L wrote its list, so removed from it:** §18's live three-provider write smoke (item 19) — built by K Task 7 as `scripts/forge_smoke.py` + `make smoke-llm-forge`, with a receipt keyed to the adapter source hash **and** all three CLI versions. Only the live run remains, and it spends money, so it is the operator's to authorize.

**What no plan fixes, restated so it is not lost.** An orchestrator that writes a **true but incomplete** ledger is invisible to every check this design has. §12.4's fallback fires on claims that are *unsatisfied*, not on claims never written down, and §13's panel reviews the candidate rather than the ledger's completeness. A row omitted is a row nothing here can miss. Spec-level; deliberately unsolved.

---

## Self-review

**1. Spec coverage.** Every ⚠ entry from Plan L's deferral list has a task: item 2 → Task 1; item 7 → Task 2; item 5 → Task 3; items 4 and 6 → Task 4 (grouped, because both are coverage's treatment of a row's identity and fixing one alone leaves the pair disagreeing); item 3 → Task 5; item 12 → Task 6; item 1 → Task 7. Nothing marked ⚠ is deferred again.

**2. Placeholder scan.** Two deliberate under-specifications remain, each named in its task with the reason: `test_an_unreadable_path_in_b1_fails_the_run_closed`'s body (Task 7 Step 1 — the breach mechanism is `screen_tree`'s and `tests/test_forge_screen.py` already provokes one) and `_a_run_with_failures` (Task 6 — its shape is `progress.from_runs`'s two arguments, `.stdout`/`.stderr`/`.exit_code`). `progress.oscillation`'s call was a third and is now written out, measured rather than guessed. No "TBD", no "add error handling", no "similar to Task N".

**2b. What measurement changed after the first draft**, recorded because a plan whose premises were never checked is a plan that schedules work nobody verified exists:
- **Task 1's `kind` half was overstated.** Its two FIFO tests passed before any fix — `_special_entry` folds the file type into the digest — so the finding is structural duplication, not a reproducible miss, and the tests were replaced.
- **Task 1's setuid half is worse than recorded, and is now measured at the bracket.** `review.worktree_identity` returns the identical digest either side of `chmod u+s` — so the claim is not "the inventory drops a bit" but "the round bracket three unattended reviewers are measured by reads a setuid binary as no change at all". The task asserts it there as well as at the leaf.
- **Task 5's decision overshot.** Moving `GATE_CHANGED` to last also ranks it below `HARVEST_INCOMPLETE`, which is a second claim no finding argued. It moves to 4.
- **Task 7's screen was placed after a ref write.** `manifest` is complete at `baseline.py:395` and the `update-ref` follows, so screening there means a refusal leaves nothing in the user's own repository.
- **Task 4's premise held on inspection:** `ledger._check` runs `_check_rows` plus scalars, version, degradation consistency and a duplicate-id pass, so a ledger `_check_rows` accepts and `_check` refuses is constructible.
- **Task 2's premise held but its MECHANISM was backwards, and its test would have passed before the fix.** The variable is indeed absent from `HOSTILE_ENV` (`gitcmd.py:149-164`) and pinned only in `baseline.py:429` — but `LITERAL=1` does not stop a glob matching a literal name, it disables pathspec MAGIC. Measured, `:(literal)a.txt` and `:/` both answer `''` at **exit 0** under it, and the package passes one or the other at four sites including `verify.py:1232`'s `git add -f`. The finding is wider than Plan L recorded it.
- **Task 3's three premises all reproduce** (run 2026-08-05; the answers are tabulated in its Step 1) — **and its first fix was wrong.** Narrowing `_PYTEST_BANNERS` to literal section rules would have returned `None` for every green `pytest -q` run, whose entire output is `35 passed in 0.08s`. The discriminator is now a line-anchored regex over pytest's shape, verified against seven real outputs.

**3. Type consistency.** `snapshot.Entry`'s five fields are unchanged in name and order; only `mode`'s **domain** widens (Task 1). `coverage.NOT_ACCEPTED` is introduced in Task 4 and read only there and in its tests. `rubric.GATE_RANK` keeps its keys and changes its values (Task 5); nothing reads a literal rank in production. `fix`'s contract widens from 2 to 4 values in Task 6, and `review.loop` is the only caller. `gate.SCREEN_RACE` is *removed* in Task 7, so grep for it across `shared/` and `tests/` before deleting the constant.

**4. Ordering constraint.** Task 1 changes the predicate Task 6's journal assertions run through (`snapshot.diff` is in `loop`'s bracket). Execute Task 1 before Task 6. Tasks 2–5 are independent of each other and of both.

**5. What this plan does not claim.** It closes the residuals that shipped work rests on. It does **not** close the founding premise — a check the builder could have rigged is still not a check, and Plan L's L1 closed one route of six. The review verb, the rank, and the isolation boundary are all in Plan N, and until they land the fusion tool's two headline judgements decline by construction. That is honest and it is not finished.
