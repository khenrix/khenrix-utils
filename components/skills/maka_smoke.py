#!/usr/bin/env python3
"""Run bounded explicit and natural Maka routing smoke cases.

The runner never passes ``--yolo``.  Every case receives a fresh temporary cwd,
and the summary receipt is bound to the case manifest, installed skill hashes,
and the observed Maka version. The runner verifies explicit admissions and
natural ``model_tool`` loads against Maka's read-only SQLite event records;
behavior quality remains covered by the provider eval harness.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import skillctl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "maka" / "interactive"))
from maka_profile import MakaProfileError, resolve_maka_profile  # noqa: E402


class SmokeError(RuntimeError):
    """A smoke precondition or bounded invocation failed."""


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def default_runtime_db(
    home: Path,
    *,
    platform: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve Maka's managed platform profile instead of assuming macOS."""
    try:
        profile = resolve_maka_profile(
            home, platform=platform, environment=environment, managed=True
        )
    except MakaProfileError as error:
        raise SmokeError(str(error)) from error
    return profile / "workspaces" / "default" / "runtime.sqlite"


def load_cases(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SmokeError(f"cannot read smoke manifest {path}: {error}") from error
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise SmokeError("smoke manifest schema_version must be 1")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SmokeError("smoke manifest requires cases")
    ids: set[str] = set()
    routes: set[tuple[str, str]] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise SmokeError("each smoke case must be an object")
        required = {"id", "route", "skill", "prompt", "timeout_seconds", "max_steps"}
        if not required <= set(case):
            raise SmokeError(f"smoke case is missing {sorted(required - set(case))}")
        if case["id"] in ids:
            raise SmokeError(f"duplicate smoke case ID {case['id']!r}")
        ids.add(case["id"])
        if case["route"] not in {"explicit", "natural"}:
            raise SmokeError(f"case {case['id']} has invalid route")
        if case["skill"] not in {"khenrix-quality", "khenrix-writing"}:
            raise SmokeError(f"case {case['id']} has unmanaged skill")
        token = f"/skill:{case['skill']}"
        if case["route"] == "explicit" and token not in case["prompt"]:
            raise SmokeError(f"explicit case {case['id']} lacks {token}")
        if case["route"] == "natural" and "/skill:" in case["prompt"]:
            raise SmokeError(f"natural case {case['id']} contains an explicit skill token")
        if not isinstance(case["timeout_seconds"], int) or not 1 <= case["timeout_seconds"] <= 300:
            raise SmokeError(f"case {case['id']} timeout must be 1..300 seconds")
        if not isinstance(case["max_steps"], int) or not 1 <= case["max_steps"] <= 4:
            raise SmokeError(f"case {case['id']} max_steps must be 1..4")
        routes.add((case["skill"], case["route"]))
    expected = {
        ("khenrix-quality", "explicit"),
        ("khenrix-quality", "natural"),
        ("khenrix-writing", "explicit"),
        ("khenrix-writing", "natural"),
    }
    if routes != expected:
        raise SmokeError(f"smoke manifest must cover each skill explicitly and naturally; found {routes}")
    flows = data.get("flows")
    if not isinstance(flows, list) or len(flows) != 1:
        raise SmokeError("smoke manifest requires one bounded multi-turn ADHD flow")
    flow = flows[0]
    if not isinstance(flow, dict) or flow.get("skill") != "khenrix-quality":
        raise SmokeError("ADHD flow must target khenrix-quality")
    steps = flow.get("steps")
    if not isinstance(steps, list) or len(steps) != 3:
        raise SmokeError("ADHD flow must contain activate, follow-up, and opt-out steps")
    if steps[0].get("continue") is not False or "/skill:khenrix-quality" not in steps[0].get("prompt", ""):
        raise SmokeError("ADHD flow must begin with explicit khenrix-quality activation")
    if any(step.get("continue") is not True for step in steps[1:]):
        raise SmokeError("ADHD follow-up and opt-out steps must continue the same session")
    if steps[1].get("check") != "adhd_action_first":
        raise SmokeError("ADHD follow-up must use the structural action-first check")
    if steps[2].get("check") != "normal_mode_ack":
        raise SmokeError("ADHD opt-out must use the bounded state-acknowledgement check")
    if any("must_contain" in step for step in steps[1:]):
        raise SmokeError("ADHD behavior steps must not put the expected answer in the prompt")
    for step in steps:
        if not isinstance(step.get("timeout_seconds"), int) or not 1 <= step["timeout_seconds"] <= 300:
            raise SmokeError(f"ADHD step {step.get('id')} timeout must be 1..300 seconds")
        if not isinstance(step.get("max_steps"), int) or not 1 <= step["max_steps"] <= 4:
            raise SmokeError(f"ADHD step {step.get('id')} max_steps must be 1..4")
    return data


def installed_closure(config: skillctl.Configuration, manifest: dict[str, Any]) -> dict[str, Any]:
    _, entries = skillctl.plan(config)
    drift = [entry.key for entry in entries if entry.action != "MATCH"]
    if drift:
        raise SmokeError("skills and instructions must be applied before Maka smoke: " + ", ".join(drift))
    receipt_path = config.state_dir / skillctl.RECEIPT_NAME
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise SmokeError(f"missing safe install receipt: {receipt_path}")
    try:
        receipt = json.loads(receipt_path.read_text())
    except json.JSONDecodeError as error:
        raise SmokeError(f"invalid install receipt: {error}") from error
    skillctl.validate_install_receipt_header(receipt)
    skills: dict[str, str] = {}
    for name in config.skills:
        desired = next(
            entry.desired_hash for entry in entries if entry.kind == "skill" and entry.skill == name
        )
        recorded = receipt.get("skills", {}).get(name, {}).get("source_hash")
        if recorded != desired:
            raise SmokeError(f"install receipt is stale for {name}")
        skills[name] = desired
    return {
        "manifest_hash": digest(canonical(manifest)),
        "runner_hash": digest(Path(__file__).read_bytes()),
        "skill_hashes": skills,
        "install_plan_id": receipt.get("plan_id"),
    }


def maka_version(binary: str) -> str:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeError(f"cannot run {binary} --version: {error}") from error
    if completed.returncode:
        raise SmokeError(f"{binary} --version failed: {completed.stderr.strip()}")
    return completed.stdout.strip() or completed.stderr.strip() or "unknown"


def open_runtime_db(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise SmokeError(f"Maka runtime database is missing or unsafe: {path}")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        required = {"session_metadata", "message_admissions", "core_agent_run_events"}
        actual = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
                tuple(sorted(required)),
            )
        }
        if actual != required:
            connection.close()
            raise SmokeError(f"Maka runtime database lacks event tables: {sorted(required - actual)}")
        return connection
    except sqlite3.Error as error:
        raise SmokeError(f"cannot inspect Maka runtime database {path}: {error}") from error


