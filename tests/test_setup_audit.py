"""Hermetic tests for the khenrix-audit engine. No real HOME, no network."""
import importlib.util
import json
import pytest
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
    argv = ["serve", "--token", "xox" + "b-123456789012-abcdefghij", "--port", "80"]
    red = sa.redact_argv(argv)
    assert "xoxb-123456789012-abcdefghij" not in json.dumps(red)
    assert "--port" in red and "80" in red


def test_scan_artifact_text_fails_closed_on_fake_token():
    hits = sa.scan_artifact_text("cmd ghp_" + "b" * 36 + " end")
    assert hits, "artifact scan must flag a token-shaped string"
    assert sa.scan_artifact_text("plain text, no secrets") == []


def make_claude_home(tmp_path):
    h = tmp_path / "home"
    cache = h / ".claude/plugins/cache/mkt/plug/1.0.0"
    (cache / "skills/alpha").mkdir(parents=True)
    (cache / "skills/alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: >-\n  Does alpha things. "
        'Triggers: "run alpha".\n---\nbody\n')
    (cache / "hooks").mkdir()
    (cache / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    (h / ".claude/plugins").mkdir(parents=True, exist_ok=True)
    (h / ".claude/plugins/installed_plugins.json").write_text(json.dumps({
        "version": 2, "plugins": {"plug@mkt": [{
            "scope": "user", "installPath": str(cache), "version": "1.0.0"}]}}))
    (h / ".claude").mkdir(exist_ok=True)
    (h / ".claude/settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo hi"}]}]}}))
    (h / ".claude.json").write_text(json.dumps({
        "mcpServers": {"ctx": {"type": "stdio", "command": "npx",
                               "args": ["-y", "ctx"], "env": {"CTX_TOKEN": "ghp_" + "c" * 36}}},
        "projects": {}}))
    return h


def test_walk_claude_inventories_plugin_skill_hook_mcp(tmp_path):
    h = make_claude_home(tmp_path)
    items = sa.walk_claude(h, None, None)
    kinds = {(i["kind"], i["name"]) for i in items}
    assert ("plugin", "plug") in kinds
    assert ("skill", "plug:alpha") in kinds
    assert ("mcp", "ctx") in kinds
    hooks = [i for i in items if i["kind"] == "hook"]
    assert len(hooks) == 2  # plugin Stop hook + user settings Stop hook
    assert hooks[0]["meta"]["body_hash"] == hooks[1]["meta"]["body_hash"]


def test_walk_claude_redacts_mcp_env(tmp_path):
    h = make_claude_home(tmp_path)
    blob = json.dumps(sa.walk_claude(h, None, None))
    assert "ghp_" not in blob
    mcp = next(i for i in json.loads(blob) if i["kind"] == "mcp")
    assert mcp["meta"]["env_keys"] == ["CTX_TOKEN"]
    assert "endpoint_hash" in mcp["meta"]


def test_walk_claude_skill_carries_description(tmp_path):
    h = make_claude_home(tmp_path)
    skill = next(i for i in sa.walk_claude(h, None, None) if i["kind"] == "skill")
    assert "Does alpha things" in skill["meta"]["description"]


def make_codex_home(tmp_path):
    h = tmp_path / "home"
    (h / ".codex/skills/.system/sys1").mkdir(parents=True)
    (h / ".codex/skills/.system/sys1/SKILL.md").write_text("---\nname: sys1\ndescription: s\n---\n")
    inst = h / ".codex/plugins/cache/mkt/plug/skills/beta"
    inst.mkdir(parents=True)
    (inst / "SKILL.md").write_text("---\nname: beta\ndescription: b\n---\n")
    cat = h / ".codex/.tmp/plugins/plugins/zoom/skills/z1"
    cat.mkdir(parents=True)
    (cat / "SKILL.md").write_text("---\nname: z1\ndescription: catalog only\n---\n")
    (h / ".codex/config.toml").write_text(
        '[mcp_servers.ctx]\ncommand = "npx"\nargs = ["-y", "ctx"]\n')
    return h


