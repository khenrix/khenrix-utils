"""Crash-safe fanout run journal contracts."""
from __future__ import annotations

import importlib.util
import dataclasses
import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_runstate_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


RunInputs = fanout.RunInputs
RawRunJournal = fanout.RunJournal
RunLimits = fanout.RunLimits
RunStateError = fanout.RunStateError
RunLockError = fanout.RunLockError
RunAuthorizationError = fanout.RunAuthorizationError
AnchorRevision = fanout.AnchorRevision
RemoteAnchorAuthority = fanout.RemoteAnchorAuthority


class MemoryAnchorStore:
    """Deterministic external authority used only by run-state contract tests."""

    def __init__(self, identity: str = "test-anchor-service") -> None:
        self.identity = identity
        self.records: dict[str, AnchorRevision] = {}
        self.fail_operation: str | None = None

    def create(self, key: str, value: bytes) -> AnchorRevision:
        self._check("create")
        if key in self.records:
            raise RuntimeError("exists")
        result = AnchorRevision(1, value)
        self.records[key] = result
        return result

    def read(self, key: str) -> AnchorRevision:
        self._check("read")
        return self.records[key]

    def compare_and_set(self, key: str, expected_revision: int, value: bytes) -> AnchorRevision:
        self._check("compare_and_set")
        current = self.records[key]
        if current.revision != expected_revision:
            raise RuntimeError("stale")
        result = AnchorRevision(expected_revision + 1, value)
        self.records[key] = result
        return result

    def _check(self, operation: str) -> None:
        if self.fail_operation == operation:
            raise RuntimeError(f"injected {operation} failure")


class FaultingAnchorStore(MemoryAnchorStore):
    def __init__(self, identity: str = "faulting-anchor-service") -> None:
        super().__init__(identity)
        self.cas_calls = 0
        self.fail_cas_call: int | None = None
        self.mutate_then_fail_cas_call: int | None = None

    def compare_and_set(self, key: str, expected_revision: int, value: bytes) -> AnchorRevision:
        self.cas_calls += 1
        if self.fail_cas_call == self.cas_calls:
            raise RuntimeError("injected stale CAS")
        result = super().compare_and_set(key, expected_revision, value)
        if self.mutate_then_fail_cas_call == self.cas_calls:
            raise RuntimeError("injected ambiguous CAS response")
        return result


class StaleAnchorStore(MemoryAnchorStore):
    def compare_and_set(self, key: str, expected_revision: int, value: bytes) -> AnchorRevision:
        return self.records[key]


_AUTHORITIES: dict[Path, object] = {}


class RunJournal:
    """Test harness that supplies a process-external authority by default."""

    @staticmethod
    def create(root: Path | str, inputs: RunInputs, **kwargs):
        path = Path(root).absolute()
        backend = kwargs.pop("anchor_store", None) or MemoryAnchorStore(f"test/{hash(path)}")
        authority = sys.modules[f"{SPEC.name}.runstate"]._test_anchor_authority(backend)
        _AUTHORITIES[path] = authority
        return RawRunJournal._create_for_test(path, inputs, anchor_store=authority, **kwargs)

    @staticmethod
    def resume(root: Path | str, inputs: RunInputs, capability, **kwargs):
        path = Path(root).absolute()
        backend = kwargs.pop("anchor_store", None)
        authority = (_AUTHORITIES[path] if backend is None
                     else sys.modules[f"{SPEC.name}.runstate"]._test_anchor_authority(backend))
        return RawRunJournal._resume_for_test(path, inputs, capability, anchor_store=authority, **kwargs)

    @staticmethod
    def inspect(root: Path | str, inputs: RunInputs, capability, **kwargs):
        path = Path(root).absolute()
        backend = kwargs.pop("anchor_store", None)
        authority = (_AUTHORITIES[path] if backend is None
                     else sys.modules[f"{SPEC.name}.runstate"]._test_anchor_authority(backend))
        return RawRunJournal._inspect_for_test(path, inputs, capability, anchor_store=authority, **kwargs)


def _inputs() -> RunInputs:
    return RunInputs(
        run_id="run-1",
        compiled_plan_sha256="a" * 64,
        source_sha256="b" * 64,
        draft_sha256="c" * 64,
        compiler_sha256="c" * 64,
        parser_sha256="d" * 64,
        provider_profiles={"claude": "e" * 64, "codex": "f" * 64},
        skill_manifests={"work": "1" * 64},
        repo_baseline_sha256="e" * 64,
    )


def _branch_inputs(tmp_path: Path) -> RunInputs:
    targets = {}
    for target_id in ("address", "booking"):
        spec = fanout.TargetSpec(
            target_id, f"github.com/example/{target_id}", "TASK-123",
            f"refs/heads/feat/TASK-123-{target_id}",
        )
        targets[target_id] = fanout.TargetBinding(
            spec=spec, root=tmp_path / target_id,
            common_dir=tmp_path / target_id / ".git", common_device=1,
            common_inode=2 if target_id == "address" else 3,
            base_oid="a" * 40, branch_oid=None, baseline_sha256="e" * 64,
            head_ref="refs/heads/main",
        )
    return dataclasses.replace(
        _inputs(), repo_baseline_sha256=None, profile_shape="class-tier",
        provider_profiles={"claude/read-only/standard": "e" * 64}, targets=targets,
    )


def _branch_intent(inputs: RunInputs, candidate, verification):
    binding = inputs.targets["address"]
    return fanout.BranchHandoverIntentV2(
        run_id=inputs.run_id, plan_revision=1,
        plan_sha256=inputs.compiled_plan_sha256, inputs_digest=inputs.digest,
        task_id="work", target_id="address", repository=binding.spec.repository,
        branch_ref=binding.spec.branch_ref, base_oid=binding.base_oid,
        old_ref_oid="0" * 40, candidate_ref=candidate,
        verification_ref=verification, expected_head_oid=binding.base_oid,
        expected_index_sha256="4" * 64, expected_tree_oid="5" * 40,
    )


def _candidate_wrapper(store, inputs):
    binding = inputs.targets["address"]
    manifest = fanout.CandidateBundle(binding.baseline_sha256, (), ())
    manifest_ref = store.write_bytes("candidate.manifest.json", manifest.manifest_bytes)
    wrapper_ref = store.write_json("candidate.wrapper.json", {
        "schema_version": "fanout-target-candidate-v1",
        "target": {
            "run_id": inputs.run_id, "task_id": "work", "target_id": "address",
            "repository": binding.spec.repository, "branch_ref": binding.spec.branch_ref,
            "base_oid": binding.base_oid, "baseline_sha256": binding.baseline_sha256,
        },
        "candidate": dataclasses.asdict(manifest_ref),
        "controller_evidence": {"name": "candidate-evidence.json", "digest": "f" * 64},
    })
    return wrapper_ref, manifest_ref.digest


def _verification_wrapper(store, candidate):
    issued = store.read_json(candidate)
    manifest = fanout.ArtifactRef(**issued["candidate"])
    result = store.write_bytes(
        fanout.candidate_result_path("work", manifest.digest, "d" * 64),
        store.read_bytes(manifest),
    )
    return store.write_json("verification.wrapper.json", {
        "schema_version": "fanout-target-verification-v1",
        "target": issued["target"], "candidate": dataclasses.asdict(result),
        "candidate_evidence": dataclasses.asdict(candidate),
        "controller_evidence": {"name": "verification-evidence.json", "digest": "f" * 64},
    })


def _branch_terminal(intent, evidence, candidate_sha256, *, intent_sha256=None):
    fields = {
        "schema_version": "fanout-branch-handover-terminal-v2",
        "intent_sha256": intent.sha256 if intent_sha256 is None else intent_sha256,
        "commit_oid": "b" * 40,
        "evidence": dataclasses.asdict(evidence),
        "candidate_sha256": candidate_sha256,
    }
    return fanout.HandoverTerminalV2(
        fields["intent_sha256"], fields["commit_oid"], evidence,
        fields["candidate_sha256"], hashlib.sha256(fanout.canonical_json(fields)).hexdigest(),
    )


