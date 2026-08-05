#!/usr/bin/env python3
"""khenrix-utils reconcile engine.

Reads the bundled capabilities.toml and reconciles one CLI's live config
(MCP servers, baseline settings, base instructions) toward it — ADDITIVELY.

Guarantees:
  * Only entries declared in capabilities.toml are ever written. Anything else
    in the live config (machine-specific extras) is reported but never touched.
  * ADD-only by default: missing declared entries are added. Drift on an entry
    that already exists is reported but NOT overwritten unless --update-drift.
  * Every file is backed up to *.khenrix-backup before being modified.

Runs both from the repo (scripts/lib/reconcile.py, capabilities.toml at repo
root) and bundled inside a plugin (…/skills/khenrix-setup/scripts/reconcile.py,
capabilities.toml copied next to the plugin root) — it locates the manifest by
walking upwards.

Usage:
  reconcile.py --cli claude                 # read-only review (default)
  reconcile.py --cli claude --apply         # add missing declared entries
  reconcile.py --cli codex --apply --update-drift
  reconcile.py --status --all               # review every CLI
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

CLIS = ("claude", "codex", "agy")
MANAGED_BEGIN = "<!-- khenrix-managed:begin house-style -->"
MANAGED_END = "<!-- khenrix-managed:end house-style -->"
ALIASES_BEGIN = "# khenrix-managed:begin shell-aliases"
ALIASES_END = "# khenrix-managed:end shell-aliases"

# ----- pretty status markers -------------------------------------------------
MARK = {"MATCH": "✅", "ADD": "➕", "UPDATE": "✏️ ", "EXTRA": "⏭️ ", "INFO": "•"}


# ----- discovery / helpers ---------------------------------------------------
def find_upwards(name: str, start: Path) -> Path | None:
    d = Path(start).resolve()
    for p in [d, *d.parents]:
        cand = p / name
        if cand.exists():
            return cand
    return None


def load_caps() -> dict:
    here = Path(__file__).resolve().parent
    caps_path = find_upwards("capabilities.toml", here)
    if not caps_path:
        sys.exit("error: capabilities.toml not found (looked upward from %s)" % here)
    with open(caps_path, "rb") as f:
        data = tomllib.load(f)
    data["_dir"] = caps_path.parent
    return data


def expand(v):
    if isinstance(v, str):
        return os.path.expandvars(os.path.expanduser(v))
    if isinstance(v, list):
        return [expand(x) for x in v]
    if isinstance(v, dict):
        return {k: expand(x) for k, x in v.items()}
    return v


def current_os() -> str:
    return {"linux": "linux", "darwin": "darwin", "windows": "windows"}.get(
        platform.system().lower(), platform.system().lower()
    )


def is_wsl() -> bool:
    return "microsoft" in platform.release().lower()


def host_platforms() -> set:
    """Platform tags this host can launch a server on. An MCP `platform` gate
    matches if it is in this set. WSL satisfies both "linux" (native binaries)
    and "windows" (Windows binaries via interop, e.g. powershell.exe), so
    interop-launched servers like chrome-devtools reconcile here too."""
    tags = {current_os()}
    if current_os() == "linux" and is_wsl():
        tags.add("windows")
    return tags


def run(cmd: list[str]):
    return subprocess.run(cmd, capture_output=True, text=True)


def backup(path) -> Path | None:
    """A copy of `path` that no later call can destroy, or `None` when there is nothing there.

    IT NEVER OVERWRITES AN EARLIER BACKUP, and that is the whole change. This wrote to a
    FIXED name, so the second reconcile clobbered the first backup — reproduced: two calls
    either side of an edit left one file holding only the SECOND state. The lost one is the
    valuable one: it is the user's configuration from before khenrix ever touched the file,
    and this package's stated invariant is that reconcile never removes machine-specific
    config. A backup that destroys the previous backup breaks that promise with the very
    mechanism that exists to keep it.

    NUMBERED RATHER THAN TIMESTAMPED, so the same inputs give the same names: `.khenrix-backup`
    first, then `.khenrix-backup.1`, `.2`, and so on, taking the first free one. Nothing is
    ever removed and nothing is ever written over.
    """
    p = Path(path)
    if not p.exists():
        return None
    b = p.with_suffix(p.suffix + ".khenrix-backup")
    n = 1
    while b.exists():
        b = p.with_suffix(p.suffix + f".khenrix-backup.{n}")
        n += 1
    shutil.copy2(p, b)
    return b


class ReconcileReadError(RuntimeError):
    """A config file this engine will not treat as data it may replace."""


def read_json_object(path: Path) -> dict:
    """The JSON object at `path`, or `{}` when there is genuinely nothing there.

    ABSENT AND UNPARSEABLE ARE DIFFERENT ANSWERS AND THIS IS WHERE THEY DIVERGE. Every
    caller reads `{}` as "this CLI has none of the desired keys yet" and builds a full ADD
    list from it; the apply path then hands that list to `write_json_object`, which does an
    unconditional `write_text` over the whole file. So a settings file that exists and does
    not parse — a half-written save, a merge conflict marker, a hand edit — used to be
    silently replaced with a file containing only what khenrix wanted, and the user's
    machine-specific configuration was gone. Reconcile is non-destructive by design; the
    only way to keep that promise here is to refuse a file this engine cannot read.

    ZERO-LENGTH IS DELIBERATELY STILL `{}`, which is the contract this reader has had since
    the first commit (and had a second copy of in `agy_mcp_load`). A file created but not yet
    written to is an ordinary state on a fresh machine — `write_json_object` itself passes
    through it, since `write_text` truncates before it writes — and refusing it would refuse
    the machine this engine exists to set up. A file with bytes in it that are not a JSON
    object is not that state.

    A NON-OBJECT REFUSES THROUGH THE SAME DOOR, and it is the input that made the old
    `isinstance` line load-bearing in the wrong direction: `[1, 2]` and `"hello"` PARSE, so
    the decode-error branch never saw them, and the old `return ... else {}` turned a file
    with contents into the same `{}` an absent one returns. One refusal, both routes.
    """
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ReconcileReadError(
            f"{path} exists but is not readable as JSON ({e}). Reconcile will not treat an "
            "unreadable config as an empty one: the caller would report every desired key as "
            "missing and the apply path would write over the whole file. Fix or move the file, "
            "then re-run.") from e
    if not isinstance(data, dict):
        raise ReconcileReadError(
            f"{path} holds a JSON {type(data).__name__}, not an object. The same refusal as an "
            "unparseable file and for the same reason — an empty object here is what an ABSENT "
            "file returns, and the caller cannot tell the two apart.")
    return data


def write_json_object(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def shell_command_executable(command: str) -> Path | None:
    try:
        parts = shlex.split(expand(command))
    except ValueError:
        return None
    if not parts:
        return None
    return Path(parts[0])


# ----- desired state ---------------------------------------------------------
def mcp_clis(name: str, spec: dict) -> tuple:
    """The CLIs a declared MCP server may be reconciled onto — all of them unless
    `clis = [...]` narrows it.

    AN ALLOW-LIST IN THE SAME SHAPE AS `platform`, ASKING A DIFFERENT QUESTION. `platform`
    gates on what the machine can launch; this gates on whose MCP client has to hold the
    connection open, and the two diverge — a server every CLI on this host can spawn can
    still be one that a particular client cannot talk to. See [mcp_servers.vercel] in
    capabilities.toml for the measured case. Withheld, not broken: the declaration stays,
    and the CLIs that are listed still get it.

    AN UNKNOWN NAME REFUSES RATHER THAN PICKING A DIRECTION. Both silent answers are wrong
    in a way nobody would notice: silently including means `clis = ["agi"]` still adds the
    server to the CLI the gate was written to keep it off — the defect the gate exists to
    prevent, now invisible — and silently excluding means one typo withholds a working
    server from all three at once, reported as three rows that quietly stop appearing.
    capabilities.toml is hand-edited and decides what gets WRITTEN into a live config, so
    a gate this engine cannot read stops the run, exactly as an unreadable config does
    (read_json_object).
    """
    raw = spec.get("clis")
    if raw is None:
        return CLIS
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        raise ReconcileReadError(
            f"[mcp_servers.{name}] clis must be a list of CLI names, got {raw!r}. "
            f"Known CLIs: {list(CLIS)}.")
    if not raw:
        raise ReconcileReadError(
            f"[mcp_servers.{name}] clis is empty, so no CLI would ever be given this "
            f"server. If that is the intent, delete the declaration instead — and note "
            f"that deleting it is a NO-OP on its own (reconcile is additive-only; the "
            f"live config needs explicit removal too).")
    unknown = [c for c in raw if c not in CLIS]
    if unknown:
        raise ReconcileReadError(
            f"[mcp_servers.{name}] clis names {unknown}, which this repo does not "
            f"configure (known: {list(CLIS)}). Refusing rather than guessing — including "
            f"would add the server to the CLI the gate was written to withhold it from, "
            f"and excluding would withhold it everywhere with nothing to read as an error.")
    return tuple(raw)


def desired_mcp(caps: dict, cli: str) -> dict:
    """name -> spec, platform- and CLI-gated, including the provider docs server."""
    out = {}
    hosts = host_platforms()
    for name, spec in caps.get("mcp_servers", {}).items():
        # Validated BEFORE the platform skip so a malformed gate refuses on every host,
        # not only on the ones that would have reached the server anyway.
        clis = mcp_clis(name, spec)
        plat = spec.get("platform")
        if plat and plat not in hosts:
            continue
        if cli not in clis:
            continue
        out[name] = spec
    docs = caps.get("docs_mcp", {}).get(cli)
    if docs:
        out[docs["name"]] = {k: v for k, v in docs.items() if k != "name"}
    return out


def is_http(spec: dict) -> bool:
    return spec.get("transport") == "http" or (spec.get("url") and not spec.get("command"))


# ----- live state: MCP -------------------------------------------------------
def claude_mcp_path() -> Path:
    return Path(expand("${HOME}/.claude.json"))


def claude_mcp_load() -> dict:
    """User-scope MCP servers, exactly where `claude mcp add --scope user` writes
    them. This is the structured source of truth — `claude mcp list` renders a
    lossy one-line summary (args space-joined, multi-line args split across
    lines), which cannot be compared field-by-field against capabilities.toml."""
    servers = read_json_object(claude_mcp_path()).get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def claude_mcp_listed() -> dict:
    """Names `claude mcp list` reports, for presence only. Covers servers that
    live outside ~/.claude.json (plugin-provided, claude.ai-managed) so they are
    still reported as EXTRA. Endpoint text is NOT used for drift comparison."""
    try:
        res = run(["claude", "mcp", "list"])
    except (OSError, subprocess.SubprocessError):
        return {}
    cur = {}
    for line in res.stdout.splitlines():
        line = line.rstrip()
        # Everything below this header is advice/warnings ("Location: …",
        # "For help configuring MCP servers, see: …") — all of which contain
        # ": " and were being reported as EXTRA servers that do not exist.
        if line.startswith("MCP config diagnostics"):
            break
        if not line or line.startswith("Checking") or ": " not in line:
            continue
        name, rest = line.split(": ", 1)
        endpoint = rest.rsplit(" - ", 1)[0].strip() if " - " in rest else rest.strip()
        # `claude mcp list` appends a transport annotation like " (HTTP)"/" (SSE)".
        endpoint = re.sub(r"\s*\((?:HTTP|SSE|STDIO|stdio)\)\s*$", "", endpoint)
        cur[name.strip()] = {"endpoint": endpoint}
    return cur


def claude_mcp_current() -> dict:
    cur = {name: dict(spec) for name, spec in claude_mcp_load().items()}
    for name, entry in claude_mcp_listed().items():
        cur.setdefault(name, entry)
    return cur


def codex_config_path() -> Path:
    return Path(expand("${HOME}/.codex/config.toml"))


def codex_load() -> dict:
    p = codex_config_path()
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def codex_mcp_current() -> dict:
    return codex_load().get("mcp_servers", {})


def agy_mcp_path() -> Path:
    return Path(expand("${HOME}/.gemini/config/mcp_config.json"))


def agy_mcp_load() -> dict:
    """Delegates so the refusal above is not one reader's private rule. This function had its
    own copy of the absent/empty/decode-error logic, and `apply_mcp` rebuilds the whole file
    from what it returns — so an unparseable mcp_config.json was replaced by khenrix's servers
    alone, by the same route and one function over."""
    return read_json_object(agy_mcp_path())


def agy_mcp_current() -> dict:
    return agy_mcp_load().get("mcpServers", {})


def mcp_current(cli: str) -> dict:
    return {"claude": claude_mcp_current, "codex": codex_mcp_current, "agy": agy_mcp_current}[cli]()


def mcp_live(cur: dict) -> dict:
    """Normalise one backend's live MCP entry into a common shape.

    The three CLIs spell the same fields differently — claude/codex use `url`,
    agy uses `httpUrl` — but the *comparison* must not differ per CLI, or a
    backend can silently skip a check. Normalise here, compare once below."""
    return {"url": cur.get("url") or cur.get("httpUrl"),
            "command": cur.get("command"),
            "args": list(cur.get("args") or [])}


def mcp_drift(spec: dict, cur: dict) -> str | None:
    """Drift between a declared spec and the live entry, or None if in sync.

    Deliberately takes no `cli`: the comparison is identical for every backend,
    and not receiving the CLI makes re-introducing a per-CLI skip impossible.
    Note also the absence of `and have` / `and hc` guards — a live entry MISSING
    the field we declared is drift (the declared transport is not what is
    installed), not a match. Guarding on it reported ✅ MATCH for a server that
    was plainly wrong."""
    spec = expand(spec)
    have = mcp_live(cur)
    if is_http(spec):
        want = spec.get("url")
        if want and want != have["url"]:
            return f"url: {have['url']} → {want}"
        return None
    wc, wa = spec.get("command"), list(spec.get("args", []))
    if wc and (wc != have["command"] or wa != have["args"]):
        return "command/args differ"
    return None


def classify_mcp(desired: dict, current: dict):
    rows, extras = [], [n for n in current if n not in desired]
    for name, spec in desired.items():
        if name in current:
            d = mcp_drift(spec, current[name])
            rows.append([name, "UPDATE" if d else "MATCH", d or ""])
        else:
            rows.append([name, "ADD", "will add"])
    return rows, extras


# ----- live state: settings --------------------------------------------------
def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return toml_scalar(v)
    if isinstance(v, list):
        return toml_array(v)
    raise TypeError(f"unsupported TOML value: {v!r}")


def toml_table_bounds(lines: list[str], table: str) -> tuple[int, int] | None:
    header = f"[{table}]"
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*\[[^\]]+\]\s*(?:#.*)?$", line):
            if line.split("#", 1)[0].strip() == header:
                start = i
                break
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^\s*\[[^\]]+\]\s*(?:#.*)?$", lines[i]):
            end = i
            break
    return start, end


def set_toml_table_key(text: str, table: str, key: str, value) -> str:
    rendered = f"{key} = {toml_value(value)}\n"
    lines = text.splitlines(keepends=True)
    bounds = toml_table_bounds(lines, table)
    if bounds is None:
        subtable_re = re.compile(rf"^\s*\[{re.escape(table)}\.")
        for i, line in enumerate(lines):
            if subtable_re.match(line):
                block = [f"[{table}]\n", rendered, "\n"]
                lines[i:i] = block
                return "".join(lines)
        sep = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        return text + sep + f"[{table}]\n{rendered}"

    start, end = bounds
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            lines[i] = rendered
            return "".join(lines)

    insert_at = end
    if insert_at > start + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    lines.insert(insert_at, rendered)
    return "".join(lines)


def settings_report(cli: str, caps: dict):
    """Return (rows, apply_fn). apply_fn(update_drift) -> list[str] of actions."""
    want = caps.get("settings", {})
    if cli == "codex":
        return codex_settings(want)
    if cli == "agy":
        return agy_settings(want)
    return claude_settings(want)


def desired_statusline(want: dict, cli: str) -> dict | None:
    spec = want.get(cli, {}).get("statusLine")
    return expand(spec) if isinstance(spec, dict) else None


def statusline_asset_report(cli: str, caps: dict):
    spec = desired_statusline(caps.get("settings", {}), cli)
    if not spec:
        return [], (lambda update_drift: [])
    command = spec.get("command")
    dest = shell_command_executable(command) if isinstance(command, str) else None
    src = caps["_dir"] / "statusline" / "khenrix-statusline"
    if not dest:
        return [["statusline executable", "INFO", "command path could not be parsed"]], (lambda update_drift: [])
    if not src.exists():
        return [[str(src), "INFO", "source asset missing"]], (lambda update_drift: [])
    same = dest.exists() and dest.read_bytes() == src.read_bytes()
    if same:
        rows = [[str(dest), "MATCH", "installed"]]
        status = "MATCH"
    elif dest.exists():
        rows = [[str(dest), "UPDATE", "installed renderer drifted"]]
        status = "UPDATE"
    else:
        rows = [[str(dest), "ADD", "install statusline renderer"]]
        status = "ADD"

    def apply(update_drift):
        if status == "MATCH":
            return []
        if status == "UPDATE" and not update_drift:
            return [f"statusline asset: {dest} drifted (skipped; use --update-drift)"]
        backup(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        dest.chmod(dest.stat().st_mode | 0o755)
        return [f"statusline asset: installed renderer to {dest}"]

    return rows, apply


def hook_asset_report(cli: str, caps: dict):
    """Install any bundled hook scripts declared under [settings.claude.hooks.<event>] to
    ~/.claude/hooks/ (the settings.json stanza that points at them is written by claude_settings,
    exactly as the statusline binary is installed here and its path configured by the settings).
    Claude-only for now."""
    if cli != "claude":
        return [], (lambda update_drift: [])
    hooks = ((caps.get("settings", {}).get("claude") or {}).get("hooks") or {})
    rows, todo = [], []
    for event, spec in hooks.items():
        script = spec.get("script")
        if not script:
            continue
        src = caps["_dir"] / script
        dest = Path(claude_hook_command(script))
        if not src.exists():
            rows.append([str(src), "INFO", "source hook missing"]); continue
        if dest.exists() and dest.read_bytes() == src.read_bytes():
            rows.append([str(dest), "MATCH", f"{event} hook installed"]); continue
        status = "UPDATE" if dest.exists() else "ADD"
        rows.append([str(dest), status, f"install {event} hook script"])
        todo.append((status, src, dest))

    def apply(update_drift):
        out = []
        for status, src, dest in todo:
            if status == "UPDATE" and not update_drift:
                out.append(f"hook asset: {dest} drifted (skipped; use --update-drift)"); continue
            backup(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            dest.chmod(dest.stat().st_mode | 0o755)
            out.append(f"hook asset: installed {dest}")
        return out

    return rows, apply


def codex_settings(want: dict):
    cfg = codex_load()
    rows, todo = [], []
    checks = [("approval_policy", want.get("approval_policy")),
              ("sandbox_mode", want.get("sandbox"))]
    for key, val in checks:
        if val is None:
            continue
        have = cfg.get(key)
        if have == val:
            rows.append([key, "MATCH", val])
        elif have is None:
            rows.append([key, "ADD", val]); todo.append((key, val))
        else:
            rows.append([key, "UPDATE", f"{have} → {val}"])
    projects = cfg.get("projects", {})
    for path in expand(want.get("trusted_paths", [])):
        trusted = projects.get(path, {}).get("trust_level") == "trusted"
        if trusted:
            rows.append([f"trust {path}", "MATCH", "trusted"])
        else:
            rows.append([f"trust {path}", "ADD", "trusted"]); todo.append(("trust", path))

    tui_want = want.get("codex", {}).get("tui", {})
    tui_have = cfg.get("tui", {})
    for key, val in tui_want.items():
        have = tui_have.get(key)
        row_key = f"tui.{key}"
        if have == val:
            rows.append([row_key, "MATCH", toml_value(val)])
        elif have is None:
            rows.append([row_key, "ADD", toml_value(val)])
            todo.append(("tui", key, val, "ADD"))
        else:
            rows.append([row_key, "UPDATE", f"{toml_value(have)} → {toml_value(val)}"])
            todo.append(("tui", key, val, "UPDATE"))

    def apply(update_drift):
        pending = [t for t in todo if len(t) != 4 or t[3] == "ADD" or update_drift]
        if not pending:
            return []
        p = codex_config_path()
        backup(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        chunks = []
        tui_updates = []
        skipped_tui_updates = []
        for item in todo:
            if len(item) == 4:
                kind, key, val, status = item
                if status == "UPDATE" and not update_drift:
                    skipped_tui_updates.append(key)
                    continue
                tui_updates.append((key, val))
                continue
            kind, a = item
            if kind == "trust":
                chunks.append(f'\n[projects."{a}"]\ntrust_level = "trusted"\n')
            else:
                chunks.append(f'{kind} = "{a}"\n')
        # top-level keys must precede any table; prepend simple keys, append tables.
        simple = "".join(c for c in chunks if not c.lstrip().startswith("["))
        tables = "".join(c for c in chunks if c.lstrip().startswith("["))
        text = p.read_text() if p.exists() else ""
        if simple:
            text = simple + text
        text = text + tables
        for key, val in tui_updates:
            text = set_toml_table_key(text, "tui", key, val)
        p.write_text(text)
        actions = [f"codex settings: wrote {len(pending)} key(s) to {p}"]
        if skipped_tui_updates:
            actions.append(
                "codex settings: skipped drifted TUI key(s) "
                + ", ".join(skipped_tui_updates)
                + " (use --update-drift)"
            )
        return actions

    return rows, apply


def agy_settings_path() -> Path:
    return Path(expand("${HOME}/.gemini/antigravity-cli/settings.json"))


def agy_keybindings_path() -> Path:
    return Path(expand("${HOME}/.gemini/antigravity-cli/keybindings.json"))


def statusline_config_rows(data: dict, want: dict | None):
    if not want:
        return [], None
    have = data.get("statusLine")
    if have == want:
        return [["statusLine", "MATCH", want.get("command", "")]], None
    if have is None:
        return [["statusLine", "ADD", want.get("command", "")]], ("ADD", want)
    return [["statusLine", "UPDATE", f"{have!r} → {want!r}"]], ("UPDATE", want)


def agy_settings(want: dict):
    p = agy_settings_path()
    data = read_json_object(p)
    rows, missing = [], []
    tw = data.get("trustedWorkspaces", [])
    for path in expand(want.get("trusted_paths", [])):
        if path in tw:
            rows.append([f"trust {path}", "MATCH", "trusted"])
        else:
            rows.append([f"trust {path}", "ADD", "trusted"]); missing.append(path)
    status_rows, status_todo = statusline_config_rows(data, desired_statusline(want, "agy"))
    rows.extend(status_rows)

    kp = agy_keybindings_path()
    key_data = read_json_object(kp)
    agy_want = want.get("agy", {})
    key_want = expand(agy_want.get("keybindings", {}))
    cleanup = list(agy_want.get("keybinding_cleanup", []))
    key_todo = []
    for key, val in key_want.items():
        have = key_data.get(key)
        row_key = f"keybindings.{key}"
        if have == val:
            rows.append([row_key, "MATCH", ", ".join(val)])
        elif have is None:
            rows.append([row_key, "ADD", ", ".join(val)])
            key_todo.append(("set", "ADD", key, val))
        else:
            rows.append([row_key, "UPDATE", f"{have!r} → {val!r}"])
            key_todo.append(("set", "UPDATE", key, val))
    cleanup_present = [key for key in cleanup if key in key_data]
    if cleanup_present:
        rows.append(["keybindings cleanup", "UPDATE", "remove unsupported mode aliases"])
        key_todo.extend(("remove", "UPDATE", key, None) for key in cleanup_present)
    elif cleanup:
        rows.append(["keybindings cleanup", "MATCH", "no unsupported mode aliases"])
    rows.append(["approval/sandbox", "INFO", "agy prompts per-action; no static key"])

    def apply(update_drift):
        actions = []
        settings_changed = False
        skipped = []
        if missing:
            data.setdefault("trustedWorkspaces", [])
            data["trustedWorkspaces"].extend(m for m in missing if m not in data["trustedWorkspaces"])
            settings_changed = True
        if status_todo:
            status, val = status_todo
            if status == "ADD" or update_drift:
                data["statusLine"] = val
                settings_changed = True
            else:
                skipped.append("statusLine")
        if settings_changed:
            backup(p)
            write_json_object(p, data)
            actions.append(f"agy settings: updated {p}")
        if skipped:
            actions.append("agy settings: skipped drifted key(s) " + ", ".join(skipped) + " (use --update-drift)")

        key_changed = False
        key_skipped = []
        for action, status, key, val in key_todo:
            if status == "ADD" or update_drift:
                if action == "set":
                    key_data[key] = val
                elif action == "remove":
                    key_data.pop(key, None)
                key_changed = True
            else:
                key_skipped.append(key)
        if key_changed:
            backup(kp)
            write_json_object(kp, key_data)
            actions.append(f"agy keybindings: updated {kp}")
        if key_skipped:
            actions.append(
                "agy keybindings: skipped drifted key(s) "
                + ", ".join(key_skipped)
                + " (use --update-drift)"
            )
        return actions

    return rows, apply


def claude_settings_path() -> Path:
    return Path(expand("${HOME}/.claude/settings.json"))


def claude_hook_command(script) -> str:
    """Installed path of a bundled hook script → ~/.claude/hooks/<basename> (the command the
    settings.json stanza points at; the file itself is installed by hook_asset_report)."""
    return expand("${HOME}/.claude/hooks/" + Path(str(script)).name)


def claude_has_hook(data: dict, event: str, command: str) -> bool:
    for group in (data.get("hooks", {}) or {}).get(event, []) or []:
        if any(h.get("command") == command for h in (group.get("hooks", []) or [])):
            return True
    return False


def _fmt(val) -> str:
    return json.dumps(val) if isinstance(val, (dict, list)) else str(val)


def claude_settings(want: dict):
    p = claude_settings_path()
    data = read_json_object(p)
    rows, status_todo = statusline_config_rows(data, desired_statusline(want, "claude"))
    cw = want.get("claude") or {}
    key_todo = []      # (key, value) baseline keys to add ONLY when absent
    hook_todo = []     # (event, command) hook stanzas to register when the event has none yet
    for key, val in cw.items():
        if key == "statusLine":
            continue   # configured above (statusline_config_rows) + installed by the asset report
        if key == "hooks":
            for event, spec in (val or {}).items():
                cmd = claude_hook_command(spec.get("script"))
                if claude_has_hook(data, event, cmd):
                    rows.append([f"hooks.{event}", "MATCH", cmd])
                elif (data.get("hooks", {}) or {}).get(event):
                    rows.append([f"hooks.{event}", "INFO", f"kept your existing {event} hook(s)"])
                else:
                    rows.append([f"hooks.{event}", "ADD", cmd]); hook_todo.append((event, cmd))
            continue
        val = expand(val)
        have = data.get(key)
        if have == val:
            rows.append([key, "MATCH", _fmt(val)])
        elif key in data:
            rows.append([key, "INFO", f"kept your {key} (baseline {_fmt(val)})"])
        else:
            rows.append([key, "ADD", _fmt(val)]); key_todo.append((key, val))
    rows.append(["approval/sandbox", "INFO", "Claude uses permissions/--permission-mode, not static keys"])
    if want.get("trusted_paths"):
        rows.append(["trusted_paths", "INFO", "Claude trusts on first run; not reconciled here"])

    def apply(update_drift):
        actions, changed = [], False
        if status_todo:
            status, val = status_todo
            if status == "UPDATE" and not update_drift:
                actions.append(f"claude settings: {p} statusLine drifted (skipped; use --update-drift)")
            else:
                data["statusLine"] = val; changed = True
                actions.append("claude settings: set statusLine")
        for key, val in key_todo:
            data[key] = val; changed = True
        if key_todo:
            actions.append("claude settings: added " + ", ".join(k for k, _ in key_todo))
        for event, cmd in hook_todo:
            data.setdefault("hooks", {}).setdefault(event, []).append(
                {"hooks": [{"type": "command", "command": cmd}]})
            changed = True
            actions.append(f"claude settings: registered {event} hook")
        if changed:
            backup(p)
            write_json_object(p, data)
        return actions

    return rows, apply


# ----- live state: shell aliases --------------------------------------------
def shell_alias_block(caps: dict) -> str:
    entries = caps.get("shell_aliases", {}).get("entries", {})
    lines = [
        ALIASES_BEGIN,
        "# Generated by khenrix-utils from capabilities.toml.",
    ]
    for name, cmd in entries.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", name):
            raise ValueError(f"invalid shell alias name: {name}")
        lines.append(f"alias {name}={shlex.quote(expand(cmd))}")
    lines.append(ALIASES_END)
    return "\n".join(lines)


def shell_aliases_report(caps: dict):
    aliases = caps.get("shell_aliases")
    if not aliases:
        return [], (lambda update_drift: [])
    target = Path(expand(aliases["target"]))
    block = shell_alias_block(caps)
    cur = target.read_text() if target.exists() else ""
    i, j = cur.find(ALIASES_BEGIN), cur.find(ALIASES_END)
    present = i != -1 and j != -1
    if present and cur[i:j + len(ALIASES_END)] == block:
        rows = [[str(target), "MATCH", "aliases up to date"]]
    elif present:
        rows = [[str(target), "UPDATE", "alias block drifted"]]
    else:
        names = ", ".join(aliases.get("entries", {}).keys())
        rows = [[str(target), "ADD", f"insert aliases: {names}"]]

    def apply(update_drift):
        new = cur
        ni, nj = new.find(ALIASES_BEGIN), new.find(ALIASES_END)
        if ni != -1 and nj != -1:
            if new[ni:nj + len(ALIASES_END)] == block:
                return []
            if not update_drift:
                return [f"shell aliases: {target} block drifted (skipped; use --update-drift)"]
            new = new[:ni] + block + new[nj + len(ALIASES_END):]
        else:
            sep = "" if new == "" else ("\n" if new.endswith("\n") else "\n\n")
            new = new + sep + block + "\n"
        backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new)
        return [f"shell aliases: updated managed aliases in {target}"]

    return rows, apply


# ----- live state: instructions ----------------------------------------------
def managed_block(caps: dict, cli: str | None = None) -> str:
    src = caps["_dir"] / caps["instructions"]["source"]
    text = src.read_text()
    i, j = text.find(MANAGED_BEGIN), text.find(MANAGED_END)
    if i == -1 or j == -1:
        # no markers in source → inject them, so the result is always a managed block
        # (keeps reconcile idempotent even if house-style.md ever loses its markers)
        body, end = MANAGED_BEGIN + "\n" + text.strip() + "\n", MANAGED_END
    else:
        body, end = text[i:j], text[j:j + len(MANAGED_END)]
    # per-CLI overlay: appended INSIDE the managed markers for the matching CLI only
    overlay_fn = (caps["instructions"].get("overlays") or {}).get(cli) if cli else None
    if overlay_fn:
        ov = (caps["_dir"] / overlay_fn).read_text().strip()
        body = body.rstrip() + "\n\n" + ov + "\n"
    return body + end


def instructions_report(cli: str, caps: dict):
    target = Path(expand(caps["instructions"]["targets"][cli]))
    block = managed_block(caps, cli)
    cur = target.read_text() if target.exists() else ""
    i, j = cur.find(MANAGED_BEGIN), cur.find(MANAGED_END)
    # AN ORPHANED MARKER IS REFUSED, NEVER GUESSED AT, and it DESTROYED user text before
    # this check existed. Reproduced in two steps: a file holding a BEGIN with no END (a
    # truncated write, a hand edit, a bad merge) reads as `present = False`, so the ADD
    # branch APPENDS a whole block below it. The next run then finds the ORPHAN's BEGIN and
    # the NEW block's END, treats everything between as the managed span, and replaces it —
    # taking every line the user had written in between with it.
    #
    # Reconcile's stated invariant is that it never removes machine-specific config, so the
    # only safe answer to "these markers do not pair" is to say so and change nothing.
    if (i == -1) != (j == -1):
        which = "begin" if i != -1 else "end"
        rows = [[str(target), "REFUSED",
                 f"a khenrix-managed {which} marker has no partner — this file's managed "
                 "span cannot be identified, and guessing at it would replace whatever sits "
                 "between the orphan and the next marker. Repair the markers by hand."]]
        return rows, (lambda update_drift: [
            f"instructions: {target} has an unpaired khenrix-managed marker; nothing written"])
    if i != -1 and j < i:
        rows = [[str(target), "REFUSED",
                 "the khenrix-managed end marker precedes its begin marker — the span is "
                 "not readable and replacing it would delete the file's own content"]]
        return rows, (lambda update_drift: [
            f"instructions: {target} has inverted khenrix-managed markers; nothing written"])
    present = i != -1 and j != -1
    if present and cur[i:j + len(MANAGED_END)] == block:
        rows = [[str(target), "MATCH", "house-style block up to date"]]
    elif present:
        rows = [[str(target), "UPDATE", "house-style block drifted"]]
    else:
        rows = [[str(target), "ADD", "insert house-style block"]]

    def apply(update_drift):
        new = cur
        ni, nj = new.find(MANAGED_BEGIN), new.find(MANAGED_END)
        if ni != -1 and nj != -1:
            if new[ni:nj + len(MANAGED_END)] == block:
                return []
            if not update_drift:
                return [f"instructions: {target} block drifted (skipped; use --update-drift)"]
            new = new[:ni] + block + new[nj + len(MANAGED_END):]
        else:
            sep = "" if new == "" else ("\n" if new.endswith("\n") else "\n\n")
            new = new + sep + block + "\n"
        backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new)
        return [f"instructions: updated house-style block in {target}"]

    return rows, apply


# ----- MCP apply -------------------------------------------------------------
def toml_scalar(s: str) -> str:
    if "\\" in s or '"' in s:
        return "'''" + s + "'''"
    return '"' + s + '"'


def toml_array(a) -> str:
    return "[" + ", ".join(toml_scalar(x) for x in a) + "]"


def apply_mcp(cli: str, name: str, spec: dict) -> str:
    spec = expand(spec)
    if cli == "claude":
        if is_http(spec):
            cmd = ["claude", "mcp", "add", "--transport", "http", "--scope", "user", name, spec["url"]]
        else:
            cmd = ["claude", "mcp", "add", "--scope", "user", name]
            for k, v in (spec.get("env") or {}).items():
                cmd += ["-e", f"{k}={v}"]
            cmd += ["--", spec["command"], *spec.get("args", [])]
        res = run(cmd)
        return f"claude mcp add {name}: {'ok' if res.returncode == 0 else res.stderr.strip()}"
    if cli == "codex":
        p = codex_config_path()
        backup(p)
        lines = [f"\n[mcp_servers.{name}]"]
        if is_http(spec):
            lines.append(f"url = {toml_scalar(spec['url'])}")
        else:
            lines.append(f"command = {toml_scalar(spec['command'])}")
            if spec.get("args"):
                lines.append(f"args = {toml_array(spec['args'])}")
        env = spec.get("env") or {}
        if env:
            lines.append(f"\n[mcp_servers.{name}.env]")
            for k, v in env.items():
                lines.append(f"{k} = {toml_scalar(v)}")
        with open(p, "a") as f:
            f.write("\n".join(lines) + "\n")
        return f"codex: appended [mcp_servers.{name}] to {p}"
    # agy
    p = agy_mcp_path()
    backup(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = agy_mcp_load()
    if is_http(spec):
        entry = {"httpUrl": spec["url"]}
    else:
        entry = {"command": spec["command"]}
        if spec.get("args"):
            entry["args"] = spec["args"]
        if spec.get("env"):
            entry["env"] = spec["env"]
    data.setdefault("mcpServers", {})[name] = entry
    p.write_text(json.dumps(data, indent=2) + "\n")
    return f"agy: added mcpServers.{name} to {p}"


# ----- reporting -------------------------------------------------------------
def print_rows(title: str, rows, extras=None):
    print(f"\n{title}")
    if not rows:
        print("  (nothing declared)")
    for name, status, detail in rows:
        print(f"  {MARK.get(status, status):<3} {status:<7} {name}" + (f"  — {detail}" if detail else ""))
    for e in (extras or []):
        print(f"  {MARK['EXTRA']} EXTRA   {e}  — not managed by khenrix (left untouched)")


def reconcile(cli: str, caps: dict, apply: bool, update_drift: bool):
    print(f"\n=== khenrix-setup · {cli} ===")
    desired = desired_mcp(caps, cli)
    cur = mcp_current(cli)
    mcp_rows, extras = classify_mcp(desired, cur)
    print_rows("MCP servers:", mcp_rows, extras)

    set_rows, set_apply = settings_report(cli, caps)
    print_rows("Settings:", set_rows)

    asset_rows, asset_apply = statusline_asset_report(cli, caps)
    print_rows("Statusline assets:", asset_rows)

    hook_rows, hook_apply = hook_asset_report(cli, caps)
    print_rows("Hook assets:", hook_rows)

    alias_rows, alias_apply = shell_aliases_report(caps)
    print_rows("Shell aliases:", alias_rows)

    ins_rows, ins_apply = instructions_report(cli, caps)
    print_rows("Base instructions:", ins_rows)

    if not apply:
        adds = sum(1 for r in mcp_rows + set_rows + asset_rows + hook_rows + alias_rows + ins_rows if r[1] == "ADD")
        drift = sum(1 for r in mcp_rows + set_rows + asset_rows + hook_rows + alias_rows + ins_rows if r[1] == "UPDATE")
        print(f"\nReview only. {adds} to add, {drift} drifted. "
              f"Re-run with --apply to add missing entries.")
        return

    print("\nApplying (additive)…")
    actions = []
    for name, status, _ in mcp_rows:
        if status == "ADD":
            actions.append(apply_mcp(cli, name, desired[name]))
        elif status == "UPDATE" and update_drift:
            actions.append("drift update for MCP not auto-applied; edit manually: " + name)
    actions += set_apply(update_drift)
    actions += asset_apply(update_drift)
    actions += hook_apply(update_drift)
    actions += alias_apply(update_drift)
    actions += ins_apply(update_drift)
    if actions:
        for a in actions:
            print(f"  • {a}")
    else:
        print("  • nothing to do — already in sync")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reconcile a CLI's config toward capabilities.toml")
    ap.add_argument("--cli", choices=CLIS)
    ap.add_argument("--all", action="store_true", help="operate on every CLI (read-only unless --apply is also given)")
    ap.add_argument("--status", action="store_true", help="read-only review (default)")
    ap.add_argument("--apply", action="store_true", help="add missing declared entries")
    ap.add_argument("--update-drift", action="store_true", help="also re-apply drifted managed entries")
    args = ap.parse_args(argv)

    caps = load_caps()
    if args.all or not args.cli:
        # --apply/--update-drift must propagate; hardcoding False here made
        # `reconcile.py --apply --all` a silent no-op (bootstrap-machine.sh:88).
        eff_apply = args.apply and not args.status
        for c in CLIS:
            reconcile(c, caps, apply=eff_apply, update_drift=args.update_drift)
        return 0
    reconcile(args.cli, caps, apply=args.apply and not args.status, update_drift=args.update_drift)
    return 0


if __name__ == "__main__":
    sys.exit(main())
