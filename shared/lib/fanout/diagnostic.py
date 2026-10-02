"""Controller-owned, disposable provider turns through the native broker boundary."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from .broker import BrokerListener
from .controller import LifecycleController, read_evidence, write_evidence
from .errors import ProviderProtocolError, ProviderRequestError
from .native_boundary import (
    NativeSeatBoundary, _broker_binding, issue_broker_admission,
    issue_native_boundary, wrap_native_command,
)
from .process import ProcessCommand, ProcessResult, ProcessStatus, run_command
from .providers import (
    AgyAdapter, ClaudeAdapter, CodexAdapter, ProviderRegistry, ProviderRequest,
    ProviderResult, _make_result,
)
from .repo import SeatWorkspaceVerification


_CATEGORY = "brokered-provider-diagnostic"


@dataclass(frozen=True, slots=True)
class BrokeredDiagnosticResult:
    result: ProviderResult
    boundary: NativeSeatBoundary
    process: ProcessResult
    receipt_name: str
    receipt_digest: str
    effective_output_tokens: tuple[int, ...]
    broker_model: str
    refused_agy_auxiliary_requests: int


def run_brokered_provider_diagnostic(
    request: ProviderRequest, *, controller: LifecycleController,
    verification: SeatWorkspaceVerification, broker_listener: BrokerListener,
    other_target_roots: Sequence[Path], denied_owner_roots: Sequence[Path],
    initial: BrokeredDiagnosticResult | None = None,
) -> BrokeredDiagnosticResult:
    """Run one fake or approved probe turn; never authorize ordinary dispatch."""
    if (not isinstance(request, ProviderRequest) or request.profile is None
            or request.native_boundary is not None or request.retries != 0
            or any(getattr(request, name) is None for name in (
                "run_id", "task_id", "target_id", "inputs_digest", "seat_id"))):
        raise ProviderRequestError("brokered diagnostic requires one exact controller seat turn")
    if request.execution_class != "read-only":
        raise ProviderRequestError("brokered diagnostic supports read-only diagnostics only")
    if request.resume:
        _authenticate_resume(controller, request, initial)
    elif initial is not None:
        raise ProviderRequestError("initial diagnostic cannot consume a prior turn")
    selected_registry = (ProviderRegistry((request.profile,)) if request.resume
                         else ProviderRegistry.for_installed((request.executor_id,)))
    profile = selected_registry.admit(
        request.executor_id, request.execution_class, request.profile.quality_tier,
    )
    if profile.digest != request.profile.digest:
        raise ProviderRequestError("diagnostic profile differs from the installed pin")
    selected_registry.assert_installed_version(profile)
    adapter = selected_registry.require(request.executor_id)
    if type(adapter) is ClaudeAdapter:
        command = adapter.build_command(request, brokered_diagnostic=True)
    elif type(adapter) is CodexAdapter:
        command = _codex_broker_command(
            request, adapter.build_command(request), broker_listener,
        )
    elif type(adapter) is AgyAdapter and request.execution_class == "read-only":
        command = adapter.build_command(request, brokered_diagnostic=True)
    else:
        raise ProviderRequestError("provider lacks a brokered diagnostic command")
    if command.environment:
        raise ProviderRequestError("brokered diagnostic command carries environment overrides")
    admission = issue_broker_admission(
        controller, verification, request, command, broker_listener,
        initial_receipt=(None if initial is None else
                         (initial.receipt_name, initial.receipt_digest)),
    )
    boundary = issue_native_boundary(
        controller, verification, run_id=request.run_id, task_id=request.task_id,
        target_id=request.target_id, inputs_digest=request.inputs_digest,
        request=request, command=command, other_target_roots=other_target_roots,
        denied_owner_roots=denied_owner_roots, broker_listener=broker_listener,
        broker_admission=admission,
    )
    started = time.monotonic()
    process = run_command(wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    ))
    parsed = None
    if process.status is ProcessStatus.EXIT and process.stdout:
        try:
            parsed = adapter.parse(process.stdout)
        except ProviderProtocolError:
            pass
    result = _make_result(request, adapter, process, parsed, 1,
                          time.monotonic() - started)
    effective_output_tokens = broker_listener.effective_output_tokens
    broker_model = broker_listener.active_binding()[3].model
    refused_agy_auxiliary_requests = broker_listener.refused_agy_auxiliary_requests
    receipt_name, receipt_digest = write_evidence(controller, _CATEGORY, {
        "schema_version": "fanout-brokered-diagnostic-v1",
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest, "profile_digest": profile.digest,
        "boundary_receipt_digest": boundary.receipt_digest,
        "route": boundary.route, "session_id": result.session_id,
        "valid": result.valid, "reason": result.reason,
        "process_status": process.status.value, "exit_code": process.returncode,
        "stdout_sha256": hashlib.sha256(process.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(process.stderr).hexdigest(),
        "observed_model": result.observed_model,
        "broker_model": broker_model,
        "effective_output_tokens": list(effective_output_tokens),
        "refused_agy_auxiliary_requests": refused_agy_auxiliary_requests,
    })
    return BrokeredDiagnosticResult(result, boundary, process,
                                    receipt_name, receipt_digest,
                                    effective_output_tokens, broker_model,
                                    refused_agy_auxiliary_requests)


def _codex_broker_command(request: ProviderRequest, command: ProcessCommand,
                          listener: BrokerListener) -> ProcessCommand:
    if request.executor_id != "codex":
        raise ProviderRequestError("Codex broker command has a different provider")
    try:
        port, _, _, placeholder = _broker_binding(listener, request)
    except ValueError as error:
        raise ProviderRequestError("Codex broker binding is unavailable") from error
    return codex_broker_command(request, command, port=port, placeholder=placeholder)


def codex_broker_command(request: ProviderRequest, command: ProcessCommand, *,
                        port: int, placeholder: str) -> ProcessCommand:
    """Apply only the stable tokenless Responses overrides to a canonical command."""
    import re
    if (request.executor_id != "codex" or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(placeholder, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{16,128}", placeholder) is None):
        raise ProviderRequestError("Codex broker binding is unavailable")
    prefix = 3 if request.resume else 2
    if tuple(command.argv[:prefix]) != (("codex", "exec", "resume") if request.resume
                                         else ("codex", "exec")):
        raise ProviderRequestError("Codex broker command differs from pinned grammar")
    options = (
        "model_provider=fanout_broker",
        "model_providers.fanout_broker.name=FanoutBroker",
        f'model_providers.fanout_broker.base_url="http://127.0.0.1:{port}/v1"',
        "model_providers.fanout_broker.wire_api=responses",
        "model_providers.fanout_broker.requires_openai_auth=false",
        "model_providers.fanout_broker.request_max_retries=0",
        "model_providers.fanout_broker.stream_max_retries=0",
        f'model_providers.fanout_broker.http_headers={{"X-Fanout-Seat"="{placeholder}"}}',
    )
    argv = (tuple(command.argv[:prefix])
            + tuple(part for option in options for part in ("-c", option))
            + tuple(command.argv[prefix:]))
    return replace(command, argv=argv)


def _authenticate_resume(controller: LifecycleController, request: ProviderRequest,
                         initial: BrokeredDiagnosticResult | None) -> None:
    if not isinstance(initial, BrokeredDiagnosticResult):
        raise ProviderRequestError("exact diagnostic resume needs an authenticated initial turn")
    try:
        receipt = read_evidence(controller, _CATEGORY,
                                initial.receipt_name, initial.receipt_digest)
    except Exception as error:
        raise ProviderRequestError("diagnostic initial turn evidence is unavailable") from error
    expected = {
        "schema_version": "fanout-brokered-diagnostic-v1",
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest,
        "profile_digest": request.profile.digest,
        "boundary_receipt_digest": initial.boundary.receipt_digest,
        "route": f"initial:assigned:{request.session_id}" if request.executor_id == "claude"
                 else "initial:unassigned",
        "session_id": request.session_id,
        "valid": True, "reason": "ok",
        "process_status": ProcessStatus.EXIT.value, "exit_code": 0,
        "stdout_sha256": initial.result.stdout_digest,
        "stderr_sha256": initial.result.stderr_digest,
        "observed_model": initial.result.observed_model,
        "broker_model": initial.broker_model,
        "effective_output_tokens": list(initial.effective_output_tokens),
        "refused_agy_auxiliary_requests": initial.refused_agy_auxiliary_requests,
    }
    if receipt != expected or initial.result.session_id != request.session_id:
        raise ProviderRequestError("exact diagnostic resume differs from initial evidence")
