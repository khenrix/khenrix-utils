#!/usr/bin/env python3
"""Deterministic source-of-truth checks for `make verify` (stdlib only).

Each check returns a list of problem strings (empty = clean). run_all() concatenates
them; render.check() prints + fails on any. Self-test (`--self-test`) covers the pure
logic with no repo/network dependency.
"""
from __future__ import annotations
import hashlib, json, re, subprocess, sys, tempfile, tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FANOUT_DIR = ROOT / "shared" / "skills" / "llm-council" / "scripts"

# The CLIs a plugin is rendered for. Defined HERE and imported by render.py rather than
# the other way round: render.py already imports this module (render.check), so the
# reverse direction would be a cycle. Checks that must not silently skip an unlisted
# plugin enumerate `marketplaces/` from disk instead of iterating this — see
# forge_packaging.
CLIS = ("claude", "codex", "agy")

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


def model_crosscheck(root: Path) -> list[str]:
    """Every model in fanout.py MODES must be registered in capabilities [models]."""
    sys.path.insert(0, str(root / "shared" / "skills" / "llm-council" / "scripts"))
    try:
        import fanout
    except Exception as e:  # noqa: BLE001
        return [f"model-crosscheck: cannot import fanout.py: {e}"]
    caps = _load_caps(root)
    registered = set()
    for v in caps.get("models", {}).values():
        if isinstance(v, list):
            registered.update(v)
    used = {cell["model"] for mode in fanout.MODES.values() for cell in mode.values()}
    missing = sorted(m for m in used if m not in registered)
    return [f"model-crosscheck: fanout MODES model '{m}' not in capabilities [models]"
            for m in missing]


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
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                         capture_output=True, check=True).stdout
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
        blob = subprocess.run(["git", "cat-file", "-p", f":{rel}"], cwd=root,
                              capture_output=True)
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
    """A plugin that bundles lib/forge/ must bundle lib/checks.py beside it.

    forge/screen.py imports SECRET_FAIL/SECRET_ALLOW_SHA from this module by path so the
    patterns have one definition. Its repo-layout candidate dies the moment a marketplace
    copies the plugin elsewhere, leaving <plugin>/lib/checks.py as the only reachable one;
    absent that, screen.py raises on a user's first forge run. This moves the failure to
    `make verify`, where someone is looking.

    Enumerated from DISK, not from CLIS. This is the one restatement of the CLI list that
    failed OPEN: a fourth plugin bundling lib/forge/ without lib/checks.py is exactly the
    state the gate exists to catch, and a hardcoded triple would say nothing about it
    while every other check that iterates CLIS at least fails loudly. The check must cover
    whatever plugins are actually on disk, so an unlisted one cannot ship past it.
    """
    problems = []
    for lib in sorted((root / "marketplaces").glob("*/plugins/khenrix-utils/lib")):
        if (lib / "forge").is_dir() and not (lib / "checks.py").is_file():
            cli = lib.parents[2].name
            problems.append(f"forge-packaging: {cli} plugin bundles lib/forge/ without "
                            f"lib/checks.py — screen.py would raise at runtime")
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
    return (model_crosscheck(root) + pricing_coverage(root)
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
                 "Makefile"]                 # names the suites a gate can name
