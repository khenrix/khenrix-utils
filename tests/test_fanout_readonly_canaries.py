"""Offline stored-proof tests; synthetic telemetry is not binary characterization."""
from __future__ import annotations

import dataclasses
import hashlib
import http.client
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from test_fanout_memory import _observation
from test_fanout_container_upstream import connection_factory  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _helper():
    path = ROOT / "scripts/fanout_readonly_canaries.py"
    assert path.is_file(), "readonly canaries need their stored-proof helper"
    spec = importlib.util.spec_from_file_location("_readonly_canary_tests", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def canaries(tmp_path, monkeypatch, request, connection_factory):
    helper = _helper()
    smoke = helper.smoke
    runtime = smoke.fanout
    from shared.lib.fanout import providers, container_credentials
    from test_fanout_container_upstream import _route
    observed = getattr(request, "param", "observed")
    registry = runtime.ProviderRegistry.for_installed(smoke.EXECUTORS,
        version_probe=lambda executor: {
            "claude": "2.1.284" if observed == "template" else "2.1.999",
            "codex": "0.159.0" if observed == "template" else "0.160.0",
            "agy": "1.2.13" if observed == "template" else "1.2.999",
        }[executor])
    # Synthetic telemetry does not characterize a binary or prove a served model.
    collaboration = sys.modules[runtime.__name__ + ".collaboration"]
    monkeypatch.setattr(collaboration, "validate_agy_readonly_guard", lambda *_: None)

    def guard(controller, verification, profile):
        return runtime.AgyReadOnlyGuard(controller, verification, profile.digest,
            tmp_path / "fake-home", tmp_path / "fake-agy", tmp_path / "fake-adc",
            "fake-guard.json", "a" * 64)

    monkeypatch.setattr(runtime, "issue_agy_readonly_guard", guard)
    executable = tmp_path / "memory_exchange.py"
    executable.write_bytes((ROOT / "components/memory/memory_exchange.py").read_bytes())
    executable.chmod(0o700)
    memory_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    receipts = []
    routes_path, policy_path = tmp_path / "machine-routes.json", tmp_path / "policy.json"
    if observed == "body-observed":
        descriptor = dataclasses.asdict(_route("codex").profile)
        descriptor["execution_classes"] = list(descriptor["execution_classes"])
        policy = runtime.canonical_json({"schema": "fanout-machine-policy-v1", "machine_id": "fixture-machine",
            "routes": [{key: value for key, value in descriptor.items() if key != "credential_ref"}]})
        policy_path.write_bytes(policy)
        routes_path.write_bytes(runtime.canonical_json({"schema": "fanout-machine-routes-v1", "machine_id": "fixture-machine",
            "policy_digest": hashlib.sha256(policy).hexdigest(), "routes": [descriptor]}))
        policy_path.chmod(0o600)
        routes_path.chmod(0o600)

        class FixtureCredential:
            fingerprint = "c" * 64

            def __init__(self, profile):
                pass

            def load(self):
                return "fixture-host-only"

        monkeypatch.setattr(container_credentials, "HostCredentialSource", FixtureCredential)
    for tier in ("standard", "deep"):
        root = tmp_path / tier
        runner = None
        observations = {}
        active_request = None

        class OfflineMemoryController(runtime.MemoryController):
            def save(self, checkpoint):
                identity = int(hashlib.sha256(checkpoint.identity.key.encode()).hexdigest()[:13], 16) + 1
                observations[identity] = _observation(checkpoint, identity)
                return identity

            def fetch(self, ids):
                return tuple(observations[identity] for identity in ids)

        def memory(artifacts):
            return runtime.MemoryCheckpointExchange(OfflineMemoryController(
                executable, executable_sha256=memory_digest), artifacts)

        def run(request, *, registry):
            nonlocal runner, active_request
            if observed == "body-observed" and request.executor_id == "codex":
                active_request = request
                return providers.run_provider(request, registry=registry)
            if runner is None:
                runner = smoke._FakeProvider(root, poison_peer=False, live_markers=True)
            result = runner(dataclasses.replace(request,
                artifact_prefix=request.artifact_prefix + "/synthetic-answer"), registry=registry)
            model = request.profile.requested_model
            if request.executor_id == "claude":
                stdout = runtime.canonical_json({"type": "result", "subtype": "success",
                    "is_error": False, "session_id": result.session_id,
                    "result": result.answer, "modelUsage": {model: {}}})
            elif request.executor_id == "codex":
                stdout = b"".join(runtime.canonical_json(event) for event in (
                    {"type": "thread.started", "thread_id": result.session_id},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": result.answer}},
                    {"type": "turn.completed", "model": model if observed != "requested-only" else None, "usage": {}},
                ))
            else:
                stdout = b"".join(runtime.canonical_json(event) for event in (
                    {"event": "init", "conversation_id": result.session_id, "init": {}},
                    {"event": "result", "result": {"conversation_id": result.session_id,
                        "status": "SUCCESS", "response": result.answer, "model": model}},
                ))
            ref = request.artifact_store.write_bytes(request.artifact_prefix + "/stdout.bin", stdout)
            return dataclasses.replace(result, stdout=stdout, stdout_ref=ref,
                stdout_digest=ref.digest, requested_model=model,
                observed_model=model if request.executor_id != "codex" or observed != "requested-only" else None)

        def process(command):
            nonlocal runner
            request = active_request
            if runner is None:
                runner = smoke._FakeProvider(root, poison_peer=False, live_markers=True)
            result = runner(dataclasses.replace(request,
                artifact_prefix=request.artifact_prefix + "/synthetic-answer"), registry=registry)
            _, response = connection_factory
            response.chunks = [json.dumps({"id": "resp_fixture", "object": "response", "status": "completed",
                "model": "gpt-6.1-sol", "error": None, "incomplete_details": None}).encode()]
            port_option = next(arg for arg in command.argv if arg.startswith("model_providers.fanout_broker.base_url="))
            port = int(port_option.split(":")[-1].split("/")[0])
            marker = next(arg for arg in command.argv if arg.startswith("model_providers.fanout_broker.http_headers="))
            placeholder = marker.split('="')[-1].split('"')[0]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("POST", "/v1/responses", json.dumps({"model": "gpt-6.1-sol", "input": []}),
                {"content-type": "application/json", "x-fanout-seat": placeholder})
            reply = connection.getresponse()
            reply.read()
            connection.close()
            assert reply.status == 200
            home = Path(command.environment["HOME"])
            rollout = home / ".codex/sessions/fixture.jsonl"
            rollout.parent.mkdir(parents=True, exist_ok=True)
            with rollout.open("ab") as stream:
                stream.write(runtime.canonical_json({"type": "session_meta" if not request.resume else "fixture_turn",
                    "payload": {"id": result.session_id}}))
            stdout = b"".join(runtime.canonical_json(event) for event in (
                {"type": "thread.started", "thread_id": result.session_id},
                {"type": "item.completed", "item": {"type": "agent_message", "text": result.answer}},
                {"type": "turn.completed", "usage": {}},
            ))
            return runtime.ProcessResult(runtime.ProcessStatus.EXIT, 0, stdout, b"")

        dependencies = {"registry": registry, "provider_runner": run,
            "memory_factory": memory,
            "preflight": lambda _: (executable, memory_digest)}
        if observed == "body-observed":
            dependencies.pop("provider_runner")
            dependencies.update(machine_routes=routes_path, machine_policy=policy_path, codex_route="codex-approved")
            monkeypatch.setattr(runtime, "run_provider", run)
            monkeypatch.setattr(providers, "run_command", process)
        smoke.prepare_live(root, authorized=True, quality_tier=tier, **dependencies)
        receipts.append(smoke.resume_live(root, authorized=True, **dependencies))
    return helper, receipts


@pytest.mark.parametrize("canaries", ["body-observed"], indirect=True)
def test_same_turn_body_proof_can_satisfy_native_codex_absence_without_replaying(canaries, monkeypatch):
    helper, receipts = canaries
    from shared.lib.fanout import providers, container_credentials, container_routes, container_upstream

    def forbidden(*args, **kwargs):
        raise AssertionError("settled canary reader made an external observation")

    monkeypatch.setattr(providers, "_installed_cli_version", forbidden)
    monkeypatch.setattr(providers, "run_command", forbidden)
    monkeypatch.setattr(container_credentials, "HostCredentialSource", forbidden)
    monkeypatch.setattr(container_routes, "read_machine_routes", forbidden)
    monkeypatch.setattr(container_upstream.HostUpstream, "__call__", forbidden)
    summary = helper.summarize_readonly_canaries(*receipts)
    assert summary["status"] == "pass"
    assert summary["tiers"]["standard"]["providers"]["codex"]["observed_model"] == "gpt-6.1-sol"


@pytest.mark.parametrize("canaries", ["body-observed"], indirect=True)
@pytest.mark.parametrize("selectors", ["changed", "partial"])
def test_live_resume_cli_rejects_supplied_route_changes_before_sender(canaries, monkeypatch, selectors, capsys):
    helper, (standard, _) = canaries
    smoke = helper.smoke
    root = Path(standard["private_run_root"])
    original = json.loads((root / "state.json").read_bytes())["codex_canary_selectors"]

    def forbidden(*args, **kwargs):
        pytest.fail("changed route selectors reached an external action")

    monkeypatch.setattr(smoke, "_canary_route", forbidden)
    monkeypatch.setattr(smoke, "_live_preflight", forbidden)
    monkeypatch.setattr(smoke.fanout, "run_provider", forbidden)
    arguments = ["--resume-live", str(root), "--allow-paid",
        "--machine-routes", original["machine_routes"]]
    if selectors == "changed":
        arguments.extend(["--machine-policy", original["machine_policy"],
            "--codex-route", "different-company-route"])
    assert smoke.main(arguments) == 1
    assert "company route selectors changed before cold resume" in capsys.readouterr().err


@pytest.mark.parametrize("canaries", ["body-observed"], indirect=True)
@pytest.mark.parametrize("damage", ["peer", "checkpoint"])
def test_codex_resume_authority_requires_original_barrier_evidence(canaries, damage):
    from shared.lib.fanout.canary_proof import issue_turn
    helper, (standard, _) = canaries
    smoke, runtime = helper.smoke, helper.smoke.fanout
    root = Path(standard["private_run_root"])
    state = json.loads((root / "state.json").read_bytes())
    controller = runtime.resume_lifecycle_controller(root / "controller",
        runtime.LifecycleCapability(state["controller_token"]))
    inputs = runtime.RunInputs.from_dict(state["inputs"])
    owner = runtime.OwnerCapability.from_token(state["owner_token"])
    authority = runtime.LocalAnchorAuthority(root / "authority",
        run_root=root / "journal", repo_root=root / "repo")
    packet = smoke._packet(root / "repo", smoke._plan(live=True), run_id=state["run_id"])
    catalog = runtime.ProviderRegistry.default()
    profiles = [runtime.ExecutorProfile.from_dict(value,
        adapter=catalog.require(value["executor_id"])) for value in state["provider_profile_descriptors"]]
    registry = runtime.ProviderRegistry(profiles)
    with runtime.RunJournal.resume(root / "journal", inputs, owner, anchor_store=authority) as journal:
        with runtime.ArtifactStore.open_existing(root / "artifacts") as store:
            baseline = runtime.capture_repository_baseline(root / "repo")
            memory = smoke._live_memory(store, Path(state["memory_controller_path"]),
                state["memory_controller_sha256"])
            coordinator = smoke._live_coordinator(root, store, journal, owner, controller,
                baseline, registry, memory, runtime.run_provider)
            seats, _ = smoke._seats(root, baseline, controller, store, restore=True,
                workspace_evidence=state["workspace_evidence"], registry=registry)
            seat = next(item for item in seats if item.executor_id == "codex")
            first = coordinator.restore_barrier(runtime.ArtifactRef(**state["barrier_ref"]), packet=packet)
            prior = next(item for item in first.terminals if item.executor_id == "codex")
            peer = coordinator.build_peer_packet(packet, first, target_seat_id=seat.seat_id)
            if damage == "peer":
                document = json.loads(peer.payload)
                document["peers"][0]["checkpoint_digest"] = "e" * 64
                payload = runtime.canonical_json(document)
                peer = dataclasses.replace(peer, payload=payload,
                    packet_sha256=hashlib.sha256(payload).hexdigest(), artifact_ref=None)
            else:
                ref = first.receipts[0].verification_ref
                (store.root / ref.path).write_bytes(b"changed original checkpoint")
            route, _ = smoke._canary_route(**state["codex_canary_selectors"])
            with pytest.raises(runtime.ProviderRequestError, match="original.*barrier"):
                issue_turn(coordinator, packet, seat, 2, route, prior=prior, peer=peer)


def test_complete_raw_canaries_reopen_profiles_models_sessions_and_distinct_hashes(canaries):
    helper, (standard, deep) = canaries
    summary = helper.summarize_readonly_canaries(standard, deep)
    for tier, receipt in (("standard", standard), ("deep", deep)):
        raw = (Path(receipt["private_run_root"]) / "receipt.json").read_bytes()
        row = summary["tiers"][tier]
        assert row["raw_receipt_sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["raw_receipt_sha256"] != receipt["receipt_sha256"]
        assert set(row["providers"]) == {"claude", "codex", "agy"}
        assert all(item["requested_model"] == item["observed_model"]
                   for item in row["providers"].values())
    assert "private_run_root" not in json.dumps(summary)
    assert "fake-session" not in json.dumps(summary)


@pytest.mark.parametrize("damage", ["status", "source", "profile", "replay", "raw",
                                     "missing-proof", "missing-model", "session", "stdout", "checkpoint"])
def test_canaries_refuse_incomplete_stale_or_tampered_raw_proof(canaries, damage):
    helper, (standard, deep) = canaries
    root = Path(deep["private_run_root"])
    barrier = json.loads((root / "artifacts" / deep["barrier_refs"]["second"]["path"]).read_bytes())
    terminal = next(item for item in barrier["terminals"] if item["evidence"]["executor_id"] == "codex")
    if damage in {"raw", "stdout"}:
        path = root / "receipt.json" if damage == "raw" else (
            root / "artifacts" / terminal["evidence"]["stdout_ref"]["path"])
        path.write_bytes(path.read_bytes() + b" ")
    elif damage in {"missing-model", "session", "profile"}:
        # Changed terminal claims are rejected even when the receipt itself is intact.
        path = root / "artifacts" / terminal["terminal_ref"]["path"]
        value = json.loads(path.read_bytes())
        value[{"missing-model": "observed_model", "session": "session_id", "profile": "profile_sha256"}[damage]] = "changed"
        path.write_bytes(helper.smoke.fanout.canonical_json(value))
    elif damage == "checkpoint":
        ref = helper.smoke.fanout.CheckpointReceipt.from_dict(barrier["receipts"][0]).verification_ref
        path = root / "artifacts" / ref.path
        path.write_bytes(b"changed checkpoint verification")
    else:
        if damage == "status":
            deep["status"] = "fail"
        elif damage == "source":
            deep["release_binding"]["components"]["memory"] = "0" * 64
        elif damage == "replay":
            deep["replayed_provider_turns"] = 1
        else:
            deep.pop("barrier_refs")
    with pytest.raises(helper.CanaryReceiptError):
        helper.summarize_readonly_canaries(standard, deep)


def test_canary_reader_rejects_unsafe_private_receipt_mode(canaries):
    helper, (standard, deep) = canaries
    (Path(deep["private_run_root"]) / "receipt.json").chmod(0o644)
    with pytest.raises(helper.CanaryReceiptError):
        helper.minimal_hash_summary(standard, deep, standard["source_sha256"])


def test_native_codex_missing_positive_model_stays_missing():
    helper = _helper()
    def probe(executor):
        assert executor == "codex"
        return "0.160.0"
    registry = helper.smoke.fanout.ProviderRegistry.for_installed(("codex",), version_probe=probe)
    parsed = registry.require("codex").parse(
        b'{"type":"thread.started","thread_id":"synthetic"}\n'
        b'{"type":"turn.completed","usage":{"input_tokens":1}}\n')
    assert parsed.observed_model is None
    profile = registry.admit("codex", "read-only", "deep")
    assert profile.cli_version == "0.160.0"
    assert profile.characterized is False


@pytest.mark.parametrize("canaries", ["requested-only"], indirect=True)
def test_authenticated_transport_pass_without_observed_model_is_not_a_canary_pass(canaries):
    helper, (standard, deep) = canaries
    assert standard["status"] == deep["status"] == "pass"
    with pytest.raises(helper.CanaryReceiptError):
        helper.summarize_readonly_canaries(standard, deep)


def _forbid_reopen_sends_and_probes(helper, monkeypatch):
    runtime = helper.smoke.fanout
    monkeypatch.setattr(runtime.MemoryController, "_invoke",
                        lambda *_: pytest.fail("stored canary reopening must not send"))
    monkeypatch.setattr(runtime, "run_provider",
                        lambda *_: pytest.fail("stored canary reopening must not dispatch"))
    providers = sys.modules[runtime.ProviderRegistry.__module__]
    monkeypatch.setattr(runtime.CollaborationCoordinator, "_provider_turn",
                        lambda *_args, **_kwargs: pytest.fail("stored canary reopening must not send a turn"))
    monkeypatch.setattr(providers, "run_command",
                        lambda *_args, **_kwargs: pytest.fail("stored canary reopening must not launch a provider"))
    monkeypatch.setattr(providers, "_installed_cli_version",
                        lambda *_: pytest.fail("stored canary reopening must not probe installed versions"))


def test_complete_canary_reopen_never_invokes_memory_or_provider_sender(canaries, monkeypatch):
    helper, (standard, deep) = canaries
    _forbid_reopen_sends_and_probes(helper, monkeypatch)
    assert helper.summarize_readonly_canaries(standard, deep)["status"] == "pass"


def _reseal_state(helper, receipt, state):
    """Keep completion authentic so a state mutation reaches descriptor validation."""
    runtime = helper.smoke.fanout
    root = Path(receipt["private_run_root"])
    raw = runtime.canonical_json(state)
    (root / "state.json").write_bytes(raw)
    controller = runtime.resume_lifecycle_controller(root / "controller",
        runtime.LifecycleCapability(state["controller_token"]))
    complete = helper.read_evidence(controller, "readonly-canary-complete",
        receipt.pop("completion_evidence_name"), receipt.pop("completion_evidence_digest"))
    receipt.pop("receipt_sha256")
    complete["state_sha256"] = hashlib.sha256(raw).hexdigest()
    complete["receipt_sha256"] = hashlib.sha256(runtime.canonical_json(receipt)).hexdigest()
    from shared.lib.fanout.controller import write_evidence
    receipt["completion_evidence_name"], receipt["completion_evidence_digest"] = write_evidence(
        controller, "readonly-canary-complete", complete)
    receipt["receipt_sha256"] = hashlib.sha256(runtime.canonical_json(receipt)).hexdigest()
    (root / "receipt.json").write_bytes(runtime.canonical_json(receipt))


@pytest.mark.parametrize("canaries", ["template"], indirect=True)
@pytest.mark.parametrize("damage", ["shape", "null", "row", "adapter", "executor", "duplicate",
                                     "binding", "artifact", "missing-artifact"])
def test_recorded_descriptors_need_exact_authenticated_escrow(canaries, monkeypatch, damage):
    helper, (standard, deep) = canaries
    runtime = helper.smoke.fanout
    root = Path(deep["private_run_root"])
    state = json.loads((root / "state.json").read_bytes())
    descriptors = state["provider_profile_descriptors"]
    descriptor = next(item for item in descriptors if item["executor_id"] == "codex"
                      and item["execution_class"] == "read-only" and item["quality_tier"] == "deep")
    digest = hashlib.sha256(runtime.canonical_json(descriptor)).hexdigest()
    path = root / "artifacts/amendments/executor-profiles" / (digest + ".json")
    if damage == "shape":
        state["provider_profile_descriptors"] = {}
    elif damage == "null":
        state["provider_profile_descriptors"] = None
    elif damage == "row":
        descriptors[0] = None
    elif damage == "adapter":
        descriptor["adapter_class"] = "untrusted.Adapter"
    elif damage == "executor":
        descriptor["executor_id"] = "unknown"
    elif damage == "duplicate":
        descriptors.append(dict(descriptor))
    elif damage == "binding":
        descriptor["cli_version"] = "0.999.0"
    elif damage == "artifact":
        path.write_bytes(path.read_bytes() + b" ")
    else:
        path.rename(path.with_suffix(".withheld"))
    _reseal_state(helper, deep, state)
    _forbid_reopen_sends_and_probes(helper, monkeypatch)
    with pytest.raises(helper.CanaryReceiptError):
        helper.summarize_readonly_canaries(standard, deep)


@pytest.mark.parametrize("canaries", ["template", "observed"], indirect=True)
def test_absent_descriptors_allow_only_the_authenticated_old_template(canaries, monkeypatch, request):
    helper, (standard, deep) = canaries
    for receipt in (standard, deep):
        root = Path(receipt["private_run_root"])
        state = json.loads((root / "state.json").read_bytes())
        state.pop("provider_profile_descriptors")
        escrow = root / "artifacts/amendments/executor-profiles"
        escrow.rename(escrow.with_name("withheld-executor-profiles"))
        _reseal_state(helper, receipt, state)
    _forbid_reopen_sends_and_probes(helper, monkeypatch)
    if request.node.callspec.params["canaries"] == "template":
        assert helper.summarize_readonly_canaries(standard, deep)["status"] == "pass"
    else:
        with pytest.raises(helper.CanaryReceiptError):
            helper.summarize_readonly_canaries(standard, deep)


@pytest.mark.parametrize("deep_passes", [False, True])
def test_cli_publishes_only_after_both_complete_tiers_without_retry(canaries, tmp_path, monkeypatch, deep_passes):
    helper, receipts = canaries
    destination = tmp_path / "public-summary.json"
    destination.write_bytes(b"previous public summary")
    monkeypatch.setattr(helper, "SUMMARY_PATH", destination)
    calls = []

    def run(root, *, authorized, quality_tier, machine_routes, machine_policy, codex_route):
        calls.append((root, authorized, quality_tier, machine_routes, machine_policy, codex_route))
        if quality_tier == "deep" and not deep_passes:
            raise helper.smoke.SmokeError("synthetic uncertain deep attempt")
        return receipts[0 if quality_tier == "standard" else 1]

    monkeypatch.setattr(helper.smoke, "run_live", run)
    root = tmp_path / "unique-attempt"
    routes, policy = tmp_path / "routes.json", tmp_path / "policy.json"
    status = helper.main(["--private-run-root", str(root), "--allow-paid", "--maximum-provider-turns", "12",
        "--machine-routes", str(routes), "--machine-policy", str(policy), "--codex-route", "codex-company"])
    assert [(call[1], call[2]) for call in calls] == [(True, "standard"), (True, "deep")]
    assert [call[3:] for call in calls] == [(routes, policy, "codex-company")] * 2
    assert len({call[0] for call in calls}) == 2
    if deep_passes:
        assert status == 0
        assert json.loads(destination.read_bytes())["status"] == "pass"
    else:
        assert status == 1
        assert destination.read_bytes() == b"previous public summary"
        assert not destination.with_name(destination.name + ".pending").exists()


@pytest.mark.parametrize("entry", ["fanout_readonly_canaries.py", "fanout_release_evaluations.py"])
def test_owned_helper_bootstraps_before_foreign_parent_package_can_execute(tmp_path, entry):
    import os
    import subprocess
    foreign = tmp_path / "foreign"
    (foreign / "shared").mkdir(parents=True)
    marker = tmp_path / "foreign-import-marker"
    (foreign / "shared/__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('foreign shared')\n")
    environment = dict(os.environ, PYTHONPATH=str(foreign), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / entry), "--help"],
        cwd=foreign, env=environment, capture_output=True, check=False)
    assert result.returncode == 0
    assert b"--private-run-root" in result.stdout
    assert not marker.exists()