def test_walk_codex_separates_catalog_from_loaded(tmp_path):
    items = sa.walk_codex(make_codex_home(tmp_path), None, None)
    by_prov = {}
    for i in items:
        by_prov.setdefault(i["provenance"], set()).add(i["name"])
    assert any("z1" in n for n in by_prov.get("catalog", set()))
    loaded = by_prov.get("loaded", set())
    assert any("beta" in n for n in loaded) and any("sys1" in n for n in loaded)
    assert not any("z1" in n for n in loaded)


def test_walk_codex_reads_mcp_from_toml(tmp_path):
    items = sa.walk_codex(make_codex_home(tmp_path), None, None)
    assert any(i["kind"] == "mcp" and i["name"] == "ctx" for i in items)


def test_walk_agy(tmp_path):
    h = tmp_path / "home"
    sk = h / ".gemini/config/plugins/khenrix-utils/skills/gamma"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: gamma\ndescription: g\n---\n")
    (h / ".gemini/config/mcp_config.json").write_text(json.dumps(
        {"mcpServers": {"ctx": {"command": "npx", "args": ["-y", "ctx"]}}}))
    items = sa.walk_agy(h, None, None)
    assert any(i["kind"] == "skill" and "gamma" in i["name"] for i in items)
    assert any(i["kind"] == "mcp" and i["name"] == "ctx" for i in items)


MANAGED_BEGIN = "<!-- khenrix-managed:begin -->"


def make_git_root(tmp_path):
    g = tmp_path / "git"
    ku = g / "khenrix-utils"
    (ku / ".git").mkdir(parents=True)
    (ku / "shared/skills/alpha").mkdir(parents=True)
    (ku / "shared/skills/alpha/SKILL.md").write_text("---\nname: alpha\ndescription: a\n---\n")
    r = ku / "marketplaces/claude/plugins/khenrix-utils/skills/alpha"
    r.mkdir(parents=True)
    (r / "SKILL.md").write_text("---\nname: alpha\ndescription: a\n---\n")
    (ku / "capabilities.toml").write_text("version = 1\n")
    (ku / "CLAUDE.md").write_text("x\n" + MANAGED_BEGIN + "\nstyle\n<!-- khenrix-managed:end -->\n")
    # test coverage: verify not is_ku guard suppresses .claude/skills inside ku checkout
    (ku / ".claude/skills/kulocal").mkdir(parents=True)
    (ku / ".claude/skills/kulocal/SKILL.md").write_text("---\nname: kulocal\ndescription: k\n---\n")
    other = g / "app"
    (other / ".claude/skills/local1").mkdir(parents=True)
    (other / ".claude/skills/local1/SKILL.md").write_text("---\nname: local1\ndescription: l\n---\n")
    (other / ".mcp.json").write_text(json.dumps({"mcpServers": {"p1": {"command": "x"}}}))
    return g


def test_walk_projects_provenance_split(tmp_path):
    items = sa.walk_projects(tmp_path / "home", None, make_git_root(tmp_path))
    prov = {i["name"]: i["provenance"] for i in items if i["kind"] == "skill"}
    # same skill name, three provenances — only project-local one is "loaded"
    assert prov["khenrix-utils:shared:alpha"] == "source"
    assert prov["khenrix-utils:rendered:claude:alpha"] == "rendered-artifact"
    assert prov["app:local1"] == "loaded"
    # regression: the canonical checkout must never re-surface its own rendered
    # copies as "loaded" — that's the flood this provenance split exists to prevent.
    loaded_alpha = [i for i in items if i["kind"] == "skill"
                    and i["provenance"] == "loaded" and "alpha" in i["name"]]
    assert loaded_alpha == []
    # verify the not is_ku guard is load-bearing: .claude/skills inside ku checkout
    # must not be surfaced as "loaded" items (the guard at walk_projects:354 blocks them)
    loaded_items = [i for i in items if i["provenance"] == "loaded"]
    assert not any("kulocal" in i["name"] for i in loaded_items), \
        f"kulocal should be suppressed by not is_ku guard, but found in: {[i['name'] for i in loaded_items if 'kulocal' in i['name']]}"


