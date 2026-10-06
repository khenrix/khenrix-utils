"""Prompt-only eval bridge: adapter parsing and supervised, isolated CLI turns.

This entry has no repository, memory, peer-round, or write-dispatch authority.
The eval harness retains credential bridging and artifact redaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ProcessValidationError, ProviderProtocolError
from .process import ProcessCommand, ProcessStatus, build_child_environment, run_prompt_only_command
from .providers import AgyAdapter, ClaudeAdapter, CodexAdapter, ProviderRegistry

MODES = ("normal", "deep")
ADAPTERS = {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "agy": AgyAdapter()}
SAFE_ENV = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TZ"}
AUTH_ENV = {
    "claude": {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AGY_ADC_AUTH",
               "GOOGLE_APPLICATION_CREDENTIALS", "CLAUDE_CODE_USE_VERTEX",
               "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION"},
    "codex": {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"},
    "agy": {"AGY_ADC_AUTH", "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_REGION"},
}

CODEX_DISABLED_FEATURES = (
    "plugins", "skill_search", "shell_tool", "unified_exec", "apps", "view_image",
    "remote_plugin", "hooks", "multi_agent", "multi_agent_v2", "deferred_executor",
    "token_budget", "sleep_tool", "current_time_reminder", "send_message_to_user_async",
    "request_permissions_tool", "tool_suggest", "image_generation", "goals",
)


def codex_catalog_bytes(model: str) -> bytes:
    """Codex 0.160 ModelInfo: one exact model with no model-advertised tools."""
    descriptor = {
        "slug": model, "display_name": model, "supported_reasoning_levels": [],
        "shell_type": "disabled", "visibility": "hide", "supported_in_api": True,
        "priority": 0, "support_verbosity": False, "apply_patch_tool_type": None,
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "experimental_supported_tools": [], "supports_search_tool": False,
        "tool_mode": "direct", "include_apps_usage_instructions": False,
        "base_instructions": "Answer the request directly from prompt context.",
    }
    return (json.dumps({"models": [descriptor]}, sort_keys=True, separators=(",", ":")) + "\n").encode()


def codex_isolation_args(cwd: Path) -> list[str]:
    return ["--ignore-user-config", "--ignore-rules", "--ephemeral",
            "--enable", "skip_host_skill_discovery",
            *(part for feature in CODEX_DISABLED_FEATURES for part in ("--disable", feature)),
            "-c", "agents.enabled=false", "-c", 'web_search="disabled"',
            "-c", "tools.experimental_request_user_input.enabled=false",
            "-c", "tools.update_plan.enabled=false",
            "-c", "suppress_unstable_features_warning=true",
            "-c", "model_catalog_json=" + json.dumps(str(cwd / "eval-models.json")),
            "--skip-git-repo-check", "-C", str(cwd)]


def validate_codex_catalog(cwd: Path, model: str) -> None:
    path = cwd / "eval-models.json"
    expected = codex_catalog_bytes(model)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size != len(expected)):
                raise ProcessValidationError("Codex eval model catalog changed")
            raw = os.read(fd, len(expected) + 1)
            if hashlib.sha256(raw).digest() != hashlib.sha256(expected).digest():
                raise ProcessValidationError("Codex eval model catalog changed")
        finally:
            os.close(fd)
    except OSError as error:
        raise ProcessValidationError("Codex eval model catalog is unavailable") from error


@dataclass
class PromptSpec:
    name: str
    argv: list[str]
    stdin: bytes = field(repr=False)
    model: str
    thinking: str | None
    cwd: str = "."


def resolve_mode_config(args) -> dict:
    registry = ProviderRegistry.default()
    tier = "deep" if args.mode == "deep" else "standard"
    return {name: {"model": getattr(args, f"model_{name}", None) or
                         registry.select(name, "read-only", tier).requested_model,
                   "thinking": registry.select(name, "read-only", tier).requested_effort}
            for name in ADAPTERS}


def effective_timeout(args) -> int | float:
    if args.timeout is not None:
        return args.timeout
    tier = "deep" if args.mode == "deep" else "standard"
    return ProviderRegistry.default().select("claude", "read-only", tier).default_timeout


def build_spec(name: str, prompt: str, cfg: dict) -> PromptSpec:
    if name not in ADAPTERS:
        raise RuntimeError(f"unsupported eval provider: {name}")
    model = cfg[name]["model"]
    effort = cfg[name].get("thinking")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@ ()-]{0,127}", model):
        raise RuntimeError("eval model must be an explicit model identifier")
    if effort is not None and effort not in {"low", "medium", "high", "max", "xhigh", "ultra", "ultracode"}:
        raise RuntimeError("eval effort is invalid")
    if name == "codex" and effort == "ultra":
        raise RuntimeError("Codex eval ultra requires an attested model effort mapping")
    if name == "claude":
        argv = [name, "--print", "--output-format", "json", "--model", model]
        if effort:
            argv += ["--effort", effort]
        stdin = prompt.encode("utf-8")
    elif name == "codex":
        argv = [name, "exec", "-", "--json", "--sandbox", "read-only", "-m", model]
        if effort:
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        stdin = prompt.encode("utf-8")
    else:
        # Flash encodes its tier in the model. Its CLI rejects a separate --effort.
        argv = [name, "--mode", "plan", "--input-format", "stream-json",
                "--output-format", "stream-json", "--model", model]
        stdin = (json.dumps({"event": "user", "message": {"content": prompt}},
                            ensure_ascii=False) + "\n").encode("utf-8")
    return PromptSpec(name, argv, stdin, model, effort)


def quality_session_command(request, *, catalog_path=None) -> ProcessCommand:
    """Quality-only persistent grammar; ordinary prompt-only eval remains ephemeral."""
    provider, profile = request.executor_id, request.profile
    if (provider not in ADAPTERS or profile is None or request.retries != 0
            or (request.resume and (not isinstance(request.session_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", request.session_id) is None))
            or not isinstance(profile.requested_model, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", profile.requested_model) is None
            or profile.requested_effort not in {"medium", "high", "max", "xhigh", "ultra", "ultracode"}):
        raise ProcessValidationError("quality session identity or zero-retry grammar differs")
    model, effort = profile.requested_model, profile.requested_effort
    stdin = request.prompt_bytes
    if provider == "claude":
        import uuid
        try:
            if str(uuid.UUID(request.session_id)) != request.session_id:
                raise ValueError("UUID")
        except (ValueError, TypeError, AttributeError) as error:
            raise ProcessValidationError("quality Claude requires its own exact UUID") from error
        argv = ("claude", "--print", "--output-format", "json",
                "--resume" if request.resume else "--session-id", request.session_id,
                "--model", model, "--effort", effort, "--safe-mode",
                "--disable-slash-commands", "--strict-mcp-config", "--no-chrome", "--tools", "")
    elif provider == "codex":
        if (not isinstance(catalog_path, str) or not Path(catalog_path).is_absolute()
                or any(char in catalog_path for char in ('"', "\n", "\r", "\x00"))):
            raise ProcessValidationError("quality Codex requires an authenticated input catalog")
        disabled = ("shell_tool", "unified_exec", "view_image", "apps", "plugins",
                    "remote_plugin", "hooks", "skill_search", "multi_agent", "multi_agent_v2",
                    "deferred_executor", "token_budget", "sleep_tool", "current_time_reminder",
                    "send_message_to_user_async", "request_permissions_tool", "tool_suggest", "goals")
        prefix = ("codex", "exec", "resume") if request.resume else ("codex", "exec")
        argv = (*prefix, "--ignore-user-config", "--ignore-rules", "--json", "--skip-git-repo-check",
                "--enable", "skip_host_skill_discovery",
                *(part for feature in disabled for part in ("--disable", feature)),
                "-c", "agents.enabled=false", "-c", "suppress_unstable_features_warning=true",
                "-c", 'web_search="disabled"',
                "-c", "tools.experimental_request_user_input.enabled=false",
                "-c", "tools.update_plan.enabled=false", "-c", f'model_catalog_json="{catalog_path}"',
                "-m", model, "-c", f'model_reasoning_effort="{effort}"')
        argv += (("-c", 'sandbox_mode="read-only"', request.session_id, "-") if request.resume
                 else ("--sandbox", "read-only", "-C", str(request.cwd), "-"))
    else:
        argv = ("agy", "--conversation", request.session_id) if request.resume else ("agy", "--new-project")
        argv += ("--mode", "plan", "--input-format", "stream-json", "--output-format", "stream-json",
                 "--model", model, "--effort", effort, "--agent", "eval-no-tools", "--disable-slash-commands")
        try:
            prompt = request.prompt_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProcessValidationError("quality agy prompt must be UTF-8") from error
        stdin = (json.dumps({"event": "user", "message": {"content": prompt}},
                            ensure_ascii=False) + "\n").encode()
    return ProcessCommand(argv, stdin, cwd=request.cwd, timeout=request.timeout)


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProcessValidationError("eval cwd/home/config must be private real directories")


def validate_launch(command: ProcessCommand, provider: str, environment: dict,
                    real_home: Path) -> dict[str, str]:
    """Recheck the entire supported argv and env before shared supervision."""
    if provider not in ADAPTERS or command.environment or any(
        getattr(command, name) is not None for name in (
            "native_original", "native_boundary", "native_request", "native_controller", "native_verification",
        )
    ):
        raise ProcessValidationError("eval launch has unsupported execution authority")
    argv = list(command.argv)
    try:
        model_flag = "-m" if provider == "codex" else "--model"
        model = argv[argv.index(model_flag) + 1]
        if provider == "claude":
            effort = argv[argv.index("--effort") + 1] if "--effort" in argv else None
        elif provider == "codex":
            efforts = [arg for arg in argv if arg.startswith('model_reasoning_effort="')]
            effort = efforts[0].removeprefix('model_reasoning_effort="').removesuffix('"') if efforts else None
        else:
            effort = None
        expected = build_spec(provider, "", {provider: {"model": model, "thinking": effort}}).argv
    except (ValueError, IndexError, RuntimeError) as error:
        raise ProcessValidationError("eval argv has an invalid model/effort grammar") from error
    if provider == "claude":
        expected += ["--safe-mode", "--disable-slash-commands", "--no-session-persistence",
                     "--strict-mcp-config", "--no-chrome", "--tools", ""]
    elif provider == "codex":
        expected += codex_isolation_args(command.cwd)
    else:
        binary = real_home / ".local/libexec/agy-bin"
        if binary.is_file():
            expected[0] = str(binary)
        expected += ["--agent", "eval-no-tools", "--disable-slash-commands"]
    if argv != expected:
        raise ProcessValidationError("eval argv differs from the exact no-tools command")
    _private_directory(command.cwd)
    if provider == "codex":
        validate_codex_catalog(command.cwd, model)
    # Claude's proven safe-mode auth path keeps real HOME; the other CLIs have empty homes.
    if not isinstance(environment.get("HOME"), str):
        raise ProcessValidationError("eval environment lacks its auth HOME")
    home = Path(environment["HOME"])
    if provider == "claude":
        if home != real_home:
            raise ProcessValidationError("Claude eval auth HOME changed")
    else:
        if home != command.cwd.parent / "home":
            raise ProcessValidationError("eval HOME must be the isolated sibling")
        _private_directory(home)
        config = home / ".config"
        if environment.get("XDG_CONFIG_HOME") != str(config):
            raise ProcessValidationError("eval config HOME changed")
        _private_directory(config)
        if provider == "codex":
            if environment.get("CODEX_HOME") != str(home / ".codex"):
                raise ProcessValidationError("Codex eval config HOME changed")
            _private_directory(home / ".codex")
        else:
            agent = home / ".gemini/config/agents/eval-no-tools.md"
            if agent.is_symlink() or not agent.is_file() or stat.S_IMODE(agent.stat().st_mode) != 0o600:
                raise ProcessValidationError("agy eval agent must be a private no-tools definition")
            if agent.read_text() != AGY_AGENT_TEXT:
                raise ProcessValidationError("agy eval agent definition changed")
    allowed = SAFE_ENV | AUTH_ENV[provider] | {"HOME", "KHENRIX_EVAL_TOOL_CANARY"}
    if provider != "claude":
        allowed.add("XDG_CONFIG_HOME")
    if provider == "codex":
        allowed.add("CODEX_HOME")
    if set(environment) - allowed:
        raise ProcessValidationError("eval environment contains ambient or unsupported state")
    if any(not isinstance(value, str) or "\x00" in value for value in environment.values()):
        raise ProcessValidationError("eval environment values must be NUL-free strings")
    if not environment.get("KHENRIX_EVAL_TOOL_CANARY"):
        raise ProcessValidationError("eval environment lacks its artifact canary")
    if environment.get("AGY_ADC_AUTH") not in {None, "true"}:
        raise ProcessValidationError("eval ADC mode must be literal true")
    adc = environment.get("GOOGLE_APPLICATION_CREDENTIALS")
    if adc is not None and adc != str(real_home / ".config/gcloud/application_default_credentials.json"):
        raise ProcessValidationError("eval ADC must name the selected auth home's default file")
    child = build_child_environment()
    child.update(environment)
    child.update({"LLM_FANOUT_DEPTH": "2", "LLM_COUNCIL_DEPTH": "1"})
    return child


AGY_AGENT_TEXT = """---
name: eval-no-tools
description: Eval responder with no tools
tools: []
excludeDefaultComponents: true
enable_mcp_tools: false
---
Answer the request directly from prompt context. You have no tools.
"""


def run(spec: PromptSpec, workdir: Path, *, timeout: int, retries: int,
        environment: dict, real_home: Path) -> dict:
    if type(retries) is not int or not 0 <= retries <= 10:
        raise RuntimeError("eval spawn retry budget must be between 0 and 10")
    command = ProcessCommand(spec.argv, spec.stdin, cwd=spec.cwd, timeout=timeout,
                             max_output_bytes=8 * 1024 * 1024)
    started = time.monotonic()
    parsed = None
    for attempt in range(1, retries + 2):
        result = run_prompt_only_command(command, provider=spec.name,
                                         environment=environment, real_home=real_home)
        stdout = workdir / f"{spec.name}.stdout.attempt-{attempt}.txt"
        stderr = workdir / f"{spec.name}.stderr.attempt-{attempt}.txt"
        stdout.write_bytes(result.stdout)
        stderr.write_bytes(result.stderr)
        if result.status is ProcessStatus.SPAWN_ERROR and result.pid is None and attempt <= retries:
            continue
        break
    if result.stdout:
        try:
            parsed = ADAPTERS[spec.name].parse(result.stdout)
        except ProviderProtocolError:
            pass
    valid = (result.status is ProcessStatus.EXIT and result.returncode == 0 and
             parsed is not None and parsed.final and not parsed.error and bool(parsed.answer.strip()))
    reason = ("ok" if valid else result.status.value if result.status is not ProcessStatus.EXIT else
              "provider-error" if parsed and parsed.error else
              "process-error" if result.returncode != 0 else "protocol-error")
    answer = parsed.answer if parsed else ""
    answer_file = workdir / f"{spec.name}.result.txt"
    answer_file.write_text(answer)
    record = {"name": spec.name, "model": spec.model, "thinking": spec.thinking,
              "valid": valid, "reason": reason,
              "hint": "provider reported a structured failure" if parsed and parsed.error else None,
              "attempts": attempt, "duration_sec": round(time.monotonic() - started, 4),
              "result_file": str(answer_file), "stdout_file": str(stdout), "stderr_file": str(stderr),
              "usage": dict(parsed.usage) if parsed and parsed.usage is not None else None,
              "observed_model": parsed.observed_model if parsed else None}
    (workdir / "manifest.json").write_text(json.dumps({"providers": [record]}, indent=2) + "\n")
    return record
