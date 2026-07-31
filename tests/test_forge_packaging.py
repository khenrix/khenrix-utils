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


def test_render_check_fails_when_a_forge_plugin_lacks_checks_py(tmp_path):
    """The packaging gate must actually fire — a validation nobody can see fail is none.

    Without this the omission surfaces as screen.py's RuntimeError on a user's first
    forge run, instead of in `make verify` where someone is looking.
    """
    checks = _checks_mod()
    lib = tmp_path / "marketplaces" / "claude" / "plugins" / "khenrix-utils" / "lib"
    (lib / "forge").mkdir(parents=True)
    (lib / "forge" / "screen.py").write_text("# bundled engine\n")
    problems = checks.forge_packaging(tmp_path)
    assert problems and "checks.py" in problems[0], problems
    (lib / "checks.py").write_text("SECRET_FAIL = []\n")
    assert checks.forge_packaging(tmp_path) == []


def test_render_check_is_silent_for_a_plugin_without_forge(tmp_path):
    """The gate keys on lib/forge/, not on every plugin — wikisync-only plugins predate
    forge and must not be failed for lacking a file they never import."""
    checks = _checks_mod()
    lib = tmp_path / "marketplaces" / "codex" / "plugins" / "khenrix-utils" / "lib"
    (lib / "wikisync").mkdir(parents=True)
    assert checks.forge_packaging(tmp_path) == []


def test_the_real_repo_passes_the_packaging_gate():
    assert _checks_mod().forge_packaging(ROOT) == []