def test_branch_handover_is_target_bound_durable_and_independent_of_file_handover(tmp_path: Path):
    root = tmp_path / "run"
    inputs = _branch_inputs(tmp_path)
    journal, owner = RunJournal.create(root, inputs)
    with fanout.ArtifactStore(root / "artifacts") as store:
        candidate, candidate_sha256 = _candidate_wrapper(store, inputs)
        verification = _verification_wrapper(store, candidate)
        intent = _branch_intent(inputs, candidate, verification)
        evidence = store.write_bytes("branch-terminal.json", b"terminal evidence\n")
        terminal = _branch_terminal(intent, evidence, candidate_sha256)
        try:
            journal.append_branch_intent(intent, owner=owner)
            assert journal.branch_handover_state("work") == (intent, None)
            journal.append_branch_terminal("work", terminal, owner=owner)
            assert journal.state.task_phases == {}
            assert journal.state.handovers == {}
            journal.snapshot(owner)
        finally:
            journal.close()
        resumed = RunJournal.resume(root, inputs, owner)
        try:
            assert resumed.branch_handover_state("work") == (intent, terminal)
            assert resumed.state.handovers == {}
        finally:
            resumed.close()


def test_branch_handover_rejects_target_ref_swap_and_duplicate_terminal(tmp_path: Path):
    root = tmp_path / "run"
    inputs = _branch_inputs(tmp_path)
    journal, owner = RunJournal.create(root, inputs)
    with fanout.ArtifactStore(root / "artifacts") as store:
        candidate, candidate_sha256 = _candidate_wrapper(store, inputs)
        verification = _verification_wrapper(store, candidate)
        intent = _branch_intent(inputs, candidate, verification)
        evidence = store.write_bytes("branch-terminal.json", b"terminal evidence\n")
        terminal = _branch_terminal(intent, evidence, candidate_sha256)
        try:
            with pytest.raises(RunStateError, match="target|binding|ref"):
                journal.append_branch_intent(dataclasses.replace(intent, target_id="booking"), owner=owner)
            with pytest.raises(RunStateError, match="ref|binding"):
                journal.append_branch_intent(dataclasses.replace(intent, old_ref_oid="a" * 40), owner=owner)
            journal.append_branch_intent(intent, owner=owner)
            with pytest.raises(RunStateError, match="duplicate|intent"):
                journal.append_branch_intent(intent, owner=owner)
            with pytest.raises(RunStateError, match="intent"):
                journal.append_branch_terminal("work", _branch_terminal(intent, evidence, candidate_sha256, intent_sha256="f" * 64), owner=owner)
            journal.append_branch_terminal("work", terminal, owner=owner)
            with pytest.raises(RunStateError, match="duplicate|intent"):
                journal.append_branch_terminal("work", terminal, owner=owner)
        finally:
            journal.close()


def test_branch_terminal_requires_stored_evidence_before_append_and_on_recovery(tmp_path: Path):
    root = tmp_path / "run"
    inputs = _branch_inputs(tmp_path)
    journal, owner = RunJournal.create(root, inputs)
    with fanout.ArtifactStore(root / "artifacts") as store:
        candidate, candidate_sha256 = _candidate_wrapper(store, inputs)
        verification = _verification_wrapper(store, candidate)
        intent = _branch_intent(inputs, candidate, verification)
        journal.append_branch_intent(intent, owner=owner)
        missing = fanout.ArtifactRef("missing.json", "d" * 64, 10)
        with pytest.raises(RunStateError, match="evidence|artifact"):
            journal.append_branch_terminal("work", _branch_terminal(intent, missing, candidate_sha256), owner=owner)
        evidence = store.write_bytes("branch-terminal.json", b"terminal evidence\n")
        journal.append_branch_terminal("work", _branch_terminal(intent, evidence, candidate_sha256), owner=owner)
    journal.close()
    (root / "artifacts" / evidence.path).write_bytes(b"tampered evidence\n")
    with pytest.raises(RunStateError, match="evidence|artifact"):
        RunJournal.resume(root, inputs, owner)


def test_branch_terminal_candidate_digest_matches_issued_manifest(tmp_path: Path):
    root = tmp_path / "run"
    inputs = _branch_inputs(tmp_path)
    journal, owner = RunJournal.create(root, inputs)
    with fanout.ArtifactStore(root / "artifacts") as store:
        candidate, candidate_sha256 = _candidate_wrapper(store, inputs)
        verification = _verification_wrapper(store, candidate)
        intent = _branch_intent(inputs, candidate, verification)
        evidence = store.write_bytes("branch-terminal.json", b"terminal evidence\n")
        try:
            journal.append_branch_intent(intent, owner=owner)
            with pytest.raises(RunStateError, match="candidate"):
                journal.append_branch_terminal(
                    "work", _branch_terminal(intent, evidence, "e" * 64), owner=owner,
                )
            journal.append_branch_terminal(
                "work", _branch_terminal(intent, evidence, candidate_sha256), owner=owner,
            )
        finally:
            journal.close()


def test_branch_intent_rejects_unbound_verification_wrapper(tmp_path: Path):
    root = tmp_path / "run"
    inputs = _branch_inputs(tmp_path)
    journal, owner = RunJournal.create(root, inputs)
    with fanout.ArtifactStore(root / "artifacts") as store:
        candidate, _candidate_sha256 = _candidate_wrapper(store, inputs)
        unrelated = store.write_json("verification.json", {
            "schema_version": "fanout-target-verification-v1",
            "target": {"target_id": "booking"},
            "candidate": {"path": "other", "digest": "f" * 64, "size": 1},
            "candidate_evidence": dataclasses.asdict(candidate),
            "controller_evidence": {"name": "evidence.json", "digest": "f" * 64},
        })
        try:
            with pytest.raises(RunStateError, match="evidence|verification|candidate"):
                journal.append_branch_intent(
                    _branch_intent(inputs, candidate, unrelated), owner=owner,
                )
        finally:
            journal.close()


def test_v3_inputs_round_trip_distinct_targets_without_singular_baseline(tmp_path):
    """Two equal baseline digests still bind two different target identities."""
    targets = {}
    for target_id in ("address", "booking"):
        spec = fanout.TargetSpec(
            target_id, f"github.com/example/{target_id}", "TASK-123",
            f"refs/heads/feat/TASK-123-{target_id}",
        )
        targets[target_id] = fanout.TargetBinding(
            spec=spec, root=tmp_path / target_id,
            common_dir=tmp_path / target_id / ".git",
            common_device=1, common_inode=2 if target_id == "address" else 3,
            base_oid="a" * 40, branch_oid=None, baseline_sha256="e" * 64,
            head_ref="refs/heads/main",
        )
    original = _inputs()
    inputs = RunInputs(
        run_id=original.run_id,
        compiled_plan_sha256=original.compiled_plan_sha256,
        source_sha256=original.source_sha256,
        draft_sha256=original.draft_sha256,
        compiler_sha256=original.compiler_sha256,
        parser_sha256=original.parser_sha256,
        provider_profiles={"claude/read-only/standard": "e" * 64},
        skill_manifests=original.skill_manifests,
        profile_shape="class-tier", targets=targets,
    )
    wire = inputs.to_dict()
    assert wire["schema_version"] == "fanout-run-inputs-v3"
    assert "repo_baseline_sha256" not in wire
    assert set(wire["targets"]) == {"address", "booking"}
    assert RunInputs.from_dict(wire).digest == inputs.digest
    with pytest.raises(RunStateError, match="unknown or missing"):
        RunInputs.from_dict({**wire, "repo_baseline_sha256": "e" * 64})


