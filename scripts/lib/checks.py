#!/usr/bin/env python3
"""Deterministic source-of-truth checks for `make verify` (stdlib only).

Each check returns a list of problem strings (empty = clean). run_all() concatenates
them; render.check() prints + fails on any. Self-test (`--self-test`) covers the pure
logic with no repo/network dependency.
"""
from __future__ import annotations
import hashlib, json, re, subprocess, sys, tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FANOUT_DIR = ROOT / "shared" / "skills" / "llm-council" / "scripts"

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
# Allowlist of KNOWN-benign matches, keyed by sha256(matched_string) so the
# allowlist file can never itself be the next false positive.
SECRET_ALLOW_SHA: set[str] = {
    # example fake tokens embedded in docs/archive-adoption/implementation-plan.md
    # (they quote this module's own self-test fixtures — not real credentials):
    "492e9901d38877c93a3610b0ca256381302215dc88a3c90281440c29aea8c8eb",  # xoxp-1234567890abcde
    "1a5d44a2dca19669d72edf4c4f1c27c4c1ca4b4408fbb17f6ce4ad452d78ddb3",  # AKIAIOSFODNN7EXAMPLE
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


def scan_secrets(root: Path) -> list[str]:
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                           text=True, check=True).stdout.splitlines()  # splitlines: tolerate spaces in paths
    problems = []
    for rel in files:
        if rel.endswith(SCAN_SKIP_SUFFIX) or any(rel.startswith(d) for d in SCAN_SKIP_DIRS):
            continue
        if rel == "scripts/lib/checks.py":
            continue
        try:
            text = (root / rel).read_text(errors="ignore")
        except OSError:
            continue
        for rx in SECRET_FAIL:
            m = rx.search(text)
            if m and hashlib.sha256(m.group(0).encode()).hexdigest() not in SECRET_ALLOW_SHA:
                problems.append(f"{rel}: matches secret pattern /{rx.pattern[:20]}…/")
                break
    return problems


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
    for cli in ("claude", "codex", "agy"):
        sk = root / "marketplaces" / cli / "plugins" / "khenrix-utils" / "skills"
        if sk.is_dir():
            names = [p.name for p in sk.glob("*/") if (p / "SKILL.md").exists()]
            for n in {x for x in names if names.count(x) > 1}:
                problems.append(f"structure: duplicate skill '{n}' in {cli} plugin")
    return problems


def run_all(root: Path = ROOT) -> list[str]:
    caps = _load_caps(root)
    return model_crosscheck(root) + scan_secrets(root) + structure_checks(root, caps)


def _self_test() -> int:
    ok = []
    ok.append(("secret regex detects slack", any(rx.search("xoxp-1234567890abcde") for rx in SECRET_FAIL)))
    ok.append(("secret regex ignores prose", not any(rx.search("the quick brown fox jumps") for rx in SECRET_FAIL)))
    ok.append(("secret regex detects AKIA", any(rx.search("AKIAIOSFODNN7EXAMPLE") for rx in SECRET_FAIL)))
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return 0 if all(p for _, p in ok) else 1


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else (1 if run_all() else 0))