@pytest.mark.parametrize("association", ["plan", "revision", "baseline"])
def test_authentic_synthesis_with_wrong_current_association_cannot_be_reused(canaries, association):
    helper, (standard, deep) = canaries
    runtime = helper.smoke.fanout
    root = Path(deep["private_run_root"])
    state = json.loads((root / "state.json").read_bytes())
    controller = runtime.resume_lifecycle_controller(root / "controller",
        runtime.LifecycleCapability(state["controller_token"]))
    with runtime.ArtifactStore.open_existing(root / "artifacts") as store:
        original = runtime.load_answer_synthesis(controller, store, run_id=deep["run_id"],
            task_id=helper.smoke.TASK_ID, answer_ref=runtime.ArtifactRef(**deep["synthesis_ref"]))
        alternate = runtime.verify_answer_synthesis(store, run_id=original.run_id,
            task_id=original.task_id,
            plan_sha256="f" * 64 if association == "plan" else original.plan_sha256,
            plan_revision=2 if association == "revision" else 1,
            sources=dict(original.source_answers), synthesizer_id="smoke-orchestrator",
            answer=store.read_bytes(original.answer_ref), checks=(), controller=controller,
            baseline=None if association == "baseline" else runtime.capture_repository_baseline(root / "repo"))
        assert alternate.valid
        deep["synthesis_ref"] = dataclasses.asdict(alternate.answer_ref)
    complete = helper.read_evidence(controller, "readonly-canary-complete",
        deep.pop("completion_evidence_name"), deep.pop("completion_evidence_digest"))
    deep.pop("receipt_sha256")
    complete["receipt_sha256"] = hashlib.sha256(runtime.canonical_json(deep)).hexdigest()
    from shared.lib.fanout.controller import write_evidence
    deep["completion_evidence_name"], deep["completion_evidence_digest"] = write_evidence(
        controller, "readonly-canary-complete", complete)
    deep["receipt_sha256"] = hashlib.sha256(runtime.canonical_json(deep)).hexdigest()
    (root / "receipt.json").write_bytes(runtime.canonical_json(deep))
    with pytest.raises(helper.CanaryReceiptError):
        helper.summarize_readonly_canaries(standard, deep)
