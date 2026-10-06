"""Material failure diagnostics, without VM, credentials, or provider dispatch."""
from contextlib import nullcontext
from types import SimpleNamespace
import json

import pytest

from shared.lib.fanout import quality_case, container_boundary, container_coordinator, diagnostic
from shared.lib.fanout import container_transfer_transport as transfer
from shared.lib.fanout.artifacts import canonical_json
from shared.lib.fanout.container_broker import PermitLedger
from shared.lib.fanout.container_routes import RouteProfile
from shared.lib.fanout.controller import read_evidence, write_evidence
from shared.lib.fanout.providers import ProviderRequest, ProviderRegistry
from test_fanout_quality_case import _declared_container_backend


def _fixture(tmp_path, monkeypatch, failing_stage, *, cleanup_error=None):
    module, controller, store, authority, allocation, value, _ = _declared_container_backend(
        tmp_path, route_digest="f" * 64)
    row = allocation["rows"][0]
    controls = container_boundary.quality_docker_runner(controller,
        allocation_digest=authority[1], max_commands=32, command_seconds=1, outer_seconds=60)
    value["container"]["docker_controls"] = list(controls.controls)
    attachment = write_evidence(controller, "quality-installed-case-attachment", value)
    request = ProviderRequest("claude", b"SENTINEL_PROMPT_MUST_NOT_APPEAR", cwd=tmp_path,
        profile=ProviderRegistry.default().select("claude", "read-only", "standard"), timeout=30,
        run_id="run", task_id="question", target_id="quality-normal-01", seat_id="claude-seat",
        inputs_digest="a" * 64, session_id=row["permit"]["session_id"], artifact_store=store)
    error = module.ProviderRequestError("SENTINEL_PRIMARY_ARGS_MUST_NOT_APPEAR")
    events = []

    def boundary(stage, result=None):
        def call(*args, **kwargs):
            events.append(stage)
            if stage == failing_stage:
                try:
                    transfer._quality_context(None)
                except module.ProviderRequestError as cause:
                    raise error from cause
            return result
        return call

    class Transport:
        stop_evidence = None
        _control = object()
        handle_evidence_name = "a" * 64 + ".json"
        handle_evidence_digest = "a" * 64
        handle = object()
        handoff_evidence = ("a" * 64 + ".json", "a" * 64)
        attest_socket = boundary("socket", object())
        stage_inputs = boundary("input_transfer", object())
        def handoff_quality_inputs(self, *args):
            return boundary("handoff", self)(*args)
        def close(self):
            events.append("close")
            self.stop_evidence = ("b" * 64 + ".json", "b" * 64)
            self._control = None
            if cleanup_error is not None:
                raise cleanup_error

    transport = Transport()
    seat = SimpleNamespace(executor_id="claude", seat_id="claude-seat")
    preparation = SimpleNamespace(seats=(seat,), packet=object())
    registry = SimpleNamespace(assert_installed_version=boundary("installed_version"))
    coordinator = SimpleNamespace(owner=object(), registry=registry)
    service = SimpleNamespace(lifecycle_controller=controller, artifacts=store, coordinator=coordinator,
        _authorize_command=boundary("owner_authorization"))
    runner = module.QualityCaseRunner(module.QualityCaseAttachment(*attachment),
        helper=tmp_path / "synthetic-helper.py", runtime_digest="a" * 64)
    runner._service = service
    monkeypatch.setattr(runner, "_context", boundary("context", (value, (None, preparation, allocation, None))))
    scope = SimpleNamespace(guard=nullcontext(), authorize=boundary("scope_authorization"),
        reserve_registered=boundary("reserve"), fail=lambda *args: pytest.fail("diagnostics globally blocked scope"))
    monkeypatch.setattr(container_coordinator, "_coordinator_lifecycle", lambda *args: scope)
    real_register = diagnostic.register_quality_permit
    if failing_stage == "registration":
        monkeypatch.setattr(diagnostic, "register_quality_permit", boundary("registration"))
    else:
        def register(*args, **kwargs):
            events.append("registration")
            return real_register(*args, **kwargs)
        monkeypatch.setattr(diagnostic, "register_quality_permit", register)
    monkeypatch.setattr(container_coordinator, "bind_quality_turn_context", boundary("bind", ("a" * 64 + ".json", "a" * 64)))
    class SyntheticCredentialSource:
        def __init__(self, profile):
            self.profile = profile
        load = boundary("credential", "SENTINEL_SYNTHETIC_CREDENTIAL")
    monkeypatch.setattr(module, "HostCredentialSource", SyntheticCredentialSource)
    monkeypatch.setattr(container_boundary.PinnedDockerTransport, "start_live", boundary("relay_start", transport))
    monkeypatch.setattr(container_coordinator, "stage_input_material", boundary("material_stage", (tmp_path, "a" * 64 + ".json", "a" * 64)))
    monkeypatch.setattr(transfer, "issue_input_source_grant", boundary("input_grant", object()))
    monkeypatch.setattr(container_boundary, "run_quality_container_readonly_turn", boundary("quality_turn", "synthetic-success"))
    route = SimpleNamespace(profile=RouteProfile("synthetic", "claude", "claude-subscription",
        "anthropic-messages", "https://api.anthropic.com", None, None, "claude-opus-4-6",
        "claude-opus-4-6", "claude-opus-4-6", "adapter:claude", "protected", ("read-only",), 1))
    return runner, controller, store, request, value, row, route, seat, error, events


