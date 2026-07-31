#!/usr/bin/env python3
"""Scaffold a fixture $HOME + canonical repo checkout for the khenrix-audit
behavior eval (evals/khenrix-audit/evals.json, case 0).

Recreates on disk the same shapes khenrix-audit's own test fixtures use (Task 3's
make_claude_home two-plugin layout, Task 8's B4 managed-absent policy test) so the
REAL engine (setup_audit.py findings) can be run against it and produce two
specific, known findings:

  B1 name collision — two Claude plugins ("plugin-a", "plugin-b") each ship a
  skill bare-named "save", reachable from two different owners.

  B4 managed-absent-but-live drift — MCP server "gdrive" is live on Claude,
  undeclared in capabilities.toml, AND the repo ledger already carries a
  desired_state=managed-absent policy for it — so it must be reported as a
  confirmed high-confidence drift, not a low-confidence "unmanaged extra".

Usage: python3 scaffold_home.py <target_dir>
Writes <target_dir>/home (fake $HOME) and <target_dir>/repo (fake canonical
khenrix-utils checkout). Absolute paths are computed from <target_dir> at run
time, so this is safe to run against any freshly materialized fixture copy.
"""
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("usage: scaffold_home.py <target_dir>")
    target = Path(sys.argv[1])
    home = target / "home"
    repo = target / "repo"

    # --- claude plugin cache: two plugins, each shipping a skill bare-named
    # "save" -> B1 collision (check_b1_name_collisions groups by (cli, bare_name)).
    installed = {"version": 2, "plugins": {}}
    for plug, mkt in (("plugin-a", "mkt-a"), ("plugin-b", "mkt-b")):
        install_path = home / ".claude/plugins/cache" / mkt / plug / "1.0.0"
        skill_dir = install_path / "skills/save"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: save\n"
            f"description: Saves things, from {plug}.\n"
            "---\n\nbody\n"
        )
        installed["plugins"][f"{plug}@{mkt}"] = [{
            "scope": "user", "installPath": str(install_path), "version": "1.0.0",
        }]
    (home / ".claude/plugins").mkdir(parents=True, exist_ok=True)
    (home / ".claude/plugins/installed_plugins.json").write_text(
        json.dumps(installed, indent=1))

    # --- ~/.claude.json: a live "gdrive" MCP server, undeclared in capabilities.toml.
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "gdrive": {"type": "stdio", "command": "gdrive-mcp", "args": []},
        },
        "projects": {},
    }, indent=1))

    # --- fake canonical repo checkout. resolve_repo_root() only requires these
    # three paths to exist (file or dir) — no real git needed.
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    (repo / "shared/skills").mkdir(parents=True, exist_ok=True)
    (repo / "capabilities.toml").write_text(
        "version = 1\n"
        "# gdrive is intentionally NOT declared here — see the ledger policy below.\n"
    )

    # --- repo ledger: managed-absent policy for gdrive. Read by load_ledger() via
    # <repo_root>/docs/setup-audit/ledger.json (check_b4_drift's evidence["direction"]
    # becomes "managed-absent-but-live" / confidence "high" instead of "unmanaged" /
    # "low" once this policy exists).
    (repo / "docs/setup-audit").mkdir(parents=True, exist_ok=True)
    (repo / "docs/setup-audit/ledger.json").write_text(json.dumps({
        "schema_version": 1,
        "entries": {},
        "policies": {
            "mcp:gdrive": {
                "desired_state": "managed-absent",
                "reason": "native Google Drive connector used instead of the MCP server",
                "created": "2026-07-01T00:00:00Z",
            },
        },
    }, indent=1))

    print(f"scaffolded fixture home at {home}")
    print(f"scaffolded fixture repo at {repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