def test_local_authority_v2_binds_ordered_target_registry(tmp_path):
    """Reopening with a retargeted registry cannot reuse an existing anchor."""
    roots = {name: tmp_path / name for name in ("address", "booking")}
    for name, root in roots.items():
        root.mkdir()
        for arguments in (("init", "-q", "-b", "main"),
                          ("config", "user.name", "Fixture"),
                          ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(("git", "-C", str(root), *arguments), check=True, capture_output=True)
        (root / "tracked.txt").write_text("fixture\n")
        subprocess.run(("git", "-C", str(root), "add", "tracked.txt"), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(root), "commit", "-qm", "seed"), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(root), "remote", "add", "origin",
                        f"https://github.com/example/{name}.git"), check=True, capture_output=True)
    specs = {
        name: fanout.TargetSpec(
            name, f"github.com/example/{name}", "TASK-123",
            f"refs/heads/feat/TASK-123-{name}",
        ) for name in roots
    }
    repo_module = sys.modules[f"{fanout.__name__}.repo"]
    bindings, _ = repo_module.capture_and_bind_targets(specs, roots)
    authority_root = tmp_path / "authority"
    run_root = tmp_path / "run"
    authority = fanout.LocalAnchorAuthority.bootstrap(
        authority_root, run_root=run_root, repo_root=roots["address"],
        target_bindings=bindings,
    )
    assert authority.identity.startswith("local-")
    reopened = fanout.LocalAnchorAuthority(
        authority_root, run_root=run_root, repo_root=roots["address"],
        target_bindings={"booking": bindings["booking"], "address": bindings["address"]},
    )
    assert reopened.identity == authority.identity
    altered = dict(bindings)
    altered["booking"] = dataclasses.replace(
        bindings["booking"], baseline_sha256="f" * 64,
    )
    with pytest.raises(RunStateError, match="target registry"):
        fanout.LocalAnchorAuthority(
            authority_root, run_root=run_root, repo_root=roots["address"],
            target_bindings=altered,
        )


def test_local_authority_rejects_stale_target_before_bootstrap_mutation(tmp_path):
    """A forged saved baseline cannot create an authority directory."""
    root = tmp_path / "address"
    root.mkdir()
    for arguments in (("init", "-q", "-b", "main"),
                      ("config", "user.name", "Fixture"),
                      ("config", "user.email", "fixture@example.invalid")):
        subprocess.run(("git", "-C", str(root), *arguments), check=True, capture_output=True)
    (root / "tracked.txt").write_text("fixture\n")
    subprocess.run(("git", "-C", str(root), "add", "tracked.txt"), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(root), "commit", "-qm", "seed"), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(root), "remote", "add", "origin",
                    "https://github.com/example/address.git"), check=True, capture_output=True)
    spec = fanout.TargetSpec(
        "address", "github.com/example/address", "TASK-123",
        "refs/heads/feat/TASK-123-address",
    )
    repo_module = sys.modules[f"{fanout.__name__}.repo"]
    bindings, _ = repo_module.capture_and_bind_targets({"address": spec}, {"address": root})
    stale = dataclasses.replace(bindings["address"], baseline_sha256="f" * 64)
    authority = tmp_path / "authority"
    with pytest.raises(RunStateError, match="target registry"):
        fanout.LocalAnchorAuthority.bootstrap(
            authority, run_root=tmp_path / "run", repo_root=root,
            target_bindings={"address": stale},
        )
    assert not authority.exists()


def _seat(journal: RunJournal, event: str, *, attempt: int = 1) -> None:
    journal.append(event, task_id="work", seat_id="claude", attempt=attempt, round=1)


def test_run_journal_requires_an_external_monotonic_authority(tmp_path: Path):
    """No replayable local-file authority may be selected as a silent default."""
    with pytest.raises(RunStateError, match="authority"):
        RawRunJournal.create(tmp_path / "run", _inputs())


def test_public_api_rejects_a_structurally_compatible_local_store(tmp_path: Path):
    """Production admission is nominal; duck-typed same-UID stores are not authorities."""
    with pytest.raises(RunStateError, match="trusted remote"):
        RawRunJournal.create(tmp_path / "run", _inputs(), anchor_store=MemoryAnchorStore())


@pytest.mark.parametrize("endpoint", [
    "https://2130706433", "https://0x7f000001", "https://127.1", "https://0177.0.0.1",
    "https://127.0.0.1", "https://0.0.0.0", "https://10.0.0.1", "https://169.254.169.254",
    "https://224.0.0.1", "https://240.0.0.1", "https://[::1]", "https://[::]",
    "https://[fc00::1]", "https://[fe80::1]", "https://[ff02::1]", "https://[2001:db8::1]",
    "https://[::ffff:8.8.8.8]", "https://8.8.8.8.", "https://anchor.example:",
    "https://localhost", "https://agent.localhost", "https://agent.local",
])
def test_remote_authority_rejects_non_global_and_ambiguous_endpoints(endpoint: str):
    """Legacy numeric spellings and every non-global address stay outside production admission."""
    with pytest.raises(RunStateError, match="endpoint"):
        RemoteAnchorAuthority(
            endpoint=endpoint, authority_id="authority.example/v1",
            credential="c" * 32, attestation_key=b"a" * 32,
        )


def test_remote_authority_rejects_dns_with_any_non_global_answer_and_resolver_errors(monkeypatch):
    """One private answer makes the complete resolved endpoint set unsafe."""
    runtime = sys.modules[f"{SPEC.name}.runstate"]

    def mixed_answers(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(runtime.socket, "getaddrinfo", mixed_answers)
    with pytest.raises(RunStateError, match="endpoint"):
        RemoteAnchorAuthority(
            endpoint="https://anchor.example", authority_id="authority.example/v1",
            credential="c" * 32, attestation_key=b"a" * 32,
        )

    monkeypatch.setattr(runtime.socket, "getaddrinfo", lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror("no DNS")))
    with pytest.raises(RunStateError, match="resolution"):
        RemoteAnchorAuthority(
            endpoint="https://anchor.example", authority_id="authority.example/v1",
            credential="c" * 32, attestation_key=b"a" * 32,
        )


@pytest.mark.parametrize(("endpoint", "resolved", "normalized"), [
    ("https://8.8.8.8:443/api/", "8.8.8.8", "https://8.8.8.8/api"),
    ("https://[2606:4700:4700::1111]/", "2606:4700:4700::1111", "https://[2606:4700:4700::1111]"),
    ("https://ANCHOR.EXAMPLE.:8443/api/", "1.1.1.1", "https://anchor.example:8443/api"),
])
def test_remote_authority_accepts_only_normalized_global_endpoints(
        endpoint: str, resolved: str, normalized: str, monkeypatch):
    """Canonical global literals and public DNS names normalize deterministically."""
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    family = socket.AF_INET6 if ":" in resolved else socket.AF_INET

    def answers(_host, port, *_args, **_kwargs):
        sockaddr = (resolved, port, 0, 0) if family == socket.AF_INET6 else (resolved, port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    monkeypatch.setattr(runtime.socket, "getaddrinfo", answers)
    authority = RemoteAnchorAuthority(
        endpoint=endpoint, authority_id="authority.example/v1",
        credential="c" * 32, attestation_key=b"a" * 32,
    )
    assert authority._endpoint == normalized


def test_remote_authority_re_resolves_and_rejects_rebinding_before_request(monkeypatch):
    """A hostname that becomes private after construction is never connected to."""
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    calls = 0

    def answers(_host, port, *_args):
        nonlocal calls
        calls += 1
        address = "1.1.1.1" if calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    monkeypatch.setattr(runtime.socket, "getaddrinfo", answers)
    monkeypatch.setattr(
        runtime, "_authority_https_post",
        lambda *_args, **_kwargs: pytest.fail("transport must not run after unsafe rebinding"),
    )
    authority = RemoteAnchorAuthority(
        endpoint="https://anchor.example", authority_id="authority.example/v1",
        credential="c" * 32, attestation_key=b"a" * 32,
    )
    with pytest.raises(RunStateError, match="non-global"):
        authority.read("fanout/run/key")


def test_remote_authority_client_verifies_nonce_identity_revision_value_and_attestation(tmp_path: Path, monkeypatch):
    """The nominal production client accepts only an exact attested HTTPS response."""
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    attestation_key = b"a" * 32
    records: dict[str, AnchorRevision] = {}
    tamper_nonce = False

    monkeypatch.setattr(runtime.socket, "getaddrinfo", lambda host, port, *_args: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", port)),
    ])

    def https_post(host, port, path, targets, body, credential, timeout):
        assert (host, port, credential, timeout) == ("anchor.example", 443, "c" * 32, 3.0)
        assert path.startswith("/v1/anchors/")
        assert targets[0][3] == ("1.1.1.1", 443)
        assert timeout == 3.0
        payload = json.loads(body)
        operation, key = payload["operation"], payload["key"]
        if operation == "create":
            result = AnchorRevision(1, base64.b64decode(payload["value_base64"]))
            records[key] = result
        elif operation == "read":
            result = records[key]
        else:
            current = records[key]
            assert current.revision == payload["expected_revision"]
            result = AnchorRevision(current.revision + 1, base64.b64decode(payload["value_base64"]))
            records[key] = result
        unsigned = {
            "schema_version": "fanout-anchor-response-v1", "operation": operation,
            "authority_id": "authority.example/v1",
            "nonce": "0" * 64 if tamper_nonce else payload["nonce"], "key": key,
            "revision": result.revision,
            "value_base64": base64.b64encode(result.value).decode("ascii"),
        }
        response = dict(unsigned)
        response["attestation"] = hmac.digest(
            attestation_key, b"fanout-anchor-response-v1" + fanout.canonical_json(unsigned), "sha256",
        ).hex()
        return 200, fanout.canonical_json(response)

    monkeypatch.setattr(runtime, "_authority_https_post", https_post)
    authority = RemoteAnchorAuthority(
        endpoint="https://anchor.example", authority_id="authority.example/v1",
        credential="c" * 32, attestation_key=attestation_key, timeout=3,
    )
    with pytest.raises(AttributeError, match="immutable"):
        authority._identity = "replayed-local-authority"
    root = tmp_path / "run"
    journal, capability = RawRunJournal.create(root, _inputs(), anchor_store=authority)
    _seat(journal, "dispatch-intent")
    journal.close()
    resumed = RawRunJournal.resume(root, _inputs(), capability, anchor_store=authority)
    resumed.close()

    tamper_nonce = True
    with pytest.raises(RunStateError, match="attestation"):
        authority.read(next(iter(records)))
    with pytest.raises(RunStateError, match="authority"):
        RawRunJournal.resume(root, _inputs(), capability, anchor_store=authority)


