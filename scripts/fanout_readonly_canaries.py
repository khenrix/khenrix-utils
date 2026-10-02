"""Reopen complete private read-only proofs before publishing a minimal summary."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Mapping

if __package__ in (None, ""):
    bootstrap_path = Path(__file__).resolve().parents[1] / "shared/skills/llm-fanout-execute/scripts/execute.py"
    bootstrap_spec = importlib.util.spec_from_file_location("_fanout_checkout_bootstrap", bootstrap_path)
    bootstrap = importlib.util.module_from_spec(bootstrap_spec)
    sys.modules[bootstrap_spec.name] = bootstrap
    bootstrap_spec.loader.exec_module(bootstrap)
    bootstrap._runtime()

from scripts import fanout_smoke as smoke
from scripts.fanout_container_certify import _write_private
from shared.lib.fanout.container_routes import _read_owned_json
from shared.lib.fanout.container_source import release_source_binding
from shared.lib.fanout.controller import read_evidence

SUMMARY_PATH = smoke.ROOT / "evals/llm-fanout/read-only-profile-canaries.json"


class CanaryReceiptError(ValueError):
    """A complete current-source read-only canary cannot be proved."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_readonly_canary(root: Path) -> dict:
    try:
        value, raw = _read_owned_json(root / "receipt.json")
        if smoke.fanout.canonical_json(value) != raw:
            raise ValueError("noncanonical receipt")
        return value
    except (OSError, ValueError) as error:
        raise CanaryReceiptError("private canary receipt is unavailable or unsafe") from error


