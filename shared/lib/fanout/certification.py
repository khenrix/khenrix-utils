"""Pure consistency checks for a multi-repository certification receipt.

A passing decision means only that the supplied receipt is internally complete.
The receipt is untrusted data: this module does not grant provider access or lift
the production repo-write gate. Live authority requires an independent owner-held
certificate for the final installed runtime and native boundary.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath


_PROVIDERS = ("claude", "codex", "agy")
_ROUTES = ("initial", "resume")
_DENIALS = (
    "other_target_read", "other_target_write", "git_objects_read",
    "git_objects_write", "controller_read", "owner_read", "descriptor_read",
    "sibling_seat_read", "credential_path_read", "symlink_alias_read",
    "hardlink_alias_read", "network_outbound", "network_inbound", "ipc",
    "mach", "keychain", "ssh_agent",
)
_CREDENTIALS = ("api_key", "adc", "cookie", "ssh_agent", "keychain", "controller_token")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,127}\Z")
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")

_ROOT_FIELDS = frozenset({
    "schema_version", "run_id", "source_commit", "runtime_sha256",
    "policy_renderer_sha256", "broker_sha256", "profile_template_sha256",
    "targets", "tasks", "providers", "two_repo_result", "maka", "quality",
})
_TARGET_FIELDS = frozenset({"id", "root", "repository", "base_oid", "baseline_sha256"})
_TASK_FIELDS = frozenset({"id", "target_id", "plan_sha256", "inputs_sha256"})
_PROVIDER_FIELDS = frozenset({
    "cli_version", "executable_sha256", "help_sha256", "profile_sha256",
    "requested_model", "credential_blind", "initial", "resume",
})
_CELL_FIELDS = frozenset({
    "status", "run_id", "source_commit", "task_id", "target_id",
    "target_root", "inputs_sha256", "plan_sha256", "route", "seat_id",
    "cwd", "session_id", "cli_version", "executable_sha256", "help_sha256",
    "profile_sha256", "runtime_sha256", "policy_renderer_sha256",
    "broker_sha256", "profile_template_sha256", "native_profile_sha256",
    "native_boundary_sha256", "observed_model", "observed_model_evidence",
    "native_probe", "native_probe_sha256",
})
_PROBE_FIELDS = frozenset({
    "status", "provider_call", "workspace_write", "verified", "evidence_kind",
    "run_id", "source_commit", "provider", "route", "task_id", "target_id",
    "target_root", "seat_id", "cwd", "profile_sha256", "native_profile_sha256",
    "native_boundary_sha256", "denials", "credential_visibility", "sealed_oracles",
})


@dataclass(frozen=True)
class CertificationDecision:
    passed: bool
    reasons: tuple[str, ...]


def _record(value: object, fields: frozenset[str], path: str,
            reasons: list[str]) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        reasons.append(f"{path}: expected a record")
        return None
    names = set(value)
    for name in sorted(fields - names):
        reasons.append(f"{path}.{name}: missing")
    if names - fields:
        reasons.append(f"{path}: unknown fields")
    return value if names == fields else None


def _items(value: object, size: int, path: str,
           reasons: list[str]) -> list[object] | None:
    if not isinstance(value, list) or len(value) != size:
        reasons.append(f"{path}: expected exactly {size} records")
        return None
    return value


def _same(value: object, expected: object, path: str, reasons: list[str]) -> None:
    if type(value) is not type(expected) or value != expected:
        reasons.append(f"{path}: mismatch")


def _digest(value: object, path: str, reasons: list[str]) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value) or value == "0" * 64:
        reasons.append(f"{path}: invalid SHA-256")


def _oid(value: object, path: str, reasons: list[str]) -> None:
    if not isinstance(value, str) or not _OID.fullmatch(value) or set(value) == {"0"}:
        reasons.append(f"{path}: invalid Git object ID")


def _identifier(value: object, path: str, reasons: list[str]) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        reasons.append(f"{path}: invalid identifier")


def _session_token(value: object, path: str, reasons: list[str]) -> None:
    if not isinstance(value, str) or not _SESSION_TOKEN.fullmatch(value):
        reasons.append(f"{path}: invalid session token")


def _text(value: object, path: str, reasons: list[str]) -> None:
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(character) < 32 for character in value)):
        reasons.append(f"{path}: invalid text")


def _path(value: object, path: str, reasons: list[str]) -> None:
    if (not isinstance(value, str) or not value.startswith("/")
            or value == "/" or "\x00" in value
            or any(part in {".", ".."} for part in value.split("/"))
            or str(PurePosixPath(value)) != value):
        reasons.append(f"{path}: invalid absolute path")


def _ids(value: object, expected: set[str], path: str,
         reasons: list[str]) -> None:
    if (not isinstance(value, list) or not all(isinstance(item, str) for item in value)
            or len(value) != len(expected) or set(value) != expected):
        reasons.append(f"{path}: missing, duplicate, or unknown IDs")


def _probe(value: object, path: str, expected: Mapping[str, object],
           reasons: list[str]) -> None:
    probe = _record(value, _PROBE_FIELDS, path, reasons)
    if probe is None:
        return
    for field in ("status", "provider_call", "workspace_write"):
        _same(probe[field], "pass", f"{path}.{field}", reasons)
    _same(probe["verified"], True, f"{path}.verified", reasons)
    _same(probe["evidence_kind"], "native-sandbox-probe",
          f"{path}.evidence_kind", reasons)
    for field, binding in expected.items():
        _same(probe[field], binding, f"{path}.{field}", reasons)
    denials = _record(probe["denials"], frozenset(_DENIALS),
                      f"{path}.denials", reasons)
    if denials is not None:
        for name in _DENIALS:
            _same(denials[name], "denied", f"{path}.denials.{name}", reasons)
    visibility = _record(probe["credential_visibility"], frozenset(_CREDENTIALS),
                         f"{path}.credential_visibility", reasons)
    if visibility is not None:
        for name in _CREDENTIALS:
            _same(visibility[name], "not-visible",
                  f"{path}.credential_visibility.{name}", reasons)
    oracles = _record(probe["sealed_oracles"], frozenset(_DENIALS),
                      f"{path}.sealed_oracles", reasons)
    if oracles is not None:
        for name in _DENIALS:
            _digest(oracles[name], f"{path}.sealed_oracles.{name}", reasons)


def _cell(cell: Mapping[str, object], *, path: str, provider_name: str,
          provider: Mapping[str, object], route: str, receipt: Mapping[str, object],
          tasks: Mapping[str, Mapping[str, object]],
          targets: Mapping[str, Mapping[str, object]],
          reasons: list[str]) -> None:
    for field, expected in (
        ("status", "pass"), ("route", route), ("run_id", receipt["run_id"]),
        ("source_commit", receipt["source_commit"]),
        ("runtime_sha256", receipt["runtime_sha256"]),
        ("policy_renderer_sha256", receipt["policy_renderer_sha256"]),
        ("broker_sha256", receipt["broker_sha256"]),
        ("profile_template_sha256", receipt["profile_template_sha256"]),
        ("cli_version", provider["cli_version"]),
        ("executable_sha256", provider["executable_sha256"]),
        ("help_sha256", provider["help_sha256"]),
        ("profile_sha256", provider["profile_sha256"]),
        ("observed_model", provider["requested_model"]),
    ):
        _same(cell[field], expected, f"{path}.{field}", reasons)
    for field in ("native_profile_sha256", "native_boundary_sha256",
                  "native_probe_sha256"):
        _digest(cell[field], f"{path}.{field}", reasons)
    for field in ("task_id", "target_id", "seat_id"):
        _identifier(cell[field], f"{path}.{field}", reasons)
    _session_token(cell["session_id"], f"{path}.session_id", reasons)
    _path(cell["cwd"], f"{path}.cwd", reasons)
    _path(cell["target_root"], f"{path}.target_root", reasons)

    task = tasks.get(cell["task_id"]) if isinstance(cell["task_id"], str) else None
    target = targets.get(cell["target_id"]) if isinstance(cell["target_id"], str) else None
    if task is None or target is None:
        reasons.append(f"{path}: unknown task or target")
    else:
        _same(cell["target_id"], task["target_id"], f"{path}.target_id", reasons)
        _same(cell["target_root"], target["root"], f"{path}.target_root", reasons)
        _same(cell["inputs_sha256"], task["inputs_sha256"],
              f"{path}.inputs_sha256", reasons)
        _same(cell["plan_sha256"], task["plan_sha256"],
              f"{path}.plan_sha256", reasons)

    evidence = _record(cell["observed_model_evidence"],
                       frozenset({"kind", "sha256"}),
                       f"{path}.observed_model_evidence", reasons)
    if evidence is not None:
        _same(evidence["kind"], "provider-response",
              f"{path}.observed_model_evidence.kind", reasons)
        _digest(evidence["sha256"],
                f"{path}.observed_model_evidence.sha256", reasons)

    probe_path = f"{path}.native_probe"
    _probe(cell["native_probe"], probe_path, {
        "run_id": receipt["run_id"], "source_commit": receipt["source_commit"],
        "provider": provider_name, "route": route,
        "task_id": cell["task_id"], "target_id": cell["target_id"],
        "target_root": cell["target_root"], "seat_id": cell["seat_id"],
        "cwd": cell["cwd"], "profile_sha256": provider["profile_sha256"],
        "native_profile_sha256": cell["native_profile_sha256"],
        "native_boundary_sha256": cell["native_boundary_sha256"],
    }, reasons)
    try:
        encoded = json.dumps(cell["native_probe"], sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError):
        reasons.append(f"{probe_path}: not canonical JSON")
    else:
        _same(cell["native_probe_sha256"], hashlib.sha256(encoded).hexdigest(),
              f"{path}.native_probe_sha256", reasons)


def _paths_overlap(left: str, right: str) -> bool:
    left_path, right_path = PurePosixPath(left), PurePosixPath(right)
    return left_path.is_relative_to(right_path) or right_path.is_relative_to(left_path)


def evaluate_certification(
    receipt: Mapping[str, object], *, expected_runtime_sha256: str,
) -> CertificationDecision:
    """Check an untrusted receipt without granting any execution authority."""
    reasons: list[str] = []
    root = _record(receipt, _ROOT_FIELDS, "receipt", reasons)
    if root is None:
        return CertificationDecision(False, tuple(reasons))
    _same(root["schema_version"], "fanout-multirepo-certification-v1",
          "receipt.schema_version", reasons)
    _identifier(root["run_id"], "receipt.run_id", reasons)
    _oid(root["source_commit"], "receipt.source_commit", reasons)
    _digest(expected_runtime_sha256, "expected_runtime_sha256", reasons)
    for field in ("runtime_sha256", "policy_renderer_sha256", "broker_sha256",
                  "profile_template_sha256"):
        _digest(root[field], f"receipt.{field}", reasons)
    _same(root["runtime_sha256"], expected_runtime_sha256,
          "receipt.runtime_sha256", reasons)

    target_rows = _items(root["targets"], 2, "receipt.targets", reasons)
    task_rows = _items(root["tasks"], 2, "receipt.tasks", reasons)
    if target_rows is None or task_rows is None:
        return CertificationDecision(False, tuple(reasons))
    targets: dict[str, Mapping[str, object]] = {}
    for index, value in enumerate(target_rows):
        path = f"receipt.targets[{index}]"
        target = _record(value, _TARGET_FIELDS, path, reasons)
        if target is None:
            continue
        _identifier(target["id"], f"{path}.id", reasons)
        _path(target["root"], f"{path}.root", reasons)
        _text(target["repository"], f"{path}.repository", reasons)
        _oid(target["base_oid"], f"{path}.base_oid", reasons)
        _digest(target["baseline_sha256"], f"{path}.baseline_sha256", reasons)
        if isinstance(target["id"], str):
            if target["id"] in targets:
                reasons.append(f"{path}.id: duplicate target")
            targets[target["id"]] = target
    if len(targets) != 2:
        reasons.append("receipt.targets: two distinct targets required")
    if len({target["root"] for target in targets.values() if isinstance(target["root"], str)}) != 2:
        reasons.append("receipt.targets: distinct roots required")
    roots = [target["root"] for target in targets.values() if isinstance(target["root"], str)]
    if len(roots) == 2 and _paths_overlap(roots[0], roots[1]):
        reasons.append("receipt.targets: overlapping roots")
    if len({target["repository"] for target in targets.values()
            if isinstance(target["repository"], str)}) != 2:
        reasons.append("receipt.targets: distinct repositories required")

    tasks: dict[str, Mapping[str, object]] = {}
    for index, value in enumerate(task_rows):
        path = f"receipt.tasks[{index}]"
        task = _record(value, _TASK_FIELDS, path, reasons)
        if task is None:
            continue
        _identifier(task["id"], f"{path}.id", reasons)
        _identifier(task["target_id"], f"{path}.target_id", reasons)
        _digest(task["plan_sha256"], f"{path}.plan_sha256", reasons)
        _digest(task["inputs_sha256"], f"{path}.inputs_sha256", reasons)
        if isinstance(task["id"], str):
            if task["id"] in tasks:
                reasons.append(f"{path}.id: duplicate task")
            tasks[task["id"]] = task
    if len(tasks) != 2 or {task["target_id"] for task in tasks.values()
                           if isinstance(task["target_id"], str)} != set(targets):
        reasons.append("receipt.tasks: one bound task per target required")
    if reasons:
        return CertificationDecision(False, tuple(reasons))

    providers = _record(root["providers"], frozenset(_PROVIDERS),
                        "receipt.providers", reasons)
    assigned_tasks: set[str] = set()
    seat_ids: set[str] = set()
    seat_cwds: set[str] = set()
    if providers is not None:
        for provider_name in _PROVIDERS:
            path = f"receipt.providers.{provider_name}"
            provider = _record(providers[provider_name], _PROVIDER_FIELDS, path, reasons)
            if provider is None:
                continue
            _text(provider["cli_version"], f"{path}.cli_version", reasons)
            _text(provider["requested_model"], f"{path}.requested_model", reasons)
            for field in ("executable_sha256", "help_sha256", "profile_sha256"):
                _digest(provider[field], f"{path}.{field}", reasons)
            _same(provider["credential_blind"], True,
                  f"{path}.credential_blind", reasons)
            cells: dict[str, Mapping[str, object]] = {}
            for route in _ROUTES:
                cell_path = f"{path}.{route}"
                cell = _record(provider[route], _CELL_FIELDS, cell_path, reasons)
                if cell is None:
                    continue
                _cell(cell, path=cell_path, provider_name=provider_name,
                      provider=provider, route=route,
                      receipt=root, tasks=tasks, targets=targets, reasons=reasons)
                cells[route] = cell
                if isinstance(cell["task_id"], str):
                    assigned_tasks.add(cell["task_id"])
            if len(cells) == 2:
                initial, resume = cells["initial"], cells["resume"]
                for field in ("task_id", "target_id", "target_root", "seat_id",
                              "cwd", "session_id"):
                    _same(resume[field], initial[field],
                          f"{path}.resume.{field}", reasons)
                seat_id, cwd = initial["seat_id"], initial["cwd"]
                if isinstance(seat_id, str):
                    if seat_id in seat_ids:
                        reasons.append(f"{path}.initial.seat_id: shared seat")
                    seat_ids.add(seat_id)
                if isinstance(cwd, str):
                    if any(_paths_overlap(cwd, prior) for prior in seat_cwds):
                        reasons.append(f"{path}.initial.cwd: overlapping seat workspace")
                    seat_cwds.add(cwd)
                    if any(_paths_overlap(cwd, target["root"])
                           for target in targets.values()):
                        reasons.append(f"{path}.initial.cwd: overlaps a target root")
    if assigned_tasks != set(tasks):
        reasons.append("receipt.providers: missing task coverage")

    result = _record(root["two_repo_result"], frozenset({
        "status", "run_id", "source_commit", "target_ids", "task_ids", "receipt_sha256",
    }), "receipt.two_repo_result", reasons)
    if result is not None:
        for field, expected in (("status", "pass"), ("run_id", root["run_id"]),
                                ("source_commit", root["source_commit"])):
            _same(result[field], expected, f"receipt.two_repo_result.{field}", reasons)
        _ids(result["target_ids"], set(targets), "receipt.two_repo_result.target_ids", reasons)
        _ids(result["task_ids"], set(tasks), "receipt.two_repo_result.task_ids", reasons)
        _digest(result["receipt_sha256"], "receipt.two_repo_result.receipt_sha256", reasons)

    maka = _record(root["maka"], frozenset({
        "status", "role", "vote_count", "counterbalanced", "route_sha256",
        "ballots_sha256",
    }), "receipt.maka", reasons)
    if maka is not None:
        _same(maka["status"], "pass", "receipt.maka.status", reasons)
        _same(maka["role"], "router-evaluator", "receipt.maka.role", reasons)
        _same(maka["counterbalanced"], True, "receipt.maka.counterbalanced", reasons)
        if (type(maka["vote_count"]) is not int or maka["vote_count"] < 2
                or maka["vote_count"] % 2):
            reasons.append("receipt.maka.vote_count: no counterbalanced vote")
        _digest(maka["route_sha256"], "receipt.maka.route_sha256", reasons)
        _digest(maka["ballots_sha256"], "receipt.maka.ballots_sha256", reasons)

    quality = _record(root["quality"], frozenset({
        "status", "run_id", "source_commit", "target_ids", "task_ids",
        "comparison_sha256", "regressions",
    }), "receipt.quality", reasons)
    if quality is not None:
        for field, expected in (("status", "pass"), ("run_id", root["run_id"]),
                                ("source_commit", root["source_commit"]),
                                ("regressions", 0)):
            _same(quality[field], expected, f"receipt.quality.{field}", reasons)
        _ids(quality["target_ids"], set(targets), "receipt.quality.target_ids", reasons)
        _ids(quality["task_ids"], set(tasks), "receipt.quality.task_ids", reasons)
        _digest(quality["comparison_sha256"], "receipt.quality.comparison_sha256", reasons)

    return CertificationDecision(not reasons, tuple(reasons))