def locate_session(connection: sqlite3.Connection, cwd: Path, started_ms: int) -> str:
    rows = connection.execute(
        "SELECT session_id, payload_json, created_at FROM session_metadata "
        "WHERE created_at >= ? ORDER BY created_at, session_id",
        (started_ms - 1000,),
    ).fetchall()
    matches: list[str] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as error:
            raise SmokeError(f"Maka session {row['session_id']} has invalid metadata JSON") from error
        if payload.get("cwd") == str(cwd.resolve()):
            matches.append(row["session_id"])
    if len(matches) != 1:
        raise SmokeError(
            f"expected exactly one Maka session for temporary cwd {cwd}, found {len(matches)}"
        )
    return matches[0]


def verify_explicit_event(
    connection: sqlite3.Connection, session_id: str, skill: str
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT sequence, skill_invocation_json FROM message_admissions "
        "WHERE session_id = ? ORDER BY sequence",
        (session_id,),
    ).fetchall()
    matches: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        try:
            invocation = json.loads(row["skill_invocation_json"])
        except json.JSONDecodeError as error:
            raise SmokeError(f"Maka admission {row['sequence']} has invalid skill JSON") from error
        for receipt in invocation.get("receipts", []):
            if (
                receipt.get("success") is True
                and receipt.get("invocation") == "explicit"
                and receipt.get("id") == skill
                and receipt.get("source") == "agents"
                and receipt.get("ref") == f"user:agents:{skill}"
            ):
                if receipt.get("truncated") is not False:
                    raise SmokeError(f"explicit {skill} admission used truncated instructions")
                if invocation.get("failed"):
                    raise SmokeError(f"explicit {skill} admission also recorded failures")
                if not any(item.get("id") == skill for item in invocation.get("loaded", [])):
                    raise SmokeError(f"explicit {skill} receipt lacks its loaded entry")
                matches.append((row["sequence"], receipt))
    if len(matches) != 1:
        raise SmokeError(
            f"expected one successful explicit admission for {skill}, found {len(matches)}"
        )
    sequence, receipt = matches[0]
    return {
        "kind": "message_admission",
        "sequence": sequence,
        "session_id": session_id,
        "invocation": "explicit",
        "skill_id": skill,
        "skill_ref": receipt["ref"],
        "skill_source": receipt["source"],
        "truncated": False,
        "evidence_hash": digest(canonical(receipt)),
    }


