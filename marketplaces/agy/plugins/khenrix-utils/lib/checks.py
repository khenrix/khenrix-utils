#!/usr/bin/env python3
"""Deterministic source-of-truth checks for `make verify` (stdlib only).

Each check returns a list of problem strings (empty = clean). run_all() concatenates
them; render.check() prints + fails on any. Self-test (`--self-test`) covers the pure
logic with no repo/network dependency.
"""
from __future__ import annotations
import hashlib, json, os, re, stat, string, subprocess, sys, tempfile, tomllib
from dataclasses import dataclass
from pathlib import Path

_REAL_SUBPROCESS_RUN = subprocess.run

ROOT = Path(__file__).resolve().parent.parent.parent
FANOUT_DIR = ROOT / "shared" / "skills" / "llm-council" / "scripts"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_authority  # noqa: E402

# The CLIs a plugin is rendered for. Defined HERE and imported by render.py rather than
# the other way round: render.py already imports this module (render.check), so the
# reverse direction would be a cycle. Checks that must not silently skip an unlisted
# plugin enumerate `marketplaces/` from disk instead of iterating this — see
# forge_packaging.
CLIS = ("claude", "codex", "agy")
SKILL_NAME_RX = re.compile(r"^[a-z0-9-]{1,64}$")

# High-confidence secret shapes (fail). Written as full regex so they never match
# their own source text here. Loose shapes (bearer) are advisory, reported separately.
SECRET_FAIL = [
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"ghp_[0-9A-Za-z]{36}"),
    re.compile(r"glpat-[0-9A-Za-z_-]{20,}"),
]
SCAN_SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".zip", ".pyc", ".ico")
SCAN_SKIP_DIRS = ("evals/_fixtures/secrets/",)  # fixtures hold real-shaped fakes
# This module's own source, plus render.py's byte-identical copies of it in every plugin
# (SHARED_LIB_FILES). The copies need the same exemption for the same reason the original
# does — the pattern regexes and the allowlist's example tokens live here — and a copy of
# an exempted file can only ever yield a false positive, never a finding the original
# would not already carry. Exempting by exact path, not by basename: an unrelated
# checks.py elsewhere in the tree must still be scanned.
SCAN_SKIP_PATHS = {"scripts/lib/checks.py"} | {
    f"marketplaces/{cli}/plugins/khenrix-utils/lib/checks.py" for cli in CLIS}
# Allowlist of KNOWN-benign matches, keyed by sha256(matched_string) so the
# allowlist file can never itself be the next false positive.
SECRET_ALLOW_SHA: set[str] = {
    # example fake tokens embedded in docs/archive-adoption/implementation-plan.md
    # (they quote this module's own self-test fixtures — not real credentials):
    "492e9901d38877c93a3610b0ca256381302215dc88a3c90281440c29aea8c8eb",  # xoxp-1234567890abcde
    "1a5d44a2dca19669d72edf4c4f1c27c4c1ca4b4408fbb17f6ce4ad452d78ddb3",  # AKIAIOSFODNN7EXAMPLE
    "565135a2e0882e6a31d2d9b3a9ce4088557f327ba03ab7b482ba1b459ecd0d91",  # xoxb-123456789012-abcdefghij (test fixture in tests/test_setup_audit.py)
}


def _load_caps(root: Path) -> dict:
    with open(root / "capabilities.toml", "rb") as f:
        return tomllib.load(f)


@dataclass(frozen=True)
class EvalPolicy:
    """Normalized authority for receipts that are allowed to ship."""

    required_providers: tuple[str, ...]
    judge: str
    mode: str

    def semantic_dict(self) -> dict:
        return {
            "required_providers": list(self.required_providers),
            "judge": self.judge,
            "mode": self.mode,
        }


def eval_policy(root: Path) -> EvalPolicy:
    """Load the exact shipping panel, rejecting ambiguous or ignored policy keys."""
    table = _load_caps(_normalized_path(root)).get("eval")
    expected = {"required_providers", "judge", "mode"}
    if not isinstance(table, dict) or set(table) != expected:
        got = sorted(table) if isinstance(table, dict) else type(table).__name__
        raise ValueError(
            "capabilities [eval] must contain exactly required_providers, judge, and "
            f"mode (got {got!r})")
    providers = table.get("required_providers")
    if (not isinstance(providers, list) or not providers
            or any(not isinstance(item, str) or item not in CLIS for item in providers)
            or len(providers) != len(set(providers))):
        raise ValueError(
            "capabilities [eval].required_providers must be a non-empty ordered list "
            f"of unique providers from {list(CLIS)}")
    judge = table.get("judge")
    if not isinstance(judge, str) or judge not in CLIS:
        raise ValueError(f"capabilities [eval].judge must be one of {list(CLIS)}")
    mode = table.get("mode")
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError("capabilities [eval].mode must be a non-empty string")
    return EvalPolicy(tuple(providers), judge, mode)


