"""Offline boundary tests for the managed Maka blind-judge command."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_maka_transport_contracts", PACKAGE / "__init__.py",
    submodule_search_locations=[str(PACKAGE)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)
evaluation = importlib.import_module(f"{SPEC.name}.evaluation")
transport_module = importlib.import_module(f"{SPEC.name}.maka_transport")


def _request(panel_id: str = "interactive") -> evaluation.PresentedJudgeRequest:
    return evaluation.PresentedJudgeRequest(
        "case-1", panel_id, "Which answer handles NULL?",
        "NOT IN", "NOT EXISTS", "gpt-6-sol" if panel_id == "interactive" else "gpt-5.6-sol",
        "xhigh", "api-key-relay" if panel_id == "interactive" else "harbor-lab",
    )


def _launcher(
    tmp_path: Path, result: dict[str, object], *, command: str = "judge",
) -> tuple[Path, Path]:
    captured = tmp_path / "captured.sha256"
    launcher = tmp_path / "maka"
    launcher.write_text(
        "#!/bin/sh\n"
        f"test \"$1\" = {command} || exit 9\n"
        "test \"$2\" = - || exit 9\n"
        f"/usr/bin/shasum -a 256 > '{captured}'\n"
        f"printf '%s\\n' '{json.dumps(result)}'\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    return launcher, captured


def _response(boundary: str, panel_id: str = "interactive") -> dict[str, object]:
    harbor = panel_id == "harbor"
    return {
        "winner": "B", "auth_route": "harbor-lab" if harbor else "api-key-relay",
        "requested_model": "gpt-5.6-sol" if harbor else "gpt-6-sol",
        "observed_model": "gpt-5.6-sol" if harbor else "gpt-6-sol",
        "effort": "xhigh", "tool_calls": 0,
        "tool_boundary_sha256": boundary,
    }


def test_interactive_transport_uses_managed_judge_command_with_blind_answers(tmp_path):
    boundary = "a" * 64
    launcher, captured = _launcher(tmp_path, _response(boundary))
    transport = transport_module.InteractiveMakaTransport(
        launcher=launcher, no_tools_characterization_sha256=boundary,
    )

    ballot = evaluation.MakaJudgeAdapter(transport).judge(evaluation.JudgeRequest(
        "case-1", "interactive", "Which answer handles NULL?",
        "NOT IN", "NOT EXISTS", "gpt-6-sol", "xhigh", "api-key-relay",
    ))

    assert ballot["winner"] == "B"
    presented = {
        "panel_id": "interactive", "criterion": "Which answer handles NULL?",
        "answer_a": "NOT IN", "answer_b": "NOT EXISTS",
        "expected_boundary_sha256": boundary,
    }
    assert captured.read_text(encoding="ascii").split()[0] == hashlib.sha256(
        json.dumps(presented, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def test_harbor_is_unavailable_before_any_judge_process_starts(tmp_path):
    boundary = "a" * 64
    launcher, captured = _launcher(tmp_path, _response(boundary))
    transport = transport_module.InteractiveMakaTransport(
        launcher=launcher, no_tools_characterization_sha256=boundary,
    )

    with pytest.raises(evaluation.JudgeTransportError, match="Harbor|interactive"):
        transport.judge(_request("harbor"))
    assert not captured.exists()


def test_harbor_transport_requires_explicit_paid_authorization(tmp_path):
    boundary = "a" * 64
    launcher, captured = _launcher(
        tmp_path, _response(boundary, "harbor"), command="judge-harbor",
    )
    transport = transport_module.HarborMakaTransport(
        launcher=launcher, no_tools_characterization_sha256=boundary,
    )

    with pytest.raises(evaluation.JudgeTransportError, match="paid"):
        transport.judge(_request("harbor"))
    assert not captured.exists()


def test_harbor_transport_uses_managed_paid_route_and_scrubs_ambient_keys(tmp_path, monkeypatch):
    boundary = "a" * 64
    launcher, captured = _launcher(
        tmp_path, _response(boundary, "harbor"), command="judge-harbor",
    )
    environment_capture = tmp_path / "environment.txt"
    source = launcher.read_text(encoding="utf-8")
    launcher.write_text(
        source.replace(
            f"/usr/bin/shasum -a 256 > '{captured}'",
            f"test \"$MAKA_JUDGE_PAID_RUN\" = 1 || exit 9\n"
            f"env > '{environment_capture}'\n"
            f"/usr/bin/shasum -a 256 > '{captured}'",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-ambient-secret")
    transport = transport_module.HarborMakaTransport(
        launcher=launcher, no_tools_characterization_sha256=boundary,
        paid_authorized=True,
    )

    ballot = evaluation.MakaJudgeAdapter(transport).judge(evaluation.JudgeRequest(
        "case-1", "harbor", "Which answer handles NULL?",
        "NOT IN", "NOT EXISTS", "gpt-5.6-sol", "xhigh", "harbor-lab",
    ))

    assert ballot["winner"] == "B"
    assert ballot["observed_model"] == "gpt-5.6-sol"
    assert "OPENAI_API_KEY" not in environment_capture.read_text(encoding="utf-8")
    presented = {
        "panel_id": "harbor", "criterion": "Which answer handles NULL?",
        "answer_a": "NOT IN", "answer_b": "NOT EXISTS",
        "expected_boundary_sha256": boundary,
    }
    assert captured.read_text(encoding="ascii").split()[0] == hashlib.sha256(
        json.dumps(presented, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize("change", [
    lambda result: result.update(observed_model="gpt-6-sol"),
    lambda result: result.update(effort="medium"),
    lambda result: result.update(tool_calls=1),
    lambda result: result.update(tool_boundary_sha256="b" * 64),
])
def test_harbor_transport_rejects_unobserved_or_unsafe_evidence(tmp_path, change):
    boundary = "a" * 64
    result = _response(boundary, "harbor")
    change(result)
    launcher, _ = _launcher(tmp_path, result, command="judge-harbor")
    transport = transport_module.HarborMakaTransport(
        launcher=launcher, no_tools_characterization_sha256=boundary,
        paid_authorized=True,
    )

    with pytest.raises(evaluation.JudgeTransportError):
        transport.judge(_request("harbor"))


@pytest.mark.parametrize("change", [
    lambda result: result.update(observed_model="gpt-5.6-sol"),
    lambda result: result.update(effort="medium"),
    lambda result: result.update(tool_calls=1),
    lambda result: result.update(tool_boundary_sha256="b" * 64),
])
def test_transport_rejects_unobserved_or_unsafe_execution_evidence(tmp_path, change):
    boundary = "a" * 64
    result = _response(boundary)
    change(result)
    launcher, _ = _launcher(tmp_path, result)
    transport = transport_module.InteractiveMakaTransport(
        launcher=launcher, no_tools_characterization_sha256=boundary,
    )

    with pytest.raises(evaluation.JudgeTransportError):
        transport.judge(_request())
