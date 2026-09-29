"""Managed, session-isolated Maka transport for blind interactive ballots."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping

from .evaluation import JudgeTransportError, PresentedJudgeRequest, _digest


def _invoke(
    launcher: Path, command: str, payload: dict[str, str], *,
    timeout: int, environment: dict[str, str] | None = None,
) -> Mapping[str, object]:
    with tempfile.TemporaryDirectory(prefix="maka-judge-") as workdir:
        try:
            result = subprocess.run(
                [str(launcher), command, "-"],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=workdir,
                env=environment,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise JudgeTransportError("Maka judge command was unavailable") from error
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise JudgeTransportError("Maka judge command failed")
    try:
        response = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise JudgeTransportError("Maka judge returned invalid evidence") from error
    if not isinstance(response, dict) or set(response) != {
        "winner", "auth_route", "requested_model", "observed_model",
        "effort", "tool_calls", "tool_boundary_sha256",
    }:
        raise JudgeTransportError("Maka judge returned incomplete evidence")
    return response


def _validate_response(
    response: Mapping[str, object], *, auth_route: str,
    model: str, boundary: str,
) -> None:
    if (response["winner"] not in {"A", "B", "tie", "abstain"} or
            response["auth_route"] != auth_route or
            response["requested_model"] != model or
            response["observed_model"] != model or
            response["effort"] != "xhigh" or
            type(response["tool_calls"]) is not int or
            response["tool_calls"] != 0 or
            response["tool_boundary_sha256"] != boundary):
        raise JudgeTransportError("Maka judge execution evidence did not match")


class InteractiveMakaTransport:
    """Invoke the first-party interactive judge command."""

    target_id = "maka"

    def __init__(
        self,
        *,
        launcher: Path,
        no_tools_characterization_sha256: str,
    ) -> None:
        if not _digest(no_tools_characterization_sha256):
            raise JudgeTransportError("Maka no-tools transport is not characterized")
        self._launcher = Path(launcher).resolve(strict=True)
        self.no_tools_characterization_sha256 = no_tools_characterization_sha256

    def judge(self, request: PresentedJudgeRequest) -> Mapping[str, object]:
        if not isinstance(request, PresentedJudgeRequest) or request.panel_id != "interactive":
            raise JudgeTransportError("Harbor has no characterized interactive judge route")
        if (request.target_id != "maka" or request.tools or
                (request.auth_route, request.requested_model, request.effort)
                != ("api-key-relay", "gpt-6-sol", "xhigh")):
            raise JudgeTransportError("interactive judge route is not pinned")
        payload = {
            "panel_id": request.panel_id,
            "criterion": request.criterion,
            "answer_a": request.answer_a,
            "answer_b": request.answer_b,
            "expected_boundary_sha256": self.no_tools_characterization_sha256,
        }
        response = _invoke(self._launcher, "judge", payload, timeout=180)
        _validate_response(
            response, auth_route="api-key-relay", model="gpt-6-sol",
            boundary=self.no_tools_characterization_sha256,
        )
        return response


class HarborMakaTransport(InteractiveMakaTransport):
    """Invoke the pinned Harbor judge only after an explicit paid-run grant."""

    def __init__(
        self, *, launcher: Path, no_tools_characterization_sha256: str,
        paid_authorized: bool = False,
    ) -> None:
        super().__init__(
            launcher=launcher,
            no_tools_characterization_sha256=no_tools_characterization_sha256,
        )
        self._paid_authorized = paid_authorized is True

    def judge(self, request: PresentedJudgeRequest) -> Mapping[str, object]:
        if not isinstance(request, PresentedJudgeRequest) or request.panel_id != "harbor":
            raise JudgeTransportError("Harbor judge requires a Harbor ballot")
        if (request.target_id != "maka" or request.tools or
                (request.auth_route, request.requested_model, request.effort)
                != ("harbor-lab", "gpt-5.6-sol", "xhigh")):
            raise JudgeTransportError("Harbor judge route is not pinned")
        if not self._paid_authorized:
            raise JudgeTransportError("Harbor judge requires explicit paid authorization")
        payload = {
            "panel_id": request.panel_id,
            "criterion": request.criterion,
            "answer_a": request.answer_a,
            "answer_b": request.answer_b,
            "expected_boundary_sha256": self.no_tools_characterization_sha256,
        }
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 131072:
            raise JudgeTransportError("Harbor ballot exceeds the managed request limit")
        response = _invoke(
            self._launcher, "judge-harbor", payload, timeout=600,
            environment={
                "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                "MAKA_JUDGE_PAID_RUN": "1",
            },
        )
        _validate_response(
            response, auth_route="harbor-lab", model="gpt-5.6-sol",
            boundary=self.no_tools_characterization_sha256,
        )
        return response
