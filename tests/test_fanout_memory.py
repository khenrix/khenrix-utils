"""Exact, durable claude-mem checkpoint contracts for fanout rounds."""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_memory_contracts",
    FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


ArtifactLimits = fanout.ArtifactLimits
ArtifactStore = fanout.ArtifactStore
Checkpoint = fanout.Checkpoint
CheckpointBlockedError = fanout.CheckpointBlockedError
CheckpointIdentity = fanout.CheckpointIdentity
CheckpointPublication = fanout.CheckpointPublication
CheckpointReceipt = fanout.CheckpointReceipt
MemoryController = fanout.MemoryController
MemoryDurabilityError = fanout.MemoryDurabilityError
MemoryEquivocationError = fanout.MemoryEquivocationError
MemoryIntegrityError = fanout.MemoryIntegrityError
MemoryProtocolError = fanout.MemoryProtocolError
MemoryTransportError = fanout.MemoryTransportError
MemoryValidationError = fanout.MemoryValidationError
MemoryCheckpointExchange = fanout.MemoryCheckpointExchange
OwnerCapability = fanout.OwnerCapability
ProcessResult = fanout.ProcessResult
ProcessStatus = fanout.ProcessStatus
RunInputs = fanout.RunInputs
RawRunJournal = fanout.RunJournal
RunStateError = fanout.RunStateError
TimeoutEvidence = fanout.TimeoutEvidence
canonical_json = fanout.canonical_json
memory = sys.modules[f"{SPEC.name}.memory"]
runstate = sys.modules[f"{SPEC.name}.runstate"]


class _MemoryAnchor:
    def __init__(self) -> None:
        self.identity = "memory-checkpoint-tests"
        self.records: dict[str, fanout.AnchorRevision] = {}

    def create(self, key: str, value: bytes):
        result = fanout.AnchorRevision(1, value)
        self.records[key] = result
        return result

    def read(self, key: str):
        return self.records[key]

    def compare_and_set(self, key: str, expected_revision: int, value: bytes):
        assert self.records[key].revision == expected_revision
        result = fanout.AnchorRevision(expected_revision + 1, value)
        self.records[key] = result
        return result


def _inputs() -> RunInputs:
    return RunInputs(
        run_id="run-1",
        compiled_plan_sha256="a" * 64,
        source_sha256="b" * 64,
        draft_sha256="c" * 64,
        compiler_sha256="d" * 64,
        parser_sha256="e" * 64,
        provider_profiles={"claude": "f" * 64},
        skill_manifests={"work": "1" * 64},
    )


def _journal(tmp_path: Path, *, seats: tuple[str, ...] = ("claude",)):
    authority = runstate._test_anchor_authority(_MemoryAnchor())
    journal, owner = RawRunJournal._create_for_test(
        tmp_path / "journal", _inputs(), anchor_store=authority
    )
    for seat in seats:
        for event in (
            "dispatch-intent",
            "process-started",
            "provider-terminal",
            "artifacts-durable",
        ):
            journal.append(
                event, task_id="work", seat_id=seat, attempt=1, round=1
            )
    return journal, owner


def _identity(**changes: object) -> CheckpointIdentity:
    values: dict[str, object] = {
        "run_id": "run-1",
        "task_id": "work",
        "seat_id": "claude",
        "attempt": 1,
        "round": 1,
        "provider_session_id": "11111111-1111-4111-8111-111111111111",
    }
    values.update(changes)
    return CheckpointIdentity(**values)


def _checkpoint(answer: str = "peer answer", **identity: object) -> Checkpoint:
    return Checkpoint.create(_identity(**identity), answer)