# Extra behavior-affecting inputs per skill: reconcile/instructions consumers read
# capabilities.toml + house-style.md (+ overlays); llm-council bundles headless-invocation.md.
SKILL_EXTRA = {
    "khenrix-setup":   ["capabilities.toml", "house-style.md"],
    "khenrix-upgrade": ["capabilities.toml", "house-style.md"],
    "llm-council":     ["headless-invocation.md"],
    # forge/screen.py reads SECRET_FAIL + SECRET_ALLOW_SHA out of this module, so editing
    # the patterns changes what forge screens before launching a fleet — behaviour-
    # affecting by this closure's own definition, and reachable from no other entry here
    # (checks.py is in neither LIB_SCRIPTS nor GLOBAL_INPUTS).
    "llm-forge":       ["scripts/lib/checks.py"],
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


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _skill_source_files(root: Path, skill: str) -> list[Path]:
    """Full behavior-affecting input closure for a skill: its own dir, the LIB_SCRIPTS
    + render.py bundled/applied to every skill, and skill-specific extras (reconcile
    inputs / overlays / headless doc). Excludes pycache/pyc."""
    files = []
    for base in (root / "shared" / "skills" / skill,
                 root / "shared" / "skill-templates" / skill):
        if base.is_dir():
            files += [p for p in base.rglob("*") if p.is_file()
                      and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    # `dict.fromkeys`, not a set: order is what `source_manifest` sorts and a duplicate is
    # what happens when a file earns its place twice — `checks.py` is a GLOBAL_INPUT because
    # it decides what the gate accepts, and llm-forge's SKILL_EXTRA because forge/screen.py
    # reads its constants. Both reasons are right; hashing it twice is not.
    for rel in dict.fromkeys(LIB_SCRIPTS + GLOBAL_INPUTS + SKILL_EXTRA.get(skill, [])):
        p = root / rel
        if p.is_file():
            files.append(p)
    for d in SKILL_EXTRA_DIRS.get(skill, []):  # whole shared-engine dirs into the closure
        base = root / d
        if base.is_dir():
            files += [p for p in base.rglob("*") if p.is_file()
                      and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    if skill in ("khenrix-setup", "khenrix-upgrade"):  # overlays change reconcile output
        caps = _load_caps(root)
        for ov in (caps.get("instructions", {}).get("overlays") or {}).values():
            p = root / ov
            if p.is_file():
                files.append(p)
    return files


def source_manifest(root: Path, skill: str) -> list:
    """Sorted (relpath, sha256) pairs + canonical skill_facts slice for templated skills."""
    entries = []
    for p in _skill_source_files(root, skill):
        entries.append((str(p.relative_to(root)), _sha(p.read_bytes())))
    caps = _load_caps(root)
    facts = caps.get("skill_facts", {}).get(skill)
    if facts is not None:
        entries.append((f"skill_facts:{skill}",
                        _sha(json.dumps(facts, sort_keys=True).encode())))
    return sorted(entries)


def source_hash(root: Path, skill: str) -> str:
    return _sha(json.dumps(source_manifest(root, skill), sort_keys=True).encode())


def eval_set_hash(root: Path, skill: str) -> str:
    """Hash evals.json PLUS the evals/<skill>/fixtures/ tree, so changing a fixture
    re-arms the receipt. Backward-compatible: a skill with no fixtures/ dir hashes to
    exactly sha256(evals.json) as before."""
    ev_dir = root / "evals" / skill
    h = hashlib.sha256()
    h.update((ev_dir / "evals.json").read_bytes())
    fx = ev_dir / "fixtures"
    if fx.is_dir():
        for p in sorted(fx.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                h.update(str(p.relative_to(ev_dir)).encode())
                h.update(_sha(p.read_bytes()).encode())
    return h.hexdigest()


def _evald_skills(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "evals").glob("*/")
                  if (p / "evals.json").exists())


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
    # NEITHER is the seeded shape: `--seed-receipt` blesses a committed state without running
    # a gate, and says so in `provenance`. A receipt with none of the three claims nothing.
    return str(rec.get("provenance", "")).startswith("seeded")


CURRENT_RECEIPT_SCHEMA = 2


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
    rp = root / "evals" / skill / "receipt.json"
    if not rp.is_file():
        return [f"receipt: {skill} has no receipt — run `make eval SKILL={skill}` "
                f"(or `--seed-receipt`)"]
    try:
        rec = json.loads(rp.read_text())
    except Exception as e:  # noqa: BLE001
        return [f"receipt: {skill} is unreadable: {e}"]

    problems = []
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
        if not isinstance(ver, int):
            return problems + [f"receipt: {skill} has a malformed schema_version {ver!r}"]
        if ver > CURRENT_RECEIPT_SCHEMA:
            return problems + [f"receipt: {skill} has an unknown schema_version {ver} — "
                               f"this checkout understands up to {CURRENT_RECEIPT_SCHEMA}"]
    if not final:
        return problems

    # Self-test-gated skills earn their receipt from a deterministic suite, so a full
    # panel and per-provider deltas prove nothing extra about them.
    self_test_gated = (rec.get("self_test") is True
                       or str(rec.get("blind_winner", "")).startswith("n/a-"))
    # Whitelist the earned value rather than blacklisting a seeded one: the producer
    # writes "seeded: blessed current committed state", so an equality test against
    # "seed" was dead code — and that made `--seed-receipt` a one-flag way to make this
    # print "proven". Whitelisting fails closed if the producer string changes again.
    if not self_test_gated and rec.get("provenance") != "eval":
        problems.append(f"receipt: {skill} provenance is {rec.get('provenance')!r}, not "
                        f"'eval' — it was seeded, not earned; no eval actually ran")
    if not self_test_gated:
        got, want = set(rec.get("providers") or []), set(panel or [])
        if not want <= got:
            problems.append(f"receipt: {skill} is not FULL-PANEL — earned on "
                            f"{sorted(got)}, needs {sorted(want)}")
        if not v1 and not rec.get("per_provider"):
            problems.append(f"receipt: {skill} is schema v2 but carries no per_provider "
                            f"block — re-run `make eval SKILL={skill}`")
    return problems


def receipt_gate(root: Path, *, advisory: bool) -> list[str]:
    """Freshness gate for `make verify` (advisory) and `make precommit` (fatal).

    The gate itself is a non-negative assertion delta, enforced at eval time in
    eval_harness.run() before the receipt is written. The blind A/B winner is RECORDED in
    the receipt but ADVISORY — it rewards concision on a strong executor and would
    false-fail a correct, positive-delta skill — so precommit does NOT gate on it.
    """
    out = []
    for skill in _evald_skills(root):
        out.extend(validate_receipt(root, skill, final=False))
    return ["(advisory) " + m for m in out] if advisory else out


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
    # the wiki skills route their shared engine into the closure via SKILL_EXTRA_DIRS
    ok.append(("wiki skills map shared/lib/wikisync into their closure",
               SKILL_EXTRA_DIRS.get("khenrix-wiki-add") == ["shared/lib/wikisync"] and
               SKILL_EXTRA_DIRS.get("khenrix-wiki-sync") == ["shared/lib/wikisync"]))
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
    # ---- receipt schema v2 + grandfathering + the single validator -------------
    with tempfile.TemporaryDirectory() as _td:
        _r = Path(_td)
        _ev = _r / "evals" / "alpha"
        _ev.mkdir(parents=True)
        _ev.joinpath("evals.json").write_text("{}")
        (_r / "shared" / "skills" / "alpha").mkdir(parents=True)
        (_r / "shared" / "skills" / "alpha" / "SKILL.md").write_text("# a\n")
        # source_hash reaches capabilities.toml for the overlay closure.
        (_r / "capabilities.toml").write_text("[instructions]\n")

        def _v(rec, **kw):
            _ev.joinpath("receipt.json").write_text(json.dumps(rec))
            return " ".join(validate_receipt(_r, "alpha", **kw))

        _fresh = {"source_hash": source_hash(_r, "alpha"),
                  "eval_set_hash": eval_set_hash(_r, "alpha"),
                  "provenance": "eval", "certified_by": "assertion_delta"}
        ok.append(("v1 receipt with no schema_version is grandfathered", _v(_fresh) == ""))
        ok.append(("explicit null schema_version is malformed, not grandfathered",
                   "malformed" in _v({**_fresh, "schema_version": None})))
        ok.append(("unknown future schema_version is rejected",
                   "unknown schema_version" in _v({**_fresh, "schema_version": 99})))
        ok.append(("v2 without per_provider passes the NON-final gate",
                   _v({**_fresh, "schema_version": 2}) == ""))
        ok.append(("v2 without per_provider FAILS the final gate",
                   "per_provider" in _v({**_fresh, "schema_version": 2},
                                        final=True, panel=["claude"])))
        ok.append(("v2 with per_provider passes the final gate",
                   _v({**_fresh, "schema_version": 2, "providers": ["claude"],
                       "per_provider": {"claude": {"delta": 0.1}}},
                      final=True, panel=["claude"]) == ""))
        _seeded = {**_fresh, "schema_version": 2,
                   "provenance": "seeded: blessed current committed state"}
        _seeded.pop("certified_by")
        ok.append(("seeded v2 is legal for the non-final gate", _v(_seeded) == ""))
        ok.append(("seeded v2 is rejected by the final gate",
                   "seeded" in _v(_seeded, final=True, panel=["claude"])))
        ok.append(("a single-provider receipt fails the FULL-PANEL final gate",
                   "FULL-PANEL" in _v({**_fresh, "schema_version": 2,
                                       "providers": ["claude"],
                                       "per_provider": {"claude": {"delta": 0.1}}},
                                      final=True, panel=["claude", "codex", "agy"])))
        ok.append(("a self-test-gated receipt skips the panel requirement",
                   "FULL-PANEL" not in _v({**_fresh, "schema_version": 2,
                                           "self_test": True, "providers": ["claude"]},
                                          final=True, panel=["claude", "codex", "agy"])))
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
        ok.append(("a v1 receipt is NOT exempt from the final panel check",
                   "FULL-PANEL" in _v({**_fresh, "providers": ["claude"]},
                                      final=True, panel=["claude", "codex", "agy"])))
        ok.append(("a v1 receipt IS exempt from the per_provider requirement",
                   "per_provider" not in _v({**_fresh, "providers": ["claude"]},
                                            final=True, panel=["claude"])))

    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else (1 if run_all() else 0))
