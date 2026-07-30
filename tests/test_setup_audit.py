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


# --- Gap 1: plugin-provided MCP servers ------------------------------------
# Real-machine shape confirmed via ~/.claude/plugins/cache/claude-plugins-official/
# playwright/unknown/.mcp.json: a FLAT {name: entry} map at the plugin root, not
# wrapped in "mcpServers" (that wrapper is only used by project-level .mcp.json
# and by .claude-plugin/plugin.json's "mcpServers" key). The walker must accept
# both so it doesn't go dead the next time a plugin picks the other shape.
def test_walk_claude_plugin_mcp_from_dotmcpjson_flat_shape_and_redacted(tmp_path):
    h = make_claude_home(tmp_path)
    cache = h / ".claude/plugins/cache/mkt/plug/1.0.0"
    (cache / ".mcp.json").write_text(json.dumps({
        "pw": {"command": "npx", "args": ["@playwright/mcp@latest"],
               "env": {"PW_TOKEN": "ghp_" + "e" * 36}}}))
    items = sa.walk_claude(h, None, None)
    mcp = next(i for i in items if i["kind"] == "mcp" and i["name"] == "plugin:plug:pw")
    assert "endpoint_hash" in mcp["meta"]
    assert mcp["meta"]["env_keys"] == ["PW_TOKEN"]
    assert "ghp_" not in json.dumps(items)


def test_walk_claude_plugin_mcp_from_plugin_json_location(tmp_path):
    h = make_claude_home(tmp_path)
    cache = h / ".claude/plugins/cache/mkt/plug/1.0.0"
    (cache / ".claude-plugin").mkdir(exist_ok=True)
    (cache / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "plug", "mcpServers": {"aux": {"command": "aux-server"}}}))
    items = sa.walk_claude(h, None, None)
    assert any(i["kind"] == "mcp" and i["name"] == "plugin:plug:aux" for i in items)


def test_walk_claude_plugin_mcp_checks_both_locations_together(tmp_path):
    h = make_claude_home(tmp_path)
    cache = h / ".claude/plugins/cache/mkt/plug/1.0.0"
    (cache / ".mcp.json").write_text(json.dumps({"pw": {"command": "npx"}}))
    (cache / ".claude-plugin").mkdir(exist_ok=True)
    (cache / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"mcpServers": {"aux": {"command": "aux-server"}}}))
    names = {i["name"] for i in sa.walk_claude(h, None, None) if i["kind"] == "mcp"}
    assert {"plugin:plug:pw", "plugin:plug:aux"}.issubset(names)


# --- Gap 3: enabledPlugins gate --------------------------------------------
def test_walk_claude_disabled_plugin_state_and_skips_children(tmp_path):
    h = make_claude_home(tmp_path)
    cache = h / ".claude/plugins/cache/mkt/plug/1.0.0"
    (cache / ".mcp.json").write_text(json.dumps({"pw": {"command": "npx"}}))
    (h / ".claude/settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo hi"}]}]},
        "enabledPlugins": {"plug@mkt": False}}))
    items = sa.walk_claude(h, None, None)
    plugin_items = [i for i in items if i["kind"] == "plugin" and i["name"] == "plug"]
    assert len(plugin_items) == 1
    assert plugin_items[0]["effective_state"] == "disabled"
    # a disabled plugin contributes NOTHING to the loaded surface: no skill,
    # agent, command, hook, or mcp item traceable back to it.
    assert not any(i["kind"] == "skill" and i["name"].startswith("plug:") for i in items)
    assert not any(i["kind"] == "mcp" and i["name"].startswith("plugin:plug:") for i in items)
    hooks = [i for i in items if i["kind"] == "hook"]
    assert len(hooks) == 1  # only the user settings.json Stop hook survives
    assert hooks[0]["meta"]["owner"] == "<settings.json>"


def test_walk_claude_enabled_plugins_absent_keeps_existing_behavior(tmp_path):
    h = make_claude_home(tmp_path)
    items = sa.walk_claude(h, None, None)
    plugin_items = [i for i in items if i["kind"] == "plugin" and i["name"] == "plug"]
    assert plugin_items[0]["effective_state"] == "enabled"
    assert any(i["kind"] == "skill" and i["name"] == "plug:alpha" for i in items)


