"""Pure receipt checks; these synthetic bytes are never live certification."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


DENIALS = (
    "other_target_read", "other_target_write", "git_objects_read",
    "git_objects_write", "controller_read", "owner_read", "descriptor_read",
    "sibling_seat_read", "credential_path_read", "symlink_alias_read",
    "hardlink_alias_read", "network_outbound", "network_inbound", "ipc",
    "mach", "keychain", "ssh_agent",
)
CREDENTIALS = ("api_key", "adc", "cookie", "ssh_agent", "keychain", "controller_token")


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


@pytest.fixture
def certification_fixture():
    runtime_sha256 = _sha("synthetic runtime")

    def valid_receipt() -> dict[str, object]:
        source_commit = "a" * 40
        run_id = "synthetic-run"
        targets = [
            {"id": "address", "root": "/private/tmp/synthetic/address",
             "repository": "github.com/example/address", "base_oid": "b" * 40,
             "baseline_sha256": _sha("address baseline")},
            {"id": "booking", "root": "/private/tmp/synthetic/booking",
             "repository": "github.com/example/booking", "base_oid": "c" * 40,
             "baseline_sha256": _sha("booking baseline")},
        ]
        tasks = [
            {"id": "address-task", "target_id": "address", "plan_sha256": _sha("plan"),
             "inputs_sha256": _sha("address inputs")},
            {"id": "booking-task", "target_id": "booking", "plan_sha256": _sha("plan"),
             "inputs_sha256": _sha("booking inputs")},
        ]
        receipt = {
            "schema_version": "fanout-multirepo-certification-v1",
            "run_id": run_id, "source_commit": source_commit,
            "runtime_sha256": runtime_sha256,
            "policy_renderer_sha256": _sha("policy renderer"),
            "broker_sha256": _sha("broker implementation"),
            "profile_template_sha256": _sha("profile template"),
            "targets": targets, "tasks": tasks,
            "providers": {},
            "two_repo_result": {
                "status": "pass", "run_id": run_id, "source_commit": source_commit,
                "target_ids": ["address", "booking"],
                "task_ids": ["address-task", "booking-task"],
                "receipt_sha256": _sha("two-repo live result"),
            },
            "maka": {"status": "pass", "role": "router-evaluator",
                     "vote_count": 2, "counterbalanced": True,
                     "route_sha256": _sha("maka route"),
                     "ballots_sha256": _sha("counterbalanced ballots")},
            "quality": {
                "status": "pass", "run_id": run_id, "source_commit": source_commit,
                "target_ids": ["address", "booking"],
                "task_ids": ["address-task", "booking-task"],
                "comparison_sha256": _sha("quality comparison"),
                "regressions": 0,
            },
        }
        assignments = {
            "claude": ("address-task", "address"),
            "codex": ("booking-task", "booking"),
            "agy": ("address-task", "address"),
        }
        target_roots = {target["id"]: target["root"] for target in targets}
        task_inputs = {task["id"]: task["inputs_sha256"] for task in tasks}
        for provider, (task_id, target_id) in assignments.items():
            executable = _sha(f"{provider} executable")
            help_digest = _sha(f"{provider} help")
            profile = _sha(f"{provider} profile")
            model = f"synthetic-{provider}-model"
            provider_row = {
                "cli_version": "synthetic-1.0", "executable_sha256": executable,
                "help_sha256": help_digest, "profile_sha256": profile,
                "requested_model": model, "credential_blind": True,
            }
            for route in ("initial", "resume"):
                probe = {
                    "status": "pass", "provider_call": "pass",
                    "workspace_write": "pass",
                    "verified": True, "evidence_kind": "native-sandbox-probe",
                    "run_id": run_id, "source_commit": source_commit,
                    "provider": provider, "route": route,
                    "task_id": task_id, "target_id": target_id,
                    "target_root": target_roots[target_id],
                    "seat_id": f"{provider}-seat",
                    "cwd": f"/private/tmp/synthetic/seats/{provider}",
                    "profile_sha256": profile,
                    "native_profile_sha256": _sha(f"{provider} {route} native profile"),
                    "native_boundary_sha256": _sha(f"{provider} {route} boundary"),
                    "denials": {name: "denied" for name in DENIALS},
                    "credential_visibility": {name: "not-visible" for name in CREDENTIALS},
                    "sealed_oracles": {name: _sha(f"{provider} {route} {name}")
                                       for name in DENIALS},
                }
                provider_row[route] = {
                    "status": "pass", "run_id": run_id,
                    "source_commit": source_commit,
                    "task_id": task_id, "target_id": target_id,
                    "target_root": target_roots[target_id],
                    "inputs_sha256": task_inputs[task_id],
                    "plan_sha256": _sha("plan"),
                    "route": route, "seat_id": f"{provider}-seat",
                    "cwd": f"/private/tmp/synthetic/seats/{provider}",
                    "session_id": f"{provider}-exact-session",
                    "cli_version": "synthetic-1.0",
                    "executable_sha256": executable,
                    "help_sha256": help_digest,
                    "profile_sha256": profile,
                    "runtime_sha256": runtime_sha256,
                    "policy_renderer_sha256": receipt["policy_renderer_sha256"],
                    "broker_sha256": receipt["broker_sha256"],
                    "profile_template_sha256": receipt["profile_template_sha256"],
                    "native_profile_sha256": _sha(f"{provider} {route} native profile"),
                    "native_boundary_sha256": _sha(f"{provider} {route} boundary"),
                    "observed_model": model,
                    "observed_model_evidence": {
                        "kind": "provider-response",
                        "sha256": _sha(f"{provider} {route} model response"),
                    },
                    "native_probe": probe,
                    "native_probe_sha256": _canonical_sha(probe),
                }
            receipt["providers"][provider] = provider_row
        return receipt

    def release_gate(receipt, *, expected_runtime_sha256=runtime_sha256):
        name = "shared.lib.fanout.certification"
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
            pytest.fail("evaluate_certification is absent")
        evaluate = getattr(module, "evaluate_certification", None)
        if evaluate is None:
            pytest.fail("evaluate_certification is absent")
        return evaluate(receipt, expected_runtime_sha256=expected_runtime_sha256)

    return SimpleNamespace(
        runtime_sha256=runtime_sha256, valid_receipt=valid_receipt,
        release_gate=release_gate,
    )


def test_complete_synthetic_receipt_passes_pure_validation(certification_fixture):
    decision = certification_fixture.release_gate(certification_fixture.valid_receipt())
    assert decision.passed is True
    assert decision.reasons == ()


def test_release_gate_rejects_missing_agy_resume_probe(certification_fixture):
    receipt = certification_fixture.valid_receipt()
    del receipt["providers"]["agy"]["resume"]
    decision = certification_fixture.release_gate(receipt)
    assert decision.passed is False
    assert any("agy.resume" in reason for reason in decision.reasons)


def test_release_gate_rejects_token_visible_to_shell_seat(certification_fixture):
    receipt = certification_fixture.valid_receipt()
    receipt["providers"]["codex"]["credential_blind"] = False
    decision = certification_fixture.release_gate(receipt)
    assert decision.passed is False
    assert any("credential" in reason for reason in decision.reasons)


def test_release_gate_accepts_numeric_leading_exact_session_id(certification_fixture):
    receipt = certification_fixture.valid_receipt()
    session_id = "01a0b3c1-1234-4abc-8def-0123456789ab"
    for route in ("initial", "resume"):
        receipt["providers"]["codex"][route]["session_id"] = session_id
    assert certification_fixture.release_gate(receipt).passed is True


@pytest.mark.parametrize("session_id", ("", "a/b", "a\\b", "a\nb", "x" * 257))
def test_release_gate_rejects_unsafe_session_id(certification_fixture, session_id):
    receipt = certification_fixture.valid_receipt()
    for route in ("initial", "resume"):
        receipt["providers"]["codex"][route]["session_id"] = session_id
    assert certification_fixture.release_gate(receipt).passed is False


@pytest.mark.parametrize(("path", "value"), (
    (("providers", "claude", "initial", "target_id"), "booking"),
    (("providers", "claude", "resume", "route"), "initial"),
    (("providers", "agy", "resume", "session_id"), "wrong-session"),
    (("providers", "codex", "resume", "cwd"), "/private/tmp/other-seat"),
    (("providers", "claude", "initial", "profile_sha256"), _sha("wrong profile")),
    (("providers", "codex", "initial", "broker_sha256"), _sha("stale broker")),
    (("providers", "agy", "resume", "runtime_sha256"), _sha("stale runtime")),
    (("providers", "agy", "initial", "observed_model_evidence", "kind"), "requested-model"),
    (("providers", "claude", "resume", "native_probe", "denials", "network_inbound"), "allowed"),
    (("providers", "codex", "initial", "native_probe", "credential_visibility", "adc"), "visible"),
    (("providers", "codex", "resume", "native_probe_sha256"), _sha("stale probe")),
    (("providers", "agy", "initial", "status"), "unverified"),
    (("providers", "claude", "resume", "observed_model"), "unexpected-model"),
))
def test_release_gate_rejects_unbound_or_unverified_cell(
    certification_fixture, path, value,
):
    receipt = certification_fixture.valid_receipt()
    node = receipt
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert certification_fixture.release_gate(receipt).passed is False


@pytest.mark.parametrize(("path", "value"), (
    (("runtime_sha256",), _sha("old runtime")),
    (("policy_renderer_sha256",), "0" * 64),
    (("broker_sha256",), "not-a-digest"),
    (("profile_template_sha256",), ""),
    (("providers", "agy", "executable_sha256"), "0" * 64),
    (("providers", "codex", "help_sha256"), ""),
    (("two_repo_result", "status"), "missing"),
    (("quality", "status"), "fail"),
    (("quality", "regressions"), 1),
    (("maka", "vote_count"), 0),
    (("maka", "counterbalanced"), False),
))
def test_release_gate_rejects_failed_or_stale_pin(certification_fixture, path, value):
    receipt = certification_fixture.valid_receipt()
    node = receipt
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert certification_fixture.release_gate(receipt).passed is False


def test_release_gate_rejects_unsupported_or_duplicate_cells(certification_fixture):
    receipt = certification_fixture.valid_receipt()
    receipt["providers"]["maka"] = copy.deepcopy(receipt["providers"]["codex"])
    assert certification_fixture.release_gate(receipt).passed is False

    receipt = certification_fixture.valid_receipt()
    receipt["targets"].append(copy.deepcopy(receipt["targets"][0]))
    assert certification_fixture.release_gate(receipt).passed is False

    receipt = certification_fixture.valid_receipt()
    receipt["providers"]["agy"]["retry"] = copy.deepcopy(receipt["providers"]["agy"]["resume"])
    assert certification_fixture.release_gate(receipt).passed is False


def test_release_gate_rejects_expected_runtime_and_quality_binding_drift(certification_fixture):
    receipt = certification_fixture.valid_receipt()
    assert not certification_fixture.release_gate(
        receipt, expected_runtime_sha256=_sha("different installed runtime"),
    ).passed
    receipt["quality"]["target_ids"] = ["address", "address"]
    assert not certification_fixture.release_gate(receipt).passed


def test_release_gate_rejects_overlapping_target_and_seat_roots(certification_fixture):
    receipt = certification_fixture.valid_receipt()
    for route in ("initial", "resume"):
        cell = receipt["providers"]["codex"][route]
        cell["cwd"] = "/private/tmp/synthetic"
        cell["native_probe"]["cwd"] = cell["cwd"]
        cell["native_probe_sha256"] = _canonical_sha(cell["native_probe"])
    assert not certification_fixture.release_gate(receipt).passed

    receipt = certification_fixture.valid_receipt()
    for route in ("initial", "resume"):
        cell = receipt["providers"]["codex"][route]
        cell["cwd"] = "/private/tmp/synthetic/seats/claude/nested"
        cell["native_probe"]["cwd"] = cell["cwd"]
        cell["native_probe_sha256"] = _canonical_sha(cell["native_probe"])
    assert not certification_fixture.release_gate(receipt).passed

    receipt = certification_fixture.valid_receipt()
    receipt["targets"][1]["root"] = "/private/tmp/synthetic/address/nested"
    for route in ("initial", "resume"):
        cell = receipt["providers"]["codex"][route]
        cell["target_root"] = receipt["targets"][1]["root"]
        cell["native_probe"]["target_root"] = cell["target_root"]
        cell["native_probe_sha256"] = _canonical_sha(cell["native_probe"])
    assert not certification_fixture.release_gate(receipt).passed


def test_release_gate_rejects_parent_traversal_in_bound_paths(certification_fixture):
    receipt = certification_fixture.valid_receipt()
    receipt["targets"][1]["root"] = "/private/tmp/synthetic/seats/../address"
    for route in ("initial", "resume"):
        cell = receipt["providers"]["codex"][route]
        cell["target_root"] = receipt["targets"][1]["root"]
        cell["native_probe"]["target_root"] = cell["target_root"]
        cell["native_probe_sha256"] = _canonical_sha(cell["native_probe"])
    assert not certification_fixture.release_gate(receipt).passed


def test_release_gate_rejects_rebound_native_probe_even_with_fresh_digest(certification_fixture):
    for field, value in (
        ("provider", "agy"), ("route", "resume"), ("target_id", "booking"),
        ("cwd", "/private/tmp/synthetic/seats/other"),
        ("profile_sha256", _sha("other profile")),
        ("verified", False),
    ):
        receipt = certification_fixture.valid_receipt()
        cell = receipt["providers"]["claude"]["initial"]
        cell["native_probe"][field] = value
        cell["native_probe_sha256"] = _canonical_sha(cell["native_probe"])
        assert not certification_fixture.release_gate(receipt).passed


def test_release_gate_rejects_malformed_receipt_without_raising(certification_fixture):
    malformed = (None, {}, {"providers": []},
                 certification_fixture.valid_receipt() | {"extra": 1})
    for receipt in malformed:
        decision = certification_fixture.release_gate(receipt)
        assert decision.passed is False
        assert decision.reasons