def _controller_file(tmp_path: Path) -> tuple[Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "memory_exchange.py"
    path.write_bytes(b"#!/usr/bin/env python3\nraise SystemExit(99)\n")
    path.chmod(0o700)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _observation(
    checkpoint: Checkpoint,
    observation_id: int,
    **changes: object,
) -> dict[str, object]:
    session_id = f"manual-{checkpoint.project}-claude"
    content_hash = hashlib.sha256(
        f"{session_id}\0{checkpoint.title}\0{checkpoint.text}".encode()
    ).hexdigest()[:16]
    value: dict[str, object] = {
        "agent_id": None,
        "agent_type": None,
        "concepts": "[]",
        "content_hash": content_hash,
        "created_at": "2026-09-23T00:00:00.000Z",
        "created_at_epoch": 1_795_000_000_000 + observation_id,
        "discovery_tokens": 0,
        "facts": "[]",
        "files_modified": "[]",
        "files_read": "[]",
        "generated_by_model": None,
        "id": observation_id,
        "memory_session_id": session_id,
        "merged_into_project": None,
        "metadata": None,
        "narrative": checkpoint.text,
        "origin_device_id": None,
        "origin_local_id": None,
        "project": checkpoint.project,
        "prompt_number": None,
        "relevance_count": 0,
        "subtitle": "Manual memory",
        "sync_rev": "1",
        "synced_at": None,
        "text": None,
        "title": checkpoint.title,
        "type": "discovery",
    }
    value.update(changes)
    return value


class _ControllerHarness:
    def __init__(self, checkpoint: Checkpoint) -> None:
        self.checkpoints = {checkpoint.identity.key: checkpoint}
        self.saved: dict[int, Checkpoint] = {}
        self.next_id = 41
        self.commands: list[fanout.ProcessCommand] = []
        self.save_calls = 0
        self.fetch_calls = 0
        self.save_result: ProcessResult | None = None
        self.fetch_result: ProcessResult | None = None
        self.mutate: Callable[[dict[str, object]], dict[str, object]] | None = None

    def add(self, checkpoint: Checkpoint) -> None:
        self.checkpoints[checkpoint.identity.key] = checkpoint

    def __call__(self, command: fanout.ProcessCommand, **_kwargs: object) -> ProcessResult:
        self.commands.append(command)
        request = json.loads(command.stdin)
        if request["operation"] == "save":
            self.save_calls += 1
            if self.save_result is not None:
                return self.save_result
            checkpoint = next(
                item for item in self.checkpoints.values() if item.text == request["text"]
            )
            existing = next(
                (key for key, item in self.saved.items() if item.text == checkpoint.text),
                None,
            )
            observation_id = existing if existing is not None else self.next_id
            if existing is None:
                self.next_id += 1
                self.saved[observation_id] = checkpoint
            response = {
                "ok": True,
                "operation": "save",
                "result": {
                    "id": observation_id,
                    "message": f"Memory saved as observation #{observation_id}",
                    "project": checkpoint.project,
                    "success": True,
                    "title": checkpoint.title,
                },
                "schema_version": memory.CONTROLLER_SCHEMA,
            }
            return ProcessResult(ProcessStatus.EXIT, 0, canonical_json(response), b"")
        self.fetch_calls += 1
        if self.fetch_result is not None:
            return self.fetch_result
        rows = []
        for observation_id in request["ids"]:
            checkpoint = self.saved[observation_id]
            row = _observation(checkpoint, observation_id)
            rows.append(self.mutate(row) if self.mutate is not None else row)
        response = {
            "ok": True,
            "operation": "fetch",
            "result": rows,
            "schema_version": memory.CONTROLLER_SCHEMA,
        }
        return ProcessResult(ProcessStatus.EXIT, 0, canonical_json(response), b"")


def _exchange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint: Checkpoint):
    executable, digest = _controller_file(tmp_path)
    controller = MemoryController(executable, executable_sha256=digest, timeout=2)
    store = ArtifactStore(tmp_path / "artifacts")
    harness = _ControllerHarness(checkpoint)
    monkeypatch.setattr(memory, "run_command", harness)
    return MemoryCheckpointExchange(controller, store), store, harness


class _InjectedRecoveryCrash(BaseException):
    """Simulate process loss after one journal append has become durable."""


def _durable_publication(
    exchange: MemoryCheckpointExchange,
    journal: RawRunJournal,
    checkpoint: Checkpoint,
) -> CheckpointPublication:
    checkpoint_ref, intent_ref = exchange._persist_intent(checkpoint)
    journal.append(
        "publication-intent",
        task_id=checkpoint.identity.task_id,
        seat_id=checkpoint.identity.seat_id,
        attempt=checkpoint.identity.attempt,
        round=checkpoint.identity.round,
    )
    observation_id = exchange.controller.save(checkpoint)
    return exchange._persist_publication(
        checkpoint, observation_id, checkpoint_ref, intent_ref
    )