def test_walk_claude_settings_local_wins_enabled_plugins_per_key(tmp_path):
    h = make_claude_home(tmp_path)
    (h / ".claude/settings.json").write_text(json.dumps({
        "enabledPlugins": {"plug@mkt": True}}))
    (h / ".claude/settings.local.json").write_text(json.dumps({
        "enabledPlugins": {"plug@mkt": False}}))
    items = sa.walk_claude(h, None, None)
    plugin_items = [i for i in items if i["kind"] == "plugin" and i["name"] == "plug"]
    assert plugin_items[0]["effective_state"] == "disabled"


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
                   sa.item("codex", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e"),
                   sa.item("agy", "user", "plugin", "khenrix-utils", "/g", "loaded")])
    hits = sa.check_b5_cross_cli(inv, {"repo_root": repo, "policies": {}})
    assert any("agy" in h["cli"] and "ctx" in h["subjects"][0] for h in hits)


def test_b5_ignores_cli_absent_from_machine(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e")])
    hits = sa.check_b5_cross_cli(inv, {"repo_root": repo, "policies": {}})
    assert not any(h["cli"] == "agy" for h in hits), "absent CLI must not be flagged"


# --- Gap 2: platform gates on declared servers ------------------------------
# Real false positive this closes: [mcp_servers.1password] platform="windows" in
# capabilities.toml was flagged as declared-not-live on this (Linux) machine.
CAPS_PLATFORM = ('version = 1\n[mcp_servers.ctx]\ncommand = "npx"\n'
                  '[mcp_servers.winonly]\ncommand = "op"\nplatform = "windows"\n')


def test_b4_platform_gate_suppresses_declared_not_live_on_wrong_platform(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS_PLATFORM)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e")])
    hits = sa.check_b4_drift(inv, {"repo_root": repo, "policies": {}, "platform": "linux"})
    assert not any(h["evidence"].get("direction") == "declared-not-live"
                   and "winonly" in h["subjects"][0] for h in hits)


def test_b4_platform_gate_fires_when_platform_matches(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS_PLATFORM)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "ctx", "/c", "loaded", endpoint_hash="e")])
    hits = sa.check_b4_drift(inv, {"repo_root": repo, "policies": {}, "platform": "windows"})
    assert any(h["evidence"].get("direction") == "declared-not-live"
               and "winonly" in h["subjects"][0] for h in hits)


def test_b4_managed_absent_live_fires_regardless_of_platform_gate(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS_PLATFORM)
    inv = _mk_inv([sa.item("claude", "user", "mcp", "gdrive", "/c", "loaded", endpoint_hash="e")])
    pol = {"mcp:gdrive": {"desired_state": "managed-absent", "reason": "native connector"}}
    hits = sa.check_b4_drift(inv, {"repo_root": repo, "policies": pol, "platform": "linux"})
    gone = [h for h in hits if "gdrive" in h["subjects"][0]]
    assert gone and gone[0]["evidence"]["direction"] == "managed-absent-but-live"


def test_b5_platform_gate_suppresses_and_fires(tmp_path):
    repo = _repo_with_caps(tmp_path, CAPS_PLATFORM)
    inv = _mk_inv([
        sa.item("claude", "user", "mcp", "winonly", "/c", "loaded", endpoint_hash="e"),
        sa.item("agy", "user", "plugin", "khenrix-utils", "/g", "loaded")])
    hits_linux = sa.check_b5_cross_cli(inv, {"repo_root": repo, "policies": {}, "platform": "linux"})
    assert not any("winonly" in h["subjects"][0] for h in hits_linux)
    hits_win = sa.check_b5_cross_cli(inv, {"repo_root": repo, "policies": {}, "platform": "windows"})
    assert any("winonly" in h["subjects"][0] for h in hits_win)


def test_trigger_surface_extracts_quotes_triggers_usewhen():
    d = ('Does X. Use when the user wants to file a link. '
         'Triggers: "add this to the wiki", "save this link".')
    ts = sa.trigger_surface(d)
    assert "add this to the wiki" in ts and "file a link" in ts
    assert "Does X" not in ts


