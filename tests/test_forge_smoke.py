"""§18's live smoke, tested WITHOUT invoking a provider. Nothing here spends money — the
receipt logic is pure and the provider path is exercised only by `make smoke-llm-forge`."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import forge_smoke  # noqa: E402


def test_adapter_hash_moves_when_the_adapter_moves(monkeypatch):
    a = forge_smoke.adapter_hash()
    monkeypatch.setattr(forge_smoke, "ADAPTER_SOURCES",
                        forge_smoke.ADAPTER_SOURCES[:-1])
    assert forge_smoke.adapter_hash() != a


def test_a_receipt_is_stale_when_the_adapter_changed():
    r = {"adapter_sha256": "old", "cli_versions": {"claude": "1", "codex": "2", "agy": "3"}}
    ok, why = forge_smoke.receipt_is_fresh(
        r, adapter="new", versions={"claude": "1", "codex": "2", "agy": "3"})
    assert not ok and "adapter" in why


def test_a_receipt_is_stale_when_any_cli_version_changed():
    r = {"adapter_sha256": "a", "cli_versions": {"claude": "1", "codex": "2", "agy": "3"}}
    ok, why = forge_smoke.receipt_is_fresh(
        r, adapter="a", versions={"claude": "1", "codex": "9", "agy": "3"})
    assert not ok and "codex" in why


def test_an_unreadable_cli_version_makes_the_receipt_stale_never_fresh():
    """An unread version is not an unchanged one. A receipt fresh over a version nobody could
    read certifies a path that may have moved."""
    r = {"adapter_sha256": "a", "cli_versions": {"claude": "1", "codex": "2", "agy": "3"}}
    ok, why = forge_smoke.receipt_is_fresh(
        r, adapter="a", versions={"claude": "1", "codex": None, "agy": "3"})
    assert not ok and "codex" in why


def test_a_matching_receipt_is_fresh():
    v = {"claude": "1", "codex": "2", "agy": "3"}
    r = {"adapter_sha256": "a", "cli_versions": dict(v)}
    ok, why = forge_smoke.receipt_is_fresh(r, adapter="a", versions=v)
    assert ok and why == ""


def test_a_receipt_missing_a_provider_is_stale():
    r = {"adapter_sha256": "a", "cli_versions": {"claude": "1", "codex": "2"}}
    ok, why = forge_smoke.receipt_is_fresh(
        r, adapter="a", versions={"claude": "1", "codex": "2", "agy": "3"})
    assert not ok and "agy" in why


def test_the_smoke_is_in_no_gate_target():
    """The deliberate money exception must be opt-in. A target reached by `verify` or
    `precommit` spends on every commit.

    THE EXTERNAL QUESTION, not a restatement of the Makefile: it asks whether any GATE target
    can reach the smoke, which is why it reads the recipe lines under each gate as well as the
    dependency line. A check that only read `verify:`'s prerequisites would pass over a
    `$(MAKE) smoke-llm-forge` sitting in its recipe body.
    """
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    gates = ("verify", "precommit", "test", "council-test", "eval-test",
             "forge-test-slow", "forge-test")
    current = None
    for line in mk.splitlines():
        if line and not line[0].isspace() and ":" in line and not line.startswith("."):
            current = line.split(":", 1)[0].strip()
            body = line.split(":", 1)[1]
        elif line.startswith("\t"):
            body = line
        else:
            continue
        if current in gates:
            assert "smoke-llm-forge" not in body, f"{current}: {line}"
    assert "smoke-llm-forge:" in mk