def test_walk_projects_finds_project_mcp_and_instruction_files(tmp_path):
    items = sa.walk_projects(tmp_path / "home", None, make_git_root(tmp_path))
    assert any(i["kind"] == "mcp" and i["scope"] == "project:app" for i in items)
    inst = [i for i in items if i["kind"] == "instruction-file"]
    assert any(i["meta"].get("managed_block_hash") for i in inst)


def test_walk_projects_skips_symlinked_project_dirs(tmp_path):
    g = make_git_root(tmp_path)
    # create a symlink to the app project
    try:
        (g / "linked").symlink_to(g / "app")
    except OSError:
        pytest.skip("no symlink support on this platform")

    items = sa.walk_projects(tmp_path / "home", None, g)
    # verify no item has scope "project:linked"
    assert not any(i["scope"] == "project:linked" for i in items), \
        f"symlinked project dirs should be skipped, but found items: {[i for i in items if i['scope'] == 'project:linked']}"
    # verify the real app project is still walked
    assert any(i["scope"] == "project:app" for i in items), \
        "app project should be walked when accessed directly"


def test_resolve_repo_root(tmp_path):
    g = make_git_root(tmp_path)
    assert sa.resolve_repo_root(g / "khenrix-utils") == g / "khenrix-utils"
    assert sa.resolve_repo_root(g / "app") is None
    assert sa.resolve_repo_root(None) is None


def test_finding_id_order_insensitive_fingerprint_evidence_sensitive():
    f1 = sa.finding("B1", 1, "claude", "user", "skill", ["b", "a"],
                    "wrong-tool-fires", "high", "correctness", {"x": 1}, ["r1"])
    f2 = sa.finding("B1", 1, "claude", "user", "skill", ["a", "b"],
                    "wrong-tool-fires", "high", "correctness", {"x": 2}, ["r1"])
    assert f1["id"] == f2["id"]
    assert f1["fingerprint"] != f2["fingerprint"]


def test_finding_severity_ranks_silent_loss_top():
    hi = sa.finding("B7", 1, "claude", "user", "skill", ["s"],
                    "silent-capability-loss", "high", "correctness", {}, [])
    lo = sa.finding("B9", 1, "claude", "user", "plugin", ["p"],
                    "hygiene", "high", "correctness", {}, [])
    assert hi["severity"] > lo["severity"]


def test_finding_cost_mcp_forbidden():
    import pytest
    with pytest.raises(ValueError):
        sa.finding("B7", 1, "claude", "user", "mcp", ["m"],
                   "cost", "high", "cost", {}, [])


def test_finding_unverified_cli_is_informational():
    f = sa.finding("B1", 1, "codex", "user", "skill", ["x"],
                   "wrong-tool-fires", "high", "correctness", {}, [])
    assert f["informational"] is True and "semantics unverified" in f["note"]


def test_write_findings_fails_closed_on_secret(tmp_path):
    import pytest
    bad = sa.finding("B2", 1, "claude", "user", "hook", ["h"],
                     "silent-capability-loss", "high", "correctness",
                     {"cmd": "ghp_" + "d" * 36}, [])
    with pytest.raises(SystemExit):
        sa.write_findings([bad], {"items": [], "errors": []},
                          tmp_path / "f.json", {"now": "2026-07-30T00:00:00Z"})


def _mk_inv(items):
    return {"schema_version": 1, "items": items, "errors": []}


def test_run_checks_isolates_crashing_check_and_sorts():
    def bad_check(inv, ctx):
        raise RuntimeError("boom")

    def low_check(inv, ctx):
        return [sa.finding("B9", 1, "claude", "user", "plugin", ["p"],
                           "hygiene", "low", "correctness", {}, [])]

    saved = sa.CHECKS[:]
    sa.CHECKS[:] = [low_check, bad_check]
    try:
        out = sa.run_checks(_mk_inv([]), {})
    finally:
        sa.CHECKS[:] = saved
    assert len(out) == 2
    engine = [f for f in out if f["rule"] == "ENGINE"]
    assert engine and "NOT EVALUATED" in engine[0]["note"]
    assert "boom" in json.dumps(engine[0]["evidence"])
    # ENGINE finding is silent-capability-loss/high → must sort before the hygiene/low one
    assert out[0]["rule"] == "ENGINE" and out[1]["rule"] == "B9"
    assert out[0]["severity"] > out[1]["severity"]