def test_overlap_pairs_ranks_shared_trigger_vocab_first():
    skills = [
        {"name": "a:save", "meta": {"description": 'Triggers: "save this", "add this to the wiki", "keep this".'}},
        {"name": "b:wiki-add", "meta": {"description": 'Triggers: "add this to the wiki", "save this link".'}},
        {"name": "c:chart", "meta": {"description": 'Triggers: "plot a chart", "visualize data".'}},
    ]
    pairs = sa.overlap_pairs(skills, top_k=3)
    assert pairs, "must nominate at least one pair"
    top = pairs[0]
    assert {top[1], top[2]} == {"a:save", "b:wiki-add"}
    assert "add this to the wiki" in top[4]  # exact shared quoted phrase


def test_b6_emits_low_confidence_nominations():
    inv = _mk_inv([
        sa.item("claude", "user", "skill", "a:save", "/a", "loaded",
                description='Triggers: "add this to the wiki".'),
        sa.item("claude", "user", "skill", "b:wiki-add", "/b", "loaded",
                description='Triggers: "add this to the wiki".')])
    hits = sa.check_b6_trigger_overlap(inv, {})
    assert hits and hits[0]["confidence"] == "low"
    assert hits[0]["evidence"]["shared_phrases"] == ["add this to the wiki"]


def test_b6_cross_cli_corpora_are_partitioned():
    # same-named skill on codex must not shadow the claude pair (regression: name-keyed dict collision)
    inv = _mk_inv([
        sa.item("claude", "user", "skill", "a:save", "/a", "loaded",
                description='Triggers: "add this to the wiki", "save this".'),
        sa.item("codex", "user", "skill", "a:save", "/cx", "loaded",
                description='Triggers: "unrelated codex thing", "totally different".'),
        sa.item("claude", "user", "skill", "b:wiki-add", "/b", "loaded",
                description='Triggers: "add this to the wiki", "save this link".')])
    hits = sa.check_b6_trigger_overlap(inv, {})
    claude_pairs = [h for h in hits if h["cli"] == "claude"]
    assert any(set(h["subjects"]) == {"a:save", "b:wiki-add"} for h in claude_pairs), \
        "claude pair must survive a same-named codex skill"
    assert all(h["cli"] != "claude" or "codex" not in str(h["evidence"]) for h in hits)


def test_b6_exact_phrase_boost_is_load_bearing():
    # Isolate the +0.25 exact-quoted-phrase boost from the token-based cosine: the
    # comma inside "alpha, beta, gamma" doesn't change what _toks extracts, so the
    # shared-token set and term frequencies are identical between the two runs —
    # only whether the quoted phrase text matches EXACTLY differs. (Adjusted from
    # the reviewer's proposed `cosine > 1.0` assertion: measured cosine here tops
    # out at 0.596, since two non-identical unit TF-IDF vectors can't exceed 1.0 —
    # asserting the boosted-vs-unboosted delta pins the same +0.25 contract without
    # relying on a threshold the real function never reaches.)
    boosted = sa.overlap_pairs([
        {"name": "x", "meta": {"description": 'Triggers: "alpha beta gamma", "delta run".'}},
        {"name": "y", "meta": {"description": 'Triggers: "alpha beta gamma", "epsilon jump".'}},
    ])
    unboosted = sa.overlap_pairs([
        {"name": "x", "meta": {"description": 'Triggers: "alpha, beta, gamma", "delta run".'}},
        {"name": "y", "meta": {"description": 'Triggers: "alpha beta gamma", "epsilon jump".'}},
    ])
    assert boosted and boosted[0][4] == ["alpha beta gamma"]
    assert unboosted and unboosted[0][4] == [], "comma-altered phrase must not exact-match"
    assert boosted[0][0] - unboosted[0][0] == pytest.approx(0.25), \
        "exact shared phrase must add exactly the +0.25 boost above plain cosine"


def test_b6_requires_two_shared_tokens():
    skills = [
        {"name": "x", "meta": {"description": 'Triggers: "think hard about zebras".'}},
        {"name": "y", "meta": {"description": 'Triggers: "think fast, act now".'}},
    ]
    # shared non-stopword tokens: only "think" ("about" is in _STOP; "hard"/"zebras"
    # vs "fast"/"act"/"now" don't overlap) -> below the >=2 floor -> no nomination
    assert sa.overlap_pairs(skills) == []


