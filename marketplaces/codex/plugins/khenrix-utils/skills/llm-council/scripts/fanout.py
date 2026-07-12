#!/usr/bin/env python3
"""llm-council engine — fan one prompt out to all three CLIs headlessly.

Runs the same prompt on claude / codex / agy in parallel (non-interactive, full
permissions), validates each result, retries failures with backoff, and writes a
JSON manifest the orchestrating CLI reads to synthesize a best answer. The flaky,
parallel, retry mechanics live here (deterministic, tested); the synthesis lives
in SKILL.md (LLM judgment).

Stdlib only — runs on any Python 3.11+ with no install step.

Usage:
  fanout.py --prompt-file PROMPT.txt [--providers claude,codex,agy] [--out json]
  fanout.py --self-test     # deterministic engine tests (no token cost)
  fanout.py --smoke         # one real provider, 'pong' check (costs tokens, needs auth)

Headless recipes encoded below come from headless-invocation.md. Tuning knobs the
"iterate until solid" loop touches: ERROR_SENTINELS, the extract_* functions, and
the per-provider argv builders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

MANIFEST_SCHEMA = 1
DEFAULT_PROVIDERS = ["claude", "codex", "agy"]
RESULT_TRUNCATE = 4000  # chars kept in the stdout manifest; full text is on disk

# --------------------------------------------------------------------------- #
# Council models + thinking modes — THE single place to change who sits on the
# council and how hard they think. Edit a cell to swap a model or thinking tier.
#   normal — the default; all members at high thinking.
#   deep   — same models, maximum reasoning (and a longer default timeout) for
#            high-stakes / maximum-confidence questions.
# The claude seat is claude-opus-4-8 (2026-07-12): a TEMPORARY default while Claude
# Fable 5 is credit-walled — restore "claude-fable-5" here (both modes) when it returns.
# `thinking` is an ABSTRACT tier (high|max); build_real_spec maps it to each
# CLI's own flag. agy (since 1.1.1) accepts a per-run `--model`; its thinking tier is
# encoded in the model string itself (e.g. "(High)"), so the agy cell's model IS
# applied at run time — `agy models` lists the valid strings.
# --------------------------------------------------------------------------- #
MODES = {
    "normal": {
        "claude": {"model": "claude-opus-4-8",         "thinking": "high"},
        "codex":  {"model": "gpt-5.6-sol",            "thinking": "high"},
        "agy":    {"model": "Gemini 3.5 Flash (High)", "thinking": "high"},
    },
    "deep": {
        "claude": {"model": "claude-opus-4-8",         "thinking": "max"},
        "codex":  {"model": "gpt-5.6-sol",            "thinking": "max"},
        # Flash tops out at "(High)" — no Max tier exists per `agy models` (2026-07-11),
        # so agy's deep seat runs identically to normal; "high" keeps provenance truthful.
        "agy":    {"model": "Gemini 3.5 Flash (High)", "thinking": "high"},
    },
}
DEFAULT_MODE = "normal"
# Deep raised 600->1200 (2026-07-11): fable-5@max measured 649s and sol@max 796s on a
# substantive review — 600 killed both. For big deep prompts prefer --retries 0/1: a
# member that rode the window once will ride it again, and retries multiply the wait.
MODE_TIMEOUT = {"normal": 300, "deep": 1200}  # per-attempt seconds used when --timeout is unset

# Map the abstract thinking tier to each provider's own flag value.
CLAUDE_EFFORT = {"high": "high", "max": "max"}   # claude --effort: low,medium,high,xhigh,max
# gpt-5.6-sol accepts low/medium/high/xhigh/max/ultra (probed 2026-07-11); "ultra" is
# deliberately unused — it spawns internal sub-agents (a council inside a council member)
# and is Pro-plan-gated, so deep mode maps to "max".
CODEX_EFFORT = {"high": "high", "max": "max"}

# Substrings that mark a provider's output as a failure rather than an answer.
# Scanned in stderr and the provider's log file always, and in the result text ONLY
# when the exit code is nonzero (so an exit-0 answer that legitimately discusses
# "rate limits" isn't rejected). Split by whether a retry could plausibly help:
#   PERSISTENT — auth missing or a quota wall; retrying only burns the budget, so
#                these fast-fail. (e.g. agy emits nothing to stdout on a 429 and logs
#                "RESOURCE_EXHAUSTED ... Individual quota reached" to its --log-file.)
#   TRANSIENT  — momentary; worth a bounded retry.
PERSISTENT_SENTINELS = [
    "not logged in",
    "please run `claude login`",
    "please run 'claude login'",
    "resource_exhausted",
    "individual quota",
    "quota reached",
    "quota exceeded",
    "authentication failed",
    "invalid api key",
    "no credentials",
    "unauthorized",
]
TRANSIENT_SENTINELS = [
    "rate limit",
    "usage limit",
    "overloaded",
    "try again later",
    "temporarily unavailable",
]
# Real-world failure strings observed across the three CLIs (extend in place so the
# additions read as list growth, not string concatenation). All lowercase — input is lowered.
PERSISTENT_SENTINELS.extend(["unauthenticated", "permission denied"])
TRANSIENT_SENTINELS.extend(["heap out of memory", "econnreset", "503"])
NONRETRYABLE_REASONS = {"not_installed", "auth_or_quota"}


def classify_sentinel(text: str) -> Optional[str]:
    """Map error text to a reason: persistent auth/quota, transient, or None."""
    low = (text or "").lower()
    if any(s in low for s in PERSISTENT_SENTINELS):
        return "auth_or_quota"
    if any(s in low for s in TRANSIENT_SENTINELS):
        return "error_sentinel"
    return None


# --------------------------------------------------------------------------- #
# Result extraction — turn raw stdout into the substantive answer text.
# Each returns (text, extract_err) where extract_err is None on success or one of
# {"parse_failure", "claude_error"}.
# --------------------------------------------------------------------------- #
def extract_claude_json(stdout: str) -> tuple[str, Optional[str]]:
    """claude -p --output-format json → a single JSON object with a `result` field."""
    s = stdout.strip()
    if not s:
        return "", None
    obj = None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        # Tolerate a stray leading/trailing log line by grabbing the outer {...}.
        lo, hi = s.find("{"), s.rfind("}")
        if lo != -1 and hi > lo:
            try:
                obj = json.loads(s[lo:hi + 1])
            except json.JSONDecodeError:
                return "", "parse_failure"
        else:
            return "", "parse_failure"
    if not isinstance(obj, dict):
        return "", "parse_failure"
    if obj.get("is_error") or obj.get("subtype") not in (None, "success"):
        txt = obj.get("result") or obj.get("error") or ""
        return (txt if isinstance(txt, str) else json.dumps(txt)), "claude_error"
    res = obj.get("result")
    if isinstance(res, str):
        return res, None
    if res is None:
        return "", None
    return json.dumps(res), None


def extract_raw(stdout: str) -> tuple[str, Optional[str]]:
    """codex/agy have no confirmed machine-readable mode; keep stdout verbatim.

    The synthesizer receives the raw file as ground truth and strips any CLI log
    chrome itself, so a weak extractor degrades synthesis but never loses data."""
    return stdout.strip(), None


# --------------------------------------------------------------------------- #
# Provider spec + the real per-CLI invocations.
# --------------------------------------------------------------------------- #
@dataclass
class ProviderSpec:
    name: str
    argv: list                       # full command; argv[0] is the binary
    stdin: Optional[str]             # text piped to stdin, or None
    extract: Callable[[str], tuple[str, Optional[str]]]
    model: Optional[str] = None
    thinking: Optional[str] = None   # abstract tier (high|max) recorded for provenance
    log_file: Optional[str] = None   # if set, scanned for sentinels on failure
    cwd: Optional[str] = None        # if set, the provider runs from this directory


def agy_configured_model() -> Optional[str]:
    """Provenance FALLBACK only (since 1.1.1 the engine pins agy's model via --model):
    read the settings file so the manifest can still report agy's model when no MODES
    cell/override supplied one; return None if the file/key is absent."""
    try:
        p = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        return json.loads(p.read_text()).get("model")
    except Exception:  # noqa: BLE001 — best-effort; never fail a run over this
        return None


def build_real_spec(name: str, prompt: str, timeout: int,
                    cfg: dict, workdir: Path) -> ProviderSpec:
    """cfg maps provider -> {"model": str, "thinking": "high"|"max"} (from MODES,
    with per-run --model-* overrides already merged in by resolve_mode_config)."""
    pc = cfg.get(name, {})
    model, thinking = pc.get("model"), pc.get("thinking")
    if name == "claude":
        argv = ["claude", "-p", prompt, "--output-format", "json",
                "--dangerously-skip-permissions"]
        if model:
            argv += ["--model", model]
        if thinking:
            argv += ["--effort", CLAUDE_EFFORT.get(thinking, thinking)]
        return ProviderSpec("claude", argv, None, extract_claude_json, model, thinking)
    if name == "codex":
        # prompt via stdin (codex exec -) so it never enters a shell-escaped argv.
        argv = ["codex", "exec", "-", "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            argv += ["-m", model]
        if thinking:
            # codex -c parses the value as TOML, so quote the string explicitly.
            argv += ["-c", f'model_reasoning_effort="{CODEX_EFFORT.get(thinking, thinking)}"']
        return ProviderSpec("codex", argv, prompt, extract_raw, model, thinking)
    if name == "agy":
        # agy uses Go-style flag parsing: -p/--print is a boolean and the prompt is a
        # positional arg. Go's flag package STOPS at the first positional, so every flag
        # must come BEFORE the prompt — otherwise it's silently dropped, which leaves
        # --dangerously-skip-permissions un-applied and agy returns empty in seconds.
        # Since agy 1.1.1, `--model` pins the model per-run (thinking tier is encoded in
        # the model string, e.g. "Gemini 3.5 Flash (High)"; `agy models` lists them) —
        # the settings.json read remains only as manifest-provenance fallback.
        # --log-file captures agy's real failure reason: on a 429 it prints nothing to
        # stdout/stderr and only logs e.g. "RESOURCE_EXHAUSTED ... Individual quota
        # reached" — run_provider scans this file to turn an opaque `empty` into a clear
        # `auth_or_quota`. print-timeout self-terminates agy on a CLEAN idle wait (e.g. a
        # quota wall) just inside the engine timeout; capped at 120s so a quota-walled agy
        # fails FAST. The 120s cap was calibrated on pre-1.1.1 clean-idle semantics; all
        # observed 1.1.1 completions run 54-100s (under it) — if agy answers ever truncate
        # near 120s, re-probe whether 1.1.1 made print-timeout a hard wall and raise it.
        # HISTORY: pre-1.1.1 (verified 2026-06-26), agy's headless `-p` mode
        # churned without emitting on non-trivial prompts and rode the window to `timeout`;
        # agy 1.1.1's release notes fixed `-p` hanging in subprocesses, and on 2026-07-11
        # agy completed multiple substantive council reviews in 54–97s. Timeouts can still
        # happen — treat them per the failure table, not as a certainty.
        pt = max(5, min(int(timeout) - 5, 120))
        logf = str(Path(workdir) / "agy.cli.log")
        argv = ["agy", "--dangerously-skip-permissions", "--print-timeout", f"{pt}s",
                "--log-file", logf]
        if model:
            argv += ["--model", model]
        argv += ["-p", prompt]
        # agy's tier is encoded in the model label — derive the recorded tier from the
        # FINAL string so a cross-tier --model-agy override can't leave stale provenance.
        final_model = model or agy_configured_model()
        m = re.search(r"\((Low|Medium|High)\)", final_model or "")
        return ProviderSpec("agy", argv, None, extract_raw,
                            final_model, m.group(1).lower() if m else None, log_file=logf)
    raise ValueError(f"unknown provider: {name}")


def _replace_flag(argv: list, old: str, new_tokens: list) -> list:
    out: list = []
    for a in argv:
        out.extend(new_tokens) if a == old else out.append(a)
    return out


READONLY_REVIEWER_NOTE = (
    "You are a read-only council reviewer. Answer directly and completely in your final "
    "message. Do not write, create, or update any plan file, and do not use ExitPlanMode."
)


def make_readonly(spec: ProviderSpec) -> ProviderSpec:
    """Swap a provider's "bypass everything" flag for a read-and-plan-only posture, in
    place. Unlike sandboxing HOME, this keeps the real HOME (so auth still resolves) but
    forbids writes — the executor can read/plan but cannot mutate config. Shared by the
    eval harness (executor runs) and reused by any read-only council mode.

    agy's read-only flag is `--mode plan` (1.1.1+). `--sandbox` (the earlier candidate) BROKE agy
    non-interactively: agy locates/reads files via terminal commands (find/grep) that the
    sandbox's terminal restrictions block, so it stalls on "searching…" and hangs the full
    engine window with EMPTY output — verified 2026-06-26 (--sandbox, even WITH
    --dangerously-skip-permissions, never completes a file read; plain
    --dangerously-skip-permissions reads + answers in seconds). So agy stays headless and its
    read-only posture no longer rests on intent alone: since agy 1.1.1, `--mode plan`
    works headless (probed 2026-07-11: reads files, answers fast, and mechanically blocked
    a write it claimed to have made) — so the bypass flag is swapped for it, mirroring
    claude's plan mode. Two soft layers remain on top: the READONLY_POSTURE prompt line
    and isolate_agy_worktree (cwd-relative mutations land in a throwaway git worktree).
    HISTORY: `--sandbox` (the pre-1.1.1 candidate) hung agy headless — verified
    2026-06-26; do not resurrect it without re-probing."""
    if spec.name == "claude":
        # Plan mode is the read-only mechanism, but its harness invites writing a plan
        # FILE (the one write plan mode allows) — suppress that side effect mechanically
        # (deny the plan-approval tool) and by instruction (answer inline).
        spec.argv = _replace_flag(spec.argv, "--dangerously-skip-permissions",
                                  ["--permission-mode", "plan",
                                   "--disallowedTools", "ExitPlanMode",
                                   "--append-system-prompt", READONLY_REVIEWER_NOTE])
    elif spec.name == "codex":
        spec.argv = _replace_flag(spec.argv, "--dangerously-bypass-approvals-and-sandbox",
                                  ["--sandbox", "read-only"])
    elif spec.name == "agy":
        spec.argv = _replace_flag(spec.argv, "--dangerously-skip-permissions",
                                  ["--mode", "plan"])
    return spec


_LIVE_WORKTREES: set = set()   # (repo, wt) handles; registered the moment `worktree add` succeeds
_HANDLER_FIRED = False


def _signal_cleanup(signum, frame):
    """SIGTERM/SIGINT: a default-disposition SIGTERM kills Python without running
    `finally`, and sys.exit here would unwind into the executor's __exit__, which blocks
    on live member subprocesses for minutes — so remove the registered worktrees
    directly and hard-exit with the conventional 128+signum."""
    global _HANDLER_FIRED
    if not _HANDLER_FIRED:            # re-entry guard: a second signal skips straight to exit
        _HANDLER_FIRED = True
        for handle in list(_LIVE_WORKTREES):
            remove_agy_worktree(handle)
    os._exit(128 + signum)


def install_cleanup_handler() -> None:
    """Install in main()/smoke() BEFORE any worktree is created. Main-thread only —
    off-main-thread callers get ValueError, which is ignored (they also never create
    worktrees without a main-thread orchestrator)."""
    try:
        signal.signal(signal.SIGTERM, _signal_cleanup)
        signal.signal(signal.SIGINT, _signal_cleanup)
    except ValueError:  # noqa: PERF203 — not the main thread; nothing to protect here
        pass


def _warn_isolation(detail: str) -> None:
    sys.stderr.write(f"WARNING: agy worktree isolation degraded — {detail}; "
                     "agy runs in the real cwd (plan mode + posture line still apply).\n")


def isolate_agy_worktree(spec: ProviderSpec, workdir: Path,
                         repo_dir: Optional[str] = None) -> Optional[tuple]:
    """Point agy's cwd at a throwaway git worktree so cwd-relative mutations — the
    observed breakout class (2026-07-11: editing files, re-seeding receipts, `git add`)
    — land in a discarded copy instead of the real checkout. Since agy 1.1.1 the primary
    write barrier is `--mode plan` (see make_readonly); this worktree is defense in depth
    for the day plan mode fails or regresses. Identical conditions beat containment: the worktree mirrors the working
    tree (uncommitted tracked changes incl. binary; untracked files are absent), the
    caller's position inside the repo is preserved so relative paths resolve the same
    for every member, and if the mirror cannot be reproduced faithfully the isolation
    is ABANDONED with a stderr warning rather than letting agy silently review
    HEAD-only content. Quiet no-op outside a git repo.
    Returns (repo_root, worktree_path) for remove_agy_worktree."""
    handle = None
    try:
        top = subprocess.run(["git", "-C", repo_dir or ".", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return None  # not a git repo — nothing to isolate, nothing to warn about
        repo = top.stdout.strip()
        # Unregister worktrees leaked by previously crashed runs (temp dirs vanish but
        # their .git/worktrees/ registrations do not).
        subprocess.run(["git", "-C", repo, "worktree", "prune"],
                       capture_output=True, text=True, timeout=10)
        wt = str(Path(workdir) / "agy-worktree")
        add = subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", wt, "HEAD"],
                             capture_output=True, text=True, timeout=30)
        if add.returncode != 0:
            _warn_isolation(f"worktree add failed: {add.stderr.strip()[:120]}")
            return None
        handle = (repo, wt)
        _LIVE_WORKTREES.add(handle)   # from this instant a signal can clean it up
        # bytes mode end-to-end: text=True would newline-translate CRLF patch content
        # and raise on non-UTF-8 files, silently degrading isolation for such repos.
        diff = subprocess.run(["git", "-C", repo, "diff", "--binary", "--full-index", "HEAD"],
                              capture_output=True, timeout=30)
        if diff.returncode != 0:
            remove_agy_worktree(handle)
            _warn_isolation("could not read the working-tree diff")
            return None
        if diff.stdout.strip():
            ap = subprocess.run(["git", "-C", wt, "apply"], input=diff.stdout,
                                capture_output=True, timeout=30)
            if ap.returncode != 0:
                remove_agy_worktree(handle)
                _warn_isolation("could not mirror uncommitted changes")
                return None
        try:
            rel = Path(repo_dir or os.getcwd()).resolve().relative_to(Path(repo).resolve())
            spec.cwd = str(Path(wt) / rel)
        except ValueError:
            spec.cwd = wt
        Path(spec.cwd).mkdir(parents=True, exist_ok=True)  # subdir may hold only untracked files
        return handle
    except Exception:  # noqa: BLE001 — isolation is best-effort, never fail the run
        remove_agy_worktree(handle)
        _warn_isolation("unexpected error during setup")
        return None


def remove_agy_worktree(handle: Optional[tuple]) -> None:
    """Discard the throwaway worktree (and anything agy wrote into it). Idempotent —
    also deregisters the handle, so handler-then-finally double-removal is harmless."""
    if not handle:
        return
    repo, wt = handle
    try:
        # Double --force: the worktree is expected to be dirty if agy misbehaved.
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", "--force", wt],
                       capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001 — best-effort cleanup; workdir is a temp dir
        pass
    # Deregister AFTER the attempt: a signal landing mid-removal still sees the handle,
    # so the handler can retry; double removal is idempotent.
    _LIVE_WORKTREES.discard(handle)


# --------------------------------------------------------------------------- #
# Validation.
# --------------------------------------------------------------------------- #
def evaluate(exit_code: Optional[int], stdout: str, stderr: str,
             spec: ProviderSpec) -> tuple[bool, str, str]:
    """Return (valid, reason, result_text).

    A clean exit with a non-empty answer is VALID. Sentinels never veto a real
    answer — they only refine the *reason* of an already-failing attempt. This is
    essential because some CLIs stream their whole session to stderr: codex echoes
    the files it reads (e.g. this very SKILL.md, whose failure table lists "quota
    reached" / "not logged in") into stderr, and an answer must not be discarded
    just because that noise mentions a sentinel phrase. claude is the exception —
    it reports its own errors structurally (is_error in the JSON), not via the exit
    code, so that path is checked explicitly."""
    result_text, extract_err = spec.extract(stdout)
    if extract_err == "parse_failure":
        return False, "parse_failure", result_text
    if extract_err == "claude_error":
        blob = f"{result_text}\n{stderr or ''}"
        return False, classify_sentinel(blob) or "error_sentinel", result_text
    if exit_code == 0 and result_text.strip():
        return True, "ok", result_text
    base = "empty" if exit_code == 0 else "nonzero_exit"
    return False, classify_sentinel(stderr) or base, result_text


# --------------------------------------------------------------------------- #
# Execution.
# --------------------------------------------------------------------------- #
def child_env() -> dict:
    """Child env with the recursion-depth guard incremented."""
    env = dict(os.environ)
    cur = int(env.get("LLM_COUNCIL_DEPTH", "0") or "0")
    env["LLM_COUNCIL_DEPTH"] = str(cur + 1)
    return env


def _coerce_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def run_provider(spec: ProviderSpec, retries: int, timeout: int,
                 backoff: float, workdir: Path) -> dict:
    """Run one provider through its bounded attempt loop and return its record."""
    attempt_log: list = []
    final = {"stdout": "", "stderr": "", "exit_code": None,
             "reason": "unknown", "result_text": "", "valid": False,
             "duration_sec": 0.0, "status": "failed"}

    for attempt in range(retries + 1):
        n = attempt + 1
        t0 = time.monotonic()
        try:
            cp = subprocess.run(spec.argv, input=spec.stdin, capture_output=True,
                                text=True, timeout=timeout, env=child_env(),
                                cwd=spec.cwd)
        except FileNotFoundError:
            dur = round(time.monotonic() - t0, 2)
            attempt_log.append({"attempt": n, "reason": "not_installed",
                                "exit_code": None, "duration_sec": dur})
            final.update(stdout="", stderr=f"binary not found: {spec.argv[0]}",
                         exit_code=None, reason="not_installed", result_text="",
                         valid=False, duration_sec=dur, status="not_installed")
            _write_attempt(workdir, spec.name, n, "", final["stderr"])
            break  # missing binary won't appear on a retry — fail fast
        except subprocess.TimeoutExpired as e:
            dur = round(time.monotonic() - t0, 2)
            stdout, stderr = _coerce_text(e.stdout), _coerce_text(e.stderr)
            valid, reason, result_text = False, "timeout", stdout.strip()
            exit_code = None
        else:
            dur = round(time.monotonic() - t0, 2)
            stdout, stderr, exit_code = cp.stdout or "", cp.stderr or "", cp.returncode
            valid, reason, result_text = evaluate(exit_code, stdout, stderr, spec)

        if final["status"] != "not_installed":
            # A provider with a log file may hide its real failure there: agy prints
            # nothing on a 429 but logs the quota error. Promote an opaque `empty`/
            # `timeout` to a precise `auth_or_quota`/`error_sentinel` from the log.
            if not valid and spec.log_file:
                try:
                    logtail = Path(spec.log_file).read_text()[-8000:]
                except OSError:
                    logtail = ""
                sent_log = classify_sentinel(logtail)
                if sent_log:
                    reason = sent_log
            _write_attempt(workdir, spec.name, n, stdout, stderr)
            attempt_log.append({"attempt": n, "reason": reason,
                                "exit_code": exit_code, "duration_sec": dur})
            final.update(stdout=stdout, stderr=stderr, exit_code=exit_code,
                         reason=reason, result_text=result_text, valid=valid,
                         duration_sec=dur, status="ok" if valid else "failed")
            if valid:
                break
            if reason in NONRETRYABLE_REASONS:
                break  # auth/quota won't clear on a retry — don't burn the budget
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))

    # Persist final raw + extracted text and reference the files in the record.
    result_file = workdir / f"{spec.name}.result.txt"
    stdout_file = workdir / f"{spec.name}.stdout.txt"
    stderr_file = workdir / f"{spec.name}.stderr.txt"
    result_file.write_text(final["result_text"])
    stdout_file.write_text(final["stdout"])
    stderr_file.write_text(final["stderr"])

    return {
        "name": spec.name,
        "status": final["status"],
        "attempts": len(attempt_log),
        "exit_code": final["exit_code"],
        "duration_sec": final["duration_sec"],
        "valid": final["valid"],
        "reason": final["reason"],
        "result_text": _truncate(final["result_text"]),
        "result_file": str(result_file),
        "raw_stdout_file": str(stdout_file),
        "raw_stderr_file": str(stderr_file),
        "model": spec.model,
        "thinking": spec.thinking,
        "isolated_cwd": spec.cwd,
        "attempt_log": attempt_log,
    }


def run_council(specs: list[ProviderSpec], *, retries: int, timeout: int,
                backoff: float, workdir: Path,
                prompt: Optional[str] = None,
                requested: Optional[list] = None,
                mode: Optional[str] = None,
                read_only: Optional[bool] = None) -> dict:
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    started = _now_iso()
    with ThreadPoolExecutor(max_workers=max(1, len(specs))) as ex:
        futures = [ex.submit(run_provider, s, retries, timeout, backoff, workdir)
                   for s in specs]
        providers = [f.result() for f in futures]
    finished = _now_iso()

    valid = sum(1 for p in providers if p["valid"])
    requested = requested if requested is not None else [s.name for s in specs]
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "prompt_sha256": (hashlib.sha256(prompt.encode()).hexdigest()
                          if prompt is not None else None),
        "workdir": str(workdir),
        "started_at": started,
        "finished_at": finished,
        "config": {"retries": retries, "timeout": timeout, "backoff": backoff,
                   "providers": requested, "mode": mode, "read_only": read_only},
        "summary": {"requested": len(requested), "valid": valid,
                    "failed": len(requested) - valid,
                    "degraded": valid < len(requested)},
        "providers": providers,
    }
    (workdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(s: str, n: int = RESULT_TRUNCATE) -> str:
    return s if len(s) <= n else s[:n] + f"\n…[truncated {len(s) - n} chars; read result_file]"


def _write_attempt(workdir: Path, name: str, n: int, stdout: str, stderr: str) -> None:
    (workdir / f"{name}.stdout.attempt-{n}.txt").write_text(stdout)
    (workdir / f"{name}.stderr.attempt-{n}.txt").write_text(stderr)


def _render_text(manifest: dict) -> str:
    s = manifest["summary"]
    lines = [f"council: {s['valid']}/{s['requested']} valid"
             + ("  (DEGRADED)" if s["degraded"] else "")]
    cfg = manifest.get("config", {})
    tags = []
    if cfg.get("mode"):
        tags.append(f"mode: {cfg['mode']}")
    if cfg.get("read_only") is not None:
        tags.append("read-only" if cfg["read_only"] else "writes")
    if tags:
        lines[0] += "  [" + ", ".join(tags) + "]"
    for p in manifest["providers"]:
        mark = "✓" if p["valid"] else "✗"
        meta = f"{p.get('model') or '-'}/{p.get('thinking') or '-'}"
        lines.append(f"  {mark} {p['name']:<7} {p['reason']:<14} {p['attempts']}x  "
                     f"{p['duration_sec']}s  {meta}  → {p['result_file']}")
    return "\n".join(lines)


# Prepended to the prompt (identically for every member) when the council runs
# read-only. Defense in depth: every member is now mechanically constrained (claude
# plan mode, codex sandbox, agy --mode plan since 1.1.1) — this line and the agy
# worktree are the soft layers on top, added after agy executed a review-framed
# prompt (editing files, re-seeding receipts, staging) on 2026-07-11. claude also gets
# the plan-mode-specific READONLY_REVIEWER_NOTE via make_readonly — keep both in mind
# when editing either wording. Says "as text", not "prose only": answers may still
# contain code blocks/diffs — the guard is against mutating state, not against code.
READONLY_POSTURE = ("COUNCIL POSTURE: read-only — do not create, modify, stage, or "
                    "commit files, or change any repo/system state; propose any "
                    "changes as text in your answer.")


def apply_readonly_posture(prompt: str) -> str:
    """One identical posture line for all members, preserving identical conditions."""
    return f"{READONLY_POSTURE}\n\n{prompt}"


def resolve_prompt(args) -> str:
    if args.prompt is not None:
        return args.prompt
    if args.prompt_file:
        return Path(args.prompt_file).read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


# --------------------------------------------------------------------------- #
# Live smoke (Layer B) — cheap reality check against real binaries.
# --------------------------------------------------------------------------- #
def smoke(args) -> int:
    prompt = "Reply with exactly one word and nothing else: pong"
    if args.read_only:
        prompt = apply_readonly_posture(prompt)  # smoke exercises the real prompt shape
    providers = args.providers.split(",") if args.providers else ["claude"]
    workdir = Path(tempfile.mkdtemp(prefix="llm-council-smoke-"))
    timeout = effective_timeout(args)
    cfg = resolve_mode_config(args)
    specs = [build_real_spec(p, prompt, timeout, cfg, workdir) for p in providers]
    agy_wt = None
    if args.read_only:
        install_cleanup_handler()   # BEFORE any worktree exists — SIGTERM skips finally
        for s in specs:
            make_readonly(s)
        agy_spec = next((s for s in specs if s.name == "agy"), None)
        if agy_spec:
            agy_wt = isolate_agy_worktree(agy_spec, workdir)
    try:
        manifest = run_council(specs, retries=args.retries, timeout=timeout,
                               backoff=args.backoff, workdir=workdir, prompt=prompt,
                               requested=providers, mode=args.mode, read_only=args.read_only)
    finally:
        remove_agy_worktree(agy_wt)
    print(_render_text(manifest))
    ok = all(p["valid"] and "pong" in Path(p["result_file"]).read_text().lower()
             for p in manifest["providers"])
    print(f"\nsmoke {'PASS' if ok else 'FAIL'}  (artifacts: {workdir})")
    return 0 if ok else 1


def effective_timeout(args) -> int:
    """--timeout if given, else the per-mode default (deep gets a longer window)."""
    if args.timeout is not None:
        return args.timeout
    return MODE_TIMEOUT.get(args.mode, MODE_TIMEOUT[DEFAULT_MODE])


def resolve_mode_config(args) -> dict:
    """Per-provider {model, thinking} for the chosen mode, with --model-* overrides
    applied on top (ad-hoc per-run model swaps without touching the MODES table)."""
    base = {p: dict(v) for p, v in MODES.get(args.mode, MODES[DEFAULT_MODE]).items()}
    for p, m in (("claude", args.model_claude), ("codex", args.model_codex),
                 ("agy", args.model_agy)):
        if m:
            base.setdefault(p, {})["model"] = m
    return base


# --------------------------------------------------------------------------- #
# Self-test (Layer A) — drive the REAL engine with stub providers.
# --------------------------------------------------------------------------- #
STUB = Path(__file__).resolve().parent.parent / "tests" / "stub_provider.py"


def _stub_spec(name: str, mode: str, *, as_: str = "raw", sleep: float = 0.0,
               counter: Optional[Path] = None,
               extract: Optional[Callable] = None) -> ProviderSpec:
    argv = [sys.executable, str(STUB), "--mode", mode, "--as", as_]
    if sleep:
        argv += ["--sleep", str(sleep)]
    if counter is not None:
        argv += ["--counter-file", str(counter)]
    if extract is None:
        extract = extract_claude_json if as_ == "claude" else extract_raw
    return ProviderSpec(name, argv, None, extract)


def self_test() -> int:
    root = Path(tempfile.mkdtemp(prefix="llm-council-selftest-"))
    results: list[tuple[str, bool, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        results.append((label, bool(cond), detail))

    def wd(name: str) -> Path:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # S1 — all valid, single attempt each.
    m = run_council([_stub_spec("claude", "ok", as_="claude"),
                     _stub_spec("codex", "ok"), _stub_spec("agy", "ok")],
                    retries=2, timeout=5, backoff=0.1, workdir=wd("all_ok"),
                    prompt="hi")
    check("all-ok: 3/3 valid", m["summary"]["valid"] == 3)
    check("all-ok: not degraded", m["summary"]["degraded"] is False)
    check("all-ok: single attempt each",
          all(p["attempts"] == 1 for p in m["providers"]))
    check("all-ok: claude result extracted",
          "Paris" in next(p for p in m["providers"] if p["name"] == "claude")["result_text"])

    # S2 — one empty: invalid, retried to exhaustion, degraded.
    m = run_council([_stub_spec("claude", "ok", as_="claude"),
                     _stub_spec("codex", "empty"), _stub_spec("agy", "ok")],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("one_empty"),
                    prompt="hi")
    cx = next(p for p in m["providers"] if p["name"] == "codex")
    check("empty: codex invalid reason=empty", not cx["valid"] and cx["reason"] == "empty")
    check("empty: codex retried (3 attempts)", cx["attempts"] == 3)
    check("empty: degraded, 2/3 valid", m["summary"]["valid"] == 2 and m["summary"]["degraded"])

    # S3 — one nonzero exit.
    m = run_council([_stub_spec("claude", "ok", as_="claude"),
                     _stub_spec("codex", "nonzero"), _stub_spec("agy", "ok")],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("one_nonzero"),
                    prompt="hi")
    cx = next(p for p in m["providers"] if p["name"] == "codex")
    check("nonzero: reason=nonzero_exit", cx["reason"] == "nonzero_exit" and cx["attempts"] == 3)

    # S4 — timeout: process killed, runtime bounded (~timeout, not timeout×attempts).
    t0 = time.monotonic()
    m = run_council([_stub_spec("claude", "ok", as_="claude"),
                     _stub_spec("codex", "ok"), _stub_spec("agy", "timeout")],
                    retries=1, timeout=1, backoff=0.05, workdir=wd("one_timeout"),
                    prompt="hi")
    wall = time.monotonic() - t0
    ag = next(p for p in m["providers"] if p["name"] == "agy")
    check("timeout: reason=timeout", ag["reason"] == "timeout")
    check("timeout: retried (2 attempts)", ag["attempts"] == 2)
    check("timeout: each attempt bounded ≈1s",
          all(a["duration_sec"] < 1.8 for a in ag["attempt_log"]))
    check("timeout: total wall bounded (<4s)", wall < 4.0, f"wall={wall:.2f}s")
    check("timeout: others still valid", m["summary"]["valid"] == 2)

    # S5 — error sentinel.
    m = run_council([_stub_spec("codex", "error-sentinel")],
                    retries=0, timeout=5, backoff=0.05, workdir=wd("sentinel"),
                    prompt="hi")
    check("sentinel: reason=error_sentinel", m["providers"][0]["reason"] == "error_sentinel")

    # S6 — flaky:2 with retries=2 recovers on attempt 3.
    counter = wd("flaky") / "counter.txt"
    m = run_council([_stub_spec("agy", "flaky:2", counter=counter)],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("flaky"),
                    prompt="hi")
    ag = m["providers"][0]
    check("flaky: recovers to valid", ag["valid"])
    check("flaky: took 3 attempts", ag["attempts"] == 3)

    # S7 — not installed: fast-fail, no retry.
    m = run_council([ProviderSpec("agy", ["/nonexistent/xyz-not-a-binary"], None, extract_raw)],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("not_installed"),
                    prompt="hi")
    ag = m["providers"][0]
    check("not_installed: status set", ag["status"] == "not_installed")
    check("not_installed: not retried (1 attempt)", ag["attempts"] == 1)

    # S8 — parallelism: 3 × ~1.5s sleeps finish well under serial 4.5s.
    t0 = time.monotonic()
    run_council([_stub_spec("claude", "ok", as_="claude", sleep=1.5),
                 _stub_spec("codex", "ok", sleep=1.5),
                 _stub_spec("agy", "ok", sleep=1.5)],
                retries=0, timeout=5, backoff=0.05, workdir=wd("parallel"),
                prompt="hi")
    wall = time.monotonic() - t0
    check("parallel: ran concurrently (<3s)", wall < 3.0, f"wall={wall:.2f}s")

    # S9 — manifest shape + provenance + on-disk artifacts.
    m = run_council([_stub_spec("claude", "ok", as_="claude")],
                    retries=0, timeout=5, backoff=0.05, workdir=wd("shape"),
                    prompt="hello world")
    required = {"schema", "prompt_sha256", "workdir", "started_at", "finished_at",
                "config", "summary", "providers"}
    check("manifest: required keys present", required <= set(m))
    check("manifest: prompt_sha256 correct",
          m["prompt_sha256"] == hashlib.sha256(b"hello world").hexdigest())
    p0 = m["providers"][0]
    check("manifest: result/raw files exist on disk",
          Path(p0["result_file"]).exists() and Path(p0["raw_stdout_file"]).exists())
    check("manifest: manifest.json written", (Path(m["workdir"]) / "manifest.json").exists())

    # S10 — claude parse failure on non-JSON stdout, retried.
    m = run_council([_stub_spec("claude", "garbage-json", as_="raw",
                                 extract=extract_claude_json)],
                    retries=1, timeout=5, backoff=0.05, workdir=wd("parse_fail"),
                    prompt="hi")
    check("parse: reason=parse_failure", m["providers"][0]["reason"] == "parse_failure")
    check("parse: retried (2 attempts)", m["providers"][0]["attempts"] == 2)

    # S11 — silent quota wall (agy's 429): empty stdout, but log says RESOURCE_EXHAUSTED.
    # The log scan must reclassify it auth_or_quota and NOT retry it.
    qdir = wd("quota")
    qlog = qdir / "agy.cli.log"
    qspec = ProviderSpec("agy", [sys.executable, str(STUB), "--mode", "quota-log",
                                 "--log-file", str(qlog)], None, extract_raw,
                         log_file=str(qlog))
    m = run_council([qspec], retries=2, timeout=5, backoff=0.05, workdir=qdir, prompt="hi")
    ag = m["providers"][0]
    check("quota: reason=auth_or_quota (from log)", ag["reason"] == "auth_or_quota")
    check("quota: not retried (1 attempt)", ag["attempts"] == 1)

    # S12 — regression (found by a real eval): a valid exit-0 answer whose stderr is
    # full of session noise containing sentinel phrases (codex echoing files it read)
    # must stay VALID. Sentinels refine failures; they never veto a real answer.
    m = run_council([_stub_spec("codex", "noisy-ok")],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("noisy"), prompt="hi")
    cx = m["providers"][0]
    check("noisy-ok: valid despite sentinel-laden stderr", cx["valid"] and cx["reason"] == "ok")
    check("noisy-ok: single attempt (not failed+retried)", cx["attempts"] == 1)

    # S13 — classify_sentinel directly covers the real-world strings folded into the tables.
    check("sentinel: unauthenticated → persistent", classify_sentinel("UNAUTHENTICATED") == "auth_or_quota")
    check("sentinel: heap OOM → transient", classify_sentinel("heap out of memory") == "error_sentinel")
    check("sentinel: clean text → None", classify_sentinel("here is your answer") is None)

    # S14 — make_readonly argv contracts (plan-file suppression is mechanical + prompt).
    cl = build_real_spec("claude", "q", 30, {"claude": {"model": "m", "thinking": "high"}}, wd("ro"))
    make_readonly(cl)
    check("readonly: claude bypass flag swapped out", "--dangerously-skip-permissions" not in cl.argv)
    check("readonly: claude gets plan mode", "--permission-mode" in cl.argv and "plan" in cl.argv)
    check("readonly: claude denies ExitPlanMode", "--disallowedTools" in cl.argv and "ExitPlanMode" in cl.argv)
    check("readonly: claude instructed to answer inline (no plan files)",
          any("plan file" in str(a) for a in cl.argv))
    cx14 = build_real_spec("codex", "q", 30, {}, wd("ro"))
    make_readonly(cx14)
    check("readonly: codex sandboxed read-only", "--sandbox" in cx14.argv and "read-only" in cx14.argv)
    ag14 = build_real_spec("agy", "q", 30, {}, wd("ro"))
    make_readonly(ag14)
    check("readonly: agy bypass flag swapped for plan mode (1.1.1)",
          "--dangerously-skip-permissions" not in ag14.argv
          and "--mode" in ag14.argv and "plan" in ag14.argv)
    check("readonly: agy flags still precede the positional prompt (Go flag parsing)",
          ag14.argv.index("plan") < ag14.argv.index("-p"))
    ag14m = build_real_spec("agy", "q", 30,
                            {"agy": {"model": "Gemini 3.5 Flash (High)", "thinking": "high"}},
                            wd("ro"))
    check("agy: per-run --model passed and precedes the prompt (1.1.1)",
          "--model" in ag14m.argv and "Gemini 3.5 Flash (High)" in ag14m.argv
          and ag14m.argv.index("--model") < ag14m.argv.index("-p")
          and ag14m.model == "Gemini 3.5 Flash (High)")
    ag14x = build_real_spec("agy", "q", 30,
                            {"agy": {"model": "Gemini 3.5 Flash (Medium)", "thinking": "high"}},
                            wd("ro"))
    check("agy: cross-tier override records the LABEL's tier, not the mode's",
          ag14x.thinking == "medium")

    # S15 — read-only posture line (agy's defense-in-depth atop plan mode): prepended intact,
    # original prompt preserved, and identical for every member by construction.
    aug = apply_readonly_posture("original question")
    check("posture: line prepended", aug.startswith(READONLY_POSTURE))
    check("posture: original prompt preserved", aug.endswith("original question"))
    check("posture: main() honors --allow-writes wiring",
          parse_args(["--prompt", "x", "--allow-writes"]).read_only is False
          and parse_args(["--prompt", "x"]).read_only is True)
    ag15 = build_real_spec("agy", apply_readonly_posture("q"), 30, {}, wd("ro"))
    check("posture: reaches the agy argv (defense-in-depth layer)",
          any(READONLY_POSTURE in str(a) for a in ag15.argv))

    # S16 — agy worktree isolation: cwd redirected to a throwaway copy that mirrors the
    # working tree (incl. uncommitted tracked changes); cleanup removes it; non-repo no-op.
    repo16 = wd("wt_repo")
    gitc = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo16)]
    subprocess.run(gitc[:5] + ["-C", str(repo16), "init", "-q"], capture_output=True)
    (repo16 / "f.txt").write_text("committed")
    (repo16 / "sub").mkdir()
    (repo16 / "sub" / "g.txt").write_text("sub-file")
    (repo16 / "b.bin").write_bytes(bytes(range(256)))
    subprocess.run(gitc + ["add", "-A"], capture_output=True)
    subprocess.run(gitc + ["commit", "-q", "-m", "c1"], capture_output=True)
    (repo16 / "f.txt").write_text("working-tree")
    (repo16 / "b.bin").write_bytes(bytes(reversed(range(256))))  # dirty BINARY change
    # CRLF + non-UTF-8 (latin-1) content: a text-mode pipe would newline-translate or
    # raise UnicodeDecodeError — the mirror must stay byte-exact for such repos too.
    (repo16 / "crlf.txt").write_bytes(b"caf\xe9 line one\r\nline two\r\n")
    subprocess.run(gitc + ["add", "crlf.txt"], capture_output=True)
    subprocess.run(gitc + ["commit", "-q", "-m", "c2"], capture_output=True)
    (repo16 / "crlf.txt").write_bytes(b"caf\xe9 CHANGED\r\nline two\r\n")
    ag16 = build_real_spec("agy", "q", 30, {}, wd("wt_wd"))
    handle = isolate_agy_worktree(ag16, wd("wt_wd"), repo_dir=str(repo16))
    check("worktree: cwd redirected into workdir",
          handle is not None and ag16.cwd == handle[1]
          and str(wd("wt_wd")) in (ag16.cwd or ""))
    check("worktree: mirrors uncommitted working tree",
          handle is not None and (Path(handle[1]) / "f.txt").read_text() == "working-tree")
    check("worktree: mirrors dirty BINARY files (--binary diff)",
          handle is not None
          and (Path(handle[1]) / "b.bin").read_bytes() == bytes(reversed(range(256))))
    check("worktree: byte-exact for CRLF + non-UTF-8 content (bytes-mode pipe)",
          handle is not None
          and (Path(handle[1]) / "crlf.txt").read_bytes() == b"caf\xe9 CHANGED\r\nline two\r\n")
    if handle:
        (Path(handle[1]) / "escaped.txt").write_text("dirty")  # simulate a misbehaving agy
    remove_agy_worktree(handle)
    check("worktree: removed even when dirty",
          handle is not None and not Path(handle[1]).exists())
    # Invoked from a subdirectory: agy's cwd must be the SAME subdir inside the worktree,
    # so relative paths resolve identically for every member.
    ag16s = build_real_spec("agy", "q", 30, {}, wd("wt_wd_sub"))
    hs = isolate_agy_worktree(ag16s, wd("wt_wd_sub"), repo_dir=str(repo16 / "sub"))
    check("worktree: caller's subdir position preserved",
          hs is not None and ag16s.cwd == str(Path(hs[1]) / "sub")
          and Path(ag16s.cwd).is_dir())
    remove_agy_worktree(hs)
    ag16b = build_real_spec("agy", "q", 30, {}, wd("wt_wd2"))
    check("worktree: no-op outside a git repo",
          isolate_agy_worktree(ag16b, wd("wt_wd2"), repo_dir=str(wd("wt_norepo"))) is None
          and ag16b.cwd is None)

    # S17 — signal cleanup: a default-disposition SIGTERM skips `finally` (observed leak
    # 2026-07-11); the handler must remove registered worktrees and hard-exit 128+signum.
    # Direct handler test with os._exit stubbed — a subprocess signal test would be
    # disproportionate.
    repo17 = wd("sig_repo")
    g17 = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo17)]
    subprocess.run(g17[:5] + ["-C", str(repo17), "init", "-q"], capture_output=True)
    (repo17 / "f.txt").write_text("x")
    subprocess.run(g17 + ["add", "-A"], capture_output=True)
    subprocess.run(g17 + ["commit", "-q", "-m", "c"], capture_output=True)
    ag17 = build_real_spec("agy", "q", 30, {}, wd("sig_wd"))
    h17 = isolate_agy_worktree(ag17, wd("sig_wd"), repo_dir=str(repo17))
    check("signal: live worktree is registered", h17 is not None and h17 in _LIVE_WORKTREES)
    exit_codes: list = []
    real_exit = os._exit
    os._exit = exit_codes.append  # type: ignore[assignment] — stub; handler never returns in prod
    globals()["_HANDLER_FIRED"] = False
    _signal_cleanup(signal.SIGTERM, None)
    os._exit = real_exit  # type: ignore[assignment]
    check("signal: handler removed the worktree and deregistered it",
          h17 is not None and h17 not in _LIVE_WORKTREES and not Path(h17[1]).exists())
    check("signal: hard-exits with 128+signum (143)", exit_codes == [143])
    globals()["_HANDLER_FIRED"] = False  # reset for any later checks

    passed = sum(1 for _, ok, _ in results if ok)
    for label, ok, detail in results:
        line = f"  {'PASS' if ok else 'FAIL'}  {label}"
        if detail and not ok:
            line += f"   [{detail}]"
        print(line)
    print(f"\nself-test: {passed}/{len(results)} checks passed   (artifacts: {root})")
    return 0 if passed == len(results) else 1


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="llm-council fan-out engine")
    src = ap.add_argument_group("prompt source")
    src.add_argument("--prompt", help="prompt text (or use --prompt-file / stdin)")
    src.add_argument("--prompt-file", help="read prompt from a file (preferred)")
    ap.add_argument("--providers", default=",".join(DEFAULT_PROVIDERS),
                    help="comma list (default: claude,codex,agy)")
    ap.add_argument("--mode", choices=list(MODES), default=DEFAULT_MODE,
                    help=f"thinking mode → models+effort from the MODES table (default: {DEFAULT_MODE})")
    ro = ap.add_mutually_exclusive_group()
    ro.add_argument("--read-only", dest="read_only", action="store_true", default=True,
                    help="members read & plan only — they still use their skills but cannot "
                         "modify anything (default; the council's job is advice/synthesis)")
    ro.add_argument("--allow-writes", dest="read_only", action="store_false",
                    help="let members write/execute with full permissions (opt out of read-only)")
    ap.add_argument("--retries", type=int, default=2, help="max retries per provider")
    ap.add_argument("--timeout", type=int, default=None,
                    help=f"per-attempt seconds (default: per-mode — "
                         f"normal {MODE_TIMEOUT['normal']}, deep {MODE_TIMEOUT['deep']})")
    ap.add_argument("--backoff", type=float, default=5.0, help="base backoff seconds")
    ap.add_argument("--workdir", help="output dir (default: a fresh temp dir)")
    ap.add_argument("--model-claude")
    ap.add_argument("--model-codex")
    ap.add_argument("--model-agy")
    ap.add_argument("--out", choices=["json", "text"], default="json")
    ap.add_argument("--provider-cmd-override", action="append", default=[],
                    metavar="NAME=CMD", help="replace a provider's binary (test hook)")
    ap.add_argument("--self-test", action="store_true", help="run engine tests, exit")
    ap.add_argument("--smoke", action="store_true", help="live one-provider check")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test()
    if args.smoke:
        return smoke(args)

    depth = int(os.environ.get("LLM_COUNCIL_DEPTH", "0") or "0")
    if depth >= 1:
        err = {"schema": MANIFEST_SCHEMA, "error": "recursion_blocked",
               "detail": f"LLM_COUNCIL_DEPTH={depth}; refusing to fan out from inside a council run."}
        print(json.dumps(err, indent=2))
        return 2

    prompt = resolve_prompt(args)
    if not prompt.strip():
        print(json.dumps({"error": "empty_prompt",
                          "detail": "provide --prompt, --prompt-file, or pipe via stdin"}))
        return 2

    if args.read_only:
        prompt = apply_readonly_posture(prompt)

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="llm-council-"))
    workdir.mkdir(parents=True, exist_ok=True)
    timeout = effective_timeout(args)
    cfg = resolve_mode_config(args)
    specs = [build_real_spec(p, prompt, timeout, cfg, workdir) for p in providers]

    overrides = {}
    for item in args.provider_cmd_override:
        name, _, cmd = item.partition("=")
        overrides[name] = cmd.split()
    by_name = {s.name: s for s in specs}
    for name, tokens in overrides.items():
        if name in by_name and tokens:
            by_name[name].argv = tokens + by_name[name].argv[1:]

    # Read-only is the default council posture: claude/codex are mechanically
    # constrained (claude: plan mode + plan-file suppression; codex: read-only sandbox;
    # agy: --mode plan since 1.1.1) — agy additionally gets a throwaway-worktree cwd so
    # cwd-relative mutations are discarded (defense in depth). --allow-writes opts out. Applied after
    # overrides so a test override's binary is preserved (overrides replace argv[0]
    # only, so the bypass flag make_readonly swaps is always present).
    agy_wt = None
    if args.read_only:
        install_cleanup_handler()   # BEFORE any worktree exists — SIGTERM skips finally
        for s in specs:
            make_readonly(s)
        agy_spec = by_name.get("agy")  # plan-mode-constrained; worktree adds defense in depth
        if agy_spec:
            agy_wt = isolate_agy_worktree(agy_spec, workdir)

    try:
        manifest = run_council(specs, retries=args.retries, timeout=timeout,
                               backoff=args.backoff, workdir=workdir, prompt=prompt,
                               requested=providers, mode=args.mode, read_only=args.read_only)
    finally:
        remove_agy_worktree(agy_wt)
    print(json.dumps(manifest, indent=2) if args.out == "json" else _render_text(manifest))
    return 0 if manifest["summary"]["valid"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