def _crash_after_append(
    monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    original = RawRunJournal.append
    crashed = False

    def append(self: RawRunJournal, selected: str, **kwargs: object) -> None:
        nonlocal crashed
        original(self, selected, **kwargs)
        if selected == event_type and not crashed:
            crashed = True
            raise _InjectedRecoveryCrash(event_type)

    monkeypatch.setattr(RawRunJournal, "append", append)


def _resume_journal(
    journal: RawRunJournal, owner: OwnerCapability
) -> tuple[RawRunJournal, OwnerCapability]:
    root = journal.root
    inputs = journal.inputs
    limits = journal.limits
    authority = journal._anchor_store
    recovered_owner = OwnerCapability.from_token(owner.export_token())
    journal.close()
    return (
        RawRunJournal._resume_for_test(
            root,
            inputs,
            recovered_owner,
            limits=limits,
            anchor_store=authority,
        ),
        recovered_owner,
    )


def test_checkpoint_identity_and_content_are_canonical_bounded_and_immutable() -> None:
    checkpoint = _checkpoint("answer α")

    assert checkpoint.identity.to_dict() == {
        "attempt": 1,
        "provider_session_id": "11111111-1111-4111-8111-111111111111",
        "round": 1,
        "run_id": "run-1",
        "seat_id": "claude",
        "task_id": "work",
    }
    assert checkpoint.digest == hashlib.sha256(checkpoint.payload_bytes).hexdigest()
    assert checkpoint.identity.key in checkpoint.title
    assert checkpoint.digest in checkpoint.title
    assert checkpoint.identity.key in checkpoint.text
    assert checkpoint.digest in checkpoint.text
    assert json.loads(checkpoint.text)["payload"] == json.loads(checkpoint.payload_bytes)
    assert checkpoint.project.startswith("llm-fanout-v1.")
    assert len(checkpoint.project.encode()) <= 512
    with pytest.raises(dataclasses.FrozenInstanceError):
        checkpoint.identity.run_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", ""),
        ("task_id", " padded "),
        ("seat_id", "bad\x00seat"),
        ("provider_session_id", "x" * 97),
        ("attempt", True),
        ("round", 0),
    ],
)
def test_checkpoint_identity_rejects_unsafe_or_boolean_fields(
    field: str, value: object
) -> None:
    with pytest.raises(MemoryValidationError):
        _identity(**{field: value})


def test_checkpoint_identity_from_dict_rejects_unknown_fields() -> None:
    value = _identity().to_dict()
    with pytest.raises(MemoryValidationError):
        CheckpointIdentity.from_dict({**value, "unknown": "drift"})


def test_controller_uses_only_pinned_executable_and_canonical_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint("private peer answer")
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        receipt = exchange.publish(checkpoint, journal=journal, owner=owner)
    finally:
        journal.close()

    assert receipt.observation_id == 41
    assert harness.save_calls == harness.fetch_calls == 1
    for command in harness.commands:
        assert tuple(command.argv) == (sys.executable, str(exchange.controller.executable))
        assert command.environment == {}
        assert "private peer answer" not in " ".join(command.argv)
        assert "private peer answer" not in repr(command)
        assert command.stdin == canonical_json(json.loads(command.stdin))
    assert json.loads(harness.commands[0].stdin) == {
        "operation": "save",
        "project": checkpoint.project,
        "schema_version": memory.CONTROLLER_SCHEMA,
        "text": checkpoint.text,
        "title": checkpoint.title,
    }
    assert json.loads(harness.commands[1].stdin) == {
        "ids": [41],
        "operation": "fetch",
        "schema_version": memory.CONTROLLER_SCHEMA,
    }