def test_b7_over_budget_flags_silent_capability_loss():
    inv = _mk_inv([])
    ctx = {"tokens": {"claude-obsidian": 3678, "khenrix-utils": 2918},
           "context_window": 200_000}
    hits = sa.check_b7_budget(inv, ctx)
    assert hits and hits[0]["consequence"] == "silent-capability-loss"
    assert hits[0]["evidence"]["total_always_on"] == 6596
    assert hits[0]["evidence"]["budget"] == 2000
    assert hits[0]["justification"] == "correctness"  # NOT "cost" — overflow drops descriptions


def test_b7_without_tokens_file_is_not_evaluated():
    hits = sa.check_b7_budget(_mk_inv([]), {"context_window": 200_000})
    assert hits and "NOT EVALUATED" in hits[0]["note"]


def test_b8_duplicate_hook_bodies_flagged():
    inv = _mk_inv([
        sa.item("claude", "user", "hook", "<settings.json>:Stop", "/s", "loaded",
                event="Stop", matcher="*", owner="<settings.json>", body_hash="same"),
        sa.item("claude", "user", "hook", "plug:Stop", "/p", "loaded",
                event="Stop", matcher="*", owner="plug", body_hash="same")])
    hits = sa.check_b8_hook_collisions(inv, {})
    dup = [h for h in hits if h["evidence"].get("duplicate_body")]
    assert len(dup) == 1


def test_b9_flags_unknown_version_and_load_failed():
    inv = _mk_inv([
        sa.item("claude", "user", "plugin", "p1", "/x", "loaded", version="unknown"),
        sa.item("claude", "user", "plugin", "p2", "/y", "loaded",
                effective_state="load_failed", version="1.0")])
    hits = sa.check_b9_hygiene(inv, {})
    assert len(hits) == 2 and all(h["consequence"] == "hygiene" for h in hits)


def test_b10_converts_walker_errors_to_findings():
    inv = {"schema_version": 1, "items": [], "errors": ["walk_claude: ValueError: bad json"]}
    hits = sa.check_b10_parse_validity(inv, {})
    assert hits and hits[0]["consequence"] == "silent-capability-loss"


def test_b11_flags_missing_hook_executable(tmp_path):
    inv = _mk_inv([sa.item("claude", "user", "hook", "o:Stop", "/s", "loaded",
                           event="Stop", matcher="*", owner="o", body_hash="h",
                           command_head=[str(tmp_path / "gone.sh")])])
    hits = sa.check_b11_dangling_refs(inv, {})
    assert len(hits) == 1


def test_b12_flags_oversized_description():
    inv = _mk_inv([sa.item("claude", "user", "skill", "p:big", "/x", "loaded",
                           description="d" * 1100, body_lines=10)])
    assert len(sa.check_b12_frontmatter(inv, {})) == 1


def test_b13_flags_diverged_managed_blocks():
    inv = _mk_inv([
        sa.item("all", "project:p", "instruction-file", "p:CLAUDE.md", "/1", "loaded",
                chars=10, managed_block_hash="aaa"),
        sa.item("all", "project:p", "instruction-file", "p:AGENTS.md", "/2", "loaded",
                chars=10, managed_block_hash="bbb")])
    assert len(sa.check_b13_managed_block_divergence(inv, {})) == 1