def test_authority_store_mismatch_and_backend_errors_fail_closed(tmp_path: Path):
    """Resume must use the exact durable authority bound at run creation."""
    root = tmp_path / "run"
    authority = MemoryAnchorStore("authority-a")
    journal, capability = RunJournal.create(root, _inputs(), anchor_store=authority)
    journal.close()

    with pytest.raises(RunStateError, match="identity"):
        RunJournal.resume(root, _inputs(), capability, anchor_store=MemoryAnchorStore("authority-b"))

    authority.fail_operation = "read"
    with pytest.raises(RunStateError, match="authority"):
        RunJournal.resume(root, _inputs(), capability, anchor_store=authority)


def test_authority_create_and_stale_cas_errors_never_advance_run_state(tmp_path: Path):
    """Backend failures are run-state failures, not evidence that an event happened."""
    unavailable = MemoryAnchorStore()
    unavailable.fail_operation = "create"
    with pytest.raises(RunStateError, match="authority"):
        RunJournal.create(tmp_path / "unavailable", _inputs(), anchor_store=unavailable)

    authority = FaultingAnchorStore()
    journal, _capability = RunJournal.create(tmp_path / "run", _inputs(), anchor_store=authority)
    authority.fail_cas_call = 1
    try:
        with pytest.raises(RunStateError, match="compare-and-set"):
            _seat(journal, "dispatch-intent")
        assert journal.state.seq == 0
    finally:
        journal.close()

    stale = StaleAnchorStore()
    journal, _capability = RunJournal.create(tmp_path / "stale", _inputs(), anchor_store=stale)
    try:
        with pytest.raises(RunStateError, match="stale or skipped"):
            _seat(journal, "dispatch-intent")
        assert journal.state.seq == 0
    finally:
        journal.close()


def test_pending_process_start_is_owner_classified_uncertain_before_retry(tmp_path: Path, monkeypatch):
    """A crash before the journal write must preserve exact possibly billable identity."""
    root = tmp_path / "run"
    authority = MemoryAnchorStore()
    journal, capability = RunJournal.create(root, _inputs(), anchor_store=authority)
    _seat(journal, "dispatch-intent")
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    original_write = runtime._write_all

    def crash_before_write(_fd: int, _data: bytes) -> None:
        raise OSError("injected crash before journal write")

    monkeypatch.setattr(runtime, "_write_all", crash_before_write)
    with pytest.raises(OSError, match="injected"):
        _seat(journal, "process-started")
    assert journal.state.seat_phase("work", "claude", 1, 1) == "dispatch-intent"
    journal.close()
    monkeypatch.setattr(runtime, "_write_all", original_write)

    resumed = RunJournal.resume(root, _inputs(), capability, anchor_store=authority)
    try:
        with pytest.raises(RunStateError, match="owner recovery"):
            _seat(resumed, "process-started")
        assert resumed.recover_pending(capability) == "uncertain-attempt"
        assert resumed.state.seat_phase("work", "claude", 1, 1) == "uncertain-attempt"
        with pytest.raises(RunStateError, match="uncertain"):
            _seat(resumed, "dispatch-intent", attempt=2)
    finally:
        resumed.close()


def test_pending_dispatch_intent_has_explicit_safe_recovery(tmp_path: Path, monkeypatch):
    """An intent that never reached the journal can be explicitly discarded without spend claims."""
    root = tmp_path / "run"
    authority = MemoryAnchorStore()
    journal, capability = RunJournal.create(root, _inputs(), anchor_store=authority)
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    original_write = runtime._write_all
    monkeypatch.setattr(runtime, "_write_all", lambda _fd, _data: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError, match="crash"):
        _seat(journal, "dispatch-intent")
    journal.close()
    monkeypatch.setattr(runtime, "_write_all", original_write)

    resumed = RunJournal.resume(root, _inputs(), capability, anchor_store=authority)
    try:
        assert resumed.recover_pending(capability) == "discarded-intent"
        resumed.snapshot(capability)
    finally:
        resumed.close()
    resumed = RunJournal.resume(root, _inputs(), capability, anchor_store=authority)
    try:
        _seat(resumed, "dispatch-intent")
        assert resumed.state.seq == 1
    finally:
        resumed.close()