def test_controller_uses_current_python_without_ambient_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = sys.modules[f"{SPEC.name}.process"]
    open_slot_root = process._open_slot_root
    monkeypatch.setattr(
        process, "_open_slot_root",
        lambda root: open_slot_root(tmp_path / "slots" if root is None else root),
    )
    executable = tmp_path / "memory_exchange.py"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, tomllib\n"
        "assert 'PATH' not in os.environ\n"
        "request = json.load(sys.stdin)\n"
        "response = {'ok': True, 'operation': 'save', 'result': "
        "{'id': 41, 'message': 'Memory saved as observation #41', "
        "'project': request['project'], 'success': True, 'title': request['title']}, "
        "'schema_version': request['schema_version']}\n"
        "sys.stdout.write(json.dumps(response, sort_keys=True, separators=(',', ':')) + '\\n')\n"
    )
    executable.chmod(0o700)
    controller = MemoryController(
        executable, executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )

    assert controller.save(_checkpoint()) == 41


def test_publish_saves_exact_fetches_verifies_and_persists_reconstructable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint()
    exchange, store, _harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        receipt = exchange.publish(checkpoint, journal=journal, owner=owner)
        assert journal.state.seat_phase("work", "claude", 1, 1) == "checkpoint-verified"
    finally:
        journal.close()

    assert receipt.verified is True
    assert receipt.checkpoint_digest == checkpoint.digest
    assert receipt.checkpoint_key == checkpoint.identity.key
    assert store.read_bytes(receipt.checkpoint_ref) == checkpoint.text.encode()
    publication = CheckpointPublication.from_dict(
        store.read_json(receipt.publication_ref)
    )
    restored = CheckpointReceipt.from_dict(store.read_json(receipt.verification_ref))
    assert publication.observation_id == receipt.observation_id
    assert restored == receipt
    for ref in (receipt.intent_ref, receipt.publication_ref, receipt.verification_ref):
        assert checkpoint.answer.encode() not in store.read_bytes(ref)


def test_verified_replay_exact_fetches_without_saving_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint()
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        receipt = exchange.publish(checkpoint, journal=journal, owner=owner)
        replayed = exchange.verify_existing(checkpoint, receipt)
    finally:
        journal.close()

    assert replayed == receipt
    assert harness.save_calls == 1
    assert harness.fetch_calls == 2


def test_same_identity_with_a_different_digest_is_equivocation_before_a_second_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint("first")
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        exchange.publish(checkpoint, journal=journal, owner=owner)
        with pytest.raises(MemoryEquivocationError):
            exchange.publish(_checkpoint("second"), journal=journal, owner=owner)
    finally:
        journal.close()
    assert harness.save_calls == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: {**row, "project": "foreign"},
        lambda row: {**row, "memory_session_id": "manual-foreign-claude"},
        lambda row: {**row, "title": "tampered"},
        lambda row: {**row, "narrative": "tampered"},
        lambda row: {**row, "content_hash": "0" * 16},
        lambda row: {**row, "metadata": "{}"},
        lambda row: {**row, "id": True},
    ],
)
def test_foreign_or_tampered_observation_blocks_memory_and_never_retries_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    checkpoint = _checkpoint()
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    harness.mutate = mutate
    journal, owner = _journal(tmp_path)
    try:
        with pytest.raises(CheckpointBlockedError) as failure:
            exchange.publish(checkpoint, journal=journal, owner=owner)
        assert journal.state.task_phases["work"] == "blocked-memory"
    finally:
        journal.close()
    assert failure.value.publication is not None
    assert checkpoint.answer not in str(failure.value)
    assert checkpoint.answer not in repr(failure.value)
    assert harness.save_calls == 1


@pytest.mark.parametrize(
    "result",
    [
        ProcessResult(
            ProcessStatus.TIMEOUT,
            -15,
            b"",
            b"",
            timeout=TimeoutEvidence(1, True, False, True),
        ),
        ProcessResult(ProcessStatus.EXIT, 1, b'{"bad":"response"}\n', b""),
        ProcessResult(ProcessStatus.EXIT, 0, b"not-json", b""),
        ProcessResult(ProcessStatus.EXIT, 0, b"{}\n", b"contaminated"),
    ],
)
def test_ambiguous_save_failure_is_durably_blocked_and_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: ProcessResult,
) -> None:
    checkpoint = _checkpoint("secret checkpoint body")
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    harness.save_result = result
    journal, owner = _journal(tmp_path)
    try:
        with pytest.raises(CheckpointBlockedError) as failure:
            exchange.publish(checkpoint, journal=journal, owner=owner)
        assert failure.value.publication is None
        assert journal.state.task_phases["work"] == "blocked-memory"
    finally:
        journal.close()
    assert harness.save_calls == 1
    assert harness.fetch_calls == 0
    assert "secret checkpoint body" not in str(failure.value)
    assert "secret checkpoint body" not in repr(failure.value)


