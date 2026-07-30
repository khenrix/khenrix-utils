"""Hermetic tests for the khenrix-audit engine. No real HOME, no network."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "shared" / "skill-templates" / "khenrix-audit" / "scripts" / "setup_audit.py"

spec = importlib.util.spec_from_file_location("setup_audit", ENGINE)
sa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sa)


def test_item_shape_and_defaults():
    it = sa.item("claude", "user", "skill", "chunk-map",
                 "/x/skills/chunk-map/SKILL.md", "loaded", description="d")
    assert it["cli"] == "claude" and it["scope"] == "user"
    assert it["kind"] == "skill" and it["provenance"] == "loaded"
    assert it["effective_state"] == "enabled"
    assert it["meta"]["description"] == "d"
    assert it["id"] == sa.sid("claude", "user", "skill", "chunk-map")


def test_sid_stable_and_order_sensitive():
    assert sa.sid("a", "b") == sa.sid("a", "b")
    assert sa.sid("a", "b") != sa.sid("b", "a")
    assert len(sa.sid("a")) == 12


def test_canonical_json_sorts_keys():
    assert sa.canonical_json({"b": 1, "a": [2, 1]}) == '{"a": [2, 1], "b": 1}'


def test_cli_inventory_runs_on_empty_home(tmp_path, capsys):
    (tmp_path / "home").mkdir()
    rc = sa.main(["inventory", "--home-root", str(tmp_path / "home"),
                  "--out", str(tmp_path / "inv.json")])
    assert rc == 0
    inv = json.loads((tmp_path / "inv.json").read_text())
    assert inv["schema_version"] == 1
    assert inv["items"] == []
    assert isinstance(inv["errors"], list)


def test_redact_map_keeps_names_drops_values():
    r = sa.redact_map({"GITHUB_TOKEN": "ghp_" + "a" * 36, "PORT": "8080"})
    assert set(r) == {"GITHUB_TOKEN", "PORT"}
    assert r["GITHUB_TOKEN"]["redacted"] is True
    assert "ghp_" not in json.dumps(r)
    # vhash supports equality comparison without the value
    assert r["GITHUB_TOKEN"]["vhash"] == sa.vhash("ghp_" + "a" * 36)


def test_redact_url_strips_userinfo_and_query():
    u = sa.redact_url("https://user:pw@h.example/p?token=abc")
    assert "pw" not in u and "abc" not in u and "h.example/p" in u


def test_redact_argv_masks_secret_shaped_values():
    argv = ["serve", "--token", "xoxb-123456789012-abcdefghij", "--port", "80"]
    red = sa.redact_argv(argv)
    assert "xoxb-123456789012-abcdefghij" not in json.dumps(red)
    assert "--port" in red and "80" in red


def test_scan_artifact_text_fails_closed_on_fake_token():
    hits = sa.scan_artifact_text("cmd ghp_" + "b" * 36 + " end")
    assert hits, "artifact scan must flag a token-shaped string"
    assert sa.scan_artifact_text("plain text, no secrets") == []