def _failures(controller):
    result = []
    for path in (controller.root / "receipts").glob("*.json"):
        value = json.loads(path.read_bytes())
        if value["category"] == "quality-container-failure":
            result.append(read_evidence(controller, value["category"], path.name, path.stem))
    return result


@pytest.mark.parametrize("operation,stage", [
    ("installed_version", "preflight"), ("owner_authorization", "preflight"),
    ("context", "preflight"), ("registration", "registration"),
    ("credential", "credential"), ("relay_start", "relay_start"),
    ("material_stage", "material_stage"), ("input_grant", "input_grant"),
    ("input_transfer", "input_transfer"), ("handoff", "handoff"),
    ("quality_turn", "quality_turn"),
])
def test_actual_container_entry_retains_stage_and_owned_cause_without_altering_failure(tmp_path, monkeypatch, operation, stage):
    runner, controller, store, request, value, row, route, seat, error, events = _fixture(tmp_path, monkeypatch, operation)
    with store, pytest.raises(quality_case.ProviderRequestError) as raised:
        runner._turn(request, value, row, {"claude": route}, seat)
    assert raised.value is error
    records = _failures(controller)
    assert len(records) == 1, "actual container failure was discarded"
    failure = records[0]
    assert failure["stage"] == stage
    assert (failure["run_id"], failure["task_id"], failure["seat_id"], failure["provider"]) == (
        "run", "question", "claude-seat", "claude")
    assert failure["request_digest"] == quality_case.admitted_request_digest(request)
    assert failure["release_source_binding"]["schema_version"] == "fanout-release-source-v2"
    assert [cause["type"] for cause in failure["causes"]] == ["ProviderRequestError", "ProviderRequestError"]
    assert any(frame["path"] == "container_transfer_transport.py" for frame in failure["causes"][1]["frames"])
    assert any(frame["path"] == "quality_case.py" for frame in failure["causes"][0]["frames"])
    raw = canonical_json(failure)
    assert b"SENTINEL" not in raw and str(tmp_path).encode() not in raw
    assert all(set(frame) == {"path", "line"} and type(frame["line"]) is int
        for cause in failure["causes"] for frame in cause["frames"])
    ledger = PermitLedger(controller.root / "locks/spend", controller)
    if "credential" in events:
        state = ledger._load()
        assert len(state["permits"]) == 1
        assert all(entry["fenced"] and entry["attempts"] == entry["used"] == 0 for entry in state["permits"].values())
        assert not state["requests"] and not state["send_claims"]
    else:
        assert not (controller.root / "locks/spend/ledger.json").exists()
    assert events.count("close") == (1 if operation in {"material_stage", "input_grant", "input_transfer", "handoff", "quality_turn"} else 0)


def test_consumed_initial_claude_row_is_diagnosed_without_loading_credentials(tmp_path, monkeypatch):
    runner, controller, store, request, value, row, route, seat, _, events = _fixture(tmp_path, monkeypatch, None)
    import hashlib
    consumed = controller.root / "locks" / ("quality-row-" + hashlib.sha256(canonical_json({"allocation": value["allocation"][1], "row": row})).hexdigest())
    consumed.write_bytes(b"already consumed")
    with store, pytest.raises(quality_case.ProviderRequestError, match="already consumed"):
        runner._turn(request, value, row, {"claude": route}, seat)
    assert _failures(controller)[0]["stage"] == "preflight"
    assert not events and consumed.read_bytes() == b"already consumed"