def _reopen(receipt: Mapping, tier: str) -> dict:
    runtime = smoke.fanout
    try:
        root = smoke._live_root(receipt.get("private_run_root"), new=False)
        value, raw = _read_owned_json(root / "receipt.json")
        if value != dict(receipt) or runtime.canonical_json(value) != raw:
            raise ValueError("receipt bytes changed")
        unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
        if value.get("receipt_sha256") != _sha(runtime.canonical_json(unsigned)):
            raise ValueError("unsigned receipt digest changed")
        for name, expected in {
            "schema_version": "fanout-live-smoke-receipt-v1", "status": "pass",
            "quality_tier": tier, "provider_turns": 6, "peer_transfers": 6,
            "peer_facts_used": 6, "source_count": 3, "replayed_provider_turns": 0,
            "caller_unchanged": True, "scope": "transport-and-peer-use",
            "source_sha256": smoke._live_source_hash(),
            "release_binding": release_source_binding().to_dict(),
        }.items():
            if value.get(name) != expected or type(value.get(name)) is not type(expected):
                raise ValueError("receipt is incomplete or stale")
        state, state_raw = _read_owned_json(root / "state.json")
        if (runtime.canonical_json(state) != state_raw
                or state["quality_tier"] != tier or state["run_id"] != value["run_id"]
                or state["release_binding"] != value["release_binding"]
                or state["source_sha256"] != value["source_sha256"]
                or value["run_id"] != smoke._live_run_id(root)):
            raise ValueError("state association changed")
        controller = runtime.resume_lifecycle_controller(root / "controller",
            runtime.LifecycleCapability(state["controller_token"]))
        complete = read_evidence(controller, "readonly-canary-complete",
            value["completion_evidence_name"], value["completion_evidence_digest"])
        sealed = {key: item for key, item in unsigned.items()
                  if key not in {"completion_evidence_name", "completion_evidence_digest"}}
        if (complete["receipt_sha256"] != _sha(runtime.canonical_json(sealed))
                or complete["state_sha256"] != _sha(state_raw)
                or complete["sequence_before_replay"] != complete["sequence_after_replay"]):
            raise ValueError("completion or settled replay association changed")
        inputs = runtime.RunInputs.from_dict(state["inputs"])
        if (inputs.digest != value["run_inputs_sha256"]
                or inputs.source_sha256 != value["source_sha256"]
                or inputs.repo_baseline_sha256 != state["baseline_digest"]
                or inputs.skill_manifests.get("release-source") != value["release_binding"]["digest"]):
            raise ValueError("authenticated current source changed")
        if (state["memory_controller_sha256"] != value["memory_controller_sha256"]
                or state["memory_controller_sha256"] != _sha(
                    (smoke.ROOT / "components/memory/memory_exchange.py").read_bytes())):
            raise ValueError("memory controller differs from current owned source inventory")
        owner = runtime.OwnerCapability.from_token(state["owner_token"])
        authority = runtime.LocalAnchorAuthority(root / "authority",
            run_root=root / "journal", repo_root=root / "repo")
        with runtime.RunJournal.resume(root / "journal", inputs, owner, anchor_store=authority) as journal:
            with runtime.ArtifactStore.open_existing(root / "artifacts") as store:
                catalog = runtime.ProviderRegistry.default()
                registry = catalog
                if "provider_profile_descriptors" in state:
                    recorded = state["provider_profile_descriptors"]
                    if not isinstance(recorded, list):
                        raise ValueError("recorded provider descriptors are invalid")
                    profiles = []
                    for descriptor in recorded:
                        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("executor_id"), str):
                            raise ValueError("recorded provider descriptor is invalid")
                        profiles.append(runtime.ExecutorProfile.from_dict(descriptor,
                            adapter=catalog.require(descriptor["executor_id"])))
                    registry = runtime.ProviderRegistry(profiles, version_probe=catalog._version_probe)
                    for profile in profiles:
                        escrow = store.read_by_digest(
                            f"amendments/executor-profiles/{profile.digest}.json", profile.digest)
                        if escrow != runtime.canonical_json(profile.to_dict()):
                            raise ValueError("recorded provider descriptor escrow changed")
                if dict(registry.profile_digests) != dict(journal.inputs.provider_profiles):
                    raise ValueError("recorded provider profiles changed authenticated binding")
                events_ref = runtime.ArtifactRef(**complete["events_ref"])
                events_raw = store.read_bytes(events_ref)
                if (events_ref.digest != journal._loaded_journal_sha256
                        or complete["sequence_after_replay"] != journal.state.seq
                        or complete["head_checksum"] != journal.state.head_checksum
                        or journal.state.accepted_retries or journal.state.uncertainty_resolutions):
                    raise ValueError("settled journal changed or retried")
                events = [json.loads(line) for line in events_raw.splitlines()]
                identities = {(smoke.TASK_ID, f"seat-{provider}", 1, round_number)
                              for provider in smoke.EXECUTORS for round_number in (1, 2)}
                for kind in ("dispatch-intent", "process-started", "provider-terminal"):
                    rows = [row["payload"] for row in events if row["type"] == kind]
                    if len(rows) != 6 or {(row["task_id"], row["seat_id"], row["attempt"], row["round"])
                                          for row in rows} != identities:
                        raise ValueError("six exact settled turns were not retained")
                packet = smoke._packet(root / "repo", smoke._plan(live=True, quality_tier=tier),
                                       run_id=state["run_id"])
                if (inputs.compiled_plan_sha256 != packet.compiled_plan_sha256
                        or inputs.skill_manifests.get(packet.task_id) != packet.skill_manifest_sha256):
                    raise ValueError("current plan or skill differs from authenticated inputs")
                memory = smoke._live_memory(store, Path(state["memory_controller_path"]),
                                            state["memory_controller_sha256"])
                baseline = runtime.capture_repository_baseline(root / "repo")
                if baseline.digest != state["baseline_digest"]:
                    raise ValueError("canary synthetic baseline changed")
                coordinator = runtime.CollaborationCoordinator(artifacts=store, journal=journal,
                    owner=owner, memory=memory, registry=registry, lifecycle_controller=controller,
                    repository_baseline=baseline, slot_root=root / "slots")
                canary_seats = None
                refs = value["barrier_refs"]
                if set(refs) != {"first", "second"} or refs["first"] != state["barrier_ref"]:
                    raise ValueError("barrier references changed")
                barriers = [coordinator.restore_barrier(runtime.ArtifactRef(**refs[name]), packet=packet)
                            for name in ("first", "second")]
                providers = {}
                first_by_seat = {}
                for round_number, barrier in enumerate(barriers, 1):
                    if (barrier.round != round_number or barrier.status != "round-complete"
                            or len(barrier.terminals) != 3 or len(barrier.valid_terminals) != 3
                            or {item.executor_id for item in barrier.terminals} != set(smoke.EXECUTORS)
                            or barrier.barrier_ref.digest != value[
                                "first_barrier_sha256" if round_number == 1 else "second_barrier_sha256"]):
                        raise ValueError("complete exact barriers were not retained")
                    for terminal in barrier.terminals:
                        profile = registry.select(terminal.executor_id, "read-only", tier)
                        stdout = store.read_bytes(terminal.stdout_ref)
                        store.read_bytes(terminal.stderr_ref)
                        answer = store.read_bytes(terminal.answer_ref)
                        parsed = profile.adapter.parse(stdout)
                        observed_model = parsed.observed_model
                        body_proof = None
                        if terminal.executor_id == "codex" and observed_model is None:
                            from shared.lib.fanout.canary_proof import reopen_turn
                            if canary_seats is None:
                                canary_seats, _ = smoke._seats(root, baseline, controller, store,
                                    restore=True, workspace_evidence=state["workspace_evidence"])
                            seat = next(item for item in canary_seats if item.seat_id == terminal.seat_id)
                            prior = first_by_seat.get(terminal.seat_id)
                            peer = None if prior is None else coordinator.build_peer_packet(
                                packet, barriers[0], target_seat_id=terminal.seat_id)
                            body_proof = reopen_turn(coordinator, packet, seat, round_number,
                                terminal, prior=prior, peer=peer)
                            observed_model = body_proof["response_model"]
                        if terminal.executor_id == "codex" and sum(
                            json.loads(line).get("type") == "turn.completed" for line in stdout.splitlines()
                        ) != 1:
                            raise ValueError("Codex terminal model observation is ambiguous")
                        if (not parsed.final or parsed.error is not None
                                or parsed.session_id != terminal.session_id or not terminal.session_id
                                or observed_model != profile.requested_model
                                or terminal.observed_model != parsed.observed_model
                                or terminal.requested_model != profile.requested_model
                                or parsed.answer.encode("utf-8") != answer
                                or terminal.answer_sha256 != _sha(answer)
                                or journal.state.evidence_digest("provider-terminal", packet.task_id,
                                    terminal.seat_id, 1, round_number) != terminal.terminal_ref.digest):
                            raise ValueError("positive model/session/answer terminal proof is missing")
                        checkpoint = coordinator._checkpoint_from_barrier(barrier, terminal)
                        checkpoint_receipt = coordinator._receipt_for(barrier, terminal)
                        memory._verify_receipt_artifacts(checkpoint, checkpoint_receipt)
                        if (journal.state.evidence_digest("checkpoint-verified", packet.task_id,
                                terminal.seat_id, 1, round_number) != checkpoint_receipt.verification_ref.digest
                                or journal.state.evidence_digest("checkpoint-published", packet.task_id,
                                    terminal.seat_id, 1, round_number) != checkpoint_receipt.publication.publication_ref.digest):
                            raise ValueError("memory publication/verification changed authenticated association")
                        if round_number == 1:
                            first_by_seat[terminal.seat_id] = terminal
                        elif first_by_seat[terminal.seat_id].session_id != terminal.session_id:
                            raise ValueError("cold resume changed session")
                        providers[terminal.executor_id] = {"profile_sha256": profile.digest,
                            "requested_model": profile.requested_model, "observed_model": observed_model,
                            **({"native_observed_model": None, "model_evidence": "returned-response-body"}
                               if body_proof is not None else {})}
                first, second = barriers
                if len(second.peer_packets) != 3 or first.peer_packets:
                    raise ValueError("peer packet set is incomplete")
                for peer in second.peer_packets:
                    if peer.artifact_ref.path != coordinator._peer_path(packet, 1, peer.target_seat_id):
                        raise ValueError("peer artifact path changed")
                    expected = coordinator.build_peer_packet(packet, first, target_seat_id=peer.target_seat_id)
                    if expected != peer or value["peer_packet_sha256"].get(peer.target_seat_id) != peer.packet_sha256:
                        raise ValueError("peer packet associations changed")
                facts = {item.executor_id: smoke._live_answer(store, item.answer_ref,
                    {"executor_id", "source_line", "fact"})["fact"] for item in first.terminals}
                second_answers = smoke._live_second_answers(store, second.terminals, facts)
                answer = runtime.canonical_json({
                    "schema_version": "fanout-live-smoke-synthesis-v1",
                    "source_line": "dirty caller input",
                    "peer_fact_acknowledgments": {
                        executor: second_answers[executor]["peer_facts"]
                        for executor in sorted(second_answers)},
                    "seat_syntheses": {
                        executor: second_answers[executor]["synthesis"]
                        for executor in sorted(second_answers)},
                })
                synthesis = runtime.load_answer_synthesis(controller, store, run_id=packet.run_id,
                    task_id=packet.task_id, answer_ref=runtime.ArtifactRef(**value["synthesis_ref"]))
                if (not synthesis.valid or synthesis.run_id != packet.run_id
                        or synthesis.task_id != packet.task_id
                        or synthesis.plan_sha256 != packet.compiled_plan_sha256
                        or synthesis.plan_revision != 1
                        or synthesis.baseline_digest != inputs.repo_baseline_sha256
                        or synthesis.synthesizer_id != "smoke-orchestrator"
                        or synthesis.answer_ref.digest != value["synthesis_sha256"]
                        or store.read_bytes(synthesis.answer_ref) != answer
                        or dict(synthesis.source_answers) != {item.seat_id: item.answer_ref for item in second.terminals}):
                    raise ValueError("final source synthesis changed")
        return {"status": "pass", "raw_receipt_sha256": _sha(raw),
                "receipt_sha256": value["receipt_sha256"], "providers": providers}
    except Exception as error:
        raise CanaryReceiptError(f"{tier} canary proof is incomplete, stale or unsafe") from error


