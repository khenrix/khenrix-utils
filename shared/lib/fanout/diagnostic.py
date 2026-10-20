"""Non-secret observations from installed native provider launches."""
from __future__ import annotations


def describe_native_launch(profile, command, *, observed_cli_version: str,
                           observed_tools: tuple[str, ...] = ()) -> dict[str, object]:
    """Project non-secret launch facts without claiming served-model evidence.

    Installed configuration supplies skills, plugins, MCP transports and auth.
    Native tool restrictions and trusted shell access are not OS confinement.
    Paths record discovery roots, not a claim that every skill was loaded.
    """
    import os
    from pathlib import Path

    home = Path(os.environ.get("HOME", str(Path.home())))
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    roots = [home / ".agents/skills"]
    roots.extend({
        "claude": [home / ".claude/skills", home / ".claude/plugins"],
        "codex": [codex_home / "skills"],
        "agy": [home / ".gemini/antigravity/skills", home / ".gemini/config/skills"],
    }.get(profile.executor_id, []))
    return {
        "role": profile.role,
        "declared_cli_version": profile.cli_version,
        "observed_cli_version": observed_cli_version,
        "argv": list(command.argv),
        "skill_paths": [str(root) for root in roots],
        "read_capabilities": ["installed-skills", "native-plugins", "configured-mcp", "native-auth", "trusted-shell"],
        "observed_tools": list(observed_tools),
        "write_restrictions": {
            "claude": "native permission bypass with explicit known write-tool denials; trusted shell remains capable of writes; managed policy still applies",
            "codex": "configured known MCP write-tool denials; trusted shell and built-in patch remain capable of writes",
            "agy": "native permission bypass with plan mode; no per-tool CLI write restriction; trusted tools remain capable of writes; managed policy still applies",
        }.get(profile.executor_id, "installed native permissions"),
        "os_confinement": False,
    }
