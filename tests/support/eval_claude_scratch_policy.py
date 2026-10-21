"""Opt-in Claude fixture execution over a condition-local, kernel-fenced MCP tool.

Only commands sent to this tool are fenced. Native tool-catalog qualification is
separate; this is not confinement of the Claude application or its own metadata.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import stat
import subprocess
import sys
import time

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if __name__ == "__main__" and sys.flags.isolated:
    sys.path.insert(0, str(SCRIPTS))
from eval_agy_read_policy import regular_bytes

CLI_VERSION = "2.1.302"
TOOL_NAME = "mcp__scratch__run"
INPUT_SCHEMA = {"type": "object", "properties": {
    "argv": {"type": "array", "minItems": 1, "maxItems": 256,
             "items": {"type": "string", "minLength": 1, "maxLength": 65536}},
    "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000}},
    "required": ["argv"], "additionalProperties": False}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _reference(path):
    return {"path": str(path), "sha256": _sha(regular_bytes(path))}


def _directory_identity(path):
    if not path.is_absolute() or any(c in str(path) for c in "*?[]{}"):
        raise ValueError("owned private canonical scratch directory required")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if (path.resolve(strict=True) != path or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("owned private canonical scratch directory required")
        return {"device": info.st_dev, "inode": info.st_ino,
                "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)}
    except OSError as error:
        raise ValueError("owned private canonical scratch directory required") from error
    finally:
        os.close(descriptor)


def _write(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
    return _reference(path)


def _json(data):
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()


def prepare(cwd, controls, *, home=None, environment=None):
    """Prepare a fresh private MCP executor without changing native auth/settings."""
    if sys.platform != "darwin":
        raise ValueError("scratch execution requires macOS sandbox-exec")
    cwd, controls = Path(cwd), Path(controls)
    home = Path.home() if home is None else Path(home)
    environment = os.environ if environment is None else environment
    cwd_identity = _directory_identity(cwd)
    if (controls.resolve() != controls or cwd == controls or cwd in controls.parents):
        raise ValueError("canonical controls must be outside the writable cwd")
    controls.mkdir(parents=True, mode=0o700)
    controls.chmod(0o700)
    temp = cwd / "t"
    temp.mkdir(mode=0o700)
    binary = shutil.which("claude", path=environment.get("PATH"))
    if binary is None:
        raise ValueError("licensed Claude executable is unavailable")
    # Exec may replace the program image; fork denial prevents detached children
    # from outliving it. Native-managed sandbox settings do not supply this profile.
    sbpl = ('(version 1)\n(deny default)\n(allow process-exec)\n(deny process-fork)\n(allow sysctl-read)\n'
            '(allow file-read*)\n(allow file-map-executable)\n'
            '(allow file-write* (subpath ' + json.dumps(str(cwd)) + ')'
            ' (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))\n')
    refs = {"sbpl": _write(controls / "scratch.sb", sbpl.encode()),
            "handler": _reference(Path(__file__).resolve()),
            "dependency": _reference(SCRIPTS / "eval_agy_read_policy.py"),
            "sandbox": _reference(Path("/usr/bin/sandbox-exec")),
            "launcher": _reference(Path("/usr/bin/env")),
            "python": _reference(Path(sys.executable).resolve()),
            "executable": _reference(Path(binary).resolve(strict=True))}
    child_environment = {"HOME": str(home), "PATH": environment.get("PATH", os.defpath),
                         "TMPDIR": str(temp), "LC_ALL": "C.UTF-8"}
    policy = {"schema_version": 2, "cwd": str(cwd), "home": str(home),
              "directory_identity": {"cwd": cwd_identity, "temp": _directory_identity(temp)},
              "temp": str(temp), "references": refs, "environment": child_environment,
              "hook_log": str(controls / "scratch-tool-events.jsonl")}
    _write(Path(policy["hook_log"]), b"")
    policy_ref = _write(controls / "scratch-policy.json", _json(policy))
    args = ["-i", *[key + "=" + value for key, value in child_environment.items()],
            refs["python"]["path"], "-I", "-B", refs["handler"]["path"], "serve",
            policy_ref["path"], policy_ref["sha256"], refs["handler"]["sha256"]]
    mcp = {"mcpServers": {"scratch": {"type": "stdio", "command": "/usr/bin/env", "args": args}}}
    return {"schema_version": 2, "cwd": str(cwd), "home": str(home),
            "policy": policy_ref, "mcp_config": _write(controls / "scratch-mcp.json", _json(mcp)),
            "executable": refs["executable"], "environment": {}, "tool_name": TOOL_NAME,
            "cli_version": CLI_VERSION, "native_enforcement_verified": False}


def _load_policy(reference):
    raw = regular_bytes(reference["path"])
    if _sha(raw) != reference["sha256"]:
        raise ValueError("scratch policy drift")
    policy = json.loads(raw)
    for name, path in (("cwd", Path(policy["cwd"])), ("temp", Path(policy["temp"]))):
        if _directory_identity(path) != policy["directory_identity"][name]:
            raise ValueError("scratch directory identity drift")
    for reference in policy["references"].values():
        if _reference(Path(reference["path"])) != reference:
            raise ValueError("scratch executable/control drift")
    regular_bytes(policy["hook_log"])
    return policy


def check(context, *, environment=None):
    environment = os.environ if environment is None else environment
    policy = _load_policy(context["policy"])
    if _reference(Path(context["mcp_config"]["path"])) != context["mcp_config"]:
        raise ValueError("scratch MCP config drift")
    selected = shutil.which("claude", path=environment.get("PATH"))
    if selected is None or str(Path(selected).resolve(strict=True)) != context["executable"]["path"]:
        raise ValueError("Claude executable selection drift")
    if context["cwd"] != policy["cwd"] or context["home"] != policy["home"]:
        raise ValueError("scratch context drift")


def bind(spec, context, environment):
    """Keep installed Skill/Read; execution is only the dedicated MCP tool."""
    check(context, environment=environment)
    if (spec.name != "claude" or str(spec.cwd) != context["cwd"]
            or Path(environment.get("HOME", str(Path.home()))) != Path(context["home"])
            or spec.registry._version_probe("claude") != context["cli_version"]):
        raise ValueError("Claude scratch provider/cwd/version mismatch")
    extra = ("--tools", "Skill,Read", "--strict-mcp-config", "--mcp-config", context["mcp_config"]["path"])
    profile = replace(spec.profile, initial_argv=spec.profile.initial_argv + extra,
                      resume_argv=spec.profile.resume_argv + extra, profile_sha256=None)
    request = replace(spec.native_request, profile=profile)
    command = profile.adapter.build_command(request)
    return type(spec)(spec.name, list(command.argv), command.stdin,
        spec.model, spec.thinking, spec.cwd, spec.role, profile, spec.registry, request)


def _arguments(arguments):
    if (not isinstance(arguments, dict) or not {"argv"} <= arguments.keys()
            or not arguments.keys() <= {"argv", "timeout_ms"}
            or not isinstance(arguments["argv"], list) or not 1 <= len(arguments["argv"]) <= 256
            or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in arguments["argv"])
            or sum(len(arg.encode()) for arg in arguments["argv"]) > 65536):
        raise ValueError("unsupported scratch command arguments")
    timeout = arguments.get("timeout_ms", 30000)
    if type(timeout) is not int or not 100 <= timeout <= 30000:
        raise ValueError("unsupported scratch command timeout")
    return arguments["argv"], timeout / 1000


def _execute(policy, command_argv, timeout):
    refs = policy["references"]
    argv = [refs["sandbox"]["path"], "-f", refs["sbpl"]["path"], *command_argv]
    output = bytearray()
    timed_out, truncated = False, False
    with subprocess.Popen(argv, cwd=policy["cwd"], env=policy["environment"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT) as child:
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fd)
                        break
                    if len(output) + len(chunk) > 131072:
                        truncated = True
                        break
                    output.extend(chunk)
                if truncated:
                    break
        if not timed_out and not truncated:
            try:
                child.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        if child.poll() is None:
            child.kill()
        child.wait()
    return {"exit_code": child.returncode, "output": output.decode("utf-8", "replace"),
            "timed_out": timed_out, "truncated": truncated}


def call_tool(context, arguments, *, request_id):
    """Validate first; every executable command is wrapped by the kernel fence."""
    policy = _load_policy(context["policy"])
    decision = "deny"
    try:
        command_argv, timeout = _arguments(arguments)
        decision = "execute"
        result = _execute(policy, command_argv, timeout)
    except ValueError as error:
        result = {"exit_code": None, "error": str(error)}
    event = {"request_id": request_id, "decision": decision,
             "arguments_sha256": _sha(json.dumps(arguments, sort_keys=True).encode()),
             "result_sha256": _sha(_json(result)), "exit_code": result.get("exit_code")}
    descriptor = os.open(policy["hook_log"], os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")
    _load_policy(context["policy"])
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": result.get("exit_code") != 0 or result.get("timed_out", False)
                       or result.get("truncated", False)}


def serve(path, policy_sha, handler_sha):
    context = {"policy": {"path": path, "sha256": policy_sha}}
    if _sha(regular_bytes(Path(__file__).resolve())) != handler_sha:
        raise ValueError("scratch server drift")
    _load_policy(context["policy"])
    for line in sys.stdin.buffer:
        if len(line) > 1048576:
            raise ValueError("oversized MCP request")
        request = json.loads(line)
        identifier = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise ValueError("invalid MCP request")
        method, params = request["method"], request.get("params", {})
        if identifier is None:
            if method not in {"notifications/initialized", "notifications/cancelled"}:
                raise ValueError("unsupported MCP notification")
            continue
        try:
            if method == "initialize":
                version = params.get("protocolVersion")
                if not isinstance(version, str) or not version:
                    raise ValueError("unsupported MCP protocol version")
                if version not in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}:
                    version = "2025-11-25"
                result = {"protocolVersion": version, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "scratch", "version": "1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": "run", "description":
                    "Run one program using argv, without shell expansion, in the authorized cwd. "
                    "Native Bash is unavailable. Subprocess file writes use an independent cwd fence; "
                    "child creation (fork/spawn) is denied and bypass fields are unsupported. "
                    "No network or other external services. Python executable: " + sys.executable,
                    "inputSchema": INPUT_SCHEMA}]}
            elif method == "tools/call":
                if not isinstance(params, dict) or not set(params) <= {"name", "arguments", "_meta"} or params.get("name") != "run":
                    raise ValueError("unsupported MCP tool call")
                result = call_tool(context, params.get("arguments"), request_id=identifier)
            else:
                raise ValueError("unsupported MCP method")
            response = {"jsonrpc": "2.0", "id": identifier, "result": result}
        except (ValueError, OSError, TypeError, KeyError) as error:
            response = {"jsonrpc": "2.0", "id": identifier,
                        "error": {"code": -32602, "message": str(error)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "serve":
        raise SystemExit("usage: eval_claude_scratch_policy.py serve POLICY POLICY_SHA HANDLER_SHA")
    serve(*sys.argv[2:])