def eval_policy_hash(root: Path) -> str:
    """Semantic policy digest: comments/formatting do not invalidate receipts."""
    payload = json.dumps(
        eval_policy(root).semantic_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def model_crosscheck(root: Path) -> list[str]:
    """Every model in fanout.py MODES must be registered in capabilities [models]."""
    sys.path.insert(0, str(root / "shared" / "skills" / "llm-council" / "scripts"))
    try:
        import fanout
    except Exception as e:  # noqa: BLE001
        return [f"model-crosscheck: cannot import fanout.py: {e}"]
    caps = _load_caps(root)
    # PER-PROVIDER, not a flattened union. The union let a model registered under ANY
    # provider satisfy a MODES cell for a DIFFERENT one — so a Gemini label mis-filed under
    # `claude`, or a seat pointed at another provider's model, passed the lint silently.
    # MODES cells and the [models] table are keyed by the same provider names, so the
    # stricter check costs nothing and catches the mis-file this was blind to.
    models = caps.get("models", {})
    problems = []
    for mode, cells in fanout.MODES.items():
        for provider, cell in cells.items():
            m = cell["model"]
            reg = models.get(provider)
            if not isinstance(reg, list):
                problems.append(f"model-crosscheck: capabilities [models] has no list for "
                                f"provider '{provider}' (used by MODES[{mode!r}])")
            elif m not in reg:
                where = next((p for p, v in models.items()
                              if isinstance(v, list) and m in v), None)
                extra = f" — it is registered under '{where}'" if where else ""
                problems.append(f"model-crosscheck: fanout MODES[{mode!r}] model '{m}' not "
                                f"in capabilities [models].{provider}{extra}")
    return sorted(set(problems))


def eval_policy_check(root: Path) -> list[str]:
    try:
        policy = eval_policy(root)
    except Exception as exc:  # noqa: BLE001 — this is a fail-closed lint boundary
        return [f"eval-policy: {exc}"]
    # fanout owns supported mode spellings; import it here so the policy cannot name a
    # well-formed string the executor later rejects.
    sys.path.insert(0, str(root / "shared" / "skills" / "llm-council" / "scripts"))
    try:
        import fanout
    except Exception as exc:  # noqa: BLE001
        return [f"eval-policy: cannot import fanout.py to validate mode: {exc}"]
    if policy.mode not in fanout.MODES:
        return [f"eval-policy: mode {policy.mode!r} is not in fanout.MODES"]
    return []


def pricing_coverage(root: Path) -> list[str]:
    """Every registered Claude model must have a scripts/pricing.toml entry.

    claude_session_stats.price() matches the longest pricing key that PREFIXES the model
    id and returns 0.0 when none does — and ids do not nest ("claude-opus-4-8" is not a
    prefix of "claude-opus-5"), so a missing entry silently reports $0 rather than an
    approximation or an error. That is invisible until someone reads a cost of zero and
    believes it, so make it a lint failure at the moment the model is registered.
    """
    caps = _load_caps(root)
    pricing_path = root / "scripts" / "pricing.toml"
    if not pricing_path.is_file():
        return ["pricing-coverage: scripts/pricing.toml is missing"]
    try:
        table = tomllib.loads(pricing_path.read_text())
    except Exception as e:  # noqa: BLE001
        return [f"pricing-coverage: cannot parse pricing.toml: {e}"]
    keys = set(table)
    need = ("input", "output", "cache_read", "cache_write")
    out = []
    for mid in caps.get("models", {}).get("claude", []):
        matches = sorted((k for k in keys if mid.startswith(k)), key=len, reverse=True)
        if not matches:
            out.append(f"pricing-coverage: '{mid}' is in capabilities [models].claude but "
                       f"has no scripts/pricing.toml entry — it would price at $0")
            continue
        # Presence isn't enough: price() indexes all four rates, so a half-filled table
        # trades a silent $0 for a KeyError on the statusline path — strictly worse.
        entry = table.get(matches[0])
        missing = [f for f in need if not isinstance(entry, dict) or f not in entry]
        # Presence is not enough. This lint exists because a MISSING key silently reported
        # $0; a negative, non-numeric or NaN rate reaches the statusline by the same route
        # and is just as invisible. `bool` is excluded explicitly — isinstance(True, int)
        # is True in Python, so `input = true` would otherwise pass as the number 1.
        if isinstance(entry, dict) and not missing:
            for f in need:
                v = entry[f]
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    out.append(f"pricing-coverage: {mid}.{f} is not a number ({v!r})")
                elif v != v or v in (float("inf"), float("-inf")):     # NaN / inf
                    out.append(f"pricing-coverage: {mid}.{f} is not finite ({v!r})")
                elif v < 0:
                    out.append(f"pricing-coverage: {mid}.{f} is negative ({v!r})")
        if missing:
            out.append(f"pricing-coverage: '{matches[0]}' is missing {missing} — "
                       f"price() would raise rather than price '{mid}'")
    return out


def scan_secrets(root: Path) -> list[str]:
    """Secret shapes across every tracked file. Empty means CLEAN, so it may only be
    empty over files this actually read.

    NOT-SCANNED IS NOT CLEAN, AND `except OSError: continue` MADE IT SO. Every unreadable
    tracked file was skipped in silence and the gate went green over it — a secret scanner
    failing open, which is the one direction a scanner must never fail.

    THE SPLIT IS ON ERRNO BECAUSE THE CAUSES ARE NOT ALIKE. ENOENT is ordinary: `git
    ls-files` reads the INDEX, so a tracked file deleted from the working tree (mid-rebase,
    a `rm` not yet staged, a broken symlink) is listed with no bytes on disk — and a file
    with no working-tree bytes has no working-tree secret to leak, so skipping it is not
    merely tolerable, it is correct. EACCES/EPERM on a root-owned or mode-000 file, EISDIR,
    ENOTDIR, EIO and everything else are the opposite claim: the bytes are there and this
    did not read them.

    THE CALLER FAILS THE GATE, deliberately. These strings flow through `run_all` into
    `render.check`, which prints each and exits 1 — so an unreadable tracked file turns
    `make verify` red rather than green. That is the only honest disposition: this function's
    emptiness IS the assertion "there are no secrets in this tree", `make verify` is the sole
    reader of it, and an advisory warning printed beside a passing gate is a verdict reading
    cleaner than its evidence. Fixing it costs one `chmod`; not fixing it is a clean bill of
    health over a file nobody has looked at.
    """
    # `-z`, BECAUSE WITHOUT IT GIT HANDS BACK A NAME IT WILL NOT ACCEPT BACK. `git ls-files`
    # prints a QUOTED, C-escaped DISPLAY form for any path outside plain ASCII — a tracked
    # `café.txt` arrives as `"caf\303\251.txt"` — and opening that literal raised
    # FileNotFoundError, which the ENOENT branch below reads as an ordinary deletion. So the
    # file was never scanned and the gate went green over it. Measured 2026-08-04.
    out = git_authority.run(
        ["ls-files", "-z"], repo=root, capture_output=True, check=True).stdout
    files = [b.decode("utf-8", "surrogateescape") for b in out.split(b"\0") if b]
    problems = []
    for rel in files:
        if rel.endswith(SCAN_SKIP_SUFFIX) or any(rel.startswith(d) for d in SCAN_SKIP_DIRS):
            continue
        if rel in SCAN_SKIP_PATHS:
            continue
        # TWO NAMESPACES, BECAUSE A COMMIT SHIPS THE INDEX AND NOT THE WORKING TREE. The
        # ENOENT argument above is sound about the working tree and was standing in for the
        # whole claim: a token staged and then cleaned from the worktree WITHOUT staging the
        # cleanup is still the bytes that get committed, and a working-tree-only scan calls
        # that clean.
        sources = []
        try:
            sources.append(("working tree", (root / rel).read_text(errors="ignore")))
        except FileNotFoundError:
            pass                        # no working-tree bytes; the index read below still runs
        except OSError as e:
            problems.append(
                f"{rel}: NOT SCANNED for secrets ({type(e).__name__}: "
                f"{e.strerror or e}) — this file is tracked and its bytes were never read, "
                f"so `make verify` cannot certify it. Make it readable and re-run.")
            continue
        blob = git_authority.run(
            ["cat-file", "-p", f":{rel}"], repo=root, capture_output=True)
        if blob.returncode == 0:
            sources.append(("index", blob.stdout.decode("utf-8", "ignore")))
        if not sources:
            # `ls-files` named it and NEITHER namespace resolved. Emptiness here would be a
            # clean bill of health over a file nobody read, which is the one direction this
            # function may not fail in.
            problems.append(
                f"{rel}: NOT SCANNED for secrets — `git ls-files` names it, but it has "
                f"neither working-tree bytes nor an index blob, so nothing was read.")
            continue
        for where, text in sources:
            hit = False
            for rx in SECRET_FAIL:
                m = rx.search(text)
                if m and hashlib.sha256(m.group(0).encode()).hexdigest() not in SECRET_ALLOW_SHA:
                    problems.append(
                        f"{rel} ({where}): matches secret pattern /{rx.pattern[:20]}…/")
                    hit = True
                    break
            if hit:
                break                   # one report per file; the namespaces are not two findings
    return problems


def scan_path(path: Path) -> list[str]:
    """Shape-limited secret scan over ONE file (for gitignored artifacts the
    git-ls-files walk in scan_secrets can't see). Empty if the file is absent —
    so a not-yet-generated report never crashes a gate."""
    if not path.exists():
        return []
    hits: list[str] = []
    text = path.read_text(errors="replace")
    for i, line in enumerate(text.splitlines(), 1):
        for rx in SECRET_FAIL:
            m = rx.search(line)
            if m and hashlib.sha256(m.group(0).encode()).hexdigest() not in SECRET_ALLOW_SHA:
                hits.append(f"{path}:{i}: matches secret pattern /{rx.pattern[:20]}…/")
                break
    return hits


def structure_checks(root: Path, caps: dict | None = None) -> list[str]:
    """Template/declaration parity + duplicate rendered skills. (Frontmatter rules
    stay in render.validate_skill; this only covers what's deterministic here.)"""
    caps = caps or _load_caps(root)
    problems = []
    declared = {s["name"] for s in caps.get("skills", [])}
    tmpl = {p.name for p in (root / "shared" / "skill-templates").glob("*/") if p.is_dir()}
    # every per_cli declared skill must have a template; every template must be declared
    for s in caps.get("skills", []):
        if s.get("per_cli") and s["name"] not in tmpl:
            problems.append(f"structure: declared per_cli skill '{s['name']}' has no template dir")
    for name in tmpl:
        if name not in declared:
            problems.append(f"structure: template '{name}' not declared in [[skills]]")
    # duplicate rendered skill dirs within a plugin
    for cli in CLIS:
        sk = root / "marketplaces" / cli / "plugins" / "khenrix-utils" / "skills"
        if sk.is_dir():
            names = [p.name for p in sk.glob("*/") if (p / "SKILL.md").exists()]
            for n in {x for x in names if names.count(x) > 1}:
                problems.append(f"structure: duplicate skill '{n}' in {cli} plugin")
    return problems


def forge_packaging(root: Path) -> list[str]:
    """A plugin that bundles lib/forge/ must bundle checks and its Git authority.

    forge/screen.py imports SECRET_FAIL/SECRET_ALLOW_SHA from this module by path so the
    patterns have one definition. Its repo-layout candidate dies the moment a marketplace
    copies the plugin elsewhere, leaving <plugin>/lib/checks.py as the only reachable one.
    checks.py in turn imports <plugin>/lib/git_authority.py. Absent either file, screen.py
    raises on a user's first forge run. This moves the failure to `make verify`, where
    someone is looking.

    Enumerated from DISK, not from CLIS. This is the one restatement of the CLI list that
    failed OPEN: a fourth plugin bundling lib/forge/ without one of these modules is exactly
    the state the gate exists to catch, and a hardcoded triple would say nothing about it
    while every other check that iterates CLIS at least fails loudly. The check must cover
    whatever plugins are actually on disk, so an unlisted one cannot ship past it.
    """
    problems = []
    for lib in sorted((root / "marketplaces").glob("*/plugins/khenrix-utils/lib")):
        if not (lib / "forge").is_dir():
            continue
        cli = lib.parents[2].name
        missing = [name for name in ("checks.py", "git_authority.py")
                   if not (lib / name).is_file()]
        if missing:
            problems.append(
                f"forge-packaging: {cli} plugin bundles lib/forge/ without "
                f"{', '.join('lib/' + name for name in missing)} — screen.py would "
                "raise at runtime")
    return problems


def _optional(root: Path, module: str, fn: str) -> list[str]:
    """Run a sibling lint if it is on disk, and say so loudly if it is not importable.

    IMPORTED HERE RATHER THAN AT MODULE SCOPE because `render.py` imports this file and
    `render.py` is in every skill's source-hash closure — a top-level import of a module
    that later grows a dependency would rewrite twelve receipts for a reason nobody chose.
    The plan that specified both lints makes this its first constraint.

    A MISSING FILE IS A PROBLEM, NOT A SKIP. `checks.run_all` is what `make verify` calls, and
    a lint that silently does not run is indistinguishable from one that passed — the vacuous
    green this module refuses everywhere else.
    """
    path = root / "scripts" / "lib" / f"{module}.py"
    if not path.is_file():
        return [f"{module}: {path} is missing, so its checks did not run — "
                "a lint that does not run is not a lint that passed"]
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"khenrix_{module}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(getattr(mod, fn)(root))


def run_all(root: Path = ROOT) -> list[str]:
    caps = _load_caps(root)
    return (model_crosscheck(root) + eval_policy_check(root) + pricing_coverage(root)
            + scan_secrets(root) + structure_checks(root, caps)
            + forge_packaging(root)
            # THE TWO SIBLING LINTS, appended AFTER forge_packaging — the plans that
            # specified them both name preserving its position as a constraint.
            + _optional(root, "portability", "run")
            + _optional(root, "charts", "check_charts"))


# --------------------------------------------------------------------------- #
# Eval-receipt gate (Increment 7) — source-input closure → hash → freshness gate.
# --------------------------------------------------------------------------- #
LIB_SCRIPTS = ["scripts/lib/reconcile.py", "scripts/lib/inventory.py"]  # bundled into every skill
# THE CERTIFIER AND THE TEST MANIFEST ARE INPUTS TO EVERY RECEIPT, and leaving them out meant
# a gate could be NARROWED — a suite dropped from DETERMINISTIC_GATED, a test deleted from the
# Makefile — while every existing receipt stayed fresh. A receipt says "this source was
# certified"; what "certified" means is decided by these files, so a change to them has to
# stale it exactly as a change to the skill does.
GLOBAL_INPUTS = ["scripts/render.py",        # render assembly affects EVERY rendered body
                 "scripts/eval_harness.py",  # decides what the gate RUNS
                 "scripts/lib/checks.py",    # decides what the gate ACCEPTS
                 "scripts/lib/git_authority.py",  # decides which Git state gates OBSERVE
                 "Makefile"]                 # names the suites a gate can name
# Extra behavior-affecting inputs per skill: reconcile/instructions consumers read
# capabilities.toml + house-style.md (+ overlays); llm-council bundles headless-invocation.md.
SKILL_EXTRA = {
    "khenrix-setup":   ["capabilities.toml", "house-style.md"],
    "khenrix-upgrade": ["capabilities.toml", "house-style.md"],
    "llm-council":     ["headless-invocation.md"],
    # tuneup.py's `approved_models()` reads capabilities [models] AT RUNTIME and
    # `tag_model()` returns "current" vs "stale-candidate" from it — so registering a new
    # model changes what skill-tuneup REPORTS while nothing staled its receipt. That is the
    # same "a gate could be NARROWED" hole GLOBAL_INPUTS was written to close, and it went
    # live on 2026-08-14 when Gemini 3.7 was registered: every skill still naming 3.6 kept
    # being tagged `current` by a skill whose receipt said it was unchanged.
    "skill-tuneup":    ["capabilities.toml"],
}
# Extra behavior-affecting DIRECTORIES per skill (rglob'd into the closure). The wiki
# skills' SKILL.md drives a shared stdlib engine — editing it must stale both receipts.
SKILL_EXTRA_DIRS = {
    "khenrix-wiki-add":  ["shared/lib/wikisync"],
    "khenrix-wiki-sync": ["shared/lib/wikisync"],
    # the council engine moved out of the skill dir; without this line, engine edits
    # no longer move llm-council's source_hash and precommit stops gating them.
    "llm-council":       ["shared/lib/council"],
    # llm-forge drives BOTH shared engines; editing either must stale its receipt. The
    # skill itself arrives in a later plan — the entry is inert until evals/llm-forge
    # exists, because receipt_gate only walks skills that have an evals.json.
    "llm-forge":         ["shared/lib/forge", "shared/lib/council"],
}

# Test files that directly earn deterministic receipts. Keeping the patterns beside the
# source-closure authority makes additions and removals stale the receipt just as edits do;
# eval_harness imports this map when it constructs the certifying command.
DETERMINISTIC_GATE_INPUT_GLOBS = {
    "llm-forge": ["tests/test_forge_*.py"],
}
DETERMINISTIC_GATE_AMBIENT_INPUT_GLOBS = {
    # The certifier runs with repository pytest configuration disabled, but conftests and
    # imported/scanned repository Python remain behavior-affecting inputs even though they
    # are not positional test arguments. Two Forge meta-tests deliberately inspect every
    # Python module under shared/scripts and every test module, so that complete transitive
    # read set belongs in the receipt closure too.
    "llm-forge": [
        "conftest.py", "tests/**/conftest.py", "tests/forge_fixtures.py",
        "shared/**/*.py", "scripts/**/*.py", "tests/test_*.py", "tests/test_*.bats",
        # Packaging tests execute the checked-in plugin facade and bundled engine. These
        # are derived outputs, but they are still direct certifier inputs: deleting one
        # must stale the receipt before the suite is re-run.
        "marketplaces/*/plugins/khenrix-utils/lib/forge/**/*",
        "marketplaces/*/plugins/khenrix-utils/lib/council/**/*",
        "marketplaces/*/plugins/khenrix-utils/lib/checks.py",
        "marketplaces/*/plugins/khenrix-utils/skills/llm-forge/**/*",
    ],
}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def validate_skill_name(skill: object) -> str:
    """Return one safe skill-name component or refuse it before any path join."""
    if not isinstance(skill, str) or not SKILL_NAME_RX.fullmatch(skill):
        raise ValueError(
            f"invalid skill name {skill!r}; expected lowercase letters, digits, and "
            "hyphens (1-64 characters)")
    return skill


def validate_provider_name(provider: object) -> str:
    if provider not in CLIS:
        raise ValueError(f"invalid provider {provider!r}; expected one of {list(CLIS)}")
    return str(provider)


def _normalized_path(path: Path) -> Path:
    """Absolute lexical normalization without following a possibly hostile symlink."""
    return Path(os.path.abspath(os.fspath(path)))


def _skill_source_files(root: Path, skill: str) -> list[Path]:
    """Full behavior-affecting input closure for a skill: its own dir, the LIB_SCRIPTS
    + render.py bundled/applied to every skill, and skill-specific extras (reconcile
    inputs / overlays / headless doc). Excludes pycache/pyc."""
    root = _normalized_path(root)
    skill = validate_skill_name(skill)
    files = []
    for base in (root / "shared" / "skills" / skill,
                 root / "shared" / "skill-templates" / skill):
        if base.is_dir():
            files += [p for p in base.rglob("*") if p.is_file()
                      and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    # `dict.fromkeys`, not a set: a file can earn its place through more than one closure
    # edge, but source_manifest must hash it once.
    for rel in dict.fromkeys(LIB_SCRIPTS + GLOBAL_INPUTS + SKILL_EXTRA.get(skill, [])):
        p = root / rel
        if p.is_file():
            files.append(p)
    for d in SKILL_EXTRA_DIRS.get(skill, []):  # whole shared-engine dirs into the closure
        base = root / d
        if base.is_dir():
            files += [p for p in base.rglob("*") if p.is_file()
                      and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    gate_patterns = (
        DETERMINISTIC_GATE_INPUT_GLOBS.get(skill, [])
        + DETERMINISTIC_GATE_AMBIENT_INPUT_GLOBS.get(skill, []))
    for pattern in gate_patterns:
        files += [p for p in root.glob(pattern) if p.is_file()
                  and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    if skill in ("khenrix-setup", "khenrix-upgrade"):  # overlays change reconcile output
        caps = _load_caps(root)
        for ov in (caps.get("instructions", {}).get("overlays") or {}).values():
            p = root / ov
            if p.is_file():
                files.append(p)
    return list(dict.fromkeys(files))


def _regular_file_state(path: Path, root: Path) -> tuple[int, bytes]:
    """Return mode and bytes without crossing a symlink inside the receipt root."""
    root = _normalized_path(root)
    path = _normalized_path(path)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{path}: receipt input is outside {root}") from exc
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise ValueError(
                f"{path}: receipt inputs must be regular files under regular directories, "
                f"not symlinks ({current})")
    if not path.is_file():
        raise ValueError(f"{path}: receipt input is not a regular file")
    return path.lstat().st_mode & 0o7777, path.read_bytes()


@dataclass(frozen=True)
class SourceInputSnapshot:
    """Exact regular source files and semantic facts behind one source hash."""

    source_files: tuple[tuple[str, int, bytes], ...]
    manifest: tuple[tuple[str, str], ...]
    source_hash: str


@dataclass(frozen=True)
class GateTreeSnapshot:
    """Typed Git-visible working-tree state used by deterministic certifiers."""

    directories: tuple[tuple[str, int], ...]
    files: tuple[tuple[str, int, bytes], ...]
    gate_tree_hash: str


_GATE_CACHE_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
})
def sanitized_git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a Git environment bound to arguments/cwd, never an ambient repository.

    Candidate capture and private-snapshot execution share this authority. Otherwise an
    exported GIT_DIR or GIT_INDEX_FILE can make `git -C <candidate>` enumerate a foreign
    index while still exiting successfully, silently omitting force-tracked ignored files.
    Isolating config also keeps a machine-global excludes file from changing the tree the
    receipt claims to certify.
    """
    return git_authority.sanitized_environment(base)


def _gate_tree_excluded(rel: str, *, is_dir: bool) -> bool:
    parts = Path(rel).parts
    if not parts or ".git" in parts or any(part in _GATE_CACHE_DIRS for part in parts):
        return True
    # Tune-up run logs are mutable bookkeeping, deliberately outside receipt closure.
    # Exclude the directory itself as well as its file roster so appending the terminal
    # marker after certification cannot immediately stale the receipt it records.
    if parts[:3] == ("docs", "tuneups", "log"):
        return True
    if not is_dir and (rel.endswith(".pyc")
                       or (Path(rel).name.startswith(".receipt.")
                           and Path(rel).name.endswith(".tmp"))):
        return True
    if len(parts) >= 3 and parts[0] == "evals" and parts[2] == "workspace":
        return True
    if (not is_dir and len(parts) == 3 and parts[0] == "evals"
            and parts[2] == "receipt.json"):
        return True
    return False


def _git_paths(root: Path) -> set[str]:
    # Repository enumeration is part of candidate capture, not part of the certifier.
    # Replacing the latter for a seam test must not also replace this trusted operation.
    result = git_authority.run(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        repo=root, capture_output=True, runner=_REAL_SUBPROCESS_RUN)
    if result.returncode != 0:
        raise ValueError(
            f"{root}: cannot enumerate deterministic gate tree: "
            f"{result.stderr.decode(errors='replace').strip()}")
    paths = set()
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"{root}: Git returned unsafe gate-tree path {rel!r}")
        if not _gate_tree_excluded(rel, is_dir=False):
            paths.add(candidate.as_posix())
    return paths


def _ignored_gate_directories(root: Path, rels: list[str]) -> set[str]:
    if not rels:
        return set()
    payload = b"\0".join(os.fsencode(rel) for rel in rels) + b"\0"
    result = git_authority.run(
        ["check-ignore", "--no-index", "--stdin", "-z"], repo=root,
        # check-ignore rejects Git's global --literal-pathspecs mode for --stdin. Its
        # NUL-delimited stdin records are already path records rather than shell patterns;
        # ambient pathspec variables remain scrubbed by the authority either way.
        literal_pathspecs=False, input=payload, capture_output=True,
        runner=_REAL_SUBPROCESS_RUN)
    if result.returncode not in (0, 1):
        raise ValueError(
            f"{root}: cannot classify gate-tree directories: "
            f"{result.stderr.decode(errors='replace').strip()}")
    return {Path(os.fsdecode(raw)).as_posix()
            for raw in result.stdout.split(b"\0") if raw}


def gate_tree_snapshot(root: Path) -> GateTreeSnapshot:
    """Capture regular tracked/untracked inputs plus non-ignored directory identity.

    Git supplies the file roster so ignored local archives and secrets never enter the
    snapshot. A filesystem walk contributes typed directories (including empty ones) and
    rejects any Git-visible symlink or special file before a certifier can observe it.
    """
    root = _normalized_path(root)
    file_roster = _git_paths(root)
    directory_candidates: set[str] = set()
    special_candidates: list[tuple[str, int]] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        rel_current = current_path.relative_to(root)
        kept_dirs = []
        for name in sorted(dirnames):
            rel = (rel_current / name).as_posix()
            if _gate_tree_excluded(rel, is_dir=True):
                continue
            path = current_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                special_candidates.append((rel, mode))
                continue
            if not stat.S_ISDIR(mode):
                special_candidates.append((rel, mode))
                continue
            directory_candidates.add(rel)
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in filenames:
            rel = (rel_current / name).as_posix()
            if _gate_tree_excluded(rel, is_dir=False):
                continue
            mode = (current_path / name).lstat().st_mode
            if not stat.S_ISREG(mode) and rel in file_roster:
                special_candidates.append((rel, mode))

    ignored_dirs = _ignored_gate_directories(
        root, sorted(directory_candidates | {rel for rel, _mode in special_candidates}))
    directories = []
    for rel in sorted(directory_candidates - ignored_dirs):
        path = root / rel
        mode = path.lstat().st_mode
        if not stat.S_ISDIR(mode):
            raise ValueError(f"{path}: gate-tree directory changed type during capture")
        directories.append((rel, mode & 0o7777))

    for rel, mode in special_candidates:
        if rel in file_roster or rel not in ignored_dirs:
            kind = "symlink" if stat.S_ISLNK(mode) else "special file"
            raise ValueError(f"{root / rel}: deterministic gate tree contains a {kind}")

    files = []
    for rel in sorted(file_roster):
        path = root / rel
        if not os.path.lexists(path):
            continue  # tracked deletion: absence is the candidate state
        mode, content = _regular_file_state(path, root)
        files.append((rel, mode, content))

    identity = ([('directory', rel, mode, '') for rel, mode in directories]
                + [('regular', rel, mode, _sha(content))
                   for rel, mode, content in files])
    return GateTreeSnapshot(
        directories=tuple(directories), files=tuple(files),
        gate_tree_hash=_sha(json.dumps(identity, separators=(",", ":")).encode()),
    )


def source_input_snapshot(root: Path, skill: str) -> SourceInputSnapshot:
    """Read each behavior-affecting source once for hashing and hermetic execution."""
    root = _normalized_path(root)
    skill = validate_skill_name(skill)
    files = []
    entries = []
    for p in _skill_source_files(root, skill):
        mode, content = _regular_file_state(p, root)
        rel = p.relative_to(root).as_posix()
        framed = f"regular:{mode:o}\0".encode() + content
        files.append((rel, mode, content))
        entries.append((rel, _sha(framed)))
    caps = _load_caps(root)
    facts = caps.get("skill_facts", {}).get(skill)
    if facts is not None:
        entries.append((f"skill_facts:{skill}",
                        _sha(json.dumps(facts, sort_keys=True).encode())))
    # Every receipt is interpreted through the same shipping policy.  Bind its
    # normalized meaning—not TOML whitespace or comments—into every source identity.
    entries.append(("eval_policy", eval_policy_hash(root)))
    manifest = tuple(sorted(entries))
    return SourceInputSnapshot(
        source_files=tuple(sorted(files, key=lambda item: item[0])),
        manifest=manifest,
        source_hash=_sha(json.dumps(manifest, sort_keys=True).encode()),
    )


def source_manifest(root: Path, skill: str) -> list:
    """Sorted (relpath, identity+content hash) pairs plus canonical skill facts."""
    return list(source_input_snapshot(root, skill).manifest)


def source_hash(root: Path, skill: str) -> str:
    validate_skill_name(skill)
    return source_input_snapshot(root, skill).source_hash


def expected_rendered_skill(root: Path, skill: str, provider: str) -> bytes:
    """Compute the exact SKILL.md bytes render.py should have produced for one provider."""
    root = _normalized_path(root)
    skill = validate_skill_name(skill)
    provider = validate_provider_name(provider)
    shared = root / "shared" / "skills" / skill / "SKILL.md"
    if shared.is_file():
        _mode, body = _regular_file_state(shared, root)
        return body
    template = root / "shared" / "skill-templates" / skill / "SKILL.md.tmpl"
    _mode, raw = _regular_file_state(template, root)
    facts = _load_caps(root).get("skill_facts", {}).get(skill, {}).get(provider)
    if not isinstance(facts, dict):
        raise ValueError(
            f"{skill}: no [skill_facts.{skill}.{provider}] in capabilities.toml")
    try:
        return string.Template(raw.decode("utf-8")).substitute(facts).encode("utf-8")
    except (UnicodeDecodeError, KeyError, ValueError) as exc:
        raise ValueError(f"{skill}/{provider}: cannot render canonical skill: {exc}") from exc


@dataclass(frozen=True)
class EvalInputSnapshot:
    """One exact manifest/fixture read, suitable for both execution and hashing."""

    spec: dict
    manifest: bytes
    fixture_dirs: tuple[tuple[str, int], ...]
    fixture_files: tuple[tuple[str, int, bytes], ...]
    roster: tuple[tuple[object, str], ...]
    eval_set_hash: str


def eval_input_snapshot(root: Path, skill: str) -> EvalInputSnapshot:
    """Capture the complete eval input set once, refusing traversal and symlinks."""
    root = _normalized_path(root)
    skill = validate_skill_name(skill)
    ev_dir = root / "evals" / skill
    spec, manifest = load_eval_manifest(root, skill)
    fixture_dirs: list[tuple[str, int]] = []
    fixture_files: list[tuple[str, int, bytes]] = []
    fx = ev_dir / "fixtures"
    if fx.is_symlink():
        raise ValueError(
            f"{fx}: receipt inputs must be regular files under regular directories, "
            f"not symlinks ({fx})")
    if fx.exists() and not fx.is_dir():
        raise ValueError(f"{fx}: fixture root must be a regular directory")
    if fx.is_dir():
        fixture_dirs.append(("fixtures", fx.lstat().st_mode & 0o7777))
        for p in sorted(fx.rglob("*")):
            if p.is_symlink():
                raise ValueError(
                    f"{p}: receipt inputs must be regular files under regular directories, "
                    f"not symlinks ({p})")
            rel = p.relative_to(ev_dir).as_posix()
            if p.is_dir():
                fixture_dirs.append((rel, p.lstat().st_mode & 0o7777))
            elif p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                mode, content = _regular_file_state(p, root)
                fixture_files.append((rel, mode, content))
            elif not p.is_file():
                raise ValueError(f"{p}: fixture input is not a regular file or directory")

    available = ({rel for rel, _mode in fixture_dirs}
                 | {rel for rel, _mode, _content in fixture_files})
    for index, case in enumerate(spec["evals"]):
        for requested in case.get("files", []):
            key = f"fixtures/{requested}"
            if key not in available:
                raise _manifest_problem(
                    ev_dir / "evals.json", index, "files entry",
                    f"requested fixture {requested!r} does not exist in fixtures/")

    h = hashlib.sha256()
    h.update(manifest)
    for rel, mode in fixture_dirs:
        h.update(f"directory\0{rel}\0{mode:o}\0".encode())
    for rel, mode, content in fixture_files:
        h.update(f"regular\0{rel}\0{mode:o}\0".encode())
        h.update(_sha(content).encode())
    return EvalInputSnapshot(
        spec=spec,
        manifest=manifest,
        fixture_dirs=tuple(fixture_dirs),
        fixture_files=tuple(fixture_files),
        roster=tuple((case["id"], case["name"]) for case in spec["evals"]),
        eval_set_hash=h.hexdigest(),
    )


def eval_set_hash(root: Path, skill: str) -> str:
    """Hash the exact manifest and fixture bytes returned by eval_input_snapshot()."""
    validate_skill_name(skill)
    return eval_input_snapshot(root, skill).eval_set_hash


EVAL_COMPONENT_MAX_BYTES = 120
EVAL_FIXTURE_PATH_MAX_BYTES = 240


def _manifest_problem(path: Path, index: int, field: str, detail: str) -> ValueError:
    return ValueError(f"{path}: eval at index {index} has invalid {field}: {detail}")


def _safe_eval_component(path: Path, index: int, field: str, value: object) -> int | str:
    """Validate a single filename component used in an eval workspace path."""
    if field == "id" and type(value) is int:
        if value < 0 or len(str(value).encode("utf-8")) > EVAL_COMPONENT_MAX_BYTES:
            raise _manifest_problem(path, index, field, "integer is outside the safe range")
        return value
    if not isinstance(value, str):
        raise _manifest_problem(path, index, field, "must be a non-empty string"
                                + (" or non-negative integer" if field == "id" else ""))
    encoded = value.encode("utf-8")
    if (not value.strip() or len(encoded) > EVAL_COMPONENT_MAX_BYTES
            or value in (".", "..") or value.startswith(".")
            or "/" in value or "\\" in value
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise _manifest_problem(
            path, index, field,
            f"must be one safe component of at most {EVAL_COMPONENT_MAX_BYTES} UTF-8 bytes")
    return value


def _safe_fixture_path(path: Path, index: int, value: object) -> str:
    """Validate a normalized, portable relative fixture path."""
    if not isinstance(value, str):
        raise _manifest_problem(path, index, "files entry", "must be a string")
    encoded = value.encode("utf-8")
    parts = value.split("/")
    if (not value or len(encoded) > EVAL_FIXTURE_PATH_MAX_BYTES
            or value.startswith("/") or "\\" in value or ":" in value
            or any(not part or part in (".", "..") for part in parts)
            or any(len(part.encode("utf-8")) > EVAL_COMPONENT_MAX_BYTES for part in parts)
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise _manifest_problem(
            path, index, "files entry",
            "must be a normalized relative path with safe bounded components")
    return value


def load_eval_manifest(root: Path, skill: str) -> tuple[dict, bytes]:
    """Read an eval manifest once and validate every runtime-consumed field.

    The returned bytes are the exact bytes receipt hashing must include. Keeping parsing and
    validation together means the eval runner validates the same single read it later runs;
    malformed paths cannot reach workspace creation first.
    """
    root = _normalized_path(root)
    skill = validate_skill_name(skill)
    path = root / "evals" / skill / "evals.json"
    _mode, raw = _regular_file_state(path, root)
    try:
        spec = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(spec, dict) or not isinstance(spec.get("evals"), list):
        raise ValueError(f"{path}: root must contain an evals list")
    if not spec["evals"]:
        raise ValueError(f"{path}: evals list must not be empty")
    ids: list[int | str] = []
    rendered: set[str] = set()
    for index, case in enumerate(spec["evals"]):
        if not isinstance(case, dict):
            raise ValueError(f"{path}: eval at index {index} must be an object")
        if "id" not in case:
            raise _manifest_problem(path, index, "id", "is required")
        value = _safe_eval_component(path, index, "id", case["id"])
        key = str(value)
        if key in rendered:
            raise ValueError(f"{path}: duplicate rendered eval id {key!r}")
        rendered.add(key)
        ids.append(value)
        if "name" not in case:
            raise _manifest_problem(path, index, "name", "is required")
        _safe_eval_component(path, index, "name", case["name"])
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise _manifest_problem(path, index, "prompt", "must be a non-empty string")
        assertions = case.get("assertions")
        if (not isinstance(assertions, list) or not assertions
                or any(not isinstance(item, str) or not item.strip() for item in assertions)):
            raise _manifest_problem(path, index, "assertions",
                                    "must be a non-empty list of non-empty strings")
        files = case.get("files", [])
        if not isinstance(files, list):
            raise _manifest_problem(path, index, "files", "must be a list")
        for item in files:
            _safe_fixture_path(path, index, item)
    return spec, raw


def eval_ids(root: Path, skill: str) -> list[int | str]:
    """Return the unique workspace IDs from a complete validated manifest."""
    validate_skill_name(skill)
    spec, _raw = load_eval_manifest(root, skill)
    return [case["id"] for case in spec["evals"]]


def _evald_skills(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "evals").glob("*/")
                  if (p / "evals.json").exists())


def expected_eval_skills(root: Path) -> list[str]:
    """Every full-gate source plus every extant eval directory.

    Sources are authoritative for whether a receipt is required: deleting an eval manifest
    must not remove its skill from the gate. Existing directories remain in the roster so a
    stray or partially deleted eval tree is diagnosed instead of becoming invisible.
    """
    sources = set()
    for base, manifest in (
        (root / "shared" / "skills", "SKILL.md"),
        (root / "shared" / "skill-templates", "SKILL.md.tmpl"),
    ):
        if base.is_dir():
            sources.update(path.name for path in base.iterdir()
                           if path.is_dir() and (path / manifest).is_file())
    eval_root = root / "evals"
    existing = ({path.name for path in eval_root.iterdir() if path.is_dir()}
                if eval_root.is_dir() else set())
    return sorted(sources | existing)


def _receipt_is_certified(rec: dict) -> bool:
    """Whether this receipt records a certification that PASSED, as opposed to fresh inputs.

    `receipt_gate` compared two hashes and nothing else, so a receipt carrying matching
    hashes and `self_test: false` was accepted — "the certification failed" and "the
    certification passed" left the same verdict at the gate. A seeded receipt is exempt and
    says so in its own `provenance`: seeding is an explicit human act blessing a committed
    state, not a claim that a suite ran.
    """
    if "self_test" in rec:
        # PRESENT AND FALSE IS A FAILED CERTIFICATION, whatever the provenance says. Nothing
        # writes that state today — `_write_receipt` raises rather than recording a failure —
        # so reaching it means a receipt was edited or assembled by hand, which is exactly
        # when the gate should refuse rather than read the field's neighbours for reassurance.
        return rec["self_test"] is True
    if "certified_by" in rec:
        # AN ORDINARY SKILL HAS NO SELF-TEST, AND ITS EVAL IS THE CERTIFICATION. `self_test`
        # is written only by the llm-council and deterministic-gated branches, so this
        # predicate used to refuse every REAL eval of every other skill — the receipt said
        # `provenance: "eval"`, carried no `self_test`, and "absent" was read as the seeded
        # shape it is not. Measured on khenrix-setup and khenrix-upgrade: a genuine run,
        # delta +0.07 and +0.03, refused at the gate, with seeding over the real result the
        # only way past. `certified_by` names the gate that ran, so the receipt says which.
        return bool(rec["certified_by"])
    # NEITHER is the ordinary seeded shape: deterministic targets run their real certifier
    # and therefore take one of the branches above. For other skills `--seed-receipt`
    # blesses a committed state without running a gate and says so in `provenance`.
    return str(rec.get("provenance", "")).startswith("seeded")


CURRENT_RECEIPT_SCHEMA = 3

# Exact deterministic certifier expected for each panel-exempt skill. A receipt is an
# input file, not an authority: `self_test: true` cannot be allowed to choose its own gate.
# eval_harness.py asserts that its producer-side routing is identical to this map.
SELF_TEST_CERTIFIERS = {
    "khenrix-wiki-add": "wikisync-unittests",
    "khenrix-wiki-sync": "wikisync-unittests",
    "llm-council": "fanout --self-test",
    "llm-forge": "forge-suite-all",
}
GATE_EVIDENCE_SKILLS = frozenset({"khenrix-wiki-add", "khenrix-wiki-sync", "llm-forge"})
GATE_COUNT_KEYS = frozenset({"tests_run", "skipped", "failed"})


def requires_deterministic_gate(skill: str) -> bool:
    """Whether a target's receipt must be earned by a deterministic certifier.

    This target-only question is deliberately separate from receipt evidence: it remains
    true while a receipt is missing, corrupt, stale, or names the wrong certifier.
    """
    return skill in SELF_TEST_CERTIFIERS


def deterministic_gate_command(root: Path, skill: str) -> list[str] | None:
    """Return the exact command allowed to earn this skill's deterministic receipt.

    Commands use repo-relative paths and must run with cwd=root. The receipt producer and
    verifier both call this function, so a receipt cannot nominate a narrower command.
    """
    root = _normalized_path(root)
    skill = validate_skill_name(skill)
    if skill in ("khenrix-wiki-add", "khenrix-wiki-sync"):
        return ["python3", "-m", "unittest", "discover", "-s",
                "shared/lib/wikisync/tests"]
    if skill == "llm-council":
        return ["python3", "shared/skills/llm-council/scripts/fanout.py", "--self-test"]
    if skill == "llm-forge":
        tests = [
            p.relative_to(root).as_posix()
            for pattern in DETERMINISTIC_GATE_INPUT_GLOBS[skill]
            for p in sorted(root.glob(pattern))
        ]
        return ["uvx", "--with", "pytest==9.1.1", "pytest", "-q",
                "-c", os.devnull, "--rootdir", ".", *tests]
    return None


def _deterministic_gate_evidence_problem(root: Path, skill: str,
                                         rec: dict) -> str | None:
    """Validate the producer evidence carried by test-suite-gated receipts.

    llm-council remains separate: its receipt is earned by the fanout self-test, which does
    not emit this subprocess command/count shape. Do not fabricate evidence it has not
    produced.
    """
    if not requires_deterministic_gate(skill):
        return None
    command = rec.get("gate_command")
    if (not isinstance(command, list) or not command
            or any(not isinstance(item, str) or not item.strip() for item in command)):
        return "gate_command must be a non-empty list of non-empty strings"
    expected_command = deterministic_gate_command(root, skill)
    if command != expected_command:
        return ("gate_command does not exactly match the current deterministic certifier "
                f"(expected {expected_command!r})")
    gate_tree_hash = rec.get("gate_tree_hash")
    if (not isinstance(gate_tree_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", gate_tree_hash) is None):
        return "gate_tree_hash must be a lowercase SHA-256 digest"
    if skill == "llm-council":
        return None
    counts = rec.get("gate_counts")
    if not isinstance(counts, dict) or set(counts) != GATE_COUNT_KEYS:
        return "gate_counts must contain exactly tests_run, skipped, and failed"
    if any(type(counts[key]) is not int for key in GATE_COUNT_KEYS):
        return "gate_counts values must be non-bool integers"
    if counts["tests_run"] <= 0 or counts["skipped"] != 0 or counts["failed"] != 0:
        return "gate_counts must record tests_run > 0 with skipped=0 and failed=0"
    return None


def is_self_test_gated(skill: str, rec: object, root: Path = ROOT) -> bool:
    """Does this receipt come from a deterministic suite rather than a judge panel?

    ONE definition, because there are now two readers. `validate_receipt` uses it to
    SKIP the provenance and full-panel checks; `verify-final-receipt` uses it to say
    which gate it actually proved. That command used to print "receipt is full-panel"
    unconditionally — including for the very skills exempted from the panel check — so
    its success line asserted the one property it had deliberately not verified. Copying
    the predicate into the CLI would have recreated the two-rulebook drift this module's
    own docstring warns about, so it is exported instead.
    """
    if not isinstance(rec, dict):
        return False
    expected = SELF_TEST_CERTIFIERS.get(skill)
    if (not requires_deterministic_gate(skill)
            or rec.get("self_test") is not True
            or rec.get("certified_by") != expected
            or rec.get("provenance") != "eval"):
        return False
    if _deterministic_gate_evidence_problem(root, skill, rec) is not None:
        return False
    if skill == "llm-council":
        return True
    return rec.get("deterministic_gate") == expected


def _shipping_evidence_problems(root: Path, skill: str, rec: dict) -> list[str]:
    """Validate the canonical advisory panel for every shipping receipt.

    Deterministic skills retain their stronger suite gate, but that gate no longer
    exempts them from recording a completed Codex/Gemini review of the same candidate.
    """
    try:
        policy = eval_policy(root)
        policy_digest = eval_policy_hash(root)
    except Exception as exc:  # noqa: BLE001
        return [f"receipt: {skill} cannot load canonical eval policy: {exc}"]
    problems = []
    if rec.get("eval_policy_hash") != policy_digest:
        problems.append(
            f"receipt: {skill} eval policy digest is stale or missing — re-run the "
            "canonical panel")
    if rec.get("providers") != list(policy.required_providers):
        problems.append(
            f"receipt: {skill} provider order is {rec.get('providers')!r}, needs exact "
            f"canonical order {list(policy.required_providers)!r}")
    if rec.get("judge") != policy.judge:
        problems.append(
            f"receipt: {skill} judge is {rec.get('judge')!r}, needs {policy.judge!r}")
    if rec.get("mode") != policy.mode:
        problems.append(
            f"receipt: {skill} mode is {rec.get('mode')!r}, needs {policy.mode!r}")
    models = rec.get("models")
    if (not isinstance(models, dict)
            or not isinstance(models.get("judge"), str)
            or not models["judge"].strip()):
        problems.append(
            f"receipt: {skill} has no recorded model provenance for the "
            f"{policy.judge} judge")
    try:
        expected_n_evals = len(eval_ids(root, skill))
    except Exception as exc:  # noqa: BLE001
        return problems + [
            f"receipt: {skill} cannot read current eval ids: {exc}"]
    evidence = rec.get("per_provider")
    evidence = evidence if isinstance(evidence, dict) else {}
    if set(evidence) != set(policy.required_providers):
        problems.append(
            f"receipt: {skill} per_provider keys are {sorted(evidence)!r}, need exactly "
            f"{sorted(policy.required_providers)!r}")
    for provider in policy.required_providers:
        row = evidence.get(provider)
        if (not isinstance(row, dict)
                or type(row.get("delta")) not in (int, float)
                or not -1 <= row["delta"] <= 1
                or type(row.get("n_evals")) is not int
                or row["n_evals"] != expected_n_evals
                or row.get("status") != "ok"):
            problems.append(
                f"receipt: {skill} has no completed {provider} evidence for all "
                f"{expected_n_evals} current evals")
    return problems


def validate_receipt(root: Path, skill: str, *, final: bool = False,
                     panel: list | None = None) -> list[str]:
    """The single source of receipt truth. Freshness + certification always; provenance,
    full panel and per-provider data only when `final`.

    TWO CALLERS, ONE RULEBOOK: `receipt_gate` (make precommit) calls it with final=False,
    and skill-tuneup's `verify-final-receipt` with final=True. They used to reimplement
    overlapping rules in two files, which is how `verify-final-receipt` could say "proven"
    on a receipt `make precommit` would reject.

    GRANDFATHERING: a receipt with NO `schema_version` key predates the per-provider
    schema and is judged on freshness alone, so existing receipts do not have to be
    re-earned until their skills next change. An explicit `"schema_version": null` is NOT
    the same thing — that is a malformed receipt and is rejected.
    """
    root = _normalized_path(root)
    skill = validate_skill_name(skill)
    rp = root / "evals" / skill / "receipt.json"
    if not os.path.lexists(rp):
        return [f"receipt: {skill} has no receipt — run `make eval SKILL={skill}`"]
    try:
        _mode, raw = _regular_file_state(rp, root)
        rec = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return [f"receipt: {skill} is unreadable: {e}"]
    if not isinstance(rec, dict):
        return [(f"receipt: {skill} root must be a JSON object, got "
                 f"{type(rec).__name__}")]

    problems = []
    providers = rec.get("providers")
    providers_valid = (
        isinstance(providers, list)
        and all(isinstance(provider, str) and bool(provider.strip())
                for provider in providers)
        and len(providers) == len(set(providers))
    )
    if "providers" in rec and not providers_valid:
        problems.append(
            f"receipt: {skill} providers must be a list of unique non-empty strings")
    self_test_gated = is_self_test_gated(skill, rec, root)
    evidence_problem = _deterministic_gate_evidence_problem(root, skill, rec)
    if rec.get("self_test") is True and evidence_problem:
        problems.append(f"receipt: {skill} {evidence_problem}")
    if rec.get("self_test") is True and not self_test_gated:
        expected = SELF_TEST_CERTIFIERS.get(skill)
        if expected is None:
            problems.append(
                f"receipt: {skill} is not eligible for a self-test panel exemption")
        else:
            problems.append(
                f"receipt: {skill} does not prove its expected deterministic certifier "
                f"{expected!r}")
    if not _receipt_is_certified(rec):
        problems.append(f"receipt: {skill} records a certification that did not pass "
                        f"(self_test={rec.get('self_test')!r}) — run `make eval SKILL={skill}`")
    # FAIL CLOSED, NEVER RAISE. A gate that crashes on an unreadable closure is worse
    # than one that reports it: the caller (make precommit, verify-final-receipt) gets a
    # traceback instead of a verdict, and a traceback is not a refusal anyone can act on.
    try:
        if rec.get("source_hash") != source_hash(root, skill):
            problems.append(f"receipt: {skill} changed since last eval — "
                            f"run `make eval SKILL={skill}`")
        elif rec.get("eval_set_hash") != eval_set_hash(root, skill):
            problems.append(f"receipt: {skill} eval set changed — "
                            f"run `make eval SKILL={skill}`")
        if (requires_deterministic_gate(skill)
                and rec.get("gate_tree_hash") != gate_tree_snapshot(root).gate_tree_hash):
            problems.append(f"receipt: {skill} deterministic gate tree changed — "
                            f"run `make eval SKILL={skill}`")
    except Exception as e:  # noqa: BLE001
        problems.append(f"receipt: {skill} — cannot recompute hashes: {e}")

    # GRANDFATHERING IS NARROW ON PURPOSE: a receipt with no `schema_version` predates
    # the per-provider schema, so it is exempt from the `per_provider` requirement ONLY.
    # It is NOT exempt from the final gate's provenance and full-panel checks — those read
    # `providers` and `provenance`, which every receipt has carried since v1. Exempting
    # them too would let a v1 receipt print "proven" at the convergence gate having been
    # neither earned nor full-panel, which is the exact assurance that gate exists to give.
    v1 = "schema_version" not in rec
    if not v1:
        ver = rec["schema_version"]
        if type(ver) is not int:
            return problems + [f"receipt: {skill} has a malformed schema_version {ver!r}"]
        if ver > CURRENT_RECEIPT_SCHEMA:
            return problems + [(f"receipt: {skill} has an unknown schema_version {ver} — "
                                f"this checkout understands up to {CURRENT_RECEIPT_SCHEMA}")]
        if ver != CURRENT_RECEIPT_SCHEMA:
            return problems + [(f"receipt: {skill} has unsupported schema_version {ver} — "
                                f"explicit receipts must use {CURRENT_RECEIPT_SCHEMA}; only "
                                "an absent schema_version receives v1 grandfathering")]
    if not final:
        return problems

    try:
        policy = eval_policy(root)
    except Exception as exc:  # noqa: BLE001
        return problems + [f"receipt: {skill} cannot load canonical eval policy: {exc}"]
    if panel is not None and list(panel) != list(policy.required_providers):
        problems.append(
            f"receipt: {skill} validator panel {list(panel)!r} is not the canonical "
            f"ordered panel {list(policy.required_providers)!r}")

    # Self-test-gated skills retain their deterministic authority.  They also carry the
    # canonical advisory panel below; one kind of evidence no longer erases the other.
    self_test_gated = is_self_test_gated(skill, rec, root)
    if skill in SELF_TEST_CERTIFIERS and not self_test_gated:
        problems.append(
            f"receipt: {skill} requires its deterministic certifier "
            f"{SELF_TEST_CERTIFIERS[skill]!r}; an ordinary panel receipt cannot replace it")
    # Whitelist the earned value rather than blacklisting a seeded one: the producer
    # writes "seeded: blessed current committed state", so an equality test against
    # "seed" was dead code — and that made `--seed-receipt` a one-flag way to make this
    # print "proven". Whitelisting fails closed if the producer string changes again.
    if rec.get("provenance") != "eval":
        problems.append(f"receipt: {skill} provenance is {rec.get('provenance')!r}, not "
                        f"'eval' — it was seeded, not earned; no eval actually ran")
    # A historical schema-less receipt can remain a local freshness record, but it can
    # never prove today's canonical shipping policy.
    if v1:
        problems.append(
            f"receipt: {skill} is legacy schema v1 and cannot prove the canonical panel")
    else:
        problems.extend(_shipping_evidence_problems(root, skill, rec))
    return problems


def receipt_gate(root: Path, *, advisory: bool) -> list[str]:
    """Freshness gate for `make verify` (advisory) and `make precommit` (fatal).

    The gate itself is a non-negative assertion delta, enforced at eval time in
    eval_harness.run() before the receipt is written. The blind A/B winner is RECORDED in
    the receipt but ADVISORY — it rewards concision on a strong executor and would
    false-fail a correct, positive-delta skill — so precommit does NOT gate on it.
    """
    root = _normalized_path(root)
    out = []
    for skill in expected_eval_skills(root):
        try:
            validate_skill_name(skill)
        except ValueError as exc:
            out.append(f"receipt: {exc}")
            continue
        manifest = root / "evals" / skill / "evals.json"
        if not manifest.is_file():
            out.append(
                f"receipt: {skill} has no eval manifest at evals/{skill}/evals.json — "
                "restore it or remove the canonical skill source")
            continue
        out.extend(validate_receipt(root, skill, final=False))
    return ["(advisory) " + m for m in out] if advisory else out


def final_receipt_gate(root: Path = ROOT) -> list[str]:
    """Fail-closed commit gate for every canonical skill receipt."""
    root = _normalized_path(root)
    try:
        policy = eval_policy(root)
    except Exception as exc:  # noqa: BLE001
        return [f"receipt: cannot load canonical eval policy: {exc}"]
    out = []
    for skill in expected_eval_skills(root):
        try:
            validate_skill_name(skill)
        except ValueError as exc:
            out.append(f"receipt: {exc}")
            continue
        manifest = root / "evals" / skill / "evals.json"
        if not manifest.is_file():
            out.append(
                f"receipt: {skill} has no eval manifest at evals/{skill}/evals.json")
            continue
        out.extend(validate_receipt(
            root, skill, final=True, panel=list(policy.required_providers)))
    return out


def _raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001 - a DIFFERENT error is not the contract either
        return False
    return False


def _self_test() -> int:
    ok = []
    policy_toml = (
        "[eval]\nrequired_providers=['codex','agy']\n"
        "judge='codex'\nmode='normal'\n")
    ok.append(("secret regex detects slack", any(rx.search("xoxp-1234567890abcde") for rx in SECRET_FAIL)))
    ok.append(("secret regex ignores prose", not any(rx.search("the quick brown fox jumps") for rx in SECRET_FAIL)))
    ok.append(("secret regex detects AKIA", any(rx.search("AKIAIOSFODNN7EXAMPLE") for rx in SECRET_FAIL)))
    # render.parse_frontmatter must FOLD block scalars. It did not, so `description: >-`
    # measured as len(">-") == 2 and the documented 1024-char limit was inert for 7 of 8
    # skills while appearing to pass. A check that cannot fail is a false assurance.
    # These assert REAL YAML semantics: the first version of this test asserted the
    # implementation's own (wrong) output for `|` and `>`, which made the regression test
    # authoritative for the bug it was meant to prevent.
    import importlib.util as _ilu
    _sp = _ilu.spec_from_file_location("_render", ROOT / "scripts" / "render.py")
    _rn = _ilu.module_from_spec(_sp); sys.modules["_render"] = _rn; _sp.loader.exec_module(_rn)

    def _fm(ind: str, body: str = "  one\n  two\n") -> str:
        return _rn.parse_frontmatter(f"---\nd: {ind}\n{body}---\n")["d"]

    for _ind, _want in (("|", "one\ntwo\n"), ("|-", "one\ntwo"),
                        (">", "one two\n"), (">-", "one two")):
        ok.append((f"frontmatter: `{_ind}` folds and chomps per YAML ({_want!r})",
                   _fm(_ind) == _want))
    ok.append(("frontmatter: a blank line in a folded scalar is ONE newline, not two",
               _fm(">-", "  one\n\n  two\n") == "one\ntwo"))
    ok.append(("frontmatter: two blank lines fold to two newlines",
               _fm(">-", "  one\n\n\n  two\n") == "one\n\ntwo"))
    ok.append(("frontmatter: a plain scalar is untouched",
               _rn.parse_frontmatter("---\nd: plain text\n---\n")["d"] == "plain text"))
    ok.append(("frontmatter: an unsupported block form RAISES rather than mis-parsing",
               _raises(lambda: _rn.parse_frontmatter("---\nd: >2\n  x\n---\n"), ValueError)))
    # The regression guard proper: a block scalar must measure at its REAL length, not 2.
    ok.append(("frontmatter: a block scalar is measured at full length, not 2",
               len(_rn.parse_frontmatter(
                   "---\ndescription: >-\n" + "  word word word\n" * 100 + "---\n"
               )["description"]) > 1024))
    # pricing_coverage value shapes. A one-time probe proves the edit; these stop a revert.
    # Driven through a temp root because the function reads both files from disk. Each
    # table is COMPLETE (all four rates present) so only the value test can reject it —
    # a missing-key diagnostic would otherwise mask a deleted value check and pass anyway.
    with tempfile.TemporaryDirectory() as _td:
        _root = Path(_td)
        (_root / "scripts").mkdir()
        (_root / "capabilities.toml").write_text('[models]\nclaude = ["m"]\n')
        _need = ("input", "output", "cache_read", "cache_write")

        def _price(literal: str) -> list[str]:
            (_root / "scripts" / "pricing.toml").write_text(
                f"[m]\ninput = {literal}\noutput = 0.0\n"
                "cache_read = 0.0\ncache_write = 0.0\n")
            return pricing_coverage(_root)

        for _label, _lit, _diag in (("negative", "-1.0", "is negative"),
                                    ("string", '"1.0"', "is not a number"),
                                    ("bool", "true", "is not a number"),
                                    ("NaN", "nan", "is not finite"),
                                    ("inf", "inf", "is not finite")):
            _p = _price(_lit)
            ok.append((f"pricing_coverage rejects a {_label} rate",
                       any(f"m.input {_diag}" in x for x in _p)))
        # Every one of the four fields, not just `input`: an implementation that validated
        # only the first would otherwise pass all five shape cases above.
        def _price_field(field: str, literal: str) -> list[str]:
            (_root / "scripts" / "pricing.toml").write_text(
                "[m]\n" + "".join(
                    f"{k} = {literal if k == field else '0.0'}\n" for k in _need))
            return pricing_coverage(_root)
        for _f in _need:
            ok.append((f"pricing_coverage validates the {_f} field too",
                       any(f"m.{_f} is negative" in x for x in _price_field(_f, "-1.0"))))
        ok.append(("pricing_coverage accepts a valid zero rate", _price("0.0") == []))
        ok.append(("pricing_coverage accepts a valid positive rate", _price("2.5") == []))
    # hash stability + closure membership (mutating any listed file WILL change source_hash)
    ok.append(("source_hash stable", source_hash(ROOT, "llm-council") == source_hash(ROOT, "llm-council")))
    ok.append(("llm-council closure includes the moved engine",
               any(r == "shared/lib/council/engine.py"
                   for r, _ in source_manifest(ROOT, "llm-council"))))
    ok.append(("every skill closure includes reconcile.py (LIB_SCRIPTS)",
               any("reconcile.py" in r for r, _ in source_manifest(ROOT, "expense-review"))))
    ok.append(("khenrix-setup closure includes capabilities.toml + render.py",
               {"capabilities.toml", "scripts/render.py"} <=
               {r for r, _ in source_manifest(ROOT, "khenrix-setup")}))
    # eval_set_hash stays backward-compatible for a skill with no fixtures/ dir
    ok.append(("eval_set_hash == sha256(evals.json) when no fixtures",
               eval_set_hash(ROOT, "llm-council") ==
               _sha((ROOT / "evals" / "llm-council" / "evals.json").read_bytes())))
    ok.append(("eval roster covers every canonical shared skill/template source",
               expected_eval_skills(ROOT) == sorted(set(_evald_skills(ROOT)) | {
                   path.name for path in (ROOT / "shared" / "skills").iterdir()
                   if path.is_dir() and (path / "SKILL.md").is_file()
               } | {
                   path.name for path in (ROOT / "shared" / "skill-templates").iterdir()
                   if path.is_dir() and (path / "SKILL.md.tmpl").is_file()
               })))
    # the wiki skills route their shared engine into the closure via SKILL_EXTRA_DIRS
    ok.append(("wiki skills map shared/lib/wikisync into their closure",
               SKILL_EXTRA_DIRS.get("khenrix-wiki-add") == ["shared/lib/wikisync"] and
               SKILL_EXTRA_DIRS.get("khenrix-wiki-sync") == ["shared/lib/wikisync"]))
    # Complete receipt-closure matrix. Spot checks repeatedly missed the exact fan-out cost
    # of a shared edit; compute every evaluated skill from source_manifest, the gate's own
    # authority, and pin every global/file/directory edge in one assertion family.
    evaluated = _evald_skills(ROOT)
    manifests = {
        skill: {rel for rel, _ in source_manifest(ROOT, skill)}
        for skill in evaluated
    }
    for skill in evaluated:
        rows = [rel for rel, _ in source_manifest(ROOT, skill)]
        ok.append((f"receipt closure: {skill} hashes every path exactly once",
                   len(rows) == len(set(rows))))
    for rel in LIB_SCRIPTS + GLOBAL_INPUTS:
        missing = [skill for skill in evaluated if rel not in manifests[skill]]
        ok.append((f"receipt closure: {rel} reaches every evaluated skill", not missing))
    for skill, relpaths in SKILL_EXTRA.items():
        if skill not in manifests:
            continue
        for rel in relpaths:
            ok.append((f"receipt closure: {skill} includes extra file {rel}",
                       rel in manifests[skill]))
    for skill, directories in SKILL_EXTRA_DIRS.items():
        if skill not in manifests:
            continue
        for directory in directories:
            expected = {
                str(path.relative_to(ROOT))
                for path in (ROOT / directory).rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            }
            ok.append((f"receipt closure: {skill} includes every file under {directory}",
                       expected <= manifests[skill]))
    for skill, patterns in DETERMINISTIC_GATE_INPUT_GLOBS.items():
        if skill not in manifests:
            continue
        for pattern in patterns:
            expected = {
                str(path.relative_to(ROOT)) for path in ROOT.glob(pattern)
                if path.is_file() and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            }
            ok.append((f"receipt closure: {skill} includes every certifier input {pattern}",
                       expected <= manifests[skill]))
    for skill, patterns in DETERMINISTIC_GATE_AMBIENT_INPUT_GLOBS.items():
        if skill not in manifests:
            continue
        for pattern in patterns:
            expected = {
                str(path.relative_to(ROOT)) for path in ROOT.glob(pattern)
                if path.is_file() and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            }
            ok.append((f"receipt closure: {skill} includes ambient certifier input {pattern}",
                       expected <= manifests[skill]))
    for skill in ("khenrix-setup", "khenrix-upgrade"):
        ok.append((f"receipt closure: {skill} includes every instruction overlay",
                   set((_load_caps(ROOT).get("instructions", {}).get("overlays") or {}).values())
                   <= manifests[skill]))
    for skill in _load_caps(ROOT).get("skill_facts", {}):
        if skill in manifests:
            ok.append((f"receipt closure: {skill} includes its semantic facts slice",
                       f"skill_facts:{skill}" in manifests[skill]))
    with tempfile.TemporaryDirectory() as _td:
        _r = Path(_td)
        (_r / "capabilities.toml").write_text("[models]\n" + policy_toml)
        (_r / "shared" / "skills" / "llm-forge").mkdir(parents=True)
        (_r / "shared" / "skills" / "llm-forge" / "SKILL.md").write_text("# forge\n")
        (_r / "tests").mkdir()
        (_r / "tests" / "test_forge_one.py").write_text("ONE = 1\n")
        _before = source_hash(_r, "llm-forge")
        (_r / "tests" / "test_forge_one.py").write_text("ONE = 2\n")
        _edited = source_hash(_r, "llm-forge")
        (_r / "tests" / "test_forge_two.py").write_text("TWO = 2\n")
        _added = source_hash(_r, "llm-forge")
        (_r / "tests" / "test_unrelated.py").write_text("OTHER = 1\n")
        _transitive_test = source_hash(_r, "llm-forge")
        (_r / "scripts").mkdir()
        (_r / "scripts" / "forge_smoke.py").write_text("SMOKE = 1\n")
        _transitive_script = source_hash(_r, "llm-forge")
        (_r / "pytest.ini").write_text("[pytest]\naddopts = -q\n")
        _configured = source_hash(_r, "llm-forge")
        (_r / "tests" / "conftest.py").write_text("VALUE = 1\n")
        _conftest_added = source_hash(_r, "llm-forge")
        ok.append(("editing a deterministic certifier input stales its receipt",
                   _before != _edited))
        ok.append(("adding a deterministic certifier input stales its receipt",
                   _edited != _added))
        ok.append(("adding a test scanned by a Forge meta-test stales its receipt",
                   _added != _transitive_test))
        ok.append(("adding an imported/scanned script stales its receipt",
                   _transitive_test != _transitive_script))
        ok.append(("disabled repository pytest configuration does not affect the receipt",
                   _transitive_script == _configured))
        ok.append(("adding a deterministic certifier conftest stales its receipt",
                   _configured != _conftest_added))
        _mode_before = source_hash(_r, "llm-forge")
        (_r / "tests" / "test_forge_one.py").chmod(0o755)
        _mode_after = source_hash(_r, "llm-forge")
        ok.append(("changing a certifier input's executable mode stales its receipt",
                   _mode_before != _mode_after))
        _same_bytes = _r / "same-bytes.py"
        _same_bytes.write_bytes((_r / "tests" / "test_forge_one.py").read_bytes())
        (_r / "tests" / "test_forge_one.py").unlink()
        (_r / "tests" / "test_forge_one.py").symlink_to(_same_bytes)
        try:
            source_hash(_r, "llm-forge")
            _linked_refused = False
        except ValueError as e:
            _linked_refused = "not symlinks" in str(e)
        ok.append(("a byte-identical symlink cannot masquerade as a regular input",
                   _linked_refused))
        (_r / "tests" / "test_forge_one.py").unlink()
        (_r / "tests" / "test_forge_one.py").write_text("ONE = 2\n")
        (_r / "tests").rename(_r / "alternate-tests")
        (_r / "tests").symlink_to(_r / "alternate-tests", target_is_directory=True)
        try:
            source_hash(_r, "llm-forge")
            _linked_source_ancestor_refused = False
        except ValueError as e:
            _linked_source_ancestor_refused = "regular directories" in str(e)
        ok.append(("a symlinked source ancestor cannot redirect certifier inputs",
                   _linked_source_ancestor_refused))
    with tempfile.TemporaryDirectory() as _td:
        _r = Path(_td)
        _ev = _r / "evals" / "ids"
        _ev.mkdir(parents=True)

        def _ids(cases):
            base = {"name": "case", "prompt": "p", "assertions": ["a"], "files": []}
            _ev.joinpath("evals.json").write_text(
                json.dumps({"evals": [{**base, **case} for case in cases]}))
            try:
                return eval_ids(_r, "ids"), ""
            except ValueError as e:
                return [], str(e)

        ok.append(("integer and non-empty string eval ids are accepted",
                   _ids([{"id": 0}, {"id": "named"}])[0] == [0, "named"]))
        for _label, _cases in (
            ("missing", [{}]), ("null", [{"id": None}]),
            ("bool", [{"id": True}]), ("blank", [{"id": "  "}]),
            ("duplicate", [{"id": 1}, {"id": 1}]),
            ("render-collision", [{"id": 1}, {"id": "1"}]),
        ):
            ok.append((f"{_label} eval ids are rejected", bool(_ids(_cases)[1])))
        _ids([{"id": 0}])
        _fixture = _ev / "fixtures" / "generator.py"
        _fixture.parent.mkdir()
        _fixture.write_text("VALUE = 1\n")
        _fixture_mode_before = eval_set_hash(_r, "ids")
        _fixture.chmod(0o755)
        _fixture_mode_after = eval_set_hash(_r, "ids")
        ok.append(("changing an eval fixture's executable mode changes its hash",
                   _fixture_mode_before != _fixture_mode_after))
        _outside_fixture = _r / "same-fixture.py"
        _outside_fixture.write_bytes(_fixture.read_bytes())
        _fixture.unlink()
        _fixture.symlink_to(_outside_fixture)
        try:
            eval_set_hash(_r, "ids")
            _linked_fixture_refused = False
        except ValueError as e:
            _linked_fixture_refused = "not symlinks" in str(e)
        ok.append(("a byte-identical symlink cannot masquerade as an eval fixture",
                   _linked_fixture_refused))
        _fixture.unlink()
        _fixture.write_text("VALUE = 1\n")
        (_ev / "fixtures").rename(_ev / "alternate-fixtures")
        (_ev / "fixtures").symlink_to(
            _ev / "alternate-fixtures", target_is_directory=True)
        try:
            eval_set_hash(_r, "ids")
            _linked_fixture_ancestor_refused = False
        except ValueError as e:
            _linked_fixture_ancestor_refused = "regular directories" in str(e)
        ok.append(("a symlinked fixture ancestor cannot redirect eval inputs",
                   _linked_fixture_ancestor_refused))
        (_ev / "alternate-fixtures" / "generator.py").unlink()
        try:
            eval_set_hash(_r, "ids")
            _empty_linked_fixture_ancestor_refused = False
        except ValueError as e:
            _empty_linked_fixture_ancestor_refused = "regular directories" in str(e)
        ok.append(("an empty symlinked fixture directory is still refused",
                   _empty_linked_fixture_ancestor_refused))
    with tempfile.TemporaryDirectory() as _td:
        _r = Path(_td)
        (_r / "capabilities.toml").write_text("[models]\n" + policy_toml)
        (_r / "shared" / "skills" / "alpha").mkdir(parents=True)
        (_r / "shared" / "skills" / "alpha" / "SKILL.md").write_text("# alpha\n")
        (_r / "shared" / "skill-templates" / "beta").mkdir(parents=True)
        (_r / "shared" / "skill-templates" / "beta" / "SKILL.md.tmpl").write_text("# beta\n")
        (_r / "evals" / "alpha").mkdir(parents=True)
        (_r / "evals" / "alpha" / "evals.json").write_text(
            '{"evals":[{"id":0,"name":"alpha","prompt":"p",'
            '"assertions":["a"]}]}')
        (_r / "evals" / "orphan").mkdir(parents=True)
        _roster_before = expected_eval_skills(_r)
        _closure_before = source_manifest(_r, "alpha")
        (_r / "evals" / "alpha" / "evals.json").unlink()
        _closure_after = source_manifest(_r, "alpha")
        _gate_after_delete = receipt_gate(_r, advisory=False)
        ok.append(("eval roster unions canonical sources and extant eval directories",
                   _roster_before == ["alpha", "beta", "orphan"]))
        ok.append(("deleting a canonical skill manifest remains a fatal receipt-gate problem",
                   any("alpha has no eval manifest" in problem for problem in _gate_after_delete)))
        ok.append(("deleting an eval manifest cannot remove its canonical source closure",
                   _closure_before == _closure_after and bool(_closure_after)))
        ok.append(("an extant orphan eval directory is diagnosed too",
                   any("orphan has no eval manifest" in problem for problem in _gate_after_delete)))
    # scan_path: file-scoped shim for gitignored artifacts. Build a slack-shaped token from
    # fragments at RUNTIME so no contiguous token literal lives in this file — otherwise the
    # synthetic test value itself trips push-protection / secret scanners.
    _planted = "xox" + "b-" + ("2" * 12) + "-" + ("3" * 12) + "-" + ("abcd" * 6)
    _d = Path(tempfile.mkdtemp())
    _p = _d / "leak.txt"
    _p.write_text(f"token = {_planted}\n")
    ok.append(("scan_path flags a planted token", len(scan_path(_p)) >= 1))
    _p.unlink()
    _d.rmdir()
    ok.append(("scan_path on missing file is empty", scan_path(Path("/nonexistent/xyz.json")) == []))
    # scan_secrets: an unreadable TRACKED file must not be scanned as clean. Driven through
    # a throwaway git repo because the function walks `git ls-files` (the INDEX), which is
    # precisely why the tracked-but-deleted case exists and has to stay silent.
    with tempfile.TemporaryDirectory() as _td:
        _r = Path(_td)
        subprocess.run(["git", "init", "-q", str(_r)], check=True, capture_output=True)
        (_r / "plain.txt").write_text("nothing to see\n")
        (_r / "locked.txt").write_text("nothing to see either\n")
        (_r / "gone.txt").write_text("nothing to see either\n")
        subprocess.run(["git", "add", "-A"], cwd=_r, check=True, capture_output=True)
        ok.append(("scan_secrets is clean over a readable tree", scan_secrets(_r) == []))
        # ENOENT: tracked in the index, absent from the working tree. Ordinary, and a CLEAN
        # one must stay silent or every rebase is noise.
        (_r / "gone.txt").unlink()
        ok.append(("a clean tracked-but-deleted file is silent", scan_secrets(_r) == []))
        # ...AND THE SAME CASE CARRYING A SECRET IS NOT. The fixture above holds "nothing to
        # see either", so it passed whether the file was skipped or scanned — a fixture too
        # clean to distinguish the two. The index is what a commit ships, so a token staged
        # and then removed from the worktree without staging the removal is still going out.
        _tok = "AKIA" + "Q7ZB3KXJ2M9WLPRT"
        (_r / "staged.txt").write_text(f"key = {_tok}\n")
        subprocess.run(["git", "add", "staged.txt"], cwd=_r, check=True, capture_output=True)
        (_r / "staged.txt").unlink()
        ok.append(("a tracked-but-deleted file carrying a secret is reported",
                   any("staged.txt" in p for p in scan_secrets(_r))))
        subprocess.run(["git", "rm", "-q", "-f", "--cached", "staged.txt"],
                       cwd=_r, check=True, capture_output=True)
        # EACCES: the bytes ARE there and were not read. The PRECONDITION is checked by
        # trying the read, not by `geteuid() != 0` — root defeats `chmod 000`, and so do
        # some mounts and ACLs, so a euid test would still assert on a machine where the
        # file is readable and fail there. An environment-sensitive assertion inside a
        # commit gate is the same fail-open one layer up: it teaches its reader to re-run.
        (_r / "locked.txt").chmod(0o000)
        try:
            (_r / "locked.txt").read_text()
            _blocked = False
        except OSError:
            _blocked = True
        if not _blocked:
            ok.append(("SKIP: chmod 000 does not block this user (root or ACL), so there "
                       "is no EACCES here to assert on", True))
        else:
            _p = scan_secrets(_r)
            # `any` over the list, not `_p[0]`: this is a bare-script suite, so an
            # IndexError from an empty result exits 1 exactly like a FAIL does and a
            # mutation run would score it CAUGHT off a crash. Measured — see mutate.py.
            ok.append(("an unreadable tracked file is reported, not skipped",
                       len(_p) == 1 and any(x.startswith("locked.txt: NOT SCANNED")
                                            for x in _p)))
            ok.append(("...and it names the errno cause, not just the path",
                       any("PermissionError" in x for x in _p)))
        (_r / "locked.txt").chmod(0o644)
    # ---- receipt schema v3 + grandfathering + the single validator -------------
    with tempfile.TemporaryDirectory() as _td:
        _r = Path(_td)
        _ev = _r / "evals" / "alpha"
        _ev.mkdir(parents=True)
        _ev.joinpath("evals.json").write_text(
            '{"evals":[{"id":0,"name":"alpha","prompt":"p",'
            '"assertions":["a"],"files":[]}]}')
        (_r / "shared" / "skills" / "alpha").mkdir(parents=True)
        (_r / "shared" / "skills" / "alpha" / "SKILL.md").write_text("# a\n")
        # source_hash reaches capabilities.toml for the overlay closure.
        (_r / "capabilities.toml").write_text("[instructions]\n" + policy_toml)
        subprocess.run(["git", "init", "-q", str(_r)], check=True,
                       capture_output=True)

        def _v(rec, **kw):
            _ev.joinpath("receipt.json").write_text(json.dumps(rec))
            return " ".join(validate_receipt(_r, "alpha", **kw))

        _fresh = {"source_hash": source_hash(_r, "alpha"),
                  "eval_set_hash": eval_set_hash(_r, "alpha"),
                  "provenance": "eval", "certified_by": "assertion_delta"}
        for malformed_root in ([], "x", None):
            ok.append((f"a {type(malformed_root).__name__} receipt root is refused cleanly",
                       "root must be a JSON object" in _v(malformed_root)))
        ok.append(("v1 receipt with no schema_version is grandfathered", _v(_fresh) == ""))
        ok.append(("explicit null schema_version is malformed, not grandfathered",
                   "malformed" in _v({**_fresh, "schema_version": None})))
        ok.append(("unknown future schema_version is rejected",
                   "unknown schema_version" in _v({**_fresh, "schema_version": 99})))
        for malformed_version in (True, False, 0, 1, -1):
            ok.append((f"explicit schema_version={malformed_version!r} is not v1",
                       "schema_version" in _v({**_fresh,
                                               "schema_version": malformed_version})))
        ok.append(("schema v2 is rejected rather than treated as current",
                   "unsupported schema_version 2" in _v(
                       {**_fresh, "schema_version": 2})))
        ok.append(("v3 without shipping evidence passes the NON-final freshness gate",
                   _v({**_fresh, "schema_version": CURRENT_RECEIPT_SCHEMA}) == ""))
        ok.append(("v3 without canonical panel evidence FAILS the final gate",
                   "eval policy digest" in _v(
                       {**_fresh, "schema_version": CURRENT_RECEIPT_SCHEMA},
                       final=True, panel=["codex", "agy"])))
        _canonical = {
            **_fresh,
            "schema_version": CURRENT_RECEIPT_SCHEMA,
            "providers": ["codex", "agy"],
            "judge": "codex",
            "mode": "normal",
            "models": {"judge": "gpt-test"},
            "eval_policy_hash": eval_policy_hash(_r),
            "per_provider": {
                provider: {"delta": 0.1, "n_evals": 1, "status": "ok"}
                for provider in ("codex", "agy")
            },
        }
        ok.append(("v3 canonical Codex+agy evidence passes the final gate",
                   _v(_canonical, final=True, panel=["codex", "agy"]) == ""))
        _seeded = {**_fresh, "schema_version": CURRENT_RECEIPT_SCHEMA,
                   "provenance": "seeded: blessed current committed state"}
        _seeded.pop("certified_by")
        ok.append(("seeded v3 is legal for the non-final freshness gate",
                   _v(_seeded) == ""))
        ok.append(("seeded v3 is rejected by the final shipping gate",
                   "seeded" in _v(_seeded, final=True, panel=["codex", "agy"])))
        ok.append(("a single-provider receipt fails the canonical Codex+agy gate",
                   "provider order" in _v(
                       {**_canonical, "providers": ["codex"],
                        "per_provider": {
                            "codex": {"delta": 0.1, "n_evals": 1, "status": "ok"}
                        }},
                       final=True, panel=["codex", "agy"])))
        for label, malformed_providers in (
            ("null", None),
            ("mapping", {"claude": True}),
            ("unhashable member", ["claude", ["agy"]]),
            ("duplicate", ["claude", "claude"]),
            ("empty member", ["claude", ""]),
        ):
            ok.append((f"{label} providers are refused without set conversion",
                       "providers must be a list of unique non-empty strings" in _v(
                           {**_fresh, "schema_version": CURRENT_RECEIPT_SCHEMA,
                            "providers": malformed_providers, "per_provider": {}},
                           final=True, panel=["codex", "agy"])))
        _deterministic = {
            **_fresh,
            "schema_version": CURRENT_RECEIPT_SCHEMA,
            "self_test": True,
            "providers": ["claude"],
            "certified_by": "wikisync-unittests",
            "deterministic_gate": "wikisync-unittests",
            "gate_command": deterministic_gate_command(_r, "khenrix-wiki-add"),
            "gate_tree_hash": gate_tree_snapshot(_r).gate_tree_hash,
            "gate_counts": {"tests_run": 1, "skipped": 0, "failed": 0},
        }
        ok.append(("an eligible, exactly certified self-test receipt is recognized",
                   is_self_test_gated("khenrix-wiki-add", _deterministic, _r)))
        ok.append(("only the skill's exact deterministic certifier earns the exemption",
                   is_self_test_gated("khenrix-wiki-add", _deterministic, _r)
                   and not is_self_test_gated("alpha", _deterministic, _r)
                   and not is_self_test_gated(
                       "khenrix-wiki-add", {**_deterministic,
                                            "certified_by": "some --self-test"}, _r)))
        _without_command = dict(_deterministic)
        _without_command.pop("gate_command")
        _without_counts = dict(_deterministic)
        _without_counts.pop("gate_counts")
        for _label, _evidence in (
            ("missing command", _without_command),
            ("missing counts", _without_counts),
            ("zero tests", {**_deterministic,
                            "gate_counts": {"tests_run": 0, "skipped": 0, "failed": 0}}),
            ("skipped tests", {**_deterministic,
                               "gate_counts": {"tests_run": 1, "skipped": 1, "failed": 0}}),
            ("failed tests", {**_deterministic,
                              "gate_counts": {"tests_run": 1, "skipped": 0, "failed": 1}}),
            ("malformed counts", {**_deterministic, "gate_command": [""],
                                  "gate_counts": {"tests_run": True, "skipped": 0,
                                                  "failed": 0}}),
        ):
            ok.append((f"{_label} deterministic gate evidence is refused",
                       not is_self_test_gated("khenrix-wiki-add", _evidence, _r)))
        ok.append(("valid deterministic gate evidence earns the exemption",
                   is_self_test_gated("khenrix-wiki-add", _deterministic, _r)))
        _council = {**_deterministic, "certified_by": "fanout --self-test"}
        _council.pop("deterministic_gate")
        _council.pop("gate_counts")
        _council["gate_command"] = deterministic_gate_command(_r, "llm-council")
        ok.append(("llm-council requires its exact command but not test counts",
                   is_self_test_gated("llm-council", _council, _r)))
        ok.append(("an ordinary skill cannot forge a self-test panel exemption",
                   "not eligible" in _v(
                       {**_fresh, "schema_version": CURRENT_RECEIPT_SCHEMA,
                        "self_test": True,
                        "providers": ["claude"], "certified_by": "some --self-test"},
                       final=True, panel=["codex", "agy"])))
        ok.append(("an n/a blind_winner alone cannot bypass the panel requirement",
                   "provider order" in _v(
                       {**_fresh, "schema_version": CURRENT_RECEIPT_SCHEMA,
                        "providers": ["codex"],
                        "blind_winner": "n/a-deterministic",
                        "per_provider": {
                            "codex": {"delta": 0.1, "n_evals": 1, "status": "ok"}
                        }},
                       final=True, panel=["codex", "agy"])))
        ok.append(("stale source_hash fails even a grandfathered v1",
                   "changed since last eval" in _v({**_fresh, "source_hash": "deadbeef"})))
        ok.append(("a missing receipt is reported, not skipped",
                   "no receipt" in " ".join(validate_receipt(_r, "zeta"))))
        # Grandfathering is exemption from per_provider ONLY. A v1 receipt must still
        # face the final gate's provenance and panel checks — both read fields v1 has
        # always carried, and exempting them would let a v1 receipt print "proven" at the
        # convergence gate having been neither earned nor full-panel.
        _v1seed = {**_fresh, "provenance": "seeded: blessed current committed state"}
        _v1seed.pop("certified_by")
        ok.append(("a v1 receipt is NOT exempt from the final provenance check",
                   "seeded, not earned" in _v(_v1seed, final=True, panel=["claude"])))
        ok.append(("a v1 receipt cannot prove today's canonical shipping panel",
                   "legacy schema v1" in _v(
                       {**_fresh, "providers": ["codex"]},
                       final=True, panel=["codex", "agy"])))
        ok.append(("a v1 receipt IS exempt from the per_provider requirement",
                   "per_provider" not in _v({**_fresh, "providers": ["claude"]},
                                            final=True, panel=["claude"])))

    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else (1 if run_all() else 0))
