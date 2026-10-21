"""Non-secret observations from installed native provider launches."""
from __future__ import annotations


def describe_native_launch(profile, command, *, observed_cli_version: str,
                           observed_tools: tuple[str, ...] = ()) -> dict[str, object]:
    """Project non-secret launch facts without claiming served-model evidence.

    Installed configuration supplies skills, plugins, MCP transports and auth.
    Native tool restrictions and trusted shell access are not OS confinement.
    Paths record discovery roots, not a claim that every skill was loaded.
    """
    native_roots = command.native_roots
    roots = []
    if native_roots is not None:
        home = native_roots.home
        roots = [home / ".agents/skills"]
        roots.extend({
            "claude": [native_roots.claude / "skills", native_roots.claude / "plugins"],
            "codex": [native_roots.codex / "skills"],
            "agy": [home / ".gemini/antigravity/skills", home / ".gemini/config/skills"],
        }.get(profile.executor_id, []))
    return {
        "role": profile.role,
        "native_roots": native_roots.to_dict() if native_roots is not None else None,
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
