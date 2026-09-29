"""A synthetic Forge pin contract that needs no private campaign data."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def _synthetic_campaign(root: Path, fixtures, canonical_json) -> tuple[Path, Path]:
    fixture_root = root / "forge-fixtures"
    held_out = fixture_root / "held-out"
    held_out.mkdir(parents=True)
    (held_out / "probe.py").write_text("# public synthetic probe\n")

    cases = [
        {
            "id": f"council-{index:02d}", "kind": "council",
            "mode": "normal" if index < 30 else "deep",
            "prompt": f"Public synthetic Council question {index}",
            "assertions": [f"Public synthetic assertion {index}"],
        }
        for index in range(60)
    ]
    manifest_cases = []
    modes = ("review", "deep-review", "ingress", "fusion")
    for index in range(12):
        case_id = f"forge-{index + 1:02d}"
        source = fixture_root / "public" / case_id
        source.mkdir(parents=True)
        (source / "task.txt").write_text(f"Public synthetic source {case_id}\n")
        (held_out / f"{case_id}.py").write_text(
            f"# Public synthetic oracle {case_id}\n"
        )
        mode = modes[index % len(modes)]
        manifest_cases.append({
            "id": case_id, "mode": mode, "owned_paths": ["task.txt"],
            "check": {"id": "synthetic-check", "argv": ["/usr/bin/true"], "timeout": 1},
        })
        cases.append({
            "id": case_id, "kind": "forge", "mode": mode,
            "prompt": f"Public synthetic Forge task {case_id}",
            "assertions": [f"Public synthetic check {case_id}"],
        })

    (fixture_root / "manifest.json").write_bytes(canonical_json({
        "schema_version": 1, "cases": manifest_cases,
    }))
    casebook = root / "quality-cases.json"
    casebook.write_bytes(canonical_json({
        "schema_version": 1, "cases": cases,
        "cases_sha256": hashlib.sha256(canonical_json(cases)).hexdigest(),
        "forge_fixture_manifest_sha256": fixtures.manifest_sha256(fixture_root),
    }))
    return casebook, fixture_root


def test_public_synthetic_pin_rejects_changed_oracle(tmp_path: Path) -> None:
    try:
        from scripts import fanout_forge_fixtures as fixtures
        from shared.lib.fanout import canonical_json, quality
    except ImportError as error:
        pytest.fail(f"public fixture runtime unavailable: {error}")
    casebook, fixture_root = _synthetic_campaign(tmp_path, fixtures, canonical_json)
    assert len(quality.load_casebook(casebook)["cases"]) == 72
    assert len(fixtures.verify_pin(casebook, fixture_root)) == 12

    oracle = fixture_root / "held-out" / "forge-01.py"
    oracle.write_text(oracle.read_text() + "# changed\n")
    with pytest.raises(fixtures.FixtureError, match="pin"):
        fixtures.verify_pin(casebook, fixture_root)