def test_engine_capabilities_reflects_repo_root():
    caps_none = sa.engine_capabilities({"repo_root": None})
    caps_some = sa.engine_capabilities({"repo_root": Path("/x")})
    assert caps_none["writable_ledger"] is False
    assert caps_some["writable_ledger"] is True
    assert set(caps_none) == {"can_probe", "can_token_count",
                              "semantics_verified_for", "writable_ledger"}
    assert caps_none["semantics_verified_for"] == ["claude"]


def test_write_findings_secret_refusal_leaves_no_file(tmp_path):
    import pytest
    bad = sa.finding("B2", 1, "claude", "user", "hook", ["h"],
                     "silent-capability-loss", "high", "correctness",
                     {"cmd": "ghp_" + "d" * 36}, [])
    out = tmp_path / "f.json"
    with pytest.raises(SystemExit):
        sa.write_findings([bad], {"items": [], "errors": []}, out,
                          {"now": "2026-07-30T00:00:00Z"})
    assert not out.exists()


def test_cmd_findings_end_to_end_writes_doc(tmp_path):
    def one_check(inv, ctx):
        return [sa.finding("B9", 1, "claude", "user", "plugin", ["p1"],
                           "hygiene", "low", "correctness", {"issue": "x"}, [])]

    home = tmp_path / "home"
    home.mkdir()
    out = tmp_path / "findings.json"
    saved = sa.CHECKS[:]
    sa.CHECKS[:] = [one_check]
    try:
        rc = sa.main(["findings", "--home-root", str(home),
                      "--now", "2026-07-30T00:00:00Z", "--out", str(out)])
    finally:
        sa.CHECKS[:] = saved
    assert rc == 0
    doc = json.loads(out.read_text())
    assert doc["schema_version"] == 1
    assert doc["generated"] == "2026-07-30T00:00:00Z"
    assert doc["counts"]["findings"] == 1
    assert doc["findings"][0]["rule"] == "B9"
    assert set(doc["capabilities"]) == {"can_probe", "can_token_count",
                                        "semantics_verified_for", "writable_ledger"}


def test_b1_flags_same_bare_skill_name_two_plugins():
    inv = _mk_inv([
        sa.item("claude", "user", "skill", "pa:save", "/a", "loaded", description="x"),
        sa.item("claude", "user", "skill", "pb:save", "/b", "loaded", description="y"),
        sa.item("claude", "user", "skill", "pa:other", "/c", "loaded", description="z")])
    hits = sa.check_b1_name_collisions(inv, {})
    assert len(hits) == 1 and hits[0]["rule"] == "B1"
    assert set(hits[0]["subjects"]) == {"pa:save", "pb:save"}


def test_b1_ignores_catalog_and_rendered():
    inv = _mk_inv([
        sa.item("claude", "user", "skill", "pa:save", "/a", "loaded"),
        sa.item("codex", "user", "skill", "zoom:save", "/b", "catalog")])
    assert sa.check_b1_name_collisions(inv, {}) == []


def test_b2_flags_bare_key_reference_to_namespaced_plugin_server():
    inv = _mk_inv([
        sa.item("claude", "user", "mcp", "plugin:pw:pw", "/m", "loaded", endpoint_hash="e1"),
        sa.item("claude", "user", "permission-rule", "mcp__pw__click", "/s", "loaded"),
        sa.item("claude", "user", "hook", "<settings.json>:PostToolUse", "/s", "loaded",
                event="PostToolUse", matcher="mcp__pw__.*", owner="<settings.json>",
                body_hash="h")])
    hits = sa.check_b2_bare_key_refs(inv, {})
    assert len(hits) == 2
    assert all(h["consequence"] == "silent-capability-loss" for h in hits)


def test_b3_flags_same_endpoint_two_scopes():
    inv = _mk_inv([
        sa.item("claude", "user", "mcp", "cdt", "/u", "loaded", endpoint_hash="same"),
        sa.item("claude", "project:app", "mcp", "cdt2", "/p", "loaded", endpoint_hash="same"),
        sa.item("claude", "user", "mcp", "other", "/u", "loaded", endpoint_hash="diff")])
    hits = sa.check_b3_endpoint_dupes(inv, {})
    assert len(hits) == 1 and hits[0]["kind"] == "mcp"
    assert hits[0]["justification"] == "correctness"