def test_diagnostic_write_failure_keeps_primary_cleanup_and_fence(tmp_path, monkeypatch):
    cleanup = RuntimeError("SENTINEL_CLEANUP_MUST_NOT_APPEAR")
    runner, controller, store, request, value, row, route, seat, error, events = _fixture(
        tmp_path, monkeypatch, "input_grant", cleanup_error=cleanup)
    real_write = quality_case.write_evidence
    def write(controller, category, payload):
        if category == "quality-container-failure":
            raise OSError("SENTINEL_DIAGNOSTIC_MUST_NOT_APPEAR")
        return real_write(controller, category, payload)
    monkeypatch.setattr(quality_case, "write_evidence", write)
    with store, pytest.raises(quality_case.ProviderRequestError) as raised:
        runner._turn(request, value, row, {"claude": route}, seat)
    assert raised.value is error and events.count("close") == 1
    assert error.__notes__ == ["Quality container cleanup also failed: RuntimeError", "Quality failure diagnostic unavailable"]
    state = PermitLedger(controller.root / "locks/spend", controller)._load()
    assert all(entry["fenced"] for entry in state["permits"].values())
    assert not state["requests"] and not state["send_claims"]


def test_success_adds_no_failure_receipt_or_provider_requests(tmp_path, monkeypatch):
    runner, controller, store, request, value, row, route, seat, _, events = _fixture(tmp_path, monkeypatch, None)
    with store:
        assert runner._turn(request, value, row, {"claude": route}, seat) == "synthetic-success"
    assert not _failures(controller) and events.count("close") == 1
    state = PermitLedger(controller.root / "locks/spend", controller)._load()
    assert all(entry["fenced"] and entry["attempts"] == 0 for entry in state["permits"].values())
    assert not state["requests"] and not state["send_claims"]


@pytest.mark.parametrize("kind", ["long-chain", "cycle", "foreign-type", "spoof-type", "interrupt"])
def test_failure_metadata_bounds_chains_and_never_serializes_foreign_types_or_error_text(tmp_path, monkeypatch, kind):
    runner, controller, store, request, value, row, route, seat, _, _ = _fixture(tmp_path, monkeypatch, None)
    class UntrustedError(BaseException):
        def __str__(self):
            pytest.fail("diagnostics formatted untrusted exception text")
    if kind == "spoof-type":
        class PretendTrustedType(type):
            def __eq__(self, other):
                return True
        error = PretendTrustedType("SENTINEL_TYPE_SECRET", (BaseException,), {})("SENTINEL_SECRET")
    elif kind == "foreign-type":
        error = UntrustedError("SENTINEL_SECRET")
    elif kind == "interrupt":
        error = KeyboardInterrupt("SENTINEL_SECRET")
    else:
        error = ValueError("SENTINEL_SECRET")
        tail = error
        for _ in range(32):
            tail.__cause__ = ValueError("SENTINEL_SECRET")
            tail = tail.__cause__
        if kind == "cycle":
            tail.__cause__ = error
    def reject(*args):
        raise error
    monkeypatch.setattr(quality_case.HostCredentialSource, "load", reject)
    with store, pytest.raises(BaseException) as raised:
        runner._turn(request, value, row, {"claude": route}, seat)
    assert raised.value is error
    failure = _failures(controller)[0]
    assert 1 <= len(failure["causes"]) <= 8
    assert failure["causes_truncated"] == (kind in {"long-chain", "cycle"})
    assert failure["causes"][0]["type"] == {"foreign-type": "other", "spoof-type": "other", "interrupt": "KeyboardInterrupt"}.get(kind, "ValueError")
    assert all(len(cause["frames"]) <= 16 for cause in failure["causes"])
    assert b"SENTINEL" not in canonical_json(failure) and b"UntrustedError" not in canonical_json(failure)
    assert len(canonical_json(failure)) < 16 * 1024


def test_foreign_frame_cannot_spoof_an_owned_source_filename(tmp_path, monkeypatch):
    runner, controller, store, request, value, row, route, seat, _, _ = _fixture(tmp_path, monkeypatch, None)
    namespace = {"__name__": "foreign.module"}
    exec(compile("def reject(*args):\n    raise ValueError('SENTINEL_SECRET')\n", quality_case.__file__, "exec"), namespace)
    monkeypatch.setattr(quality_case.HostCredentialSource, "load", namespace["reject"])
    with store, pytest.raises(ValueError):
        runner._turn(request, value, row, {"claude": route}, seat)
    frames = _failures(controller)[0]["causes"][0]["frames"]
    assert frames and all(frame["line"] > 2 for frame in frames)


