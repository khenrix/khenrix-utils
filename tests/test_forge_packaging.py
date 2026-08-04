"""forge must ship in the rendered plugins and be inside the receipt closure."""
import ast
import io
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _plugin(cli: str) -> Path:
    return ROOT / "marketplaces" / cli / "plugins" / "khenrix-utils"


def _checks_mod():
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    try:
        import checks
    finally:
        sys.path.pop(0)
    return checks


# Borrowed, never restated: a fifth copy of the CLI list is a fifth thing that can be
# right about the plugins that exist and silent about the one that does not.
CLIS = _checks_mod().CLIS


def test_forge_is_bundled_into_the_claude_plugin():
    p = _plugin("claude") / "lib" / "forge"
    assert (p / "baseline.py").is_file(), "run `make render`"
    assert (p / "fleet.py").is_file()
    assert not (p / "tests").exists(), "SHARED_LIBS must strip tests/"


def test_forge_is_bundled_into_every_cli():
    for cli in CLIS:
        p = _plugin(cli) / "lib" / "forge"
        assert (p / "gitcmd.py").is_file(), cli


def test_the_tests_stripper_is_live_and_not_merely_unexercised():
    """shared/lib/forge/ ships no tests/ dir, so the assertion above cannot fail today —
    it is a forward guard, not evidence that render.py strips anything. wikisync does
    ship one, and is the witness that SHARED_LIBS' ignore_patterns actually fires."""
    assert (ROOT / "shared" / "lib" / "wikisync" / "tests").is_dir(), \
        "witness moved — re-point this at whichever shared lib still ships tests/"
    for cli in CLIS:
        assert not (_plugin(cli) / "lib" / "wikisync" / "tests").exists(), cli


def _make_variable(name: str) -> set:
    """One `NAME := …` assignment from the Makefile, with its continuations folded.

    The lookbehind is what folds them: the assignment ends at the first line-end NOT
    preceded by a backslash, so a multi-line list reads as one value. Parsing the file
    rather than running `make` keeps this a test of what the gate says it runs.

    The `#` cut comes AFTER the fold and not before, because a make comment runs to the end
    of the LOGICAL line — a backslash-newline continues the comment too (measured, GNU Make
    4.4.1). Without the cut a legal trailing `# note` reads back as extra filenames and the
    caller's set-difference names words nobody wrote.
    """
    text = (ROOT / "Makefile").read_text()
    m = re.search(rf"^{name} :=(.*?)(?<!\\)$", text, re.M | re.S)
    assert m, f"the Makefile no longer assigns {name}"
    folded = m.group(1).replace("\\\n", " ")
    return set(folded.split("#", 1)[0].split())


def test_every_forge_suite_is_named_in_the_makefile_gate():
    """A suite the Makefile does not name is a suite `make council-test` never runs.

    Invisible from inside the suite itself — every test in it passes locally and the gate
    stays green while covering nothing — and it has happened in this package before.

    Equality, not containment, so the other direction fails too: a RENAMED suite leaves a
    dead name in the variable, and `pytest` is handed a path that no longer exists.
    """
    on_disk = {f"tests/{p.name}" for p in (ROOT / "tests").glob("test_forge_*.py")}
    assert _make_variable("FORGE_TESTS") == on_disk, \
        "add the new suite to FORGE_TESTS in the Makefile, or drop the stale name"


def test_llm_forge_closure_covers_both_libs():
    dirs = _checks_mod().SKILL_EXTRA_DIRS.get("llm-forge", [])
    assert "shared/lib/forge" in dirs
    assert "shared/lib/council" in dirs, "forge imports the council engine"