def test_controller_runner_crash_is_typed_blocked_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint("secret runner crash answer")
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)

    def crash(*_args: object, **_kwargs: object) -> ProcessResult:
        raise RuntimeError("secret runner crash answer")

    monkeypatch.setattr(memory, "run_command", crash)
    journal, owner = _journal(tmp_path)
    try:
        with pytest.raises(CheckpointBlockedError) as failure:
            exchange.publish(checkpoint, journal=journal, owner=owner)
        assert journal.state.task_phases["work"] == "blocked-memory"
    finally:
        journal.close()
    assert failure.value.publication is None
    assert harness.save_calls == 0
    assert "secret runner crash answer" not in str(failure.value)
    assert "secret runner crash answer" not in repr(failure.value)
    assert "secret runner crash answer" not in "".join(
        traceback.format_exception(failure.value)
    )


def test_recovery_exact_fetches_durable_publication_without_resaving_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint()
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    harness.mutate = lambda row: {**row, "narrative": "tampered during outage"}
    journal, owner = _journal(tmp_path)
    try:
        with pytest.raises(CheckpointBlockedError) as failure:
            exchange.publish(checkpoint, journal=journal, owner=owner)
        publication = failure.value.publication
        assert publication is not None
        harness.mutate = None
        receipt = exchange.recover(
            checkpoint, publication, journal=journal, owner=owner
        )
        assert journal.state.task_phases["work"] == "memory-recovered"
        assert journal.state.seat_phase("work", "claude", 1, 1) == "checkpoint-verified"
    finally:
        journal.close()

    assert receipt.verified
    assert harness.save_calls == 1
    assert harness.fetch_calls == 2


def test_crash_before_publication_intent_replays_only_local_artifacts_then_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint()
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        exchange._persist_intent(checkpoint)
        receipt = exchange.publish(checkpoint, journal=journal, owner=owner)
    finally:
        journal.close()
    assert receipt.verified
    assert harness.save_calls == 1


def test_commit_before_durable_observation_id_is_an_unrecoverable_owner_block_not_a_resave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint()
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        exchange._persist_intent(checkpoint)
        journal.append(
            "publication-intent",
            task_id="work",
            seat_id="claude",
            attempt=1,
            round=1,
        )
        assert exchange.controller.save(checkpoint) == 41
        with pytest.raises(CheckpointBlockedError) as failure:
            exchange.publish(checkpoint, journal=journal, owner=owner)
        assert failure.value.stage == "ambiguous-publication-intent"
        assert failure.value.publication is None
        assert journal.state.task_phases["work"] == "blocked-memory"
    finally:
        journal.close()
    assert harness.save_calls == 1
    assert harness.fetch_calls == 0


@pytest.mark.parametrize("boundary", ["receipt", "published", "verified"])
def test_durable_observation_id_recovers_each_later_crash_boundary_without_resave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    checkpoint = _checkpoint()
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        checkpoint_ref, intent_ref = exchange._persist_intent(checkpoint)
        journal.append(
            "publication-intent",
            task_id="work",
            seat_id="claude",
            attempt=1,
            round=1,
        )
        observation_id = exchange.controller.save(checkpoint)
        publication = exchange._persist_publication(
            checkpoint, observation_id, checkpoint_ref, intent_ref
        )
        if boundary in {"published", "verified"}:
            journal.append(
                "checkpoint-published",
                task_id="work",
                seat_id="claude",
                attempt=1,
                round=1,
            )
        if boundary == "verified":
            observation = exchange.controller.fetch((observation_id,))[0]
            exchange._verify_and_persist(checkpoint, publication, observation)

        receipt = exchange.recover(
            checkpoint, publication, journal=journal, owner=owner
        )
        assert journal.state.task_phases["work"] == "memory-recovered"
        assert journal.state.seat_phase("work", "claude", 1, 1) == "checkpoint-verified"
    finally:
        journal.close()
    assert receipt.verified
    assert harness.save_calls == 1
    assert harness.fetch_calls == (2 if boundary == "verified" else 1)


