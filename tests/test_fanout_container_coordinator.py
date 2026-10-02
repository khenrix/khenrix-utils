"""Production composition uses real persisted owner authority; provider I/O stays off."""
from types import SimpleNamespace
import importlib

import pytest

from test_fanout_container_certification import (
    complete_private_probe, persisted_probe, certificate_authority, trial_roots,
    certifier, company_manifest, complete_receipt,
)
from shared.lib.fanout.providers import ProviderRegistry
from shared.lib.fanout.errors import CollaborationValidationError
from test_fanout_container_boundary import boundary_module


def test_coordinator_retains_observed_vm_identity_across_actual_turn_cleanup(
    boundary_module, company_manifest, tmp_path, monkeypatch,
):
    """Exercise lifetime/capture only; certificate/startup admission is inert setup."""
    from dataclasses import replace
    from shared.lib import fanout as runtime
    from shared.lib.fanout import container_coordinator as helper, container_certification as authority, container_credentials
    from shared.lib.fanout.providers import run_provider
    from shared.lib.fanout.controller import read_evidence, write_evidence
    from test_fanout_container_boundary import _launched_seat
    from scripts import fanout_container_certify as probe
    request, original_boundary, transport, docker, _socket, sent = _launched_seat(boundary_module, tmp_path)
    controller, receipt = original_boundary.controller, original_boundary.admission.input_receipt
    observed = {"profile": "fanout-isolated", "profile_dir": {"inode": 7}}
    control = object.__new__(boundary_module.PinnedSSHSeatControl)
    control.controller, control.spec, control._lease = controller, transport.spec, SimpleNamespace(_fd=1)
    control.vm_observation = {"profile_identity": observed}
    control.close = lambda: observed["profile_dir"].update(inode=999)
    transport._control = control
    with original_boundary.admission.ledger._locked() as state:
        state.update(permits={}, requests={}, send_claims={})
        original_boundary.admission.ledger._save(state)
    path, policy, _ = company_manifest
    requested = {provider: provider + "-company" for provider in ("claude", "codex", "agy")}
    _, routes = probe._admit_route_set(path, policy, requested)
    certificate = authority.TrialCertificate(runtime.canonical_json({"probe_root": str(tmp_path),
        "image_digest": transport.spec.image_digest, "cli_digests": {"claude": transport.spec.cli_digest},
        "route_digests": {"claude": routes["claude"].digest},
        "vm": {"claude": {"profile_identity": {"not_observed": True}}}}), "0" * 64)
    coordinator = SimpleNamespace(provider_runner=run_provider, lifecycle_controller=controller)
    packet = SimpleNamespace(attempt=1, provider_policy=SimpleNamespace(quality_tier="standard"),
                             to_dict=lambda: {"task_id": request.task_id})
    seat = SimpleNamespace(executor_id="claude")
    monkeypatch.setattr(helper, "authenticate_inputs", lambda *_: (SimpleNamespace(digest=request.inputs_digest), None))
    context = write_evidence(controller, "container-coordinator-context", {"fixture": "lifetime only"})
    monkeypatch.setattr(helper, "bind_turn_context", lambda *_: context)
    monkeypatch.setattr(helper, "admit_profile", lambda *_: request.profile)
    monkeypatch.setattr(helper, "_certificate", lambda *_: certificate)
    monkeypatch.setattr(probe, "_read_private_receipt", lambda _: {"manifest_path": str(path), "policy_path": str(policy),
        "machine_routes": {provider: {"route_id": route_id} for provider, route_id in requested.items()}})
    monkeypatch.setattr(container_credentials, "HostCredentialSource", lambda _: SimpleNamespace(
        load=lambda: "synthetic-only-credential", fingerprint="f" * 64))
    material = (controller.root / "quarantine/source", receipt.material_evidence_name, receipt.material_evidence_digest)
    monkeypatch.setattr(helper, "stage_input_material", lambda *_: material)
    monkeypatch.setattr(transport, "stage_inputs", lambda *_: receipt)
    def existing_owned_seat(_cls, admitted_controller, spec, **kwargs):
        assert admitted_controller is controller and spec == transport.spec
        control.permit_id, control.epoch = kwargs["permit_id"], kwargs["epoch"]
        return transport
    monkeypatch.setattr(boundary_module.PinnedDockerTransport, "start_live", classmethod(existing_owned_seat))
    closed = []
    def owned_turn(launched, _certificate, _roots):
        result = boundary_module.run_container_turn(launched, launched.container_boundary)
        closed.append(transport._control is None)
        return result
    monkeypatch.setattr(authority, "run_trial_turn", owned_turn)
    with runtime.ArtifactStore(tmp_path / "private-transcripts") as transcripts:
        request = replace(request, artifact_store=transcripts)
        result = helper.run_coordinator_turn(coordinator, packet, seat, 1, request, None)
        assert result.valid and closed == [True] and len(sent) == 1
        assert transport._control is None and transport.stop_evidence is not None
        assert docker.inspect["State"]["Running"] is False
        completed = read_evidence(controller, "container-coordinator-turn", *result.container_completion)
        assert completed["vm_profile_identity"] == {"profile": "fanout-isolated", "profile_dir": {"inode": 7}}
        assert completed["process"]["stdout"] == {"path": result.stdout_ref.path, "digest": result.stdout_ref.digest,
                                                 "size": result.stdout_ref.size}
        _, _, _, fenced = probe._ledger_snapshot(controller, completed["permit_id"], completed["permit_epoch"])
        assert fenced and completed["stop"] == list(transport.stop_evidence)