def test_llm_forge_closure_covers_the_shared_secret_patterns():
    """Asserted against the COMPUTED closure, not the declaration — the declaration is
    one of three routes into it, and checks.py arrives by a different one (SKILL_EXTRA).

    screen.py reads SECRET_FAIL/SECRET_ALLOW_SHA from checks.py, so editing the patterns
    changes what forge screens before a fleet launches. checks.py is in neither
    LIB_SCRIPTS nor GLOBAL_INPUTS, so without the entry nothing would stale the receipt.
    """
    rels = {r for r, _ in _checks_mod().source_manifest(ROOT, "llm-forge")}
    assert "scripts/lib/checks.py" in rels
    assert any(r.startswith("shared/lib/forge/") for r in rels)
    assert any(r.startswith("shared/lib/council/") for r in rels)


def test_checks_py_is_bundled_beside_forge_in_every_cli():
    """screen.py imports the secret patterns from checks.py and must never fork them.

    A plugin is copied away from this repo by the marketplace, so the repo-layout
    candidate in screen._checks() is unreachable there; the bundled copy is the only
    thing standing between a forge run and screen.py's RuntimeError.
    """
    for cli in CLIS:
        assert (_plugin(cli) / "lib" / "checks.py").is_file(), cli


def test_a_rendered_plugin_resolves_checks_from_its_own_lib(tmp_path):
    """End-to-end proof that the bundled copy is placed where the resolver looks.

    Runs against a COPY of the plugin's lib/ outside the repo, so screen._checks()'s
    repo-layout candidate cannot resolve and mask a wrongly-placed bundle. In a
    subprocess because the forge package is already imported (from shared/lib) by the
    sibling suites in this session.
    """
    lib = tmp_path / "lib"
    shutil.copytree(_plugin("claude") / "lib", lib)
    prog = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from forge import screen;"
        "m = screen._checks();"
        "assert m.SECRET_FAIL and m.SECRET_ALLOW_SHA is not None;"
        "print(m.__file__)"
    )
    r = subprocess.run([sys.executable, "-c", prog, str(lib)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(lib / "checks.py")


def _packaging_diagnostics(root: Path) -> list[str]:
    """Drive the gate through run_all() — the entry point `render.py --check` calls.

    Calling forge_packaging() directly would leave the wiring untested: deleting
    `+ forge_packaging(root)` from run_all() would keep every test here green while
    `make verify` silently stopped enforcing it. Filtered to this check's own
    diagnostics so unrelated problems on a synthetic root are not this test's business.
    """
    return [p for p in _checks_mod().run_all(root) if p.startswith("forge-packaging:")]


def _fake_root(tmp_path: Path) -> Path:
    """The minimum run_all() needs before it reaches forge_packaging: a git repo
    (scan_secrets shells out to `git ls-files` with check=True) and a capabilities.toml
    it can parse. An empty repo lists no files, so the scan passes cleanly."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "capabilities.toml").write_text("[models]\n")
    return tmp_path


def test_run_all_reports_a_forge_plugin_that_lacks_checks_py(tmp_path):
    """The packaging gate must actually fire — a validation nobody can see fail is none.

    Without it the omission surfaces as screen.py's RuntimeError on a user's first forge
    run, instead of in `make verify` where someone is looking.
    """
    root = _fake_root(tmp_path)
    lib = root / "marketplaces" / "claude" / "plugins" / "khenrix-utils" / "lib"
    (lib / "forge").mkdir(parents=True)
    (lib / "forge" / "screen.py").write_text("# bundled engine\n")
    problems = _packaging_diagnostics(root)
    assert problems and "checks.py" in problems[0], problems
    (lib / "checks.py").write_text("SECRET_FAIL = []\n")
    assert _packaging_diagnostics(root) == []


def test_run_all_is_silent_for_a_plugin_without_forge(tmp_path):
    """The gate keys on lib/forge/, not on every plugin — a wikisync-only plugin predates
    forge and must not be failed for lacking a file it never imports."""
    root = _fake_root(tmp_path)
    lib = root / "marketplaces" / "codex" / "plugins" / "khenrix-utils" / "lib"
    (lib / "wikisync").mkdir(parents=True)
    assert _packaging_diagnostics(root) == []


def test_the_gate_covers_a_cli_no_hardcoded_list_has_heard_of(tmp_path):
    """This was the one restatement of the CLI list that failed OPEN.

    The others fail CLOSED on a fourth CLI — a loud false positive, a structure check that
    visibly stops running. Here a fourth plugin would bundle lib/forge/ without
    lib/checks.py, `make verify` would say nothing, and screen.py would raise on the
    user's first forge run: exactly the failure this gate was added to move forward in
    time. So it enumerates marketplaces/ from disk rather than iterating CLIS.
    """
    root = _fake_root(tmp_path)
    lib = root / "marketplaces" / "newcli" / "plugins" / "khenrix-utils" / "lib"
    (lib / "forge").mkdir(parents=True)
    (lib / "forge" / "screen.py").write_text("# bundled engine\n")
    assert "newcli" not in _checks_mod().CLIS, "fixture precondition: an UNLISTED cli"
    problems = _packaging_diagnostics(root)
    assert problems and "newcli" in problems[0], problems
    (lib / "checks.py").write_text("SECRET_FAIL = []\n")
    assert _packaging_diagnostics(root) == []


def test_the_cli_list_has_exactly_one_definition():
    """render.py must BORROW the list, not restate it. render already imports checks, so
    checks is the only end of the dependency that can own it."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import render
    finally:
        sys.path.pop(0)
    assert render.CLIS is _checks_mod().CLIS


def test_run_all_passes_the_packaging_gate_on_the_real_repo():
    assert _packaging_diagnostics(ROOT) == []


def test_the_bundled_checks_copies_are_exempt_from_the_secret_scan():
    """checks.py holds the pattern regexes and the allowlist's example tokens, so its own
    source is exempt from scan_secrets. The rendered copies are byte-identical: scanning
    them can only raise a false positive the original would never raise."""
    skip = _checks_mod().SCAN_SKIP_PATHS
    for cli in CLIS:
        rel = f"marketplaces/{cli}/plugins/khenrix-utils/lib/checks.py"
        assert (ROOT / rel).is_file(), rel
        assert rel in skip, rel


# A `test_` name cited in PROSE. `(?!\.py)` drops module citations — `test_forge_seams.py`
# is a file that exists, not a function that might not.
_CITED_TEST = re.compile(r"\btest_[a-z0-9_]+\b(?!\.py)")


def _prose_blocks(source: str) -> list:
    """Every docstring and comment in `source` as SEPARATE blocks, and nothing that is code.

    Parsed rather than grepped because a `test_`-prefixed PARAMETER reads identically to a
    citation: `_gate_role(path, test_globs)` is not a reference to a test named `test_globs`,
    and a text search over the whole file says it is.

    Blocked rather than concatenated because a caller that flattens gets a phantom adjacency
    otherwise: the last docstring in walk order joins the first comment in the file, and a
    phrase spanning that join matches while existing nowhere a reader can find. Measured on a
    fixture whose two halves sat 234 lines apart.

    Consecutive comment LINES are one block. `tokenize` emits a token per line, and the
    referent this exists to catch is wrapped across two of them — one token per block would
    miss exactly the case that motivated the sweep.
    """
    tree = ast.parse(source)
    blocks = [d for n in ast.walk(tree)
              if isinstance(n, (ast.Module, ast.ClassDef,
                                ast.FunctionDef, ast.AsyncFunctionDef))
              for d in [ast.get_docstring(n)] if d]
    runs, prev = [], None
    for t in tokenize.generate_tokens(io.StringIO(source).readline):
        if t.type != tokenize.COMMENT:
            continue
        line = t.start[0]
        if prev is not None and line == prev + 1:
            runs[-1].append(t.string)
        else:
            runs.append([t.string])
        prev = line
    return blocks + [" ".join(r) for r in runs]


def _prose(source: str) -> str:
    return "\n".join(_prose_blocks(source))


def _flat(prose: str) -> str:
    """One line, comment markers gone — so a referent wrapped across two comment
    lines reads as the phrase it is."""
    return " ".join(prose.replace("#", " ").split())


def test_every_test_named_in_shipped_forge_prose_still_exists():
    """A docstring that cites a test by name is a cross-reference nothing resolves.

    Three renames in two waves left one dangling each, and one shipped into all three
    plugins before a reviewer caught it by reading — past the point where review is the
    reliable mechanism. `make verify` stayed green for every one.

    Guards the direction that SHIPS: `shared/lib/forge/` prose naming a test in `tests/`.
    A citation pointing the other way, and prose naming a test CONCEPT rather than a
    symbol, still needs a reader. Scoped to forge rather than all of `shared/lib/` because
    `wikisync` vendors its own suite, whose names resolve against a different directory.
    """
    forge = sorted((ROOT / "shared" / "lib" / "forge").glob("**/*.py"))
    defined = {m.group(1) for p in (ROOT / "tests").glob("test_*.py")
               for m in re.finditer(r"^def (test_[a-z0-9_]+)", p.read_text(), re.M)}
    # A typo in that pattern would empty the set and report every citation as dangling; an
    # empty glob would report none. Both failure directions are pinned before the sweep.
    assert "test_every_test_named_in_shipped_forge_prose_still_exists" in defined
    assert forge, "the forge glob matched nothing"

    dangling = sorted(f"{p.relative_to(ROOT)}: {name}" for p in forge
                      for name in _CITED_TEST.findall(_prose(p.read_text()))
                      if name not in defined)
    assert dangling == [], (
        f"{dangling} — shipped prose names a test that does not exist; rename the citation "
        "with the test, or drop it")


# Referents that name a MOMENT rather than a constraint. Matched against prose with its
# newlines flattened, because two of the three that shipped were wrapped across a line and
# one was additionally split by a `#` — no line-oriented search finds those.
#
# Deliberately narrow. "yet", "currently" and "will" are the same family but occur far more
# often in prose that is durably true, and a gate whose failures are usually false is one
# people learn to edit around. These three are the spellings that shipped.
_TEMPORAL = (
    re.compile(r"\bthis (?:same )?commit\b", re.I),
    re.compile(r"\buntil now\b", re.I),
    re.compile(r"\bTask \d", re.I),
)


def test_no_shipped_forge_prose_dates_itself_to_a_commit():
    """A comment that says "this commit" names nothing a reader can resolve.

    Every one was true when written. `bundle.py` has been rewritten six times since its
    said so; the reader who arrives now has no way to find the change it means. Three
    shipped into all three plugins and two survived a whole-branch review, which is where a
    class stops being a review's job.

    A task NUMBER is the same defect one step out: it resolves against a plan document that
    is not shipped beside the code, so it means nothing to anyone reading the plugin.

    WHAT THIS CANNOT CATCH, and the list is not short:

    * The referent that produced it. `bundle.py`'s "the gate-surface detector, which does
      not exist yet" matches none of these patterns — it dates itself by asserting a
      NON-EXISTENCE, and the same shape is durably true four modules over
      (`baseline.py`'s "has no producer yet"). A pattern separating them would have to know
      which is which, so this gate does not try.
    * A claim durably phrased and factually stale. `fleet`'s "this function now has five
      refusal paths" is true, reads as a constraint, and a sixth path falsifies it silently.

    Narrow on purpose either way: "yet", "currently" and "will" occur far more often in
    prose that is durably true — `baseline.py:58` is the measured example — and a gate whose
    failures are mostly false is one people learn to edit around.
    """
    forge = sorted((ROOT / "shared" / "lib" / "forge").glob("**/*.py"))
    assert forge, "the forge glob matched nothing"
    # Every pattern gets a known positive: one silently mis-written leaves the other two
    # reporting a clean sweep for it. The first sample also exercises the flattening, which
    # is what finds a referent split across two comment lines — most of them, at this width.
    for pat, sample in zip(_TEMPORAL, ("# fixed in this same\n    # commit",
                                       "# until now it read\n    # otherwise",
                                       "# routed to Task 5")):
        assert pat.search(_flat(sample)), pat.pattern

    dated = sorted(
        f"{p.relative_to(ROOT)}: {m.group(0)!r} in {flat[:90]!r}"
        for p in forge
        for flat in map(_flat, _prose_blocks(p.read_text()))
        for pat in _TEMPORAL
        for m in [pat.search(flat)] if m)
    assert dated == [], (
        f"{dated} — shipped prose dates itself to a moment a reader cannot resolve; say "
        "what the constraint is instead of when it was written")


# A shipped module may not cite a plan document: the plans are not shipped, so a reader of the
# installed plugin is pointed at a file they do not have — and a citation is a claim about a
# moment rather than about the code, which is the same defect `_TEMPORAL` catches one tense
# over.
#
# `(?!-)` IS LOAD-BEARING, NOT A STYLE CHOICE. Without it the pronoun branch also matches "the
# plan-mode" in `taskbundle`'s agy probe note (agy's actual mode name) and "a headless
# plan-mode reviewer" in `review.py` — two correct sentences a naive sweep would corrupt. `-`
# is a word boundary, so `\bplan\b` alone cannot tell "plan" the noun from "plan-" the prefix
# of a compound word; the lookahead is what tells them apart.
#
# THE PRONOUN LIST IS NOT THE WHOLE SHAPE. A first draft of this pattern — "Decision N",
# "Contradiction N", "Plan X", and (this|the|a later|a previous|an earlier|the next|a next)
# plan — measured 15 citations across this package where a plain-text read found 18: it missed
# "one plan away", "two plans away" and "Seven plans built the pieces" in `launch.py` and
# `runner.py`, because a bare cardinal before "plan(s)" is a citation too and no pronoun in the
# list spells it. The cardinal branch below closes that gap; a regex that only special-cased
# the three known strings would still miss the next one written the same way.
#
# RUN OVER BOTH VIEWS. `_prose_blocks` + `_flat` catches a referent wrapped across two comment
# lines, which no line-oriented grep finds (`harvest.py`'s "that decision belongs to a later
# plan" spans exactly such a wrap); the WHOLE FILE flattened additionally catches a runtime
# string literal, which the prose extraction misses.
_PLAN_CITATION = re.compile(
    r"\b(?:Decision \d+|Contradiction \d+|Plan [A-Z]\d?"
    r"|(?:this|the|a later|a previous|an earlier|the next|a next"
    r"|one|two|three|four|five|six|seven|eight|nine|ten|\d+) plans?)\b(?!-)", re.I)


def test_no_shipped_forge_module_cites_a_plan_document():
    """`(?!-)` is not a convenience: without it the pattern matches `the plan-mode` in
    `taskbundle`'s agy probe note and `a headless plan-mode reviewer` in `review.py`, both of
    which name agy's actual mode and neither of which is a plan. A detector that fires on a
    correct sentence is one an author learns to write around."""
    bad = []
    for p in sorted((ROOT / "shared" / "lib" / "forge").glob("**/*.py")):
        text = p.read_text()
        for flat in map(_flat, _prose_blocks(text)):
            for m in _PLAN_CITATION.finditer(flat):
                bad.append(f"{p.name}: {m.group(0)!r} in prose")
        whole = re.sub(r"\s+", " ", text)
        for m in _PLAN_CITATION.finditer(whole):
            bad.append(f"{p.name}: {m.group(0)!r} in {whole[max(0, m.start()-50):m.end()+30]!r}")
    assert not bad, "\n".join(sorted(set(bad)))


def test_launch_does_not_claim_it_has_no_production_caller():
    """The substance half. A sweep that rewrote 'no production caller since Plan H' into 'no
    production caller' would pass the pattern above while leaving a comment asserting
    something the code does not do — `cli.start` has called `make_launcher` since Task 6."""
    src = (ROOT / "shared" / "lib" / "forge" / "launch.py").read_text().lower()
    assert "no production caller" not in src
    assert "cli" in src, "the module no longer names the caller that exists"