@pytest.mark.parametrize(
    "event_type",
    [
        "blocked-memory",
        "checkpoint-published",
        "checkpoint-verified",
        "memory-recovered",
    ],
)
def test_recovery_resumes_after_crash_following_each_durable_journal_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    checkpoint = _checkpoint()
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        publication = _durable_publication(exchange, journal, checkpoint)
        _crash_after_append(monkeypatch, event_type)
        with pytest.raises(_InjectedRecoveryCrash):
            exchange.recover(
                checkpoint, publication, journal=journal, owner=owner
            )
        journal, owner = _resume_journal(journal, owner)

        receipt = exchange.recover(
            checkpoint, publication, journal=journal, owner=owner
        )
        assert receipt.verified
        assert journal.state.task_phases["work"] == "memory-recovered"
        assert journal.state.seat_phase("work", "claude", 1, 1) == "checkpoint-verified"
    finally:
        journal.close()
    assert harness.save_calls == 1


def test_completed_recovery_is_idempotently_revalidated_without_journal_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint()
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        publication = _durable_publication(exchange, journal, checkpoint)
        first = exchange.recover(
            checkpoint, publication, journal=journal, owner=owner
        )
        sequence = journal.state.seq

        second = exchange.recover(
            checkpoint, publication, journal=journal, owner=owner
        )
        assert second == first
        assert journal.state.seq == sequence
        assert journal.state.task_phases["work"] == "memory-recovered"
        assert journal.state.seat_phase("work", "claude", 1, 1) == "checkpoint-verified"
    finally:
        journal.close()
    assert harness.save_calls == 1
    assert harness.fetch_calls == 2


def test_two_durable_seats_recover_serially_without_resave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _checkpoint(seat_id="claude")
    codex = _checkpoint(seat_id="codex", provider_session_id="codex-thread-1")
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, claude)
    harness.add(codex)
    journal, owner = _journal(tmp_path, seats=("claude", "codex"))
    try:
        claude_publication = _durable_publication(exchange, journal, claude)
        codex_publication = _durable_publication(exchange, journal, codex)

        claude_receipt = exchange.recover(
            claude, claude_publication, journal=journal, owner=owner
        )
        codex_receipt = exchange.recover(
            codex, codex_publication, journal=journal, owner=owner
        )

        assert claude_receipt.observation_id == 41
        assert codex_receipt.observation_id == 42
        assert journal.state.task_phases["work"] == "memory-recovered"
        assert journal.state.seat_phase("work", "claude", 1, 1) == "checkpoint-verified"
        assert journal.state.seat_phase("work", "codex", 1, 1) == "checkpoint-verified"
    finally:
        journal.close()
    assert harness.save_calls == 2
    assert harness.fetch_calls == 2


def test_second_seat_resumes_after_crash_following_reentry_block_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _checkpoint(seat_id="claude")
    codex = _checkpoint(seat_id="codex", provider_session_id="codex-thread-1")
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, claude)
    harness.add(codex)
    journal, owner = _journal(tmp_path, seats=("claude", "codex"))
    try:
        claude_publication = _durable_publication(exchange, journal, claude)
        codex_publication = _durable_publication(exchange, journal, codex)
        exchange.recover(
            claude, claude_publication, journal=journal, owner=owner
        )
        assert journal.state.task_phases["work"] == "memory-recovered"

        _crash_after_append(monkeypatch, "blocked-memory")
        with pytest.raises(_InjectedRecoveryCrash):
            exchange.recover(
                codex, codex_publication, journal=journal, owner=owner
            )
        assert journal.state.task_phases["work"] == "blocked-memory"
        journal, owner = _resume_journal(journal, owner)

        receipt = exchange.recover(
            codex, codex_publication, journal=journal, owner=owner
        )
        assert receipt.observation_id == 42
        assert journal.state.task_phases["work"] == "memory-recovered"
        assert journal.state.seat_phase("work", "codex", 1, 1) == "checkpoint-verified"
    finally:
        journal.close()
    assert harness.save_calls == 2
    assert harness.fetch_calls == 2