@pytest.mark.parametrize("changed", [None, "profile", "tier", "roots", "certificate"])
def test_coordinator_profile_admission_reopens_owner_trial_without_host_probe(
    complete_private_probe, certificate_authority, trial_roots, monkeypatch, changed,
):
    from shared.lib.fanout import container_certification as authority
    helper = importlib.import_module("shared.lib.fanout.container_coordinator")
    certificate = certificate_authority.issue_trial(complete_private_probe[0], trial_roots)
    monkeypatch.setattr(authority, "_owner_authority", lambda: certificate_authority)
    registry = ProviderRegistry.default(version_probe=lambda _: pytest.fail("host CLI probe reached"))
    profile = registry.select("codex", "repo-write", "standard")
    inputs = SimpleNamespace(provider_profiles={profile.binding_key: profile.digest},
        targets={str(index): SimpleNamespace(root=root) for index, root in enumerate(trial_roots)})
    coordinator = SimpleNamespace(trial_certificate=certificate, registry=registry)
    if changed == "profile":
        inputs.provider_profiles[profile.binding_key] = "0" * 64
    elif changed == "tier":
        pass
    elif changed == "roots":
        inputs.targets = {"changed": SimpleNamespace(root=trial_roots[0])}
    elif changed == "certificate":
        coordinator.trial_certificate = authority.TrialCertificate(certificate.payload, "0" * 64)
    policy = SimpleNamespace(quality_tier="deep" if changed == "tier" else "standard")
    seat = SimpleNamespace(executor_id="codex")
    admit = getattr(helper, "admit_profile", None)
    assert callable(admit), "certificate-bound coordinator profile admission is missing"
    if changed:
        with pytest.raises(CollaborationValidationError):
            admit(coordinator, inputs, seat, policy)
    else:
        admitted = admit(coordinator, inputs, seat, policy)
        assert admitted.digest == profile.digest
        assert admitted.characterized is False
        with pytest.raises(Exception):
            registry.admit("codex", "repo-write", "standard")


def test_coordinator_selected_profile_uses_actual_certificate_admission(
    complete_private_probe, certificate_authority, trial_roots, monkeypatch,
):
    from shared.lib.fanout import container_certification as authority
    from shared.lib.fanout.collaboration import CollaborationCoordinator, RoundPolicy
    from shared.lib.fanout.plan import ProviderPolicyV1
    certificate = certificate_authority.issue_trial(complete_private_probe[0], trial_roots)
    monkeypatch.setattr(authority, "_owner_authority", lambda: certificate_authority)
    registry = ProviderRegistry.default(version_probe=lambda _: pytest.fail("host CLI probe reached"))
    profile = registry.select("codex", "repo-write", "standard")
    inputs = SimpleNamespace(profile_shape="class-tier", provider_profiles={profile.binding_key: profile.digest},
        targets={str(index): SimpleNamespace(root=root) for index, root in enumerate(trial_roots)})
    coordinator = object.__new__(CollaborationCoordinator)
    coordinator.registry, coordinator.trial_certificate, coordinator._container_execution = registry, certificate, True
    packet = SimpleNamespace(execution_class="repo-write", provider_policy=ProviderPolicyV1(
        executor_ids=("claude", "codex"), rounds=2, timeout=120, retries=0, minimum_success=2))
    selected, timeout = coordinator._selected_profile(packet, SimpleNamespace(executor_id="codex"),
        RoundPolicy.from_provider_policy(packet.provider_policy), inputs)
    assert selected.digest == profile.digest and timeout == 120


def test_checkout_cli_uses_the_certifier_runtime_identity(complete_private_probe):
    from pathlib import Path
    import importlib.util
    import sys
    path = Path(__file__).resolve().parents[1] / "shared/skills/llm-fanout-execute/scripts/execute.py"
    spec = importlib.util.spec_from_file_location("container_execute_identity", path)
    cli = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    spec.loader.exec_module(cli)
    runtime = cli._runtime()
    reader = importlib.import_module(runtime.__name__ + ".container_certification")
    proof = reader.read_private_route_evidence(complete_private_probe[0], require_attestations=True)
    assert proof.attestations_complete
    from shared.lib import fanout
    assert runtime is fanout