def verify_natural_event(
    connection: sqlite3.Connection, session_id: str, skill: str
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT event_id, record_json FROM core_agent_run_events "
        "WHERE session_id = ? AND event_type = 'skill_loaded' ORDER BY sequence",
        (session_id,),
    ).fetchall()
    matches: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except json.JSONDecodeError as error:
            raise SmokeError(f"Maka skill event {row['event_id']} has invalid JSON") from error
        data = record.get("data", {})
        if (
            data.get("success") is True
            and data.get("invocation") == "model_tool"
            and data.get("skillId") == skill
            and data.get("skillSource") == "agents"
            and data.get("skillRef") == f"user:agents:{skill}"
        ):
            if data.get("truncated") is not False:
                raise SmokeError(f"natural {skill} load used truncated instructions")
            shadow_fields = {
                "shadowCandidateCount",
                "shadowHitAt1",
                "shadowHitAt5",
                "shadowHitAt20",
            }
            if not shadow_fields <= set(data):
                raise SmokeError(f"natural {skill} load lacks shadow-selection evidence")
            candidate_count = data["shadowCandidateCount"]
            hits = (
                data["shadowHitAt1"],
                data["shadowHitAt5"],
                data["shadowHitAt20"],
            )
            if (
                not isinstance(candidate_count, int)
                or isinstance(candidate_count, bool)
                or candidate_count < 0
                or candidate_count > 20
                or not all(isinstance(hit, bool) for hit in hits)
            ):
                raise SmokeError(f"natural {skill} load has invalid shadow-selection evidence")
            if candidate_count == 0:
                if any(hits) or "shadowRank" in data:
                    raise SmokeError(f"natural {skill} load has inconsistent shadow-selection evidence")
            elif (
                data.get("shadowRank") != 1
                or hits != (True, True, True)
            ):
                raise SmokeError(
                    f"natural {skill} was not the first-ranked shadow-selection candidate"
                )
            matches.append((row["event_id"], data))
    if len(matches) != 1:
        raise SmokeError(f"expected one model_tool skill_loaded event for {skill}, found {len(matches)}")
    event_id, data = matches[0]
    return {
        "kind": "core_agent_run_event",
        "event_id": event_id,
        "session_id": session_id,
        "invocation": "model_tool",
        "skill_id": skill,
        "skill_ref": data["skillRef"],
        "skill_source": data["skillSource"],
        "truncated": False,
        "shadow_candidate_count": data["shadowCandidateCount"],
        "shadow_rank": data.get("shadowRank"),
        "evidence_hash": digest(canonical(data)),
    }


def run_command(binary: str, item: dict[str, Any], cwd: Path, *, continued: bool) -> subprocess.CompletedProcess[str]:
    command = [
        binary,
        "run",
        item["prompt"],
        "--cwd",
        str(cwd),
        "--timeout",
        str(item["timeout_seconds"]),
        "--max-steps",
        str(item["max_steps"]),
    ]
    if continued:
        command.append("--continue")
    if "--yolo" in command:
        raise SmokeError("internal error: smoke command attempted --yolo")
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=item["timeout_seconds"] + 30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeError(f"case {item['id']} could not complete: {error}") from error
    if completed.returncode:
        detail = completed.stderr.strip()[-1000:]
        raise SmokeError(f"case {item['id']} failed with {completed.returncode}: {detail}")
    if not completed.stdout.strip():
        raise SmokeError(f"case {item['id']} returned no model output")
    required = item.get("must_contain")
    if required and required not in completed.stdout:
        raise SmokeError(f"case {item['id']} output did not contain {required!r}")
    return completed