def test_failure_receipt_authentication_rejects_tampered_stage(tmp_path, monkeypatch):
    runner, controller, store, request, value, row, route, seat, _, _ = _fixture(tmp_path, monkeypatch, "input_grant")
    with store, pytest.raises(quality_case.ProviderRequestError):
        runner._turn(request, value, row, {"claude": route}, seat)
    failure = _failures(controller)[0]
    assert failure["stage"] == "input_grant"
    path = next(path for path in (controller.root / "receipts").glob("*.json")
        if json.loads(path.read_bytes())["category"] == "quality-container-failure")
    path.write_bytes(path.read_bytes().replace(b'"input_grant"', b'"quality_turn"'))
    from shared.lib.fanout.errors import LifecycleError
    with pytest.raises(LifecycleError):
        read_evidence(controller, "quality-container-failure", path.name, path.stem)


def test_unavailable_diagnostic_note_cannot_replace_the_original_error(tmp_path, monkeypatch):
    runner, controller, store, request, value, row, route, seat, error, _ = _fixture(tmp_path, monkeypatch, "installed_version")
    error.__notes__ = "SENTINEL_NOT_A_LIST"
    def unavailable(*args, **kwargs):
        raise OSError("SENTINEL_WRITE_FAILURE")
    monkeypatch.setattr(quality_case, "write_evidence", unavailable)
    with store, pytest.raises(quality_case.ProviderRequestError) as raised:
        runner._turn(request, value, row, {"claude": route}, seat)
    assert raised.value is error and error.__notes__ == "SENTINEL_NOT_A_LIST"


@pytest.mark.parametrize("suppressed", [False, True])
def test_implicit_exception_context_respects_python_suppression(tmp_path, monkeypatch, suppressed):
    runner, controller, store, request, value, row, route, seat, error, _ = _fixture(tmp_path, monkeypatch, None)
    def reject(*args):
        try:
            transfer._quality_context(None)
        except quality_case.ProviderRequestError:
            if suppressed:
                raise error from None
            raise error
    monkeypatch.setattr(quality_case.HostCredentialSource, "load", reject)
    with store, pytest.raises(quality_case.ProviderRequestError) as raised:
        runner._turn(request, value, row, {"claude": route}, seat)
    assert raised.value is error
    causes = _failures(controller)[0]["causes"]
    assert len(causes) == (1 if suppressed else 2)
    if not suppressed:
        assert any(frame["path"] == "container_transfer_transport.py" for frame in causes[1]["frames"])


def test_long_foreign_trace_is_bounded_without_recording_foreign_paths(tmp_path, monkeypatch):
    runner, controller, store, request, value, row, route, seat, error, _ = _fixture(tmp_path, monkeypatch, None)
    def recurse(depth):
        if depth:
            recurse(depth - 1)
        else:
            raise error
    monkeypatch.setattr(quality_case.HostCredentialSource, "load", lambda *args: recurse(128))
    with store, pytest.raises(quality_case.ProviderRequestError):
        runner._turn(request, value, row, {"claude": route}, seat)
    cause = _failures(controller)[0]["causes"][0]
    assert cause["frames_truncated"] is True and len(cause["frames"]) <= 16
    assert cause["frames"] and all(frame["path"] == "quality_case.py" for frame in cause["frames"])


@pytest.mark.parametrize("failure", ["version-drift", "probe-unavailable"])
def test_real_initial_claude_registry_rejection_is_retained_before_registration(tmp_path, monkeypatch, failure):
    runner, controller, store, request, value, row, route, seat, _, events = _fixture(tmp_path, monkeypatch, None)
    def version(provider):
        assert provider == "claude"
        if failure == "probe-unavailable":
            raise ValueError("SENTINEL_VERSION_PROBE")
        return "0.0.0"
    runner._service.coordinator.registry = ProviderRegistry.default(version_probe=version)
    with store, pytest.raises(quality_case.ProviderRequestError):
        runner._turn(request, value, row, {"claude": route}, seat)
    record = _failures(controller)[0]
    assert record["stage"] == "preflight" and not events
    assert any(frame["path"] == "providers.py" for frame in record["causes"][0]["frames"])
    assert [cause["type"] for cause in record["causes"]] == (
        ["ProviderRequestError", "ValueError"] if failure == "probe-unavailable" else ["ProviderRequestError"])
    assert not (controller.root / "locks/spend").exists()
    assert b"SENTINEL" not in canonical_json(record)
