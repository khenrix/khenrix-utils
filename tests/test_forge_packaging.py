"""forge must ship in the rendered plugins and be inside the receipt closure."""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIS = ("claude", "codex", "agy")


def _plugin(cli: str) -> Path:
    return ROOT / "marketplaces" / cli / "plugins" / "khenrix-utils"


def _checks_mod():
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    try:
        import checks
    finally:
        sys.path.pop(0)
    return checks


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
