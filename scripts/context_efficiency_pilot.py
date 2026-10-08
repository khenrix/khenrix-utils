#!/usr/bin/env python3
"""Bounded fictional native pilot; normal installed configuration remains active.

The owner runs live sessions separately. Directory separation is a trusted
same-user reading convention, not OS confinement or credential blindness.
No native consuming-route token counter has been established: candidate
injection stays unavailable, and this fixture cannot establish a 20% saving.
Live steps execute baseline controls/baseline trials only. Unsupported candidate
and evaluation steps are durably marked unavailable without a launch, allowing
later baselines to proceed. Independent Maka evaluations require the owner's
separate existing native evaluation route.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared/lib"))
from fanout.errors import ProviderProtocolError
from fanout.process import ProcessCommand, ProcessStatus, _supervise_command
from fanout.providers import AgyAdapter, ClaudeAdapter, CodexAdapter

CORPUS = ROOT / "tests/fixtures/context_efficiency/model-input"
CORPUS_SHA256 = "51bfff57d3a7d094f02745cbe63ada827180815d947ccc1d5791678d1e8e4047"
CONTROL_SOURCE = "sources/crate-attempts.py"
TARGETS = {
    "codex": {"version": "codex-cli 0.160.1", "model": "gpt-6.1-sol", "effort": "medium", "route": "owner-selected"},
    "claude": {"version": "2.1.284 (Claude Code)", "model": "claude-opus-5-5", "effort": "medium", "route": "owner-selected"},
    "agy": {"version": "1.3.1", "model": "gemini-3.8-flash-high", "effort": "high", "route": "owner-selected"},
    "maka": {"version": "0.2.0-dev.70.20261005", "model": "gpt-6.1-sol", "effort": "medium", "route": "owner-selected"},
}
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens",
                "output_tokens", "provider_requests", "consumed_input_tokens")


class PilotError(ValueError):
    """Drift, uncertainty, exhausted capacity, or unavailable native operation."""


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def corpus_identity(directory):
    directory = Path(directory).resolve()
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink():
        raise PilotError("corpus manifest is a symlink")
    raw = manifest_path.read_bytes()
    document = json.loads(raw)
    if document.get("classification") != "fictional-synthetic" or document.get("schema_version") != 1:
        raise PilotError("corpus is not approved fictional input")
    if len(document.get("cases", [])) != 20:
        raise PilotError("corpus requires the frozen twenty-case packet")
    sources = {}
    for source in document["sources"]:
        path = directory / source["path"]
        if (path.is_symlink() or not path.resolve().is_relative_to(directory / "sources")
                or source["path"] in sources):
            raise PilotError("source path is unsafe or duplicated")
        actual = digest(path.read_bytes())
        if actual != source["sha256"]:
            raise PilotError("source hash drift")
        sources[source["path"]] = actual
    return {"manifest_sha256": digest(raw), "sources": sources}


def verify_identity(directory, expected):
    if corpus_identity(directory) != expected:
        raise PilotError("corpus identity drift")


def implementation_identity():
    paths = ("scripts/context_efficiency_pilot.py", "shared/lib/fanout/process.py",
             "shared/lib/fanout/providers.py", "shared/lib/fanout/context_view.py")
    return {path: digest((ROOT / path).read_bytes()) for path in paths}


def prepare_input(source, destination, identity):
    verify_identity(source, identity)
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for path in ("manifest.json", *identity["sources"]):
        target = destination / path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(Path(source) / path, target)
        target.chmod(0o600)
    verify_identity(destination, identity)


def campaign_plan():
    sessions = []
    for provider in TARGETS:
        sessions.append({"id": f"{provider}-control", "provider": provider,
                         "kind": "baseline-control", "arm": "baseline",
                         "condition": "control", "observed_condition": None})
    for index, provider in enumerate(TARGETS):
        for condition in ("cold", "warm"):
            arms = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
            for arm in arms:
                sessions.append({"id": f"{provider}-{arm}-{condition}", "provider": provider,
                    "kind": "trial", "arm": arm, "condition": condition, "observed_condition": None})
    for index, order in enumerate((["A", "B"], ["B", "A"])):
        sessions.append({"id": f"maka-evaluation-{index + 1}", "provider": "maka",
            "kind": "evaluation", "arm_order": order, "condition": "independent",
            "observed_condition": None})
    return sessions


def aggregate_usage(records):
    records = tuple(records)
    return {name: sum(record[name] for record in records)
            if records and all(type(record.get(name)) is int and record[name] >= 0 for record in records)
            else None for name in USAGE_FIELDS}


def comparison_decision(baseline, candidate, *, counter_available):
    # Quality/citation judgments and representative measurement belong to Task 4.
    return "retain-baseline"


def case_outputs(answer, manifest):
    envelope = re.fullmatch(r"\s*```json\r?\n(.*?)\r?\n```\s*", answer, re.DOTALL) if isinstance(answer, str) else None
    packet = json.loads(envelope[1] if envelope and "```" not in envelope[1] else answer)
    cases = packet.get("cases") if isinstance(packet, dict) else None
    expected = {case["id"]: case for case in manifest["cases"]}
    sources = {source["id"]: source for source in manifest["sources"]}
    if (not isinstance(cases, list) or len(cases) != 20
            or any(not isinstance(case, dict) or case.get("id") not in expected for case in cases)
            or len({case["id"] for case in cases}) != 20):
        raise PilotError("case packet is missing, duplicated, or unknown")
    for case in cases:
        if not isinstance(case.get("answer"), str) or not case["answer"].strip():
            raise PilotError("case answer is unavailable")
        evidence = case.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise PilotError("case evidence is not an approved source/locator")
        approved = {json.dumps(citation, sort_keys=True) for citation in expected[case["id"]]["citations"]}
        for citation in evidence:
            if (not isinstance(citation, dict) or not {"source_id", "locator"} <= citation.keys()
                    or citation.keys() - {"source_id", "locator", "author", "revision"}
                    or not isinstance(citation["source_id"], str) or citation["source_id"] not in sources):
                raise PilotError("case evidence has an invalid citation shape or source")
            base = {"source_id": citation["source_id"], "locator": citation["locator"]}
            source = sources[citation["source_id"]]
            if (json.dumps(base, sort_keys=True) not in approved
                    or any(field in citation and citation[field] != source[field] for field in ("author", "revision"))):
                raise PilotError("case evidence has a nonexact locator or provenance")
    return cases


def _json_events(raw):
    events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    if not events or any(not isinstance(event, dict) for event in events):
        raise PilotError("native events are unavailable")
    return events


def _required_path(value, cwd):
    if not isinstance(value, str):
        return False
    path = Path(value)
    if path.is_absolute():
        return cwd is not None and path == Path(cwd) / CONTROL_SOURCE
    return path.as_posix() == CONTROL_SOURCE


def _required_command(command, cwd):
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
        if (len(words) == 3 and words[0] in {"bash", "zsh", "sh", "/bin/bash", "/bin/zsh", "/bin/sh"}
                and words[1] in {"-c", "-lc"}):
            words = shlex.split(words[2])
    except ValueError:
        return False
    if not words:
        return False
    # Recognize literal standalone reads only; compound shell syntax is unknown.
    if words[0] in {"cat", "/bin/cat", "/usr/bin/cat"}:
        arguments = words[1:]
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        return len(arguments) == 1 and _required_path(arguments[0], cwd)
    return (len(words) == 4 and words[0] in {"sed", "/usr/bin/sed", "/bin/sed"}
            and words[1] == "-n" and re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*|,\$)?p", words[2]) is not None
            and _required_path(words[3], cwd))


def _content(event):
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [part for part in content if isinstance(part, dict)] if isinstance(content, list) else []


def _control_read(provider, events, cwd):
    outcomes = []
    if provider == "codex":
        for event in events:
            item = event.get("item")
            if (event.get("type") != "item.completed" or not isinstance(item, dict)
                    or item.get("type") != "command_execution"
                    or not _required_command(item.get("command"), cwd)):
                continue
            if item.get("status") == "failed":
                outcomes.append(False)
            elif (item.get("status") == "completed" and item.get("exit_code") == 0
                    and isinstance(item.get("aggregated_output"), str) and item["aggregated_output"].strip()):
                outcomes.append(True)
    elif provider == "claude":
        calls = {}
        ambiguous = set()
        for event in events:
            for part in _content(event) if event.get("type") == "assistant" else []:
                identity, arguments = part.get("id"), part.get("input")
                if part.get("type") != "tool_use" or not isinstance(identity, str):
                    continue
                if identity in calls:
                    ambiguous.add(identity)
                calls[identity] = (isinstance(arguments, dict) and (
                    part.get("name") == "Read" and _required_path(arguments.get("file_path"), cwd)
                    or part.get("name") == "Bash" and _required_command(arguments.get("command"), cwd)))
        for event in events:
            for part in _content(event) if event.get("type") == "user" else []:
                identity = part.get("tool_use_id")
                if (part.get("type") != "tool_result" or not isinstance(identity, str)
                        or identity in ambiguous or calls.get(identity) is not True):
                    continue
                if part.get("is_error") is True:
                    outcomes.append(False)
                elif part.get("is_error") is False or part.get("is_error") is None:
                    content = part.get("content")
                    if isinstance(content, str) and content.strip() or isinstance(content, list) and any(
                        isinstance(block, dict) and block.get("type") == "text"
                        and isinstance(block.get("text"), str) and block["text"].strip() for block in content):
                        outcomes.append(True)
    # Unsupported MCP/read contracts or unlinked telemetry remain unknown.
    return False if False in outcomes else True if True in outcomes else None


def parse_native(provider, raw, *, cwd=None):
    usage = dict.fromkeys(USAGE_FIELDS)
    if provider == "maka":
        answer = raw.decode("utf-8").strip()
        return {"valid": bool(answer), "reason": "ok" if answer else "protocol-error",
                "answer": answer, "session_id": None, "observed_model": None,
                "tool_access": None, "usage": usage}
    events = []
    if provider == "claude":
        events = _json_events(raw)
        terminals = [event for event in events if event.get("type") == "result"]
        if len(terminals) != 1 or events[-1] is not terminals[0]:
            raise PilotError("Claude native terminal is missing or ambiguous")
        parsed = ClaudeAdapter().parse(json.dumps(terminals[0]).encode())
        # num_turns is a native agent-turn metric, not a provider-request count.
        read = _control_read(provider, events, cwd)
    elif provider == "codex":
        parsed = CodexAdapter().parse(raw)
        events = _json_events(raw)
        read = _control_read(provider, events, cwd)
    elif provider == "agy":
        parsed = AgyAdapter().parse(raw)
        # The licensed parser establishes terminal usage, not successful read evidence.
        read = None
    else:
        raise PilotError("unknown pilot provider")
    native = parsed.usage or {}
    for source, target in (("input", "input_tokens"), ("cache_read", "cached_input_tokens"),
                           ("cache_write", "cache_creation_input_tokens"), ("output", "output_tokens")):
        usage[target] = native.get(source)
    if provider == "claude":
        parts = [usage[name] for name in ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens")]
        usage["consumed_input_tokens"] = sum(parts) if all(type(part) is int for part in parts) else None
    elif provider == "codex":
        usage["consumed_input_tokens"] = usage["input_tokens"]
    valid = bool(parsed.final and parsed.session_id and parsed.answer and not parsed.error
                 and not parsed.successful_file_change)
    return {"valid": valid, "reason": "ok" if valid else "protocol-error", "answer": parsed.answer,
            "session_id": parsed.session_id, "observed_model": parsed.observed_model,
            "tool_access": read, "usage": usage}


def native_argv(provider, cwd, *, evaluation=False, wall_seconds=120, tool_steps=40):
    target = TARGETS[provider]
    model, effort = target["model"], target["effort"]
    if provider == "codex":
        return ("codex", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check",
                "-m", model, "-c", f'model_reasoning_effort="{effort}"', "-")
    if provider == "claude":
        return ("claude", "--print", "--verbose", "--output-format", "stream-json",
                "--model", model, "--effort", effort, "--permission-mode", "plan",
                "--permission-prompts", "none")
    if provider == "agy":
        return ("agy", "--mode", "plan", "--input-format", "stream-json", "--output-format",
                "stream-json", "--model", model, "--effort", effort, "--print-timeout", f"{wall_seconds}s")
    return ("maka", "run", "-", "--cwd", str(cwd),
            "--model", model, "--thinking", "xhigh" if evaluation else effort,
            "--timeout", str(wall_seconds), "--max-steps", str(tool_steps))


def check_native_version(provider):
    result = subprocess.run([provider, "--version"], capture_output=True, timeout=10, check=False)
    if result.returncode != 0 or result.stdout.decode().strip() != TARGETS[provider]["version"]:
        raise PilotError("native CLI version drift or unavailable")


class Campaign:
    """One durable 22-session ledger; reservation is persisted before spawn."""

    def __init__(self, root, corpus=CORPUS):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise PilotError("run directory must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077 or self.root.stat().st_uid != os.getuid():
            raise PilotError("run directory must be owner-only")
        self.corpus = Path(corpus)
        self.plan = campaign_plan()
        self.path = self.root / "manifest.json"
        with self._lock():
            if self.path.exists():
                self._load()
                verify_identity(self.corpus, self.manifest["corpus"])
                if self.manifest["implementation"] != implementation_identity() or self.manifest["targets"] != TARGETS:
                    raise PilotError("implementation or native target drift")
            else:
                identity = corpus_identity(self.corpus)
                if identity["manifest_sha256"] != CORPUS_SHA256:
                    raise PilotError("corpus is not frozen")
                self.manifest = {"schema": "context-efficiency-pilot-v1", "corpus": identity,
                    "implementation": implementation_identity(), "targets": TARGETS,
                    "plan": self.plan, "maximum_outer_sessions": 22, "automatic_retries": 0,
                    "outer_session_attempts": 0, "confirmed_outer_launches": 0, "attempts": [],
                    "unavailable_steps": [],
                    "provider_requests": None, "counter_available": False, "decision": "retain-baseline",
                    "stopped": None, "boundary": "trusted-same-user-normal-native-read"}
                self._save()

    @contextlib.contextmanager
    def _lock(self):
        descriptor = os.open(self.root / "ledger.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _load(self):
        if self.path.is_symlink():
            raise PilotError("manifest must not be a symlink")
        self.manifest = json.loads(self.path.read_text())
        attempts = self.manifest["attempts"]
        unavailable = self.manifest.get("unavailable_steps", [])
        paid_ids = [attempt["id"] for attempt in attempts]
        unavailable_ids = [step["id"] for step in unavailable]
        processed = paid_ids + unavailable_ids
        prefix = self.plan[:len(processed)]
        if (self.manifest["schema"] != "context-efficiency-pilot-v1"
                or self.manifest["plan"] != self.plan
                or len(attempts) != self.manifest["outer_session_attempts"]
                or len(attempts) > 22
                or len(processed) > 22 or len(set(processed)) != len(processed)
                or set(processed) != {session["id"] for session in prefix}
                or paid_ids != [session["id"] for session in prefix if session["id"] not in unavailable_ids]
                or unavailable_ids != [session["id"] for session in prefix if session["id"] not in paid_ids]
                or any(step["reason"] != _unavailable_reason(next(session for session in self.plan if session["id"] == step["id"]))
                       or step["reason"] is None for step in unavailable)):
            raise PilotError("campaign ledger is invalid")
        self.manifest["unavailable_steps"] = unavailable

    def _save(self):
        temporary = self.root / "manifest.next"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(self.manifest, stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def reserve(self, session_id):
        with self._lock():
            self._load()
            verify_identity(self.corpus, self.manifest["corpus"])
            if self.manifest["implementation"] != implementation_identity():
                raise PilotError("implementation drift")
            if self.manifest["stopped"]:
                raise PilotError("campaign is stopped: " + self.manifest["stopped"])
            if any(attempt["status"] == "uncertain" for attempt in self.manifest["attempts"]):
                raise PilotError("uncertain paid attempt retained; replay is forbidden")
            if self.manifest["outer_session_attempts"] >= 22:
                raise PilotError("outer session budget exhausted")
            cursor = len(self.manifest["attempts"]) + len(self.manifest["unavailable_steps"])
            if cursor >= len(self.plan) or session_id != self.plan[cursor]["id"]:
                raise PilotError("session order or replay refused")
            self.manifest["attempts"].append({**self.plan[cursor], "status": "uncertain"})
            self.manifest["outer_session_attempts"] += 1
            self._save()

    def next_session(self):
        with self._lock():
            self._load()
            if self.manifest["stopped"]:
                raise PilotError("campaign is stopped: " + self.manifest["stopped"])
            if any(attempt["status"] == "uncertain" for attempt in self.manifest["attempts"]):
                raise PilotError("uncertain paid attempt retained; replay is forbidden")
            cursor = len(self.manifest["attempts"]) + len(self.manifest["unavailable_steps"])
            return self.plan[cursor] if cursor < len(self.plan) else None

    def unavailable(self, session_id, reason):
        with self._lock():
            self._load()
            verify_identity(self.corpus, self.manifest["corpus"])
            if self.manifest["implementation"] != implementation_identity():
                raise PilotError("implementation drift")
            if self.manifest["stopped"]:
                raise PilotError("campaign is stopped: " + self.manifest["stopped"])
            if any(attempt["status"] == "uncertain" for attempt in self.manifest["attempts"]):
                raise PilotError("uncertain paid attempt retained; replay is forbidden")
            cursor = len(self.manifest["attempts"]) + len(self.manifest["unavailable_steps"])
            if (cursor >= len(self.plan) or self.plan[cursor]["id"] != session_id
                    or reason is None or reason != _unavailable_reason(self.plan[cursor])):
                raise PilotError("unavailable step order or reason refused")
            self.manifest["unavailable_steps"].append({"id": session_id, "reason": reason})
            self._save()

    def finish(self, session_id, result, **evidence):
        with self._lock():
            self._load()
            attempt = self.manifest["attempts"][-1]
            if attempt["id"] != session_id or attempt["status"] != "uncertain":
                raise PilotError("only the reserved attempt may finish")
            attempt.update(status="complete", result=result, **evidence)
            if not result.get("valid"):
                self.manifest["stopped"] = result.get("reason", "protocol-error")
            elif attempt["kind"] == "baseline-control" and result.get("tool_access") is False:
                self.manifest["stopped"] = "required-tool-access-failed"
            self._save()

    def started(self, session_id, pid):
        with self._lock():
            self._load()
            attempt = self.manifest["attempts"][-1]
            if (attempt["id"] != session_id or attempt["status"] != "uncertain"
                    or attempt.get("pid") is not None or type(pid) is not int or pid <= 0):
                raise PilotError("native start must match one reserved launch")
            attempt["pid"] = pid
            self.manifest["confirmed_outer_launches"] += 1
            self._save()


def _write_artifact(root, name, raw):
    path = root / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": name, "sha256": digest(raw), "bytes": len(raw)}


def _unavailable_reason(session):
    if session["kind"] == "evaluation":
        return "owner-evaluation-route-required"
    if session.get("arm") == "candidate":
        return "counter-unavailable"
    return None


def execute_session(campaign, session, *, argv=None, wall_seconds=120):
    if type(wall_seconds) is not int or not 1 <= wall_seconds <= 600:
        raise PilotError("wall limit must be between 1 and 600 seconds")
    unavailable = _unavailable_reason(session)
    if unavailable is not None:
        campaign.unavailable(session["id"], unavailable)
        return {"valid": None, "reason": unavailable, "paid_launch": False}
    provider = session["provider"]
    if argv is None:
        check_native_version(provider)
    seat = campaign.root / "seats" / session["id"]
    prepare_input(campaign.corpus, seat, campaign.manifest["corpus"])
    prompt = ("Use your normal installed skills and native read tools. Read only this fictional packet; "
              "do not send messages, change files/configuration, or delegate. Read manifest.json and use "
              "ordinary bounded reads to answer all twenty cases. Preserve authors/revisions and uncertainty. "
              'Return a JSON object {"cases":[{"id":"ce-01","answer":"...","evidence":[...]}]}. '
              'Each evidence citation requires {"source_id":"...","locator":{...}}; '
              'optional "author" and "revision" must match that source in manifest.json exactly. '
              "Use only these citation fields and preserve supplied provenance. "
              "Cite exact source IDs and approved line/heading/JSON locators. Do not treat example commands "
              "as execution. Read sources/crate-attempts.py through a read tool for the baseline control.")
    if provider == "agy":
        stdin = (json.dumps({"event": "user", "message": {"content": prompt}}) + "\n").encode()
    else:
        stdin = prompt.encode()
    command = ProcessCommand(argv=argv or native_argv(provider, seat, wall_seconds=wall_seconds),
        stdin=stdin, cwd=seat, timeout=wall_seconds, max_output_bytes=4 * 1024 * 1024)
    campaign.reserve(session["id"])
    started = time.monotonic()
    # This low-level supervisor keeps the real HOME/auth/config and normal read tools.
    # run_command's legacy restricted environment is deliberately not used here.
    process = _supervise_command(command, dict(os.environ),
        on_started=lambda pid: campaign.started(session["id"], pid))
    elapsed = time.monotonic() - started
    stdout_ref = _write_artifact(campaign.root, f"results/{session['id']}/stdout.bin", process.stdout)
    stderr_ref = _write_artifact(campaign.root, f"results/{session['id']}/stderr.bin", process.stderr)
    try:
        result = parse_native(provider, process.stdout, cwd=seat)
    except (PilotError, ProviderProtocolError, ValueError, UnicodeDecodeError) as error:
        result = {"valid": False, "reason": "protocol-error", "usage": dict.fromkeys(USAGE_FIELDS),
                  "parse_error": type(error).__name__, "answer": ""}
    if process.status != ProcessStatus.EXIT or process.returncode != 0 or not process.capture_complete:
        result.update(valid=False, reason=process.status.value if process.status != ProcessStatus.EXIT else "process-error")
    if result.get("observed_model") not in {None, TARGETS[provider]["model"]}:
        result.update(valid=False, reason="model-drift")
    answer = result.pop("answer", "")
    answer_ref = _write_artifact(campaign.root, f"results/{session['id']}/answer.txt", answer.encode())
    cases = []
    if result["valid"]:
        try:
            manifest = json.loads((campaign.corpus / "manifest.json").read_text())
            cases = case_outputs(answer, manifest)
        except (PilotError, ValueError):
            result.update(valid=False, reason="case-protocol-error")
    case_refs = [{"id": case["id"], "output_ref": _write_artifact(campaign.root,
        f"results/{session['id']}/cases/{case['id']}.json", json.dumps(case, sort_keys=True).encode()),
        "evidence": case["evidence"]} for case in cases]
    campaign.finish(session["id"], result, stdout_ref=stdout_ref, stderr_ref=stderr_ref,
        answer_ref=answer_ref, case_refs=case_refs, elapsed_seconds=elapsed, pid=process.pid,
        process_status=process.status.value, returncode=process.returncode,
        capture_complete=process.capture_complete,
        native_turn_limit=None, wall_seconds=wall_seconds,
        requested_route=TARGETS[provider]["route"], observed_route=None,
        observed_condition="native-cache-read" if (result.get("usage", {}).get("cached_input_tokens") or 0) > 0 else None)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print safe counts; zero native/service launches and no writes")
    mode.add_argument("--execute-next", action="store_true", help="owner-only live step; never retries uncertain attempts")
    parser.add_argument("--run-dir", type=Path, help="persistent owner-only campaign directory")
    parser.add_argument("--wall-seconds", type=int, default=120)
    options = parser.parse_args(argv)
    try:
        identity = corpus_identity(CORPUS)
        if identity["manifest_sha256"] != CORPUS_SHA256:
            raise PilotError("corpus is not frozen")
        if not 1 <= options.wall_seconds <= 600:
            raise PilotError("wall limit must be between 1 and 600 seconds")
        if options.dry_run:
            print(json.dumps({"scope": "fictional-synthetic", "cases": 20, "source_bytes":
                sum((CORPUS / name).stat().st_size for name in identity["sources"]),
                "corpus_sha256": identity["manifest_sha256"], "planned_outer_sessions": 22,
                "baseline_controls": 4, "trial_sessions": 16, "independent_maka_evaluations": 2,
                "paid_launches": 0, "automatic_retries": 0, "provider_requests": None,
                "counter_available": False, "decision": "retain-baseline"}, sort_keys=True))
            return 0
        if options.run_dir is None:
            raise PilotError("live execution requires a persistent --run-dir")
        campaign = Campaign(options.run_dir)
        session = campaign.next_session()
        if session is None:
            print(json.dumps({"outer_session_attempts": campaign.manifest["outer_session_attempts"],
                "unavailable_steps": len(campaign.manifest["unavailable_steps"]),
                "planned_steps_complete": True, "decision": "retain-baseline"}))
            return 0
        result = execute_session(campaign, session, wall_seconds=options.wall_seconds)
        print(json.dumps({"outer_session_attempts": campaign.manifest["outer_session_attempts"],
            "unavailable_steps": len(campaign.manifest["unavailable_steps"]), "reason": result["reason"],
            "valid": result["valid"], "stopped": campaign.manifest["stopped"], "decision": "retain-baseline"}))
        return 0 if result["valid"] is not False and not campaign.manifest["stopped"] else 1
    except (PilotError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(json.dumps({"error": str(error), "decision": "retain-baseline"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