def test_verified_seat_cannot_clear_another_seats_active_recovery_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _checkpoint(seat_id="claude")
    codex = _checkpoint(seat_id="codex", provider_session_id="codex-thread-1")
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, claude)
    harness.add(codex)
    journal, owner = _journal(tmp_path, seats=("claude", "codex"))
    try:
        claude_publication = _durable_publication(exchange, journal, claude)
        codex_publication = _durable_publication(exchange, journal, codex)
        claude_receipt = exchange.recover(
            claude, claude_publication, journal=journal, owner=owner
        )

        _crash_after_append(monkeypatch, "blocked-memory")
        with pytest.raises(_InjectedRecoveryCrash):
            exchange.recover(
                codex, codex_publication, journal=journal, owner=owner
            )
        journal, owner = _resume_journal(journal, owner)
        blocked_sequence = journal.state.seq

        assert (
            exchange.recover(
                claude, claude_publication, journal=journal, owner=owner
            )
            == claude_receipt
        )
        assert journal.state.seq == blocked_sequence
        assert journal.state.task_phases["work"] == "blocked-memory"
        assert journal.state.seat_phase("work", "codex", 1, 1) == "publication-intent"
        with pytest.raises(RunStateError, match="illegal task transition"):
            journal.append("reconciliation-pending", task_id="work", owner=owner)

        codex_receipt = exchange.recover(
            codex, codex_publication, journal=journal, owner=owner
        )
        assert codex_receipt.observation_id == 42
        journal.append("reconciliation-pending", task_id="work", owner=owner)
        assert journal.state.task_phases["work"] == "reconciliation-pending"
    finally:
        journal.close()
    assert harness.save_calls == 2
    assert harness.fetch_calls == 3


def test_fetch_batch_keeps_requested_receipt_order_and_rejects_wrong_association(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _checkpoint(seat_id="claude")
    second = _checkpoint(
        seat_id="codex", provider_session_id="codex-thread-1"
    )
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, first)
    harness.add(second)
    journal, owner = _journal(tmp_path, seats=("claude", "codex"))
    try:
        first_receipt = exchange.publish(first, journal=journal, owner=owner)
        second_receipt = exchange.publish(second, journal=journal, owner=owner)
    finally:
        journal.close()

    results = exchange.fetch_verified(
        ((second, second_receipt), (first, first_receipt))
    )
    assert [item.observation_id for item in results] == [42, 41]
    assert json.loads(harness.commands[-1].stdin)["ids"] == [42, 41]

    response = {
        "ok": True,
        "operation": "fetch",
        "result": [
            _observation(first, 41),
            _observation(second, 42),
        ],
        "schema_version": memory.CONTROLLER_SCHEMA,
    }
    harness.fetch_result = ProcessResult(
        ProcessStatus.EXIT, 0, canonical_json(response), b""
    )
    with pytest.raises(MemoryProtocolError):
        exchange.fetch_verified(
            ((second, second_receipt), (first, first_receipt))
        )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"id": 41}, {"id": 41}],
        [{"id": 42}],
    ],
)
def test_controller_rejects_missing_duplicate_or_foreign_fetch_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
) -> None:
    checkpoint = _checkpoint()
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    response = {
        "ok": True,
        "operation": "fetch",
        "result": rows,
        "schema_version": memory.CONTROLLER_SCHEMA,
    }
    harness.fetch_result = ProcessResult(
        ProcessStatus.EXIT, 0, canonical_json(response), b""
    )
    with pytest.raises(MemoryProtocolError):
        exchange.controller.fetch((41,))


def test_artifact_conflict_substitution_and_quota_fail_closed_without_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint("answer larger than quota")
    executable, digest = _controller_file(tmp_path)
    controller = MemoryController(executable, executable_sha256=digest)
    store = ArtifactStore(
        tmp_path / "tiny",
        limits=ArtifactLimits(max_file_bytes=16, max_total_bytes=64),
    )
    harness = _ControllerHarness(checkpoint)
    monkeypatch.setattr(memory, "run_command", harness)
    journal, owner = _journal(tmp_path)
    try:
        with pytest.raises(MemoryDurabilityError):
            MemoryCheckpointExchange(controller, store).publish(
                checkpoint, journal=journal, owner=owner
            )
    finally:
        journal.close()
    assert harness.save_calls == 0

    exchange, store, harness = _exchange(tmp_path / "substitution", monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path / "substitution")
    try:
        receipt = exchange.publish(checkpoint, journal=journal, owner=owner)
        path = store.root / receipt.checkpoint_ref.path
        path.write_bytes(b"substituted")
        with pytest.raises(MemoryDurabilityError):
            exchange.verify_existing(checkpoint, receipt)
    finally:
        journal.close()