def test_partial_pending_event_is_truncated_then_classified_uncertain(tmp_path: Path, monkeypatch):
    """A torn pending event is never mistaken for process-started or safe-to-retry evidence."""
    root = tmp_path / "run"
    authority = MemoryAnchorStore()
    journal, capability = RunJournal.create(root, _inputs(), anchor_store=authority)
    _seat(journal, "dispatch-intent")
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    original_write = runtime._write_all

    def partial_write(fd: int, data: bytes) -> None:
        os.write(fd, data[: len(data) // 2])
        raise OSError("injected partial journal write")

    monkeypatch.setattr(runtime, "_write_all", partial_write)
    with pytest.raises(OSError, match="partial"):
        _seat(journal, "process-started")
    journal.close()
    monkeypatch.setattr(runtime, "_write_all", original_write)

    resumed = RunJournal.resume(root, _inputs(), capability, anchor_store=authority)
    try:
        assert resumed.recovery.torn_tail_bytes > 0
        assert resumed.recover_pending(capability) == "uncertain-attempt"
        assert resumed.recovery.torn_tail_bytes == 0
    finally:
        resumed.close()


def test_fsynced_pending_event_is_committed_exactly_on_resume(tmp_path: Path):
    """A crash between journal fsync and authority commit must not duplicate the event."""
    root = tmp_path / "run"
    authority = FaultingAnchorStore()
    journal, capability = RunJournal.create(root, _inputs(), anchor_store=authority)
    _seat(journal, "dispatch-intent")
    authority.fail_cas_call = 4
    with pytest.raises(RunStateError, match="compare-and-set"):
        _seat(journal, "process-started")
    assert journal.state.seq == 1
    journal.close()
    authority.fail_cas_call = None

    resumed = RunJournal.resume(root, _inputs(), capability, anchor_store=authority)
    try:
        assert resumed.state.seq == 2
        assert resumed.state.seat_phase("work", "claude", 1, 1) == "process-started"
        assert resumed.recover_pending(capability) == "none"
    finally:
        resumed.close()


def test_inspect_exposes_fsynced_pending_state_without_authority_commit(tmp_path: Path):
    """An owner can inspect pending evidence before the mutating resume CAS."""
    root = tmp_path / "run"
    authority = FaultingAnchorStore()
    journal, capability = RunJournal.create(root, _inputs(), anchor_store=authority)
    _seat(journal, "dispatch-intent")
    authority.fail_cas_call = 4
    with pytest.raises(RunStateError, match="compare-and-set"):
        _seat(journal, "process-started")
    journal.close()
    authority.fail_cas_call = None
    key, before_authority = next(iter(authority.records.items()))
    before_journal = (root / "events.jsonl").read_bytes()
    before_cas_calls = authority.cas_calls

    inspected = RunJournal.inspect(root, _inputs(), capability, anchor_store=authority)

    assert inspected.inputs == _inputs()
    assert inspected.state.seq == 2
    assert inspected.state.seat_phase("work", "claude", 1, 1) == "process-started"
    assert inspected.pending_authority is True
    assert authority.records[key] == before_authority
    assert authority.cas_calls == before_cas_calls
    assert (root / "events.jsonl").read_bytes() == before_journal
    assert capability.export_token() not in repr(inspected)
    inspected_again = RunJournal.inspect(root, _inputs(), capability, anchor_store=authority)
    assert inspected_again.state.seq == 2
    assert authority.records[key] == before_authority

    resumed = RunJournal.resume(root, _inputs(), capability, anchor_store=authority,
                                expected_inspection=inspected)
    try:
        assert resumed.state.seq == 2
        assert authority.records[key].revision == before_authority.revision + 1
    finally:
        resumed.close()


def test_inspect_replays_amendments_and_rejects_tamper_without_holding_lock(tmp_path: Path):
    """Inspection authenticates history and releases resources on success or failure."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    binding = {
        "revision": 2,
        "old_plan_sha256": "a" * 64,
        "old_inputs_digest": _inputs().digest,
        "new_plan_sha256": "2" * 64,
        "new_inputs_digest": "3" * 64,
        "old_profiles_sha256": "5" * 64,
        "new_profiles_sha256": "6" * 64,
    }
    journal.append("blocked-action", task_id="plan-revision-2", owner=capability)
    journal.append_amendment("plan-amendment-intent", **binding, owner=capability)
    journal.append_amendment("plan-amendment-accepted", **binding, owner=capability)
    journal.close()

    inspected = RunJournal.inspect(root, _inputs(), capability)
    assert inspected.state.amendments[2].phase == "plan-amendment-accepted"
    assert inspected.state.amendments[2].new_plan_sha256 == "2" * 64
    assert inspected.pending_authority is False
    with pytest.raises(RunStateError, match="inputs"):
        RunJournal.inspect(root, RunInputs.from_dict({**_inputs().to_dict(), "source_sha256": "9" * 64}), capability)
    with pytest.raises(RunStateError, match="owner capability"):
        RunJournal.inspect(root, _inputs(), fanout.OwnerCapability.from_token("wrong" * 8))

    path = root / "events.jsonl"
    untampered = path.read_bytes()
    path.write_bytes(untampered.replace(b'"revision":2', b'"revision":3', 1))
    with pytest.raises(RunStateError, match="checksum|mac|journal"):
        RunJournal.inspect(root, _inputs(), capability)
    path.write_bytes(untampered)
    assert RunJournal.inspect(root, _inputs(), capability).state.amendments[2].phase == "plan-amendment-accepted"


def test_resume_expected_inspection_refuses_pending_race_before_cas(tmp_path: Path):
    """A new exact pending event cannot be committed after a prior clean inspection."""
    root = tmp_path / "run"
    authority = FaultingAnchorStore()
    journal, capability = RunJournal.create(root, _inputs(), anchor_store=authority)
    _seat(journal, "dispatch-intent")
    journal.close()
    inspected = RunJournal.inspect(root, _inputs(), capability, anchor_store=authority)
    assert inspected.pending_authority is False

    interloper = RunJournal.resume(root, _inputs(), capability, anchor_store=authority)
    authority.fail_cas_call = 4
    with pytest.raises(RunStateError, match="compare-and-set"):
        _seat(interloper, "process-started")
    interloper.close()
    authority.fail_cas_call = None
    key, before_authority = next(iter(authority.records.items()))
    before_journal = (root / "events.jsonl").read_bytes()
    before_cas_calls = authority.cas_calls

    with pytest.raises(RunStateError, match="inspection|changed"):
        RunJournal.resume(root, _inputs(), capability, anchor_store=authority,
                          expected_inspection=inspected)

    assert authority.records[key] == before_authority
    assert authority.cas_calls == before_cas_calls
    assert (root / "events.jsonl").read_bytes() == before_journal
    assert RunJournal.inspect(root, _inputs(), capability, anchor_store=authority).pending_authority is True


def test_resume_expected_inspection_refuses_unwritten_authority_race(tmp_path: Path, monkeypatch):
    """Authority drift is detected even when no new journal record was written."""
    root = tmp_path / "run"
    authority = FaultingAnchorStore()
    journal, capability = RunJournal.create(root, _inputs(), anchor_store=authority)
    journal.close()
    inspected = RunJournal.inspect(root, _inputs(), capability, anchor_store=authority)
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    original_write = runtime._write_all
    monkeypatch.setattr(runtime, "_write_all", lambda _fd, _data: (_ for _ in ()).throw(OSError("crash")))
    interloper = RunJournal.resume(root, _inputs(), capability, anchor_store=authority)
    with pytest.raises(OSError, match="crash"):
        _seat(interloper, "dispatch-intent")
    interloper.close()
    monkeypatch.setattr(runtime, "_write_all", original_write)
    before_cas_calls = authority.cas_calls

    with pytest.raises(RunStateError, match="inspection|changed"):
        RunJournal.resume(root, _inputs(), capability, anchor_store=authority,
                          expected_inspection=inspected)
    assert authority.cas_calls == before_cas_calls


def test_owner_can_commit_an_exact_fsynced_pending_event_without_reopening(tmp_path: Path):
    """The recovery API recognizes exact durable evidence instead of downgrading it."""
    authority = FaultingAnchorStore()
    journal, capability = RunJournal.create(tmp_path / "run", _inputs(), anchor_store=authority)
    _seat(journal, "dispatch-intent")
    authority.fail_cas_call = 4
    with pytest.raises(RunStateError):
        _seat(journal, "process-started")
    authority.fail_cas_call = None
    try:
        assert journal.recover_pending(capability) == "committed"
        assert journal.state.seat_phase("work", "claude", 1, 1) == "process-started"
    finally:
        journal.close()


def test_pending_authority_payload_is_strictly_validated_before_recovery(tmp_path: Path, monkeypatch):
    """A MAC-valid pending record still needs exact typed task and attempt evidence."""
    root = tmp_path / "run"
    authority = MemoryAnchorStore()
    journal, capability = RunJournal.create(root, _inputs(), anchor_store=authority)
    _seat(journal, "dispatch-intent")
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    original_write = runtime._write_all
    monkeypatch.setattr(runtime, "_write_all", lambda _fd, _data: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        _seat(journal, "process-started")
    journal.close()
    monkeypatch.setattr(runtime, "_write_all", original_write)

    key, revision = next(iter(authority.records.items()))
    record = json.loads(revision.value)
    pending = record["pending"]
    pending["payload"]["attempt"] = True
    pending["payload_sha256"] = runtime._sha256(fanout.canonical_json(pending["payload"]))
    pending["checksum"] = runtime._checksum(pending)
    pending["mac"] = runtime._mac(capability._mac_key(), b"event", {k: v for k, v in pending.items() if k != "mac"})
    record["pending_offset"] = record["offset"] + len(fanout.canonical_json(pending))
    record["mac"] = runtime._mac(capability._mac_key(), b"authority", {k: v for k, v in record.items() if k != "mac"})
    authority.records[key] = AnchorRevision(revision.revision, fanout.canonical_json(record))

    with pytest.raises(RunStateError, match="identity"):
        RunJournal.resume(root, _inputs(), capability, anchor_store=authority)


def test_ambiguous_commit_response_recovers_from_authoritative_committed_value(tmp_path: Path):
    """A CAS that commits then loses its response leaves local state unchanged until resume."""
    root = tmp_path / "run"
    authority = FaultingAnchorStore()
    journal, capability = RunJournal.create(root, _inputs(), anchor_store=authority)
    _seat(journal, "dispatch-intent")
    authority.mutate_then_fail_cas_call = 4
    with pytest.raises(RunStateError, match="compare-and-set"):
        _seat(journal, "process-started")
    assert journal.state.seq == 1
    journal.close()
    authority.mutate_then_fail_cas_call = None

    resumed = RunJournal.resume(root, _inputs(), capability, anchor_store=authority)
    try:
        assert resumed.state.seat_phase("work", "claude", 1, 1) == "process-started"
    finally:
        resumed.close()


def test_journal_chains_canonical_events_and_reconstructs_a_snapshot(tmp_path: Path):
    """Changing an event byte or its predecessor must make recovery fail closed."""
    journal, capability = RunJournal.create(tmp_path / "run", _inputs())
    try:
        _seat(journal, "dispatch-intent")
        _seat(journal, "process-started")
        _seat(journal, "provider-terminal")
        _seat(journal, "artifacts-durable")
        journal.snapshot(capability)
    finally:
        journal.close()
    resumed = RunJournal.resume(tmp_path / "run", _inputs(), capability)
    try:
        assert resumed.state.seat_phase("work", "claude", 1, 1) == "artifacts-durable"
        assert resumed.state.seq == 4
    finally:
        resumed.close()

    path = tmp_path / "run" / "events.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"round":1', b'"round":2', 1))
    with pytest.raises(RunStateError, match="checksum"):
        RunJournal.resume(tmp_path / "run", _inputs(), capability)


def test_resume_replays_a_valid_suffix_once_and_reports_only_a_torn_final_tail(tmp_path: Path):
    """A valid snapshot cannot hide suffix events; an incomplete final line is not evidence."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    try:
        _seat(journal, "dispatch-intent")
        journal.snapshot(capability)
        _seat(journal, "process-started")
    finally:
        journal.close()
    with (root / "events.jsonl").open("ab") as handle:
        handle.write(b'{"not":"a complete record"')

    resumed = RunJournal.resume(root, _inputs(), capability)
    try:
        assert resumed.state.seq == 2
        assert resumed.recovery.torn_tail_bytes > 0
        with pytest.raises(RunStateError, match="repair"):
            _seat(resumed, "provider-terminal")
    finally:
        resumed.close()


def test_legal_seat_phases_reject_skips_duplicates_and_regressions_without_appending(tmp_path: Path):
    """A skipped or repeated phase would make a settled provider turn rerunnable."""
    journal, _capability = RunJournal.create(tmp_path / "run", _inputs())
    try:
        before = journal.state.seq
        with pytest.raises(RunStateError, match="illegal"):
            _seat(journal, "provider-terminal")
        assert journal.state.seq == before
        _seat(journal, "dispatch-intent")
        with pytest.raises(RunStateError, match="illegal"):
            _seat(journal, "dispatch-intent")
        assert journal.state.seq == 1
    finally:
        journal.close()


def test_recovery_marks_started_without_terminal_as_uncertain_and_never_retries_automatically(tmp_path: Path):
    """A missing terminal record may conceal billable work and must block automatic retry."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    try:
        _seat(journal, "dispatch-intent")
        _seat(journal, "process-started")
    finally:
        journal.close()
    resumed = RunJournal.resume(root, _inputs(), capability)
    try:
        resumed.recover_uncertain(capability)
        assert resumed.state.seat_phase("work", "claude", 1, 1) == "uncertain-attempt"
        with pytest.raises(RunStateError, match="uncertain"):
            _seat(resumed, "dispatch-intent", attempt=2)
        resumed.accept_possible_duplicate(capability, task_id="work", seat_id="claude",
                                          prior_attempt=1, next_attempt=2, round=1)
        _seat(resumed, "dispatch-intent", attempt=2)
    finally:
        resumed.close()


def test_intent_without_process_start_can_be_reissued_without_duplicate_spend_authorization(tmp_path: Path):
    """An fsynced intent alone proves no provider process began, so recovery may issue a new attempt."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    try:
        _seat(journal, "dispatch-intent")
    finally:
        journal.close()
    resumed = RunJournal.resume(root, _inputs(), capability)
    try:
        _seat(resumed, "dispatch-intent", attempt=2)
        assert resumed.state.seat_phase("work", "claude", 2, 1) == "dispatch-intent"
    finally:
        resumed.close()


def test_owner_capability_is_redacted_persisted_as_digest_and_required_for_control_events(tmp_path: Path):
    """A seat must not publish reconciliation or extract the owner credential from disk."""
    journal, capability = RunJournal.create(tmp_path / "run", _inputs())
    try:
        assert "token" not in repr(capability).lower()
        assert "token" not in (tmp_path / "run" / "owner.json").read_text()
        with pytest.raises(RunAuthorizationError):
            journal.append("reconciliation-pending", task_id="work")
        journal.append("reconciliation-pending", task_id="work", owner=capability)
    finally:
        journal.close()


def test_resume_rejects_immutable_input_drift_before_dispatch(tmp_path: Path):
    """Source, provider, and skill mutations must invalidate a stored run before spending."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    journal.close()
    values = _inputs().to_dict()
    values.pop("schema_version")
    values["source_sha256"] = "f" * 64
    changed = RunInputs(**values)
    with pytest.raises(RunStateError, match="inputs"):
        RunJournal.resume(root, changed, capability)


def test_one_controller_lock_is_exclusive_and_releases_on_close(tmp_path: Path):
    """Two controllers could otherwise append competing valid hash chains."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    try:
        with pytest.raises(RunLockError):
            RunJournal.resume(root, _inputs(), capability)
    finally:
        journal.close()
    resumed = RunJournal.resume(root, _inputs(), capability)
    resumed.close()


def test_controller_lock_rejects_a_second_process_without_waiting(tmp_path: Path):
    """A separate controller process must fail closed instead of serializing a conflicting writer."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    program = "\n".join([
        "import json, sys",
        f"sys.path.insert(0, {str(ROOT / 'shared' / 'lib')!r})",
        "from fanout import OwnerCapability, RunInputs, RunJournal, RunLockError",
        "from fanout import runstate as runtime",
        "payload = json.loads(sys.stdin.read())",
        "class Authority:",
        "    identity = payload['store_id']",
        "authority = Authority()",
        "client = runtime._test_anchor_authority(authority)",
        "try:",
        "    state = RunJournal._resume_for_test(sys.argv[1], RunInputs.from_dict(payload['inputs']), OwnerCapability.from_token(payload['token']), anchor_store=client)",
        "except RunLockError:",
        "    raise SystemExit(23)",
        "else:",
        "    state.close()",
        "    raise SystemExit(24)",
    ])
    try:
        result = subprocess.run([sys.executable, "-c", program, str(root)], input=json.dumps({"inputs": _inputs().to_dict(), "token": capability.export_token(), "store_id": _AUTHORITIES[root.absolute()].identity.removeprefix("test-only/")}),
                                text=True, capture_output=True, timeout=5, check=False)
        assert result.returncode == 23, result.stderr
    finally:
        journal.close()


def test_root_descriptor_stays_contained_after_path_replacement(tmp_path: Path):
    """A root-name swap must not redirect a controller's journal writes outside its pinned inode."""
    root = tmp_path / "run"
    journal, _capability = RunJournal.create(root, _inputs())
    pinned = tmp_path / "pinned"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.rename(pinned)
    root.symlink_to(outside, target_is_directory=True)
    try:
        _seat(journal, "dispatch-intent")
        assert (pinned / "events.jsonl").exists()
        assert not (outside / "events.jsonl").exists()
    finally:
        journal.close()


def test_journal_limits_reject_an_event_before_partial_append(tmp_path: Path):
    """A quota check after writing could leave a chain whose end is not durable evidence."""
    journal, _capability = RunJournal.create(tmp_path / "run", _inputs(),
                                              limits=RunLimits(max_events=1, max_journal_bytes=4096))
    try:
        _seat(journal, "dispatch-intent")
        with pytest.raises(RunStateError, match="event limit"):
            _seat(journal, "process-started")
        assert journal.state.seq == 1
    finally:
        journal.close()


def test_capability_export_import_authenticates_resume_and_rejects_a_forged_chain(tmp_path: Path):
    """A same-UID writer without the external token cannot forge a durable owner transition."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    token = capability.export_token()
    journal.close()
    forged = {
        "schema_version": "fanout-run-event-v1", "version": 1, "run_id": "run-1", "seq": 1,
        "previous_checksum": "0" * 64, "timestamp": "2026-01-01T00:00:00.000000Z",
        "type": "reconciliation-pending", "payload": {"task_id": "forged"},
        "authority_revision": 3,
    }
    forged["payload_sha256"] = hashlib.sha256(fanout.canonical_json(forged["payload"])).hexdigest()
    forged["checksum"] = hashlib.sha256(fanout.canonical_json(forged)).hexdigest()
    forged["mac"] = "0" * 64
    (root / "events.jsonl").write_bytes(fanout.canonical_json(forged))
    recovered = fanout.OwnerCapability.from_token(token)
    with pytest.raises(RunStateError, match="MAC"):
        RunJournal.resume(root, _inputs(), recovered)


def test_empty_snapshot_replays_later_suffix_and_recovered_capability_controls_resume(tmp_path: Path):
    """A zero-offset snapshot is a valid empty prefix, not a malformed interior offset."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    token = capability.export_token()
    try:
        journal.snapshot(capability)
        _seat(journal, "dispatch-intent")
    finally:
        journal.close()
    resumed = RunJournal.resume(root, _inputs(), fanout.OwnerCapability.from_token(token))
    try:
        assert resumed.state.seq == 1
    finally:
        resumed.close()


def test_replaced_controller_lock_fails_closed(tmp_path: Path):
    """The lock pathname must stay bound to its creation-time inode."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    token = capability.export_token()
    journal.close()
    (root / ".controller.lock").unlink()
    (root / ".controller.lock").write_bytes(b"replacement")
    with pytest.raises(RunLockError):
        RunJournal.resume(root, _inputs(), fanout.OwnerCapability.from_token(token))


@pytest.mark.parametrize("mutation", ["mode", "hardlink"])
def test_controller_lock_metadata_mutations_fail_before_append(tmp_path: Path, mutation: str):
    """The held lock remains private, singly linked, and bound to its original entry."""
    root = tmp_path / "run"
    journal, _capability = RunJournal.create(root, _inputs())
    lock = root / ".controller.lock"
    if mutation == "mode":
        lock.chmod(0o644)
    else:
        os.link(lock, root / "lock-link")
    try:
        with pytest.raises(RunLockError):
            _seat(journal, "dispatch-intent")
        assert journal.state.seq == 0
    finally:
        journal.close()


def test_explicit_block_recovery_and_amendment_paths_are_legal(tmp_path: Path):
    """Durable blocks and accepted amendments must record their equally durable recovery."""
    journal, capability = RunJournal.create(tmp_path / "run", _inputs())
    try:
        journal.append("blocked-memory", task_id="memory", owner=capability)
        journal.append("memory-recovered", task_id="memory", owner=capability)
        journal.append("reconciliation-pending", task_id="memory", owner=capability)
        journal.append("blocked-action", task_id="action", owner=capability)
        journal.append("action-complete", task_id="action", owner=capability)
        journal.append("reconciliation-pending", task_id="action", owner=capability)
        journal.append("plan-amended", task_id="amended", owner=capability)
        journal.append("amendment-accepted", task_id="amended", owner=capability)
        journal.append("reconciliation-pending", task_id="amended", owner=capability)
    finally:
        journal.close()


def test_memory_recovery_can_reenter_block_for_another_unfinished_seat(tmp_path: Path):
    """A later seat can reopen the shared task block without regressing seat state."""
    journal, capability = RunJournal.create(tmp_path / "run", _inputs())
    try:
        journal.append("blocked-memory", task_id="work", owner=capability)
        journal.append("memory-recovered", task_id="work", owner=capability)
        journal.append("blocked-memory", task_id="work", owner=capability)
        journal.append("memory-recovered", task_id="work", owner=capability)
        assert journal.state.task_phases["work"] == "memory-recovered"
    finally:
        journal.close()


def test_owned_memory_block_rejects_wrong_capability_and_recovery_cycle(tmp_path: Path):
    """Only the owner-authorized exact seat turn can clear its active memory block."""
    journal, capability = RunJournal.create(tmp_path / "run", _inputs())
    forged = fanout.OwnerCapability.from_token("forged-owner-capability-" + "x" * 32)
    try:
        with pytest.raises(RunAuthorizationError):
            journal.append(
                "blocked-memory",
                task_id="work",
                seat_id="codex",
                attempt=2,
                round=3,
                owner=forged,
            )
        assert journal.state.seq == 0

        journal.append(
            "blocked-memory",
            task_id="work",
            seat_id="codex",
            attempt=2,
            round=3,
            owner=capability,
        )
        assert journal.state.memory_block_owners["work"] == ("codex", 2, 3)
        blocked_sequence = journal.state.seq
        for identity in (
            {},
            {"seat_id": "claude", "attempt": 2, "round": 3},
            {"seat_id": "codex", "attempt": 1, "round": 3},
            {"seat_id": "codex", "attempt": 2, "round": 2},
        ):
            with pytest.raises(RunStateError, match="memory block owner"):
                journal.append(
                    "memory-recovered",
                    task_id="work",
                    owner=capability,
                    **identity,
                )
            assert journal.state.seq == blocked_sequence
            assert journal.state.task_phases["work"] == "blocked-memory"
        with pytest.raises(RunStateError, match="illegal task transition"):
            journal.append("reconciliation-pending", task_id="work", owner=capability)

        journal.append(
            "memory-recovered",
            task_id="work",
            seat_id="codex",
            attempt=2,
            round=3,
            owner=capability,
        )
        assert "work" not in journal.state.memory_block_owners
    finally:
        journal.close()


def test_memory_block_owner_survives_snapshot_external_authority_and_resume(
    tmp_path: Path,
):
    """The authenticated snapshot and replay retain the exact recovery cycle."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    recovered = fanout.OwnerCapability.from_token(capability.export_token())
    try:
        journal.append(
            "blocked-memory",
            task_id="work",
            seat_id="codex",
            attempt=2,
            round=3,
            owner=capability,
        )
        journal.snapshot(capability)
        assert journal.state.to_dict()["memory_block_owners"] == [
            {"task_id": "work", "seat_id": "codex", "attempt": 2, "round": 3}
        ]
    finally:
        journal.close()

    resumed = RunJournal.resume(root, _inputs(), recovered)
    try:
        assert resumed.state.memory_block_owners["work"] == ("codex", 2, 3)
        with pytest.raises(RunStateError, match="memory block owner"):
            resumed.append(
                "memory-recovered",
                task_id="work",
                seat_id="claude",
                attempt=2,
                round=3,
                owner=recovered,
            )
        resumed.append(
            "memory-recovered",
            task_id="work",
            seat_id="codex",
            attempt=2,
            round=3,
            owner=recovered,
        )
    finally:
        resumed.close()


def test_legacy_unowned_memory_block_snapshot_remains_resumable(tmp_path: Path):
    """The new owner field does not invalidate an authenticated Task 8 snapshot."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    try:
        journal.append("blocked-memory", task_id="work", owner=capability)
        journal.snapshot(capability)
    finally:
        journal.close()

    snapshot_path = root / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_bytes())
    snapshot["state"].pop("memory_block_owners")
    runtime = sys.modules[f"{SPEC.name}.runstate"]
    snapshot["mac"] = runtime._mac(
        capability._mac_key(),
        b"snapshot",
        {key: value for key, value in snapshot.items() if key != "mac"},
    )
    snapshot_path.write_bytes(fanout.canonical_json(snapshot))

    resumed = RunJournal.resume(
        root,
        _inputs(),
        fanout.OwnerCapability.from_token(capability.export_token()),
    )
    try:
        assert resumed.state.task_phases["work"] == "blocked-memory"
        assert resumed.state.memory_block_owners == {}
        resumed.append("memory-recovered", task_id="work", owner=capability)
    finally:
        resumed.close()


def test_two_owned_memory_block_cycles_cannot_clear_each_other(tmp_path: Path):
    """A prior recovered seat cannot finalize the next seat's recovery cycle."""
    journal, capability = RunJournal.create(tmp_path / "run", _inputs())
    try:
        journal.append(
            "blocked-memory",
            task_id="work",
            seat_id="claude",
            attempt=1,
            round=1,
            owner=capability,
        )
        journal.append(
            "memory-recovered",
            task_id="work",
            seat_id="claude",
            attempt=1,
            round=1,
            owner=capability,
        )
        journal.append(
            "blocked-memory",
            task_id="work",
            seat_id="codex",
            attempt=1,
            round=1,
            owner=capability,
        )
        with pytest.raises(RunStateError, match="memory block owner"):
            journal.append(
                "memory-recovered",
                task_id="work",
                seat_id="claude",
                attempt=1,
                round=1,
                owner=capability,
            )
        assert journal.state.memory_block_owners["work"] == ("codex", 1, 1)
        journal.append(
            "memory-recovered",
            task_id="work",
            seat_id="codex",
            attempt=1,
            round=1,
            owner=capability,
        )
    finally:
        journal.close()


def test_public_inputs_reject_secret_bearing_profiles_and_nested_manifests():
    """Persisted input bindings contain safe identifiers and digests, never provider configuration text."""
    with pytest.raises(RunStateError):
        RunInputs(
            run_id="run-1", compiled_plan_sha256="a" * 64, source_sha256="b" * 64,
            draft_sha256="c" * 64, compiler_sha256="d" * 64, parser_sha256="e" * 64,
            provider_profiles={"claude": "api-token-secret"}, skill_manifests={"work": {"token": "secret"}},
            repo_baseline_sha256="f" * 64,
        )


def test_boolean_identity_counters_are_not_accepted_as_integer_evidence(tmp_path: Path):
    """JSON booleans compare equal to integers in Python but are not durable sequence identities."""
    journal, _capability = RunJournal.create(tmp_path / "run", _inputs())
    try:
        with pytest.raises(RunStateError):
            journal.append("dispatch-intent", task_id="work", seat_id="claude", attempt=True, round=1)
    finally:
        journal.close()


def test_cached_journal_replacement_fails_before_process_state_can_advance(tmp_path: Path):
    """A cached descriptor must not let a replaced pathname hide process-start evidence."""
    root = tmp_path / "run"
    journal, _capability = RunJournal.create(root, _inputs())
    try:
        _seat(journal, "dispatch-intent")
        original = (root / "events.jsonl").read_bytes()
        (root / "events.jsonl").rename(root / "old-events.jsonl")
        (root / "events.jsonl").write_bytes(original)
        (root / "events.jsonl").chmod(0o600)
        with pytest.raises(RunStateError, match="journal"):
            _seat(journal, "process-started")
        assert journal.state.seat_phase("work", "claude", 1, 1) == "dispatch-intent"
    finally:
        journal.close()


def test_resume_rejects_byte_identical_journal_inode_replacement(tmp_path: Path):
    """Recovery must authenticate the journal object, not only its current bytes."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    journal.close()
    original = root / "events.jsonl"
    original.rename(root / "old-events.jsonl")
    original.write_bytes((root / "old-events.jsonl").read_bytes())
    original.chmod(0o600)

    with pytest.raises(RunStateError, match="journal"):
        RunJournal.resume(root, _inputs(), capability)


def test_amendment_records_use_a_revision_namespace_and_exact_digest_binding(
    tmp_path: Path,
):
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    binding = {
        "revision": 2,
        "old_plan_sha256": "a" * 64,
        "old_inputs_digest": _inputs().digest,
        "new_plan_sha256": "2" * 64,
        "new_inputs_digest": "3" * 64,
        "old_profiles_sha256": "5" * 64,
        "new_profiles_sha256": "6" * 64,
    }
    try:
        journal.append(
            "blocked-action",
            task_id="plan-revision-2",
            owner=capability,
        )
        journal.append_amendment(
            "plan-amendment-intent",
            **binding,
            owner=capability,
        )
        with pytest.raises(RunStateError, match="exact|binding|intent"):
            journal.append_amendment(
                "plan-amendment-accepted",
                **{**binding, "new_inputs_digest": "4" * 64},
                owner=capability,
            )
        with pytest.raises(RunStateError, match="exact|binding|intent"):
            journal.append_amendment(
                "plan-amendment-accepted",
                **{**binding, "new_profiles_sha256": "7" * 64},
                owner=capability,
            )
        journal.append_amendment(
            "plan-amendment-accepted",
            **binding,
            owner=capability,
        )
        journal.snapshot(capability)
    finally:
        journal.close()

    resumed = RunJournal.resume(root, _inputs(), capability)
    try:
        assert resumed.state.task_phases["plan-revision-2"] == "blocked-action"
        assert resumed.state.amendments[2].binding == (
            binding["old_plan_sha256"],
            binding["old_inputs_digest"],
            binding["new_plan_sha256"],
            binding["new_inputs_digest"],
            binding["old_profiles_sha256"],
            binding["new_profiles_sha256"],
        )
        assert resumed.state.amendments[2].phase == "plan-amendment-accepted"
    finally:
        resumed.close()


def test_handover_replay_binds_each_terminal_to_its_exact_transaction(tmp_path: Path):
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    transaction_a = "/controller/transactions/handover-a"
    transaction_b = "/controller/transactions/handover-b"
    transaction_sha256 = "7" * 64
    disposition_sha256 = "8" * 64
    try:
        journal.append("reconciliation-pending", task_id="work", owner=capability)
        journal.append("reconciliation-verifying", task_id="work", owner=capability)
        journal.append("completed", task_id="work", owner=capability)
        journal.append(
            "handover-intent",
            task_id="work",
            transaction_root=transaction_b,
            transaction_sha256=transaction_sha256,
            owner=capability,
        )
        with pytest.raises(RunStateError, match="transaction|association"):
            journal.append(
                "handover-not-mutated",
                task_id="work",
                transaction_root=transaction_a,
                transaction_sha256="6" * 64,
                disposition_sha256="5" * 64,
                owner=capability,
            )
        journal.append(
            "handover-complete",
            task_id="work",
            transaction_root=transaction_b,
            transaction_sha256=transaction_sha256,
            disposition_sha256=disposition_sha256,
            owner=capability,
        )
        journal.snapshot(capability)
    finally:
        journal.close()

    resumed = RunJournal.resume(root, _inputs(), capability)
    try:
        handover = resumed.state.handovers["work"]
        assert handover.transaction_root == transaction_b
        assert handover.transaction_sha256 == transaction_sha256
        assert handover.disposition_sha256 == disposition_sha256
        assert handover.phase == "handover-complete"
    finally:
        resumed.close()


def test_external_authority_rejects_old_journal_and_all_restored_local_run_files(tmp_path: Path):
    """Restoring a complete old run directory cannot roll back the external authority."""
    root = tmp_path / "run"
    journal, capability = RunJournal.create(root, _inputs())
    try:
        _seat(journal, "dispatch-intent")
        journal.snapshot(capability)
        old = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
        _seat(journal, "process-started")
    finally:
        journal.close()
    for name, data in old.items():
        (root / name).write_bytes(data)
        (root / name).chmod(0o600)
    with pytest.raises(RunStateError, match="authority"):
        RunJournal.resume(root, _inputs(), capability)
