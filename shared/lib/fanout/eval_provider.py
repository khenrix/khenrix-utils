"""Prompt-only eval bridge: adapter parsing and supervised, isolated CLI turns.

This entry has no repository, memory, peer-round, or write-dispatch authority.
The eval harness retains credential bridging and artifact redaction.
"""
from __future__ import annotations

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
        expected += ["--ignore-user-config", "--ignore-rules", "--ephemeral",
                     "--enable", "skip_host_skill_discovery", "--disable", "plugins",
                     "--disable", "skill_search", "--disable", "shell_tool", "--disable", "unified_exec",
                     "--disable", "apps", "-c", "agents.enabled=false", "-c", "tools.view_image=false",
                     "-c", "tools.web_search=false", "-c", 'web_search="disabled"',
                     "--skip-git-repo-check", "-C", str(command.cwd)]
    else:
        binary = real_home / ".local/libexec/agy-bin"
        if binary.is_file():
            expected[0] = str(binary)
        expected += ["--agent", "eval-no-tools", "--disable-slash-commands"]
    if argv != expected:
        raise ProcessValidationError("eval argv differs from the exact no-tools command")
    _private_directory(command.cwd)
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