@pytest.mark.parametrize("mutation", ["mode", "hardlink", "directory-mode"])
def test_artifact_owner_private_mode_and_single_link_are_rechecked_on_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    checkpoint = _checkpoint()
    exchange, store, _harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        receipt = exchange.publish(checkpoint, journal=journal, owner=owner)
        path = store.root / receipt.checkpoint_ref.path
        changed_directory: Path | None = None
        if mutation == "mode":
            path.chmod(0o644)
        elif mutation == "hardlink":
            os.link(path, store.root / "checkpoint-hardlink")
        else:
            changed_directory = path.parent
            changed_directory.chmod(0o755)
        try:
            with pytest.raises(MemoryDurabilityError):
                exchange.verify_existing(checkpoint, receipt)
        finally:
            path.chmod(0o600)
            if changed_directory is not None:
                changed_directory.chmod(0o700)
    finally:
        journal.close()


@pytest.mark.parametrize("mode", [0o600, 0o755])
def test_controller_rejects_wrong_mode_or_digest(tmp_path: Path, mode: int) -> None:
    path, digest = _controller_file(tmp_path)
    path.chmod(mode)
    with pytest.raises(MemoryValidationError):
        MemoryController(path, executable_sha256=digest)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable-parent"])
def test_controller_rejects_replaceable_or_multiply_linked_identity(
    tmp_path: Path, kind: str
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "target.py"
    target.write_bytes(b"#!/usr/bin/env python3\nraise SystemExit(0)\n")
    target.chmod(0o700)
    controller = private / "memory_exchange.py"
    if kind == "symlink":
        controller.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, controller)
    else:
        controller.write_bytes(target.read_bytes())
        controller.chmod(0o700)
        private.chmod(0o777)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    try:
        with pytest.raises(MemoryValidationError):
            MemoryController(controller, executable_sha256=digest)
    finally:
        private.chmod(0o700)


def test_publication_and_receipt_schemas_reject_unknown_mutable_or_boolean_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint()
    exchange, _store, _harness = _exchange(tmp_path, monkeypatch, checkpoint)
    journal, owner = _journal(tmp_path)
    try:
        receipt = exchange.publish(checkpoint, journal=journal, owner=owner)
    finally:
        journal.close()
    publication = receipt.publication
    with pytest.raises(MemoryValidationError):
        CheckpointPublication.from_dict(
            {**publication.to_dict(), "observation_id": True}
        )
    with pytest.raises(MemoryValidationError):
        CheckpointReceipt.from_dict({**receipt.to_dict(), "unknown": []})
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.verified = False  # type: ignore[misc]


def test_error_and_repr_surfaces_redact_checkpoint_and_controller_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _checkpoint("peer secret answer")
    exchange, _store, harness = _exchange(tmp_path, monkeypatch, checkpoint)
    harness.save_result = ProcessResult(
        ProcessStatus.EXIT,
        1,
        canonical_json(
            {
                "error": {"code": "gateway-unavailable", "message": "memory exchange failed"},
                "ok": False,
                "schema_version": memory.CONTROLLER_SCHEMA,
            }
        ),
        b"peer secret answer echoed",
    )
    journal, owner = _journal(tmp_path)
    try:
        with pytest.raises(CheckpointBlockedError) as failure:
            exchange.publish(checkpoint, journal=journal, owner=owner)
    finally:
        journal.close()
    rendered = str(failure.value) + repr(failure.value) + repr(exchange.controller)
    assert "peer secret answer" not in rendered
    assert "<redacted>" in repr(failure.value)


def test_no_search_context_or_network_fallback_surface_exists() -> None:
    source = (FANOUT_ROOT / "memory.py").read_text(encoding="utf-8")
    assert "/api/search" not in source
    assert "/context/inject" not in source
    assert "semantic" not in source.lower()
    assert not hasattr(MemoryController, "search")