def verify_flow_output(step: dict[str, Any], stdout: str) -> dict[str, Any]:
    """Check session behavior without putting the expected answer in the prompt."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise SmokeError(f"flow step {step['id']} returned no non-empty lines")
    check = step.get("check")
    if check == "adhd_action_first":
        first = lines[0].lower()
        task_terms = ("pull request", "pr", "email", "lunch")
        action_verbs = ("open", "review", "read", "start", "pick", "choose", "reply", "prepare")
        if not any(term in first for term in task_terms) or not re.search(
            r"\b(" + "|".join(action_verbs) + r")\b", first
        ):
            raise SmokeError(
                "ADHD continuation did not lead with one concrete action from the neutral task"
            )
        return {
            "check": check,
            "first_line_actionable": True,
            "first_line_hash": digest(lines[0].encode()),
        }
    if check == "normal_mode_ack":
        normalized_output = " ".join(lines).casefold()
        normalized_prompt = " ".join(step["prompt"].split()).casefold()
        if normalized_output == normalized_prompt:
            raise SmokeError("normal-mode step merely echoed the opt-out prompt")
        acknowledged = (
            "normal mode" in normalized_output
            or (
                "adhd" in normalized_output
                and any(word in normalized_output for word in ("off", "disabled", "stopped"))
            )
        )
        if len(lines) > 2 or not acknowledged:
            raise SmokeError("normal-mode step did not give a bounded state acknowledgement")
        return {"check": check, "state_acknowledged": True, "line_count": len(lines)}
    raise SmokeError(f"flow step {step['id']} has unknown behavior check {check!r}")


def run_case(binary: str, case: dict[str, Any], runtime_db: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"khenrix-maka-{case['id']}-") as temporary:
        cwd = Path(temporary).resolve()
        started_ms = time.time_ns() // 1_000_000
        completed = run_command(binary, case, cwd, continued=False)
        with open_runtime_db(runtime_db) as connection:
            session_id = locate_session(connection, cwd, started_ms)
            evidence = (
                verify_explicit_event(connection, session_id, case["skill"])
                if case["route"] == "explicit"
                else verify_natural_event(connection, session_id, case["skill"])
            )
    return {
        "id": case["id"],
        "route": case["route"],
        "skill": case["skill"],
        "prompt_hash": digest(case["prompt"].encode()),
        "stdout_hash": digest(completed.stdout.encode()),
        "stdout_bytes": len(completed.stdout.encode()),
        "stderr_hash": digest(completed.stderr.encode()),
        "event_evidence": evidence,
        "status": "pass",
    }


def run_adhd_flow(binary: str, flow: dict[str, Any], runtime_db: Path) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="khenrix-maka-adhd-session-") as temporary:
        cwd = Path(temporary).resolve()
        started_ms = time.time_ns() // 1_000_000
        session_id: str | None = None
        evidence: dict[str, Any] | None = None
        for index, step in enumerate(flow["steps"]):
            completed = run_command(binary, step, cwd, continued=step["continue"])
            with open_runtime_db(runtime_db) as connection:
                observed = locate_session(connection, cwd, started_ms)
                if session_id is None:
                    session_id = observed
                    evidence = verify_explicit_event(connection, observed, flow["skill"])
                elif observed != session_id:
                    raise SmokeError("ADHD continuation opened a new session")
            results.append(
                {
                    "id": step["id"],
                    "continued": step["continue"],
                    "prompt_hash": digest(step["prompt"].encode()),
                    "stdout_hash": digest(completed.stdout.encode()),
                    "stdout_bytes": len(completed.stdout.encode()),
                    "behavior_evidence": (
                        None if index == 0 else verify_flow_output(step, completed.stdout)
                    ),
                    "status": "pass",
                }
            )
    assert session_id is not None and evidence is not None
    return {
        "id": flow["id"],
        "skill": flow["skill"],
        "session_id": session_id,
        "activation_evidence": evidence,
        "steps": results,
        "status": "pass",
    }


def write_receipt(path: Path, receipt: dict[str, Any], config: skillctl.Configuration) -> None:
    skillctl.write_private_json(path, receipt, config)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    result.add_argument("--home", type=Path, default=Path.home())
    result.add_argument("--state-dir", type=Path)
    result.add_argument("--manifest", type=Path, default=Path(__file__).with_name("maka-smoke.json"))
    result.add_argument("--maka-bin", default="maka")
    result.add_argument("--runtime-db", type=Path)
    result.add_argument("--dry-run", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        config = skillctl.load_configuration(
            options.repo_root.resolve(), options.home.resolve(), options.state_dir
        )
        manifest = load_cases(options.manifest.resolve())
        closure = installed_closure(config, manifest)
        version = maka_version(options.maka_bin)
        runtime_db = options.runtime_db or default_runtime_db(config.home)
        if options.dry_run:
            total = len(manifest["cases"]) + sum(len(flow["steps"]) for flow in manifest["flows"])
            print(f"Maka {version}; {total} bounded turns; --yolo absent")
            for case in manifest["cases"]:
                print(f"PLAN {case['id']} {case['route']} {case['skill']}")
            for flow in manifest["flows"]:
                print(f"PLAN {flow['id']} multi-turn {flow['skill']}")
            return 0
        results = [run_case(options.maka_bin, case, runtime_db) for case in manifest["cases"]]
        flows = [run_adhd_flow(options.maka_bin, flow, runtime_db) for flow in manifest["flows"]]
        receipt = {
            "schema_version": 1,
            "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "maka_version": version,
            "input_hash": digest(canonical({**closure, "maka_version": version})),
            **closure,
            "cases": results,
            "flows": flows,
            "bounded": {"yolo": False, "temporary_cwd_per_case": True},
        }
        receipt_path = config.state_dir / "maka-smoke-receipt.json"
        write_receipt(receipt_path, receipt, config)
        print(f"PASS {len(results)} Maka routing cases and {len(flows)} multi-turn flow")
        print(f"receipt {receipt_path}")
        return 0
    except (SmokeError, skillctl.DeliveryError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