@pytest.mark.parametrize("parent", ["shared", "scripts"])
def test_checkout_loader_never_executes_a_foreign_parent(tmp_path, parent):
    from pathlib import Path
    import subprocess
    import sys
    foreign = tmp_path / "foreign"
    (foreign / parent).mkdir(parents=True)
    marker = tmp_path / "foreign-executed"
    (foreign / parent / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n")
    path = Path(__file__).resolve().parents[1] / "shared/skills/llm-fanout-execute/scripts/execute.py"
    script = f"""import importlib.util,sys
sys.path.insert(0, {str(foreign)!r})
spec=importlib.util.spec_from_file_location('isolated_execute', {str(path)!r})
cli=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=cli
spec.loader.exec_module(cli)
runtime=cli._runtime()
assert runtime.__name__ == 'shared.lib.fanout'
reader=__import__('shared.lib.fanout.container_certification', fromlist=['read_private_route_evidence'])
try:
    reader.read_private_route_evidence({str(tmp_path / 'absent-proof')!r})
except reader.CertificationRejected:
    pass
"""
    process = subprocess.run([sys.executable, "-I", "-B", "-c", script], capture_output=True)
    assert not marker.exists(), process.stderr.decode()
    assert process.returncode == 0, process.stderr.decode()


@pytest.mark.parametrize("entry", ["fanout_container_certify.py", "fanout_release_certificate.py"])
@pytest.mark.parametrize("parent", ["shared", "scripts"])
@pytest.mark.parametrize("loaded", [False, True])
def test_authoritative_checkout_entries_refuse_foreign_parent_execution(tmp_path, entry, parent, loaded):
    import os
    import subprocess
    import sys
    from pathlib import Path
    foreign = tmp_path / "foreign" / parent
    foreign.mkdir(parents=True)
    marker = tmp_path / "foreign-executed"
    (foreign / "__init__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n")
    script = Path(__file__).resolve().parents[1] / "scripts" / entry
    argv = (sys.executable, "-B", str(script), "--help")
    if loaded:
        setup = f"""import sys,types,runpy
parent=types.ModuleType({parent!r})
parent.__file__={str(foreign / '__init__.py')!r}
parent.__path__=[{str(foreign)!r}]
sys.modules[{parent!r}]=parent
sys.argv=[{str(script)!r},'--help']
runpy.run_path({str(script)!r},run_name='__main__')
"""
        argv = (sys.executable, "-B", "-c", setup)
    result = subprocess.run(argv, cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(foreign.parent), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True)
    assert not marker.exists(), result.stderr.decode()
    assert (result.returncode != 0 if loaded else result.returncode == 0), result.stderr.decode()


def test_completed_trial_certificate_reopens_actual_cas_but_cannot_launch(
    complete_private_probe, certificate_authority, trial_roots, tmp_path, monkeypatch,
):
    from pathlib import Path
    from shared.lib import fanout as runtime
    from shared.lib.fanout import container_certification as authority
    from shared.lib.fanout.container_source import release_source_binding, _SOURCE_ROOT
    from shared.lib.fanout.container_coordinator import _execute_source
    from shared.lib.fanout import container_coordinator as helper
    from scripts import fanout_container_certify as probe
    from shared.lib.fanout import container_credentials
    monkeypatch.setattr(container_credentials, "HostCredentialSource", probe.HostCredentialSource)
    from shared.lib.fanout.repo import _git
    from shared.lib.fanout import branch_handover as handover
    from test_fanout_multirepo_integration import _admitted_two_writer_packet
    packet, skills = _admitted_two_writer_packet(tmp_path, dependency_mode="artifact")
    target_roots = dict(zip(("address", "booking"), trial_roots))
    for target_id, root in target_roots.items():
        _git(root, "config", "user.name", "Synthetic CAS fixture")
        _git(root, "config", "user.email", "fixture@example.invalid")
        _git(root, "config", "remote.origin.url", f"https://github.com/example/{target_id}-service.git")
    certificate = certificate_authority.issue_trial(complete_private_probe[0], trial_roots)
    monkeypatch.setattr(authority, "_owner_authority", lambda: certificate_authority)
    root = trial_roots[0].parent.parent
    certificate_path = root / "trial-certificate.json"
    certificate_path.write_bytes(runtime.canonical_json(certificate.to_dict()))
    certificate_path.chmod(0o600)
    trial_controller = runtime.create_lifecycle_controller(root / "controller")
    probe._write_private(root.with_name(root.name + ".controller-capability"), trial_controller.capability.export_token().encode("ascii"))
    capability_path = root.with_name(root.name + ".controller-capability")
    original_capability = capability_path.read_bytes()
    with pytest.raises(runtime.CollaborationValidationError, match="prior execution state"):
        helper.produce_container_trial(root, certificate, packet, (skills,), allow_paid=True)
    assert capability_path.read_bytes() == original_capability
    raw = runtime.ArtifactStore(root / "raw")
    private = root / "private"
    private.mkdir(mode=0o700)
    execute, _ = _execute_source()
    execute.start_v2_run(packet, target_roots=target_roots, run_root=root / "run",
        authority_root=root / "authority", private_root=private, skill_roots=(skills,),
        budget=12, runtime=runtime)
    memory_path = root / "reopen-memory/memory_exchange.py"
    memory_path.parent.mkdir(mode=0o700)
    memory_path.write_bytes((Path(_SOURCE_ROOT) / "components/memory/memory_exchange.py").read_bytes())
    memory_path.chmod(0o700)
    memory_controller = runtime.MemoryController(memory_path,
        executable_sha256=__import__("hashlib").sha256(memory_path.read_bytes()).hexdigest())
    class InertMemory(runtime.MemoryCheckpointExchange):
        def preflight(self):
            return True
    with execute.open_run(private, runtime=runtime, trial_certificate=certificate, for_dispatch=True,
            memory_factory=lambda artifacts, _health: InertMemory(memory_controller, artifacts),
            memory_preflight=lambda: True) as opened:
        service = opened.service
        service.start(owner=opened.owner)
        prepared = []
        fault = None
        for task_id in ("change", "booking"):
            service.scheduler.mark_active(task_id, owner=opened.owner)
            task = next(task for task in service.plan.tasks if task.id == task_id)
            baseline = service.repository_baselines[task.target_id]
            sources, fault_ref = _persist_synthetic_core_barriers(opened, task, certificate,
                probe._read_private_receipt(complete_private_probe[0]), trial_controller, raw, partial=task_id == "change")
            fault = fault_ref or fault
            service.scheduler.begin_reconciliation(task_id, owner=opened.owner)
            service._ensure_reconciliation_journal(task_id, opened.owner)
            candidate = runtime.synthesize_candidate(baseline, sources=sources,
                entries=sources[0].entries, deleted_paths=sources[0].deleted_paths)
            fresh = service.verify_repo_synthesis(task.id, candidate, owner=opened.owner)
            assert fresh.valid
            service.submit(task.id, fresh, owner=opened.owner)
            issued = runtime.issue_target_candidate(candidate, task_id=task_id, plan=service.plan,
                inputs=service.inputs, store=opened.artifacts, controller=service.lifecycle_controller)
            verified, receipt = runtime.verify_target_candidate(issued, baseline=baseline, plan=service.plan,
                inputs=service.inputs, store=opened.artifacts, controller=service.lifecycle_controller)
            assert receipt.valid
            prepared.append((task, issued, verified))
            service.scheduler.schedule_ready(owner=opened.owner)
        assert certificate_authority.verify_trial(certificate, trial_roots, release_source_binding().digest).passed
        for task, issued, verified in prepared:
            transaction = handover.prepare_branch_handover(service.inputs.targets[task.target_id], issued, verified,
                plan=service.plan, inputs=service.inputs, artifacts=opened.artifacts, scheduler=service.scheduler,
                controller=service.lifecycle_controller, journal=opened.journal, owner=opened.owner, task_id=task.id)
            terminal = handover.deliver_branch_candidate(transaction, plan=service.plan, inputs=service.inputs,
                artifacts=opened.artifacts, scheduler=service.scheduler, controller=service.lifecycle_controller,
                journal=opened.journal, owner=opened.owner)
            assert isinstance(terminal, runtime.HandoverTerminalV2)
            assert service.scheduler.handover_terminal_for(task.id) == terminal
            handover.verify_git_handover_proof(transaction.intent, terminal, inputs=service.inputs,
                artifacts=opened.artifacts, controller=service.lifecycle_controller, journal=opened.journal)
        assert not certificate_authority.verify_trial(certificate, trial_roots, release_source_binding().digest).passed
    verify_completed = getattr(certificate_authority, "_verify_completed_trial", None)
    assert callable(verify_completed), "completed CAS cannot join historical trial certificate authority"
    with pytest.raises(authority.CertificationRejected):
        certificate_authority.admit_trial_profile(certificate, trial_roots, "codex", source_hash=release_source_binding().digest)
    for method in ("preflight", "save", "fetch", "_invoke"):
        monkeypatch.setattr(runtime.MemoryController, method,
            lambda *args, **kwargs: pytest.fail("semantic reopen executed memory"))
    for method in ("preflight", "publish", "recover", "fetch_verified"):
        monkeypatch.setattr(runtime.MemoryCheckpointExchange, method,
            lambda *args, **kwargs: pytest.fail("semantic reopen queried memory"))
    from shared.lib.fanout.container_coordinator import completed_trial_root_bindings
    certificate_authority._authenticated_certificate(certificate, release_source_binding().digest, trial=True)
    assert completed_trial_root_bindings(root) == __import__("json").loads(certificate.payload)["roots"]
    assert verify_completed(certificate, root).passed
    from shared.lib.fanout.controller import write_evidence
    from shared.lib.fanout.runstate import _read_private_file
    from dataclasses import asdict
    with helper._open_trial_authority(root) as cold:
        cold.journal.recover_uncertain(cold.owner)
        events = _read_private_file(cold.journal._root_fd, cold.journal._EVENT_FILE, cold.journal.limits.max_journal_bytes)
        cold_ref = write_evidence(trial_controller, "container-trial-cold-recovery", {
            "run_id": cold.journal.inputs.run_id, "inputs_digest": cold.journal.inputs.digest,
            "fault": list(fault), "events": asdict(raw.write_bytes("cold/events.jsonl", events))})
        common = {"schema_version": "fanout-container-trial-core-v1", "private_root": str(private),
            "run_id": cold.journal.inputs.run_id, "inputs_digest": cold.journal.inputs.digest,
            "source_binding": release_source_binding().to_dict(), "fault": list(fault), "cold": list(cold_ref),
            "tasks": {task_id: [asdict(ref) for ref in cold.service._execution_state(task_id).barriers]
                      for task_id in ("change", "booking")}}
    checks = {name: {**common, "check": name} for name in authority.TRIAL_CHECKS}
    assert helper.validate_trial_core(trial_controller, root, checks, release_source_binding()).passed
    unsigned = {"schema_version": "fanout-synthetic-trial-result-v1", "private_run_root": str(root),
        "trial_certificate": certificate.to_dict(), "release_source_binding": release_source_binding().to_dict(),
        "checks": {name: {"artifact": asdict(raw.write_json(f"core/{name}.json", value))}
                   for name, value in checks.items()}}
    completion = write_evidence(trial_controller, "container-synthetic-trial-complete", {
        "trial_result_sha256": __import__("hashlib").sha256(runtime.canonical_json(unsigned)).hexdigest()})
    result = {**unsigned, "completion_evidence_name": completion[0], "completion_evidence_digest": completion[1]}
    probe._write_private(root / "trial-result.json", runtime.canonical_json(result))
    original_proof = authority.read_private_route_evidence(complete_private_probe[0], require_attestations=True)
    assert certificate_authority._accepted_trial(root, original_proof)
    _assert_persisted_certificate_turn_refusals(root, certificate, trial_controller, checks, monkeypatch)
    missing_capability = root / "preserved-controller-capability"
    capability_path.rename(missing_capability)
    with pytest.raises(authority.CertificationRejected):
        certificate_authority._accepted_trial(root, original_proof)
    assert not capability_path.exists()
    missing_capability.rename(capability_path)
    observed_tampering = {}
    with helper._open_trial_authority(root) as cold:
        task = next(task for task in cold.service.plan.tasks if task.id == "change")
        barrier = cold.service._restore_barrier(cold.service._execution_state(task.id).barriers[0], cold.service._packet_for(task))
        checkpoint_path = cold.artifacts.root / barrier.receipts[0].publication.checkpoint_ref.path
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint_path.write_bytes(checkpoint_bytes + b"changed")
    observed_tampering["checkpoint"] = helper.validate_trial_core(trial_controller, root, checks, release_source_binding()).passed
    checkpoint_path.write_bytes(checkpoint_bytes)
    from shared.lib.fanout.controller import read_evidence
    fault_value = read_evidence(trial_controller, "container-trial-session-read-fault", *fault)
    wrong_fault = write_evidence(trial_controller, "container-trial-session-read-fault", {
        **fault_value, "initial_barrier": common["tasks"]["booking"][0]})
    wrong_cold = write_evidence(trial_controller, "container-trial-cold-recovery", {
        **read_evidence(trial_controller, "container-trial-cold-recovery", *cold_ref), "fault": list(wrong_fault)})
    wrong_checks = {name: {**value, "fault": list(wrong_fault), "cold": list(wrong_cold)} for name, value in checks.items()}
    observed_tampering["fault-barrier"] = helper.validate_trial_core(trial_controller, root, wrong_checks, release_source_binding()).passed
    certificate_bytes = certificate_path.read_bytes()
    certificate_path.write_bytes(runtime.canonical_json(authority.TrialCertificate(certificate.payload, "0" * 64).to_dict()))
    observed_tampering["certificate"] = helper.validate_trial_core(trial_controller, root, checks, release_source_binding()).passed
    certificate_path.write_bytes(certificate_bytes)
    assert observed_tampering == {"checkpoint": False, "fault-barrier": False, "certificate": False}
    raw.close()
    assert not verify_completed(authority.TrialCertificate(certificate.payload, "0" * 64), root).passed
    inputs_path = root / "run/inputs.json"
    original_inputs = inputs_path.read_bytes()
    inputs_path.write_bytes(original_inputs.replace(b'"run_id":', b'"unknown":', 1))
    assert not verify_completed(certificate, root).passed


def _assert_persisted_certificate_turn_refusals(root, certificate, trial_controller, checks, monkeypatch):
    from dataclasses import asdict
    import copy
    import hashlib
    import json
    from shared.lib import fanout as runtime
    from shared.lib.fanout import container_coordinator as helper
    from shared.lib.fanout.container_boundary import _turn_digest
    from shared.lib.fanout.container_certification import CertificationRejected
    from shared.lib.fanout.container_runtime import ContainerSpec, ContainerLauncher
    from shared.lib.fanout.container_source import release_source_binding
    from shared.lib.fanout.controller import read_evidence, write_evidence
    fields = ("image_digest", "cli_digest", "profile_digest", "upstream_route_digest",
              "vm_docker_endpoint", "vm_ssh_host_key_sha256", "vm_effective_mounts_sha256", "vm_guest_mounts_sha256")
    cases = [(field, missing) for field in fields for missing in (False, True)]
    cases.extend((field, missing) for field in ("vm_profile_identity", "schema_version") for missing in (False, True))
    cases.append(("socket_inode", True))
    results = {}
    with helper._open_trial_authority(root) as cold:
        coordinator = cold.service.coordinator
        coordinator.trial_certificate = certificate
        task = next(task for task in cold.service.plan.tasks if task.id == "change")
        packet = cold.service._packet_for(task)
        first = cold.service._restore_barrier(cold.service._execution_state(task.id).barriers[0], packet)
        terminal = next(item for item in first.valid_terminals if item.executor_id == "codex")
        seat = next(item for item in cold.service.preparations[task.id].seats if item.seat_id == terminal.seat_id)
        original = helper.reopen_turn_completion(coordinator, packet, seat, terminal, None)
        pointer = cold.artifacts.root / (coordinator._provider_prefix(packet, 1, seat.seat_id) + "/container-turn.json")
        original_pointer = pointer.read_bytes()
        boundary = read_evidence(coordinator.lifecycle_controller, "container-seat-admission", *original["boundary"])
        context = read_evidence(coordinator.lifecycle_controller, "container-coordinator-context", *original["context"])
        profile = coordinator.registry.select("codex", "repo-write", "standard")
        request = runtime.ProviderRequest(prompt=helper.transcript_store(coordinator).read_bytes(runtime.ArtifactRef(**context["prompt"])),
                                          profile=profile, **context["request"])
        stopped = read_evidence(coordinator.lifecycle_controller, "container-seat-stopped", *original["stop"])
        raw_path = coordinator.lifecycle_controller.root / "quarantine/runtime-proofs" / stopped["raw_inspect"]["path"]
        original_raw = raw_path.read_bytes()
        try:
            for field, missing in cases:
                changed_boundary, changed_proof = copy.deepcopy(boundary), copy.deepcopy(original)
                if field == "vm_profile_identity":
                    if missing:
                        changed_proof.pop(field)
                    else:
                        changed_proof[field]["profile_dir"]["inode"] += 1
                elif missing:
                    changed_boundary.pop(field)
                else:
                    changed_boundary[field] = ("fanout-seat@sha256:" + "9" * 64 if field == "image_digest" else
                        "unix:///synthetic/foreign.sock" if field == "vm_docker_endpoint" else
                        "foreign-schema" if field == "schema_version" else "8" * 64)
                if not missing and field in {"image_digest", "cli_digest"}:
                    changed_stop = copy.deepcopy(stopped)
                    changed_stop["spec"][field] = changed_boundary[field]
                    spec = ContainerSpec(**changed_stop["spec"])
                    launch = write_evidence(coordinator.lifecycle_controller, "container-seat-launch", {
                        "spec": asdict(spec), "handle": changed_stop["handle"], "route": "start"})
                    changed_stop.update(launch_evidence_name=launch[0], launch_evidence_digest=launch[1])
                    changed_boundary.update(handle_evidence_name=launch[0], handle_evidence_digest=launch[1])
                    inspect = json.loads(original_raw)
                    inspect[0]["Config"].update(Image=spec.image_digest, Labels=ContainerLauncher._labels(spec))
                    raw_bytes = runtime.canonical_json(inspect)
                    raw_path.write_bytes(raw_bytes)
                    changed_stop["raw_inspect"] = asdict(runtime.ArtifactRef(stopped["raw_inspect"]["path"],
                        hashlib.sha256(raw_bytes).hexdigest(), len(raw_bytes)))
                    changed_proof["stop"] = list(write_evidence(coordinator.lifecycle_controller, "container-seat-stopped", changed_stop))
                changed_proof["boundary"] = list(write_evidence(coordinator.lifecycle_controller, "container-seat-admission", changed_boundary))
                if not missing or field != "upstream_route_digest":
                    changed_proof["turn_digest"] = _turn_digest(request, SimpleNamespace(**changed_boundary))
                changed_ref = write_evidence(coordinator.lifecycle_controller, "container-coordinator-turn", changed_proof)
                pointer.write_bytes(runtime.canonical_json({"evidence": list(changed_ref)}))
                rejected = False
                try:
                    helper.reopen_turn_completion(coordinator, packet, seat, terminal, None)
                except CertificationRejected:
                    rejected = True
                except (ValueError, KeyError, AttributeError):
                    pass
                results[(field, missing)] = (rejected,
                    helper.validate_trial_core(trial_controller, root, checks, release_source_binding()).passed)
                pointer.write_bytes(original_pointer)
                raw_path.write_bytes(original_raw)
        finally:
            pointer.write_bytes(original_pointer)
            raw_path.write_bytes(original_raw)
    assert all(value == (True, False) for value in results.values()), results


def test_no_spend_trial_cli_prepares_targets_before_owner_issuance(tmp_path, certifier, capsys):
    root = tmp_path / "prepared-trial"
    root.mkdir(mode=0o700)
    assert certifier.main(["--prepare-synthetic-targets", "--private-run-root", str(root)]) == 0
    assert sorted(path.name for path in root.iterdir()) == ["targets"]
    assert sorted(path.name for path in (root / "targets").iterdir()) == ["repo-alpha", "repo-beta"]
    assert __import__("json").loads(capsys.readouterr().out)["provider_turns"] == 0


@pytest.mark.parametrize("unsafe", [False, True])
def test_trial_root_uses_the_certificate_private_temp_policy(tmp_path, monkeypatch, unsafe):
    import tempfile
    from pathlib import Path
    helper = importlib.import_module("shared.lib.fanout.container_coordinator")
    base = tmp_path
    with tempfile.TemporaryDirectory(prefix="fanout-core-root-", dir=base) as name:
        root = Path(name).resolve()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
        root.chmod(0o755 if unsafe else 0o700)
        if unsafe:
            with pytest.raises(CollaborationValidationError):
                helper._trial_root(root)
        else:
            assert helper._trial_root(root) == root


def test_trial_consumer_paid_gate_refuses_before_authority_or_state(tmp_path, monkeypatch):
    from shared.lib.fanout import container_coordinator as helper
    from shared.lib.fanout import container_certification as authority
    from shared.lib.fanout.errors import CollaborationValidationError
    root = tmp_path / "fresh-unpaid-trial"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(authority, "_owner_authority", lambda: pytest.fail("unpaid trial opened owner authority"))
    with pytest.raises(CollaborationValidationError, match="paid synthetic trial"):
        helper.produce_container_trial(root, None, None, (), allow_paid=False)
    assert not tuple(root.iterdir())


def _persist_synthetic_core_barriers(opened, task, certificate, probe_receipt, trial_controller, raw, *, partial):
    """Retain complete synthetic protocol bytes using the actual private codecs."""
    from dataclasses import asdict, replace
    from pathlib import Path
    from types import SimpleNamespace
    import hashlib
    import json
    from shared.lib import fanout as runtime
    from shared.lib.fanout import container_coordinator as helper
    from shared.lib.fanout.container_runtime import ContainerHandle, ContainerSpec, ContainerLauncher, private_volume_names
    from shared.lib.fanout.container_boundary import SessionStore, _turn_digest
    from shared.lib.fanout.container_broker import PermitLedger, OutboundRequestIdentity
    from shared.lib.fanout.broker import BrokerPermit
    from shared.lib.fanout.container_upstream import HostUpstream
    from shared.lib.fanout.container_transfer import TransferIdentity, snapshot_manifest, encode_manifest
    from shared.lib.fanout.container_transfer_transport import admitted_request_digest, _InputClaimJournal, CandidateExportReceipt
    from shared.lib.fanout.controller import write_evidence, read_evidence
    from shared.lib.fanout.providers import ProviderRequest, _make_result
    from shared.lib.fanout.container_source import release_source_binding
    from shared.lib.fanout.container_vm import VMProbe
    from scripts import fanout_container_certify as probe
    from test_fanout_container_boundary import _terminal, SESSION_IDS
    canonical = runtime.canonical_json
    sha = lambda data: hashlib.sha256(data).hexdigest()
    service, coordinator = opened.service, opened.service.coordinator
    packet = service._packet_for(task)
    preparation = service.preparations[task.id]
    controller = service.lifecycle_controller
    private = helper.transcript_store(coordinator)
    ledger = PermitLedger(controller.root / "locks/spend", controller)
    _, routes = probe._admit_route_set(Path(probe_receipt["manifest_path"]), Path(probe_receipt["policy_path"]),
        {provider: item["route_id"] for provider, item in probe_receipt["machine_routes"].items()})
    observations = {}
    with runtime.ArtifactStore.open_existing(Path(probe_receipt["private_run_root"]) / "raw") as proof_raw:
        proof_controller = probe._quality_controller(Path(probe_receipt["private_run_root"]))
        for provider, cells in probe_receipt["providers"].items():
            initial = cells["initial"]
            attestation = read_evidence(proof_controller, "container-private-cell-attestation",
                initial["attestation_evidence_name"], initial["attestation_evidence_digest"])
            observations[provider] = proof_raw.read_json(runtime.ArtifactRef(**attestation["vm"]))
    imports, barriers, source_bundles = {}, [], []
    fault = None
    for number in (1, 2):
        peers = coordinator.build_peer_packets(packet, barriers[0]) if number == 2 else ()
        terminals, receipts, candidates = [], [], []
        for seat in preparation.seats:
            provider = seat.executor_id
            peer = next((item for item in peers if item.target_seat_id == seat.seat_id), None)
            profile = coordinator.registry.select(provider, "repo-write", "standard")
            request = ProviderRequest(provider, coordinator._prompt(packet, seat, number, peer),
                cwd=packet.cwd, execution_class="repo-write",
                session_id=SESSION_IDS[provider] if number == 2 or provider == "claude" else None, resume=number == 2,
                profile=profile, timeout=profile.default_timeout if packet.provider_policy.timeout is None else packet.provider_policy.timeout,
                artifact_store=private, artifact_prefix=coordinator._provider_prefix(packet, number, seat.seat_id),
                slot_root=coordinator.slot_root, context_sha256=packet.context_sha256,
                skill_bundle_sha256=packet.skill_bundle_sha256, staged_skill_root=seat.staged_root,
                skill_delivery_sha256=seat.delivery_evidence_sha256, run_id=packet.run_id, task_id=packet.task_id,
                target_id=task.target_id, seat_id=seat.seat_id, inputs_digest=service.inputs.digest)
            opened.journal.append("dispatch-intent", task_id=task.id, seat_id=seat.seat_id, attempt=1, round=number, owner=opened.owner)
            opened.journal.append("process-started", task_id=task.id, seat_id=seat.seat_id, attempt=1, round=number, owner=opened.owner)
            if partial and number == 2 and provider == "agy":
                opened.journal.recover_uncertain(opened.owner)
                terminals.append(coordinator._load_or_create_uncertain(packet, seat, number, request.session_id, "provider-boundary"))
                continue
            context = helper.bind_turn_context(coordinator, packet, seat, number, request, peer)
            workspace, volume = private_volume_names(packet.run_id, task.id, task.target_id, seat.seat_id)
            spec = ContainerSpec(packet.run_id, task.id, task.target_id, seat.seat_id,
                probe_receipt["image_digest"], workspace, volume, f"/run/fanout/{seat.seat_id}.sock", probe_receipt["cli_digests"][provider])
            handle = ContainerHandle(sha(f"{task.id}/{provider}/{number}".encode()), "d" * 64, workspace, volume)
            launch = write_evidence(controller, "container-seat-launch", {"spec": asdict(spec), "handle": asdict(handle),
                "route": "start" if number == 1 else "recover"})
            permit = BrokerPermit(provider, packet.run_id, task.id, task.target_id, seat.seat_id,
                "initial" if number == 1 else "resume", profile.requested_model, "disposable-placeholder", 4, 1048576, 8388608, 1024,
                session_id=request.session_id, inputs_digest=service.inputs.digest,
                upstream_route_digest=routes[provider].digest, upstream_origin=routes[provider].profile.origin,
                upstream_model=routes[provider].profile.effective_model, upstream_path=probe_receipt["machine_routes"][provider]["path"])
            permit_id = ledger.register(permit, number)
            identity = TransferIdentity(packet.run_id, task.id, task.target_id, seat.seat_id, service.inputs.digest)
            if number == 1:
                source, material_name, material_digest = helper.stage_input_material(coordinator, packet, seat, request, handle)
                source_info = source.stat()
                grant = write_evidence(controller, "container-input-grant", {"identity": asdict(identity),
                    "original_handle": asdict(handle), "material_evidence_name": material_name, "material_evidence_digest": material_digest,
                    "source_root": str(source), "source_device": source_info.st_dev, "source_inode": source_info.st_ino,
                    "request_digest": admitted_request_digest(request)})
                imported = write_evidence(controller, "container-input-transfer", {"identity": asdict(identity),
                    "manifest_sha256": sha(encode_manifest(snapshot_manifest(source, identity))), "workspace_volume": workspace,
                    "workdir": f"/workspace/inputs/{sha(canonical(asdict(identity)))}/published/repo", "original_handle": asdict(handle),
                    "handle_evidence_name": launch[0], "handle_evidence_digest": launch[1],
                    "material_evidence_name": material_name, "material_evidence_digest": material_digest,
                    "grant_evidence_name": grant[0], "grant_evidence_digest": grant[1]})
                claim = _InputClaimJournal(controller, identity, workspace)
                claimed = SimpleNamespace(evidence_name=grant[0], evidence_digest=grant[1])
                claim.claim(claimed)
                claim.complete(claimed, *imported)
                imports[provider] = imported
            imported = imports[provider]
            vm = VMProbe(**observations[provider]["probe"])
            input_payload = read_evidence(controller, "container-input-transfer", *imported)
            boundary_value = {"schema_version": "fanout-container-boundary-v4", "controller_id": controller.controller_id,
                "run_id": packet.run_id, "task_id": task.id, "target_id": task.target_id, "seat_id": seat.seat_id,
                "provider": provider, "inputs_digest": service.inputs.digest, "profile_digest": profile.digest,
                "vm_probe_digest": vm.digest, "vm_docker_endpoint": vm.docker_endpoint,
                "vm_ssh_host_key_sha256": vm.ssh_host_key_sha256,
                "vm_effective_mounts_sha256": vm.effective_mounts_sha256, "vm_guest_mounts_sha256": vm.guest_mounts_sha256,
                "image_digest": spec.image_digest, "cli_digest": spec.cli_digest,
                "container_id": handle.container_id, "container_inspect_sha256": handle.inspect_sha256,
                "session_volume": volume, "socket_path": spec.socket_path, "socket_uid": 65532,
                "socket_mode": 0o140600, "socket_device": 1, "socket_inode": number, "socket_ctime_ns": number,
                "session_id": request.session_id, "admission_identity": number,
                "handle_evidence_name": launch[0], "handle_evidence_digest": launch[1],
                "input_manifest_sha256": input_payload["manifest_sha256"], "input_workdir": input_payload["workdir"],
                "request_digest": admitted_request_digest(request), "route": permit.route,
                "upstream_route_digest": permit.upstream_route_digest, "permit_id": permit_id, "permit_epoch": number,
                "permit_digest": sha(canonical(asdict(permit))), "workspace_volume": workspace,
                "input_receipt_name": imported[0], "input_receipt_digest": imported[1]}
            boundary = write_evidence(controller, "container-seat-admission", boundary_value)
            turn = _turn_digest(request, SimpleNamespace(**boundary_value))
            session = SessionStore(controller, packet.run_id, task.id, task.target_id, seat.seat_id, volume)
            session.reserve(provider, turn, initial=number == 1, upstream_route_digest=permit.upstream_route_digest,
                initial_session_id=request.session_id if provider == "claude" and number == 1 else None)
            parent = controller.root / "quarantine" / f"synthetic-export-{task.id}-{provider}-{number}"
            parent.mkdir(mode=0o700)
            exported = parent / "published"
            exported.mkdir(mode=0o700)
            (exported / "README.md").write_bytes(f"synthetic exported {task.id} {provider} {number}\n".encode())
            manifest = snapshot_manifest(exported, identity)
            export_payload = {"identity": asdict(identity), "turn_digest": turn, "manifest_sha256": sha(canonical(asdict(manifest))),
                "root": str(exported), "container_id": handle.container_id, "container_inspect_sha256": handle.inspect_sha256,
                "workspace_volume": workspace, "session_volume": volume}
            export_ref = write_evidence(controller, "container-candidate-export", export_payload)
            export = CandidateExportReceipt(identity, turn, export_payload["manifest_sha256"], exported,
                handle.container_id, handle.inspect_sha256, workspace, volume, *export_ref)
            inspect = [{"Id": handle.container_id, "Config": {"Labels": ContainerLauncher._labels(spec), "Image": spec.image_digest},
                "State": {"Running": False}, "HostConfig": {"RestartPolicy": {"Name": "no"}}, "Mounts": [
                {"Destination": "/workspace", "Type": "volume", "Name": workspace},
                {"Destination": "/session", "Type": "volume", "Name": volume},
                {"Destination": "/run/fanout/broker.sock", "Type": "bind", "Source": spec.socket_path}]}]
            with runtime.ArtifactStore(controller.root / "quarantine/runtime-proofs") as stops:
                stopped_raw = stops.write_json(f"stopped/{handle.container_id}.json", inspect)
            stopped = write_evidence(controller, "container-seat-stopped", {"spec": asdict(spec), "handle": asdict(handle),
                "launch_evidence_name": launch[0], "launch_evidence_digest": launch[1], "raw_inspect": asdict(stopped_raw)})
            process = _terminal(provider, session_id=SESSION_IDS[provider])
            result = _make_result(request, coordinator.registry.require(provider), process,
                coordinator.registry.require(provider).parse(process.stdout), 1, 0.1)
            assert result.valid
            normal = ({"model": profile.requested_model, "messages": [], "max_tokens": 32, "stream": True} if provider == "claude" else
                {"model": profile.requested_model, "input": [], "max_output_tokens": 32} if provider == "codex" else
                {"contents": [], "generationConfig": {"maxOutputTokens": 32}})
            body = canonical(normal)
            path = "/v1/messages" if provider == "claude" else "/v1/responses" if provider == "codex" else "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
            prepared = HostUpstream(permit, routes[provider], max_response_bytes=8388608, deadline_seconds=2).preview(
                "POST", path, {"content-type": "application/json"}, body)
            wire_identity = OutboundRequestIdentity("POST", path, sha(body), 32,
                "gemini-3.8-flash" if provider == "agy" else profile.requested_model,
                prepared.route_digest, prepared.origin, prepared.path, sha(prepared.body), prepared.effective_model)
            wire_id = f"{task.id}-{provider}-{number}"
            response = b'{"ok":true}'
            ledger.record_attempt(permit_id, number)
            ledger.reserve(permit_id, number, wire_id, wire_identity)
            ledger.claim_send(permit_id, number, wire_id, permit, wire_identity)
            ledger.settle(wire_id, sha(response))
            helper.fence_stopped_turn(controller, ledger, SimpleNamespace(spec=spec, handle=handle, stop_evidence=stopped), permit_id, number)
            def wire_ref(name, data):
                return asdict(private.write_bytes(f"{request.artifact_prefix}-wire/{wire_id}/{name}.bin", data))
            wire = {"request_id": wire_id, "http_status": 200, "content_type": "application/json",
                "normalized_body": wire_ref("normal", body), "effective_body": wire_ref("effective", prepared.body),
                "response_body": wire_ref("response", response)}
            wire["delivered_body"] = wire["response_body"]
            from shared.lib.fanout.container_credentials import HostCredentialSource
            credential_source = HostCredentialSource(routes[provider].profile)
            credential_source.load()
            completed = write_evidence(controller, "container-coordinator-turn", {
                "schema_version": "fanout-container-coordinator-turn-v1", "request_digest": admitted_request_digest(request),
                "task_packet_sha256": sha(canonical(packet.to_dict())), "release_source_binding": release_source_binding().to_dict(),
                "context": list(context), "boundary": list(boundary), "export": list(export_ref), "stop": list(stopped), "turn_digest": turn,
                "process": {"status": "exit", "returncode": 0, "capture_complete": True,
                    "stdout": asdict(result.stdout_ref), "stderr": asdict(result.stderr_ref), "answer": asdict(result.answer_ref)},
                "permit": asdict(permit), "permit_id": permit_id, "permit_epoch": number,
                "ledger": probe._ledger_snapshot(controller, permit_id, number), "wire": [wire],
                "credential_source_fingerprint": credential_source.fingerprint,
                "vm_profile_identity": observations[provider]["profile_identity"],
                "session_id": result.session_id, "valid": True})
            result = replace(result, container_completion=completed, candidate_export=export)
            terminal = coordinator._persist_terminal(packet, coordinator._terminal_from_provider(packet, seat, number, request, result))
            opened.journal.append("provider-terminal", task_id=task.id, seat_id=seat.seat_id, attempt=1, round=number,
                evidence_sha256=terminal.terminal_ref.digest, owner=opened.owner)
            opened.journal.append("artifacts-durable", task_id=task.id, seat_id=seat.seat_id, attempt=1, round=number, owner=opened.owner)
            terminals.append(terminal)
            checkpoint = coordinator._checkpoint(packet, terminal)
            memory = coordinator.memory
            checkpoint_ref, intent_ref = memory._persist_intent(checkpoint)
            publication = memory._persist_publication(checkpoint, len(ledger.request_records(permit_id, number)) + number,
                checkpoint_ref, intent_ref)
            receipt = runtime.CheckpointReceipt(publication, "synthetic-memory", "e" * 64)
            receipt = replace(receipt, verification_ref=memory._write_exact(f"{memory._prefix(checkpoint)}/verified.json", canonical(receipt.to_dict())))
            opened.journal.append("publication-intent", task_id=task.id, seat_id=seat.seat_id, attempt=1, round=number, owner=opened.owner)
            opened.journal.append("checkpoint-published", task_id=task.id, seat_id=seat.seat_id, attempt=1, round=number,
                evidence_sha256=publication.publication_ref.digest, owner=opened.owner)
            opened.journal.append("checkpoint-verified", task_id=task.id, seat_id=seat.seat_id, attempt=1, round=number,
                evidence_sha256=receipt.verification_ref.digest, owner=opened.owner)
            receipts.append(receipt)
            session.settle(provider, result.session_id, turn, initial=number == 1, upstream_route_digest=permit.upstream_route_digest)
            if number == 2:
                ref = coordinator._persist_terminal_candidate(packet, seat, number, request=request, result=result, completion_evidence=completed)
                candidates.append((seat.seat_id, ref))
                source_bundles.append(runtime.CandidateBundle.from_manifest(opened.artifacts.read_bytes(ref)))
        barrier = coordinator._persist_barrier(runtime.BarrierResult(packet.run_id, task.id, 1, number, "round-complete", 2,
            tuple(seat.seat_id for seat in preparation.seats), tuple(terminals), tuple(receipts),
            tuple(receipt.publication for receipt in receipts), tuple(peers), candidate_sources=tuple(sorted(candidates))))
        state = service._execution_state(task.id)
        service._commit_task_state(replace(state, barriers=(*state.barriers, barrier.barrier_ref)), opened.owner)
        barriers.append(barrier)
        if partial and number == 1:
            failed = next(seat for seat in preparation.seats if seat.executor_id == "agy")
            _, volume = private_volume_names(packet.run_id, task.id, task.target_id, failed.seat_id)
            session = SessionStore(controller, packet.run_id, task.id, task.target_id, failed.seat_id, volume)
            original = session._record.read_bytes()
            archived = raw.write_json("fault/session-ready.json", {"bytes": original.decode()})
            destination = controller.root / "quarantine/session-read-fault"
            destination.mkdir(mode=0o700)
            moved = destination / session._record.name
            session._record.rename(moved)
            fault = write_evidence(trial_controller, "container-trial-session-read-fault", {
                "run_id": packet.run_id, "task_id": task.id, "target_id": task.target_id, "seat_id": failed.seat_id,
                "provider": "agy", "session_id": SESSION_IDS["agy"], "round": 2, "initial_barrier": asdict(barrier.barrier_ref),
                "session_record": asdict(archived), "original_record": str(session._record), "moved_record": str(moved)})
    return source_bundles, fault