def minimal_hash_summary(standard: Mapping, deep: Mapping, source: str) -> dict:
    binding = release_source_binding().to_dict()
    if source != smoke._live_source_hash():
        raise CanaryReceiptError("canary legacy source association is stale")
    tiers = {"standard": _reopen(standard, "standard"), "deep": _reopen(deep, "deep")}
    if standard["private_run_root"] == deep["private_run_root"]:
        raise CanaryReceiptError("tiers need distinct private run roots")
    if (standard["release_binding"] != binding or deep["release_binding"] != binding
            or release_source_binding().to_dict() != binding or smoke._live_source_hash() != source):
        raise CanaryReceiptError("source changed across the two canary validations")
    return {"schema_version": "fanout-readonly-canaries-v1", "status": "pass",
            "source_sha256": source, "release_binding": binding, "tiers": tiers}


def summarize_readonly_canaries(standard: Mapping, deep: Mapping) -> dict:
    return minimal_hash_summary(standard, deep, smoke._live_source_hash())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-run-root", type=Path, required=True)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--maximum-provider-turns", type=int, required=True, choices=(12,))
    parser.add_argument("--machine-routes", type=Path, required=True)
    parser.add_argument("--machine-policy", type=Path, required=True)
    parser.add_argument("--codex-route", required=True)
    options = parser.parse_args(argv)
    try:
        if not options.allow_paid:
            raise CanaryReceiptError("two-tier provider spend needs explicit --allow-paid")
        root = smoke._live_root(options.private_run_root, new=True)
        root.mkdir(mode=0o700)
        _write_private(root / "attempt.json", smoke.fanout.canonical_json({
            "maximum_provider_turns": 12, "release_binding": release_source_binding().to_dict(),
            "tiers": ["standard", "deep"], "retry": False}))
        receipts = [smoke.run_live(root / tier, authorized=True, quality_tier=tier,
                    machine_routes=options.machine_routes, machine_policy=options.machine_policy,
                    codex_route=options.codex_route)
                    for tier in ("standard", "deep")]
        summary = summarize_readonly_canaries(*receipts)
        destination = SUMMARY_PATH
        temporary = destination.with_name(destination.name + ".pending")
        _write_private(temporary, smoke.fanout.canonical_json(summary))
        os.replace(temporary, destination)
        print(smoke.fanout.canonical_json(summary).decode(), end="")
        return 0
    except (CanaryReceiptError, smoke.SmokeError, smoke.fanout.FanoutError, OSError):
        print("read-only canaries refused; private attempts are retained and never retried", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