def test_b14_flags_stale_project_entries(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    (h / ".claude.json").write_text(json.dumps(
        {"mcpServers": {}, "projects": {str(tmp_path / "gone-dir"): {}}}))
    hits = sa.check_b14_claude_json_health(_mk_inv([]), {"home": h})
    assert hits and "gone-dir" in json.dumps(hits[0]["evidence"])


def test_b15_flags_same_skill_two_paths():
    inv = _mk_inv([
        sa.item("claude", "user", "skill", "pa:dup", "/a", "loaded", description="same text"),
        sa.item("claude", "user", "skill", "dup", "/b", "loaded", description="same text")])
    assert len(sa.check_b15_dual_path(inv, {})) == 1


# --- ledger: sole writer, fingerprint waivers, suppression ----------------
def _ledger_ctx(tmp_path, entries=None, policies=None):
    repo = tmp_path / "ku"
    (repo / ".git").mkdir(parents=True)
    (repo / "shared/skills").mkdir(parents=True)
    (repo / "capabilities.toml").write_text("version = 1\n")
    (repo / "docs/setup-audit").mkdir(parents=True)
    (repo / "docs/setup-audit/ledger.json").write_text(json.dumps(
        {"schema_version": 1, "entries": entries or {}, "policies": policies or {}}))
    home = tmp_path / "home"
    home.mkdir()
    return {"repo_root": repo, "home": home, "now": "2026-08-01T00:00:00Z"}


def _wf(**kw):
    base = dict(rule="B6", rule_version=1, cli="claude", scope="user", kind="skill",
                subjects=["a", "b"], consequence="wrong-tool-fires", confidence="low",
                justification="correctness", evidence={"cosine": 0.4}, remediation=[])
    base.update(kw)
    return sa.finding(**base)


def test_waiver_suppresses_matching_fingerprint(tmp_path):
    f = _wf()
    ctx = _ledger_ctx(tmp_path, entries={f["id"]: {
        "disposition": "waived", "fingerprint": f["fingerprint"],
        "until": "2026-12-31T00:00:00Z", "reason": "deliberate"}})
    ctx.update(sa.load_ledger(ctx))
    kept = sa.apply_ledger([f], ctx)
    assert kept == [] and ctx["waived"][0]["id"] == f["id"]


def test_stale_fingerprint_reraises(tmp_path):
    f = _wf(evidence={"cosine": 0.9})
    ctx = _ledger_ctx(tmp_path, entries={f["id"]: {
        "disposition": "waived", "fingerprint": "outdated0", "until": "2026-12-31T00:00:00Z",
        "reason": "old"}})
    ctx.update(sa.load_ledger(ctx))
    kept = sa.apply_ledger([f], ctx)
    assert kept and "situation changed" in kept[0]["note"]


def test_expired_waiver_reraises(tmp_path):
    f = _wf()
    ctx = _ledger_ctx(tmp_path, entries={f["id"]: {
        "disposition": "waived", "fingerprint": f["fingerprint"],
        "until": "2026-07-01T00:00:00Z", "reason": "was deferred"}})
    ctx.update(sa.load_ledger(ctx))
    kept = sa.apply_ledger([f], ctx)
    assert kept and "waiver expired" in kept[0]["note"]


def test_wontfix_mismatched_fingerprint_reraises(tmp_path):
    # Regression: the fingerprint check must apply to wontfix too, not just
    # waived — adjudicated semantics are fingerprint-guarded regardless of
    # disposition. A mutation that scopes this check to waived-only must fail
    # this test (bug-injection proof recorded in task-12-report.md).
    f = _wf()
    ctx = _ledger_ctx(tmp_path, entries={f["id"]: {
        "disposition": "wontfix", "fingerprint": "outdated0", "reason": "will not fix"}})
    ctx.update(sa.load_ledger(ctx))
    kept = sa.apply_ledger([f], ctx)
    assert kept and "situation changed" in kept[0]["note"]


def test_wontfix_ignores_expiry(tmp_path):
    # wontfix has no time expiry: an "until" in the past must NOT reraise it —
    # only a fingerprint mismatch can.
    f = _wf()
    ctx = _ledger_ctx(tmp_path, entries={f["id"]: {
        "disposition": "wontfix", "fingerprint": f["fingerprint"],
        "until": "2020-01-01T00:00:00Z", "reason": "will not fix"}})
    ctx.update(sa.load_ledger(ctx))
    kept = sa.apply_ledger([f], ctx)
    assert kept == [] and ctx["waived"][0]["id"] == f["id"]


def test_ledger_add_is_atomic_and_loadable(tmp_path):
    ctx = _ledger_ctx(tmp_path)
    rc = sa.main(["ledger-add", "--home-root", str(ctx["home"]),
                  "--repo-root", str(ctx["repo_root"]), "--id", "abc123def456",
                  "--state", "waived", "--reason", "test", "--fingerprint", "ff00ff00",
                  "--until", "2026-12-31T00:00:00Z"])
    assert rc == 0
    led = json.loads((ctx["repo_root"] / "docs/setup-audit/ledger.json").read_text())
    assert led["entries"]["abc123def456"]["reason"] == "test"


def test_policies_flow_from_ledger(tmp_path):
    ctx = _ledger_ctx(tmp_path, policies={
        "mcp:gdrive": {"desired_state": "managed-absent", "reason": "native connector"}})
    loaded = sa.load_ledger(ctx)
    assert loaded["policies"]["mcp:gdrive"]["desired_state"] == "managed-absent"


def test_ledger_expire_never_deletes_but_updates_until(tmp_path):
    ctx = _ledger_ctx(tmp_path, entries={"abc123def456": {
        "disposition": "waived", "fingerprint": "ff00ff00", "reason": "test",
        "until": "2026-12-31T00:00:00Z", "created": "2026-07-01T00:00:00Z", "machine": ""}})
    rc = sa.main(["ledger-expire", "--home-root", str(ctx["home"]),
                  "--repo-root", str(ctx["repo_root"]), "--id", "abc123def456",
                  "--now", "2026-08-15T00:00:00Z"])
    assert rc == 0
    led = json.loads((ctx["repo_root"] / "docs/setup-audit/ledger.json").read_text())
    entry = led["entries"]["abc123def456"]
    assert entry["until"] == "2026-08-15T00:00:00Z"
    assert entry["reason"] == "test"  # history preserved — never deleted


def test_cmd_findings_end_to_end_suppresses_waived_finding_into_waived_list(tmp_path):
    """Pins apply_ledger's placement in cmd_findings: a waived finding must be
    absent from the written doc's findings AND present in its waived list."""
    def one_check(inv, ctx):
        return [sa.finding("B9", 1, "claude", "user", "plugin", ["p1"],
                           "hygiene", "low", "correctness", {"issue": "x"}, [])]

    probe = sa.finding("B9", 1, "claude", "user", "plugin", ["p1"],
                       "hygiene", "low", "correctness", {"issue": "x"}, [])
    repo = tmp_path / "ku"
    (repo / ".git").mkdir(parents=True)
    (repo / "shared/skills").mkdir(parents=True)
    (repo / "capabilities.toml").write_text("version = 1\n")
    (repo / "docs/setup-audit").mkdir(parents=True)
    (repo / "docs/setup-audit/ledger.json").write_text(json.dumps({
        "schema_version": 1,
        "entries": {probe["id"]: {"disposition": "waived", "fingerprint": probe["fingerprint"],
                                   "until": "2026-12-31T00:00:00Z", "reason": "deliberate"}},
        "policies": {}}))
    home = tmp_path / "home"
    home.mkdir()
    out = tmp_path / "findings.json"
    saved = sa.CHECKS[:]
    sa.CHECKS[:] = [one_check]
    try:
        rc = sa.main(["findings", "--home-root", str(home), "--repo-root", str(repo),
                      "--now", "2026-07-30T00:00:00Z", "--out", str(out)])
    finally:
        sa.CHECKS[:] = saved
    assert rc == 0
    doc = json.loads(out.read_text())
    assert doc["findings"] == []
    assert doc["counts"]["findings"] == 0
    assert doc["waived"] and doc["waived"][0]["id"] == probe["id"]
    assert doc["local_waivers"] == 0


# --- report renderer + --check mode ----------------------------------------
def test_render_report_contains_skeleton_and_skips(tmp_path):
    f = _wf()
    doc = {"generated": "2026-07-30T00:00:00Z", "capabilities": {"can_probe": False},
           "inventory_hash": "abc", "counts": {"items": 3, "findings": 1, "errors": 0},
           "errors": [], "findings": [f], "waived": [], "local_waivers": 2}
    md = sa.render_report(doc, phases={"probes": "skipped — claude not on PATH",
                                       "ecosystem": "cache, 12d old"})
    assert "probes: skipped — claude not on PATH" in md
    assert "2 local waiver(s) active" in md
    assert f["slug"] in md


def test_check_mode_exit_codes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc_clean = sa.main(["findings", "--home-root", str(home),
                        "--out", str(tmp_path / "f.json"), "--check", "40"])
    assert rc_clean == 0  # empty home → no findings ≥ 40