def test_b2_composite_matcher_still_flags_bare_reference():
    inv = _mk_inv([
        sa.item("claude", "user", "mcp", "plugin:cdt:cdt", "/m", "loaded", endpoint_hash="e1"),
        sa.item("claude", "user", "hook", "<settings.json>:PreToolUse", "/s", "loaded",
                event="PreToolUse", matcher="mcp__plugin_pw_pw__click|mcp__cdt__navigate",
                owner="<settings.json>", body_hash="h")])
    hits = sa.check_b2_bare_key_refs(inv, {})
    assert len(hits) == 1, "bare cdt ref must fire even beside an unrelated namespaced ref"


def test_b2_correctly_namespaced_reference_does_not_fire():
    inv = _mk_inv([
        sa.item("claude", "user", "mcp", "plugin:pw:pw", "/m", "loaded", endpoint_hash="e1"),
        sa.item("claude", "user", "permission-rule", "mcp__plugin_pw_pw__click", "/s", "loaded")])
    assert sa.check_b2_bare_key_refs(inv, {}) == []


def test_b1_provenance_filter_is_load_bearing():
    # same CLI, same bare name, one loaded + one catalog — must NOT collide
    inv = _mk_inv([
        sa.item("codex", "user", "skill", "pa:save", "/a", "loaded"),
        sa.item("codex", "user", "skill", "zoom:save", "/b", "catalog")])
    assert sa.check_b1_name_collisions(inv, {}) == []


def test_b3_none_endpoint_hash_does_not_crash():
    inv = _mk_inv([
        sa.item("claude", "user", "mcp", "a", "/u", "loaded", endpoint_hash=None),
        sa.item("claude", "user", "mcp", "b", "/p", "loaded", endpoint_hash="same"),
        sa.item("claude", "project:x", "mcp", "c", "/p2", "loaded", endpoint_hash="same")])
    hits = sa.check_b3_endpoint_dupes(inv, {})
    assert len(hits) == 1


def _repo_with_caps(tmp_path, caps_text):
    ku = tmp_path / "ku"
    (ku / ".git").mkdir(parents=True)
    (ku / "shared/skills").mkdir(parents=True)
    (ku / "capabilities.toml").write_text(caps_text)
    return ku


CAPS = 'version = 1\n[mcp_servers.ctx]\ncommand = "npx"\n[mcp_servers.vercel]\nurl = "https://v"\n'


def test_b4_declared_but_not_live_is_drift(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e")])
    hits = sa.check_b4_drift(inv, {"repo_root": repo, "policies": {}})
    drift = [h for h in hits if h["evidence"]["direction"] == "declared-not-live"]
    assert any("vercel" in h["subjects"][0] for h in drift)


def test_b4_live_managed_absent_keeps_firing(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "gdrive", "/c", "loaded", endpoint_hash="e")])
    pol = {"mcp:gdrive": {"desired_state": "managed-absent", "reason": "native connector"}}
    hits = sa.check_b4_drift(inv, {"repo_root": repo, "policies": pol})
    gone = [h for h in hits if "gdrive" in h["subjects"][0]]
    assert gone and gone[0]["consequence"] == "state-divergence"
    assert gone[0]["confidence"] == "high"


def test_b4_live_unknown_is_info_only(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "personal", "/c", "loaded", endpoint_hash="e"),
                   sa.item("claude", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e2")])
    hits = sa.check_b4_drift(inv, {"repo_root": repo, "policies": {}})
    um = [h for h in hits if "personal" in h["subjects"][0]]
    assert um and um[0]["confidence"] == "low" and um[0]["evidence"]["direction"] == "unmanaged"


def test_b5_cross_cli_missing_server(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e"),
                   sa.item("codex", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e")])
    hits = sa.check_b5_cross_cli(inv, {"repo_root": repo, "policies": {}})
    assert any("agy" in h["cli"] and "ctx" in h["subjects"][0] for h in hits)
