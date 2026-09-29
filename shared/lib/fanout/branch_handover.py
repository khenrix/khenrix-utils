"""Journal-first local Git handover for one verified repository target."""
from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from .artifacts import ArtifactExistsError, ArtifactRef, ArtifactStore, canonical_json
from .controller import LifecycleController, read_evidence, write_evidence
from .errors import HandoverError
from .lifecycle import (
    HandoverTransaction, _PinnedDestination, _destination_lock,
    _controller_transaction_root, _recovery_state,
    handover_candidate, prepare_handover_transaction,
)
from .plan import FanoutPlanV2
from .repo import _capture_directory_entries, capture_repository_baseline
from .runstate import BranchHandoverIntentV2, HandoverTerminalV2, OwnerCapability, RunInputs, RunJournal
from .scheduler_authority import Scheduler
from .targets import TargetBinding, _git_environment, resolve_target
from .verification import TargetEvidenceEnvelope, load_target_candidate, load_target_verification
from .verification import _assert_candidate_applied, _read_entry


_ZERO_OID = "0" * 40
_PROOF_SCHEMA = "fanout-branch-git-proof-v1"
_MODES_SCHEMA = "fanout-branch-worktree-modes-v1"
_ASSOCIATION_CATEGORY = "branch-handover-association"
_COMMIT_CATEGORY = "branch-handover-prepared-commit"
_PREPARED_ISSUER = object()


def _checkpoint(_phase: str) -> None:
    """A no-op seam for deterministic process interruption tests."""


@dataclass(frozen=True, slots=True)
class PreparedBranchHandover:
    """Same-process capability for the one associated lifecycle transaction."""

    intent: BranchHandoverIntentV2
    transaction: HandoverTransaction
    binding: TargetBinding
    expected_head_ref: str
    _owner: OwnerCapability
    _issuer: object

    def __post_init__(self) -> None:
        if self._issuer is not _PREPARED_ISSUER:
            raise HandoverError("prepared branch handover is not controller-issued")

    def __reduce__(self):
        raise TypeError("prepared branch handovers cannot be serialized")


@dataclass(frozen=True, slots=True)
class BlockedHandover:
    intent_sha256: str
    reason: str


def _git_env() -> dict[str, str]:
    environment = _git_environment()
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def _git(root: Path, *args: str, input_bytes: bytes | None = None,
         strip: bool = True) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(root), "-c", "core.fsmonitor=false",
         "-c", "core.hooksPath=/dev/null", *args), input=input_bytes,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env(), check=False,
    )
    if result.returncode:
        raise HandoverError(f"Git {args[0]} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout.strip() if strip else result.stdout


def _optional_ref(root: Path, ref: str) -> str | None:
    direct = subprocess.run(
        ("git", "-C", str(root), "-c", "core.fsmonitor=false",
         "-c", "core.hooksPath=/dev/null", "symbolic-ref", "-q", "--no-recurse", ref),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env(), check=False,
    )
    if direct.returncode == 0:
        raise HandoverError("branch ref is symbolic")
    if direct.returncode != 1 or direct.stderr:
        raise HandoverError("branch ref type cannot be read")
    result = subprocess.run(
        ("git", "-C", str(root), "-c", "core.fsmonitor=false",
         "-c", "core.hooksPath=/dev/null", "rev-parse", "-q", "--verify", ref),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env(), check=False,
    )
    if result.returncode == 1 and not result.stderr:
        return None
    if result.returncode:
        raise HandoverError("branch ref cannot be read")
    return result.stdout.decode("ascii").strip()


def _index_digest(root: Path) -> str:
    index = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-path", "index").decode())
    try:
        return hashlib.sha256(index.read_bytes()).hexdigest()
    except OSError as error:
        raise HandoverError("Git index is unavailable") from error


def _git_entry_map(root: Path, *args: str) -> dict[str, tuple[int, str]]:
    entries: dict[str, tuple[int, str]] = {}
    staged = args[0] == "ls-files"
    for record in filter(None, _git(root, *args, strip=False).split(b"\0")):
        metadata, separator, encoded_path = record.partition(b"\t")
        fields = metadata.split()
        if (not separator or len(fields) != 3
                or (staged and fields[2] != b"0")
                or (not staged and fields[1] != b"blob")):
            raise HandoverError("Git tree or index has an unsupported entry")
        try:
            path = encoded_path.decode("utf-8", "strict")
            mode = int(fields[0], 8)
            oid = (fields[1] if staged else fields[2]).decode("ascii", "strict")
        except (UnicodeError, ValueError) as error:
            raise HandoverError("Git tree or index entry is malformed") from error
        if path in entries or len(oid) != 40 or any(char not in "0123456789abcdef" for char in oid):
            raise HandoverError("Git tree or index entry is duplicated or malformed")
        entries[path] = (mode, oid)
    return entries


def _raw_blob_oid(root: Path, data: bytes, *, write: bool) -> str:
    args = ("hash-object", *( ("-w",) if write else ()), "--stdin", "--no-filters")
    return _git(root, *args, input_bytes=data).decode("ascii")


def _candidate_tree(root: Path, baseline, candidate) -> tuple[str, dict[str, tuple[int, str]]]:
    original = _git_entry_map(root, "ls-tree", "-rz", "--full-tree", baseline.head)
    if original != {
        entry.path: (entry.mode, _raw_blob_oid(root, entry.data, write=False))
        for entry in baseline.head_entries
    }:
        raise HandoverError("Git base tree differs from pinned baseline")
    expected = dict(original)
    for path in candidate.deleted_paths:
        removed = tuple(item for item in expected if item == path or item.startswith(path + "/"))
        if not removed:
            raise HandoverError("candidate deletion has no Git representation")
        for item in removed:
            _git(root, "update-index", "--force-remove", "--", item)
            del expected[item]
    baseline_directories = {item.path: item.mode for item in baseline.directories}
    for entry in candidate.entries:
        if entry.kind == "directory":
            continue
        if entry.kind == "file" and entry.mode in {0o644, 0o755}:
            mode = 0o100755 if entry.mode == 0o755 else 0o100644
        elif entry.kind == "symlink" and entry.mode == 0o777:
            mode = 0o120000
        else:
            raise HandoverError("candidate entry mode has no Git representation")
        oid = _raw_blob_oid(root, entry.data, write=True)
        _git(root, "update-index", "--add", "--cacheinfo", f"{mode:o},{oid},{entry.path}")
        expected[entry.path] = (mode, oid)
    for entry in candidate.entries:
        if entry.kind == "directory" and (
            entry.path in baseline_directories
            or entry.mode != 0o755
            or not any(path.startswith(entry.path + "/") for path in expected)
        ):
            raise HandoverError("candidate directory has no Git representation")
    if (candidate.entries or candidate.deleted_paths) and expected == original:
        raise HandoverError("candidate has no Git tree change")
    tree = _git(root, "write-tree").decode("ascii")
    _assert_staged_tree(root, tree, expected)
    return tree, expected


def _assert_staged_tree(root: Path, tree: str,
                        expected: dict[str, tuple[int, str]]) -> None:
    if (_git_entry_map(root, "ls-files", "--stage", "-z") != expected
            or _git_entry_map(root, "ls-tree", "-rz", "--full-tree", tree) != expected
            or _git(root, "write-tree").decode("ascii") != tree):
        raise HandoverError("Git staged tree differs from verified candidate")


def _candidate_worktree_modes(baseline, candidate,
                              expected_tree: Mapping[str, tuple[int, str]]) -> dict[str, int]:
    original = {entry.path: entry.mode for entry in baseline.entries}
    changed = {entry.path: entry.mode for entry in candidate.entries if entry.kind != "directory"}
    if any(path not in original and path not in changed for path in expected_tree):
        raise HandoverError("Git tree lacks pinned worktree mode evidence")
    return {path: changed[path] if path in changed else original[path] for path in expected_tree}


def _candidate_directory_modes(baseline, candidate,
                               expected_tree: Mapping[str, tuple[int, str]]) -> dict[str, int]:
    modes = {entry.path: entry.mode for entry in baseline.directories}
    for deleted in candidate.deleted_paths:
        for path in tuple(modes):
            if path == deleted or path.startswith(deleted + "/"):
                del modes[path]
    modes.update({entry.path: entry.mode for entry in candidate.entries if entry.kind == "directory"})
    required = {
        path.rsplit("/", depth)[0]
        for path in expected_tree for depth in range(1, path.count("/") + 1)
    }
    if not required <= modes.keys():
        raise HandoverError("Git tree lacks pinned directory mode evidence")
    return modes


def _clean(root: Path, *, exact_modes: Mapping[str, int] | None = None,
           directory_modes: Mapping[str, int] | None = None) -> None:
    tree = _git(root, "rev-parse", "HEAD^{tree}").decode("ascii")
    if _git_entry_map(root, "ls-files", "--stage", "-z") != _git_entry_map(
        root, "ls-tree", "-rz", "--full-tree", tree,
    ):
        raise HandoverError("target checkout is dirty")
    _assert_raw_checkout(root, tree, exact_modes=exact_modes,
                         directory_modes=directory_modes)


def _assert_raw_checkout(root: Path, tree: str, *,
                         exact_modes: Mapping[str, int] | None = None,
                         directory_modes: Mapping[str, int] | None = None) -> None:
    expected = _git_entry_map(root, "ls-tree", "-rz", "--full-tree", tree)
    if _git_entry_map(root, "ls-files", "--stage", "-z") != expected:
        raise HandoverError("target index differs from exact Git tree")
    if exact_modes is not None and set(exact_modes) != set(expected):
        raise HandoverError("target worktree mode evidence differs from exact Git tree")
    for path, (mode, oid) in expected.items():
        try:
            entry = _read_entry(root, path)
        except Exception as error:
            raise HandoverError("target worktree entry is unsafe") from error
        if (entry is None
                or (mode == 0o120000 and entry.kind != "symlink")
                or (mode in {0o100644, 0o100755} and entry.kind != "file")
                or mode not in {0o120000, 0o100644, 0o100755}
                or (mode == 0o100644 and entry.mode & 0o111)
                or (mode == 0o100755 and not entry.mode & 0o111)
                or (exact_modes is not None and entry.mode != exact_modes[path])
                or _raw_blob_oid(root, entry.data, write=False) != oid):
            raise HandoverError("target worktree is dirty or differs from exact Git tree")
    if directory_modes is not None:
        try:
            actual_directories = {entry.path: entry.mode for entry in _capture_directory_entries(root)}
        except Exception as error:
            raise HandoverError("target worktree directory is unsafe") from error
        if actual_directories != directory_modes:
            raise HandoverError("target worktree directory set or mode differs from pinned evidence")
    if _git(root, "ls-files", "--others", "--exclude-standard", "-z"):
        raise HandoverError("target checkout has untracked files")


def _head_ref(root: Path) -> str:
    return _git(root, "symbolic-ref", "--no-recurse", "HEAD").decode("ascii")


@contextmanager
def _handover_lock(binding: TargetBinding) -> Iterator[_PinnedDestination]:
    common = binding.common_dir
    try:
        descriptor = os.open(common, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise HandoverError("target common-dir lock is unavailable") from error
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (binding.common_device, binding.common_inode):
            raise HandoverError("target common-dir identity changed")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with _destination_lock(binding.root) as pinned:
            yield pinned
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _target_baseline(binding: TargetBinding, *, clean: bool,
                     expected_head_ref: str | None = None) -> object:
    current = resolve_target(binding.spec, binding.root)
    if (current.root != binding.root or current.common_dir != binding.common_dir
            or (current.common_device, current.common_inode) != (binding.common_device, binding.common_inode)
            or current.base_oid != binding.base_oid or current.branch_oid != binding.branch_oid):
        raise HandoverError("target base or branch OID changed")
    if current.head_ref != binding.head_ref:
        raise HandoverError("target HEAD branch changed")
    admitted_ticket_ref = binding.spec.branch_ref if binding.branch_oid else None
    head_ref = _head_ref(binding.root)
    if expected_head_ref is not None and head_ref != expected_head_ref:
        raise HandoverError("target HEAD branch changed")
    if (head_ref == binding.spec.branch_ref) != (admitted_ticket_ref is not None):
        raise HandoverError("target HEAD branch changed")
    if clean:
        _clean(binding.root)
    baseline = capture_repository_baseline(binding.root)
    if baseline.digest != binding.baseline_sha256:
        raise HandoverError("target baseline changed")
    return baseline


def _envelopes(intent: BranchHandoverIntentV2, binding: TargetBinding,
               plan: FanoutPlanV2, inputs: RunInputs, artifacts: ArtifactStore,
               controller: LifecycleController):
    candidate_envelope = TargetEvidenceEnvelope(
        intent.run_id, intent.task_id, intent.target_id, intent.repository,
        intent.branch_ref, intent.base_oid, binding.baseline_sha256,
        "candidate", intent.candidate_ref,
    )
    verification_envelope = TargetEvidenceEnvelope(
        intent.run_id, intent.task_id, intent.target_id, intent.repository,
        intent.branch_ref, intent.base_oid, binding.baseline_sha256,
        "verification", intent.verification_ref,
    )
    candidate = load_target_candidate(
        candidate_envelope, plan=plan, inputs=inputs, store=artifacts, controller=controller,
    ).candidate
    verification = load_target_verification(
        verification_envelope, plan=plan, inputs=inputs, store=artifacts, controller=controller,
    )
    wrapper = artifacts.read_json(intent.verification_ref)
    candidate_ref = {"path": intent.candidate_ref.path, "digest": intent.candidate_ref.digest,
                     "size": intent.candidate_ref.size}
    if (not isinstance(wrapper, dict) or wrapper.get("candidate_evidence") != candidate_ref
            or not verification.valid or verification.candidate_digest != candidate.digest):
        raise HandoverError("verification wrapper differs from selected candidate issuance")
    return candidate, verification


def _source(scheduler: Scheduler, task_id: str, intent: BranchHandoverIntentV2):
    result = scheduler.handover_source_for(task_id)
    if (intent.run_id != scheduler.inputs.run_id
            or intent.plan_revision != result.plan_revision
            or intent.plan_sha256 != result.plan_sha256
            or intent.inputs_digest != result.inputs_digest
            or result.artifact.digest != _envelope_candidate_digest(scheduler, intent)):
        raise HandoverError("settled scheduler source changed")
    return result


def _envelope_candidate_digest(scheduler: Scheduler, intent: BranchHandoverIntentV2) -> str:
    binding = scheduler.inputs.targets[intent.target_id]
    envelope = TargetEvidenceEnvelope(
        intent.run_id, intent.task_id, intent.target_id, intent.repository,
        intent.branch_ref, intent.base_oid, binding.baseline_sha256,
        "candidate", intent.candidate_ref,
    )
    return load_target_candidate(
        envelope, plan=scheduler.plan, inputs=scheduler.inputs,
        store=scheduler.artifacts, controller=scheduler._lifecycle_controller,
    ).candidate.digest


def _record(journal: RunJournal, task_id: str, kind: str,
            controller: LifecycleController) -> dict[str, object] | None:
    ref = journal.branch_record_for(task_id, kind)
    if ref is None:
        return None
    category = _ASSOCIATION_CATEGORY if kind == "association" else _COMMIT_CATEGORY
    return read_evidence(controller, category, ref[0], ref[1])


def _association(intent: BranchHandoverIntentV2, transaction: HandoverTransaction,
                 expected_head_ref: str) -> dict[str, object]:
    return {"intent_sha256": intent.sha256, "transaction_root": str(transaction.transaction_root),
            "transaction_sha256": transaction.transaction_sha256,
            "baseline_sha256": transaction.baseline_digest,
            "candidate_sha256": transaction.candidate_digest,
            "task_id": intent.task_id, "expected_head_ref": expected_head_ref}


def _require_association(intent: BranchHandoverIntentV2, journal: RunJournal,
                         controller: LifecycleController, transaction: HandoverTransaction | None = None,
                         expected_head_ref: str | None = None):
    record = _record(journal, intent.task_id, "association", controller)
    if record is None:
        raise HandoverError("lifecycle association is missing")
    if (set(record) != {"intent_sha256", "transaction_root", "transaction_sha256",
                        "baseline_sha256", "candidate_sha256", "task_id", "expected_head_ref"}
            or record.get("intent_sha256") != intent.sha256 or record.get("task_id") != intent.task_id
            or record.get("baseline_sha256") != controller_baseline(intent, journal)
            or not isinstance(record.get("transaction_root"), str)
            or not isinstance(record.get("expected_head_ref"), str)
            or not record["expected_head_ref"].startswith("refs/heads/")
            or (record["expected_head_ref"] == intent.branch_ref) != (intent.old_ref_oid != _ZERO_OID)):
        raise HandoverError("lifecycle association changed")
    if transaction is not None and record != _association(intent, transaction, expected_head_ref):
        raise HandoverError("lifecycle association differs from in-memory transaction")
    root = _controller_transaction_root(controller, record["transaction_root"])
    state = _recovery_state(controller, root)
    binding = journal.inputs.targets[intent.target_id]
    if (state.transaction_sha256 != record["transaction_sha256"]
            or state.baseline_digest != record["baseline_sha256"]
            or state.candidate_digest != record["candidate_sha256"]
            or state.task_id != intent.task_id or state.destination != binding.root):
        raise HandoverError("lifecycle association differs from exact transaction")
    return state


def _associated_head_ref(intent: BranchHandoverIntentV2, journal: RunJournal,
                         controller: LifecycleController) -> str:
    record = _record(journal, intent.task_id, "association", controller)
    if record is None or not isinstance(record.get("expected_head_ref"), str):
        raise HandoverError("lifecycle association lacks original HEAD")
    return record["expected_head_ref"]


def controller_baseline(intent: BranchHandoverIntentV2, journal: RunJournal) -> str:
    return journal.inputs.targets[intent.target_id].baseline_sha256


def _write_mode_evidence(intent: BranchHandoverIntentV2, artifacts: ArtifactStore,
                         worktree_modes: Mapping[str, int],
                         directory_modes: Mapping[str, int]) -> ArtifactRef:
    path = f"branch-prepared-modes/{intent.target_id}/{intent.sha256}.json"
    payload = {"schema_version": _MODES_SCHEMA, "intent_sha256": intent.sha256,
               "worktree_modes": dict(worktree_modes), "directory_modes": dict(directory_modes)}
    data = canonical_json(payload)
    try:
        return artifacts.write_bytes(path, data)
    except ArtifactExistsError:
        ref = ArtifactRef(path, hashlib.sha256(data).hexdigest(), len(data))
        if artifacts.read_bytes(ref) != data:
            raise HandoverError("prepared mode evidence differs")
        return ref


def _prepared_commit_receipt(intent: BranchHandoverIntentV2, journal: RunJournal,
                             controller: LifecycleController) -> dict[str, object]:
    record = _record(journal, intent.task_id, "prepared-commit", controller)
    if (record is None or set(record) != {"intent_sha256", "commit_oid", "parent_oid",
                                          "tree_oid", "candidate_sha256", "mode_evidence"}
            or record.get("intent_sha256") != intent.sha256
            or record.get("parent_oid") != intent.base_oid
            or any(not isinstance(record.get(key), str) or len(record[key]) != 40
                   or any(char not in "0123456789abcdef" for char in record[key])
                   for key in ("commit_oid", "parent_oid", "tree_oid"))):
        raise HandoverError("prepared commit OID is missing")
    return record


def _prepared_commit(intent: BranchHandoverIntentV2, journal: RunJournal,
                     controller: LifecycleController,
                     artifacts: ArtifactStore) -> dict[str, object]:
    record = _prepared_commit_receipt(intent, journal, controller)
    evidence = record["mode_evidence"]
    path = f"branch-prepared-modes/{intent.target_id}/{intent.sha256}.json"
    if (not isinstance(evidence, dict) or set(evidence) != {"path", "digest", "size"}
            or evidence.get("path") != path or not isinstance(evidence.get("digest"), str)
            or type(evidence.get("size")) is not int):
        raise HandoverError("prepared mode evidence reference is invalid")
    try:
        modes = artifacts.read_json(ArtifactRef(evidence["path"], evidence["digest"], evidence["size"]))
    except Exception as error:
        raise HandoverError("prepared mode evidence is unavailable") from error
    if (not isinstance(modes, dict) or set(modes) != {"schema_version", "intent_sha256",
                                                     "worktree_modes", "directory_modes"}
            or modes.get("schema_version") != _MODES_SCHEMA
            or modes.get("intent_sha256") != intent.sha256):
        raise HandoverError("prepared mode evidence differs from intent")
    for key in ("worktree_modes", "directory_modes"):
        value = modes[key]
        if (not isinstance(value, dict)
                or any(not isinstance(name, str) or type(mode) is not int or not 0 <= mode <= 0o777
                       for name, mode in value.items())):
            raise HandoverError("prepared mode evidence is malformed")
    return {**record, "worktree_modes": modes["worktree_modes"],
            "directory_modes": modes["directory_modes"]}


def _initial_git(intent: BranchHandoverIntentV2, binding: TargetBinding,
                 expected_head_ref: str):
    baseline = _target_baseline(binding, clean=True, expected_head_ref=expected_head_ref)
    if (_git(binding.root, "rev-parse", "HEAD").decode() != intent.expected_head_oid
            or _index_digest(binding.root) != intent.expected_index_sha256
            or _git(binding.root, "rev-parse", "HEAD^{tree}").decode() != intent.expected_tree_oid
            or baseline.digest != binding.baseline_sha256):
        raise HandoverError("target checkout changed since intent")
    return baseline


def prepare_branch_handover(
    binding: TargetBinding, candidate_envelope: TargetEvidenceEnvelope,
    verification_envelope: TargetEvidenceEnvelope, *, plan: FanoutPlanV2,
    inputs: RunInputs, artifacts: ArtifactStore, scheduler: Scheduler,
    controller: LifecycleController, journal: RunJournal, owner: OwnerCapability,
    task_id: str,
) -> PreparedBranchHandover:
    journal.authorize_owner(owner)
    if (not isinstance(binding, TargetBinding) or inputs.targets is None
            or binding != inputs.targets.get(binding.spec.id)
            or not isinstance(candidate_envelope, TargetEvidenceEnvelope)
            or not isinstance(verification_envelope, TargetEvidenceEnvelope)
            or scheduler._journal is not journal or scheduler._lifecycle_controller is not controller
            or scheduler.plan != plan or scheduler.inputs != inputs or scheduler.artifacts is not artifacts):
        raise HandoverError("handover requires exact target, controller, and envelopes")
    if (candidate_envelope.evidence_kind != "candidate"
            or verification_envelope.evidence_kind != "verification"
            or candidate_envelope.task_id != task_id or verification_envelope.task_id != task_id
            or candidate_envelope.target_id != binding.spec.id
            or verification_envelope.target_id != binding.spec.id):
        raise HandoverError("handover envelopes differ from selected target task")
    with _handover_lock(binding) as pinned:
        expected_head_ref = _head_ref(binding.root)
        baseline = _target_baseline(binding, clean=True, expected_head_ref=expected_head_ref)
        source = scheduler.handover_source_for(task_id)
        intent = BranchHandoverIntentV2(
            inputs.run_id, source.plan_revision, source.plan_sha256,
            source.inputs_digest, task_id, binding.spec.id, binding.spec.repository,
            binding.spec.branch_ref, binding.base_oid, binding.branch_oid or _ZERO_OID,
            candidate_envelope.payload, verification_envelope.payload,
            binding.base_oid, _index_digest(binding.root),
            _git(binding.root, "rev-parse", "HEAD^{tree}").decode(),
        )
        if journal.state.branch_handovers.get(task_id) is not None:
            raise HandoverError("branch handover intent already exists")
        candidate, verification = _envelopes(intent, binding, plan, inputs, artifacts, controller)
        result = _source(scheduler, task_id, intent)
        if candidate.digest != result.artifact.digest:
            raise HandoverError("reconciled result differs from selected candidate")
        journal.append_branch_intent(intent, owner=owner)
        try:
            _checkpoint("after-intent")
            transaction = prepare_handover_transaction(
                baseline, verification, controller=controller, task_id=task_id,
                locked_destination=pinned,
            )
            payload = _association(intent, transaction, expected_head_ref)
            name, digest = write_evidence(controller, _ASSOCIATION_CATEGORY, payload)
            journal.append_branch_record(task_id, "association", name, digest, owner=owner)
            _checkpoint("after-association")
            return PreparedBranchHandover(
                intent, transaction, binding, expected_head_ref, owner, _PREPARED_ISSUER,
            )
        except Exception:
            reason = ("association-missing" if journal.branch_record_for(task_id, "association") is None
                      else "pre-cas-interrupted")
            _block(intent, journal, owner, reason)
            raise


def _commit_message(intent: BranchHandoverIntentV2, candidate_digest: str,
                    binding: TargetBinding) -> bytes:
    return (f"{binding.spec.ticket_key}: apply verified {intent.task_id} candidate\n\n"
            f"Fanout-Run: {intent.run_id}\nFanout-Task: {intent.task_id}\n"
            f"Fanout-Target: {intent.target_id}\nFanout-Candidate: {candidate_digest}\n"
            f"Fanout-Base: {intent.base_oid}\n"
            f"Fanout-Baseline: {binding.baseline_sha256}\n").encode()


def _block(intent: BranchHandoverIntentV2, journal: RunJournal,
           owner: OwnerCapability, reason: str) -> BlockedHandover:
    if intent.task_id not in journal.state.branch_blocked:
        journal.append_branch_blocked(intent.task_id, reason, owner=owner)
    return BlockedHandover(intent.sha256, reason)


def deliver_branch_candidate(
    prepared: PreparedBranchHandover, *, plan: FanoutPlanV2,
    inputs: RunInputs, artifacts: ArtifactStore, scheduler: Scheduler,
    controller: LifecycleController, journal: RunJournal, owner: OwnerCapability,
) -> HandoverTerminalV2 | BlockedHandover:
    if not isinstance(prepared, PreparedBranchHandover) or prepared._owner is not owner:
        raise HandoverError("delivery requires the same-process prepared handover")
    intent, transaction, binding = prepared.intent, prepared.transaction, prepared.binding
    journal.authorize_owner(owner)
    if (inputs.targets is None or inputs.targets.get(intent.target_id) != binding
            or scheduler._journal is not journal or scheduler._lifecycle_controller is not controller
            or scheduler.plan != plan or scheduler.inputs != inputs or scheduler.artifacts is not artifacts):
        raise HandoverError("delivery authority differs from prepared handover")
    if journal.branch_handover_state(intent.task_id) != (intent, None):
        raise HandoverError("branch handover is completed or changed")
    blocked = journal.state.branch_blocked.get(intent.task_id)
    if blocked is not None:
        return BlockedHandover(*blocked)
    with ExitStack() as locks:
        try:
            pinned = locks.enter_context(_handover_lock(binding))
        except Exception:
            return _block(intent, journal, owner, "delivery-failed")
        try:
            state = _require_association(
                intent, journal, controller, transaction, prepared.expected_head_ref,
            )
            if state.status != "not-mutated":
                raise HandoverError("lifecycle transaction is not ready for application")
            baseline = _initial_git(intent, binding, prepared.expected_head_ref)
            candidate, verification = _envelopes(intent, binding, plan, inputs, artifacts, controller)
            _source(scheduler, intent.task_id, intent)
            disposition = handover_candidate(
                capture_repository_baseline(binding.root), verification,
                controller=controller, task_id=intent.task_id,
                transaction=transaction, locked_destination=pinned,
            )
            if disposition.status != "committed":
                raise HandoverError("lifecycle application was not committed")
            _checkpoint("after-file-application")
            _assert_candidate_applied(pinned.descriptor, candidate)
            candidate_files = {entry.path for entry in candidate.entries if entry.kind != "directory"}
            untracked = set(filter(None, _git(
                binding.root, "ls-files", "--others", "--exclude-standard", "-z",
            ).split(b"\0")))
            if not untracked <= {path.encode("utf-8") for path in candidate_files}:
                raise HandoverError("checkout contains a file outside candidate-owned paths")
            tree, expected_tree = _candidate_tree(binding.root, baseline, candidate)
            worktree_modes = _candidate_worktree_modes(baseline, candidate, expected_tree)
            directory_modes = _candidate_directory_modes(baseline, candidate, expected_tree)
            _assert_candidate_applied(pinned.descriptor, candidate)
            message = _commit_message(intent, candidate.digest, binding)
            oid = _git(binding.root, "commit-tree", tree, "-p", intent.base_oid,
                       input_bytes=message).decode("ascii")
            _checkpoint("after-commit-tree")
            mode_ref = _write_mode_evidence(intent, artifacts, worktree_modes, directory_modes)
            record = {"intent_sha256": intent.sha256, "commit_oid": oid,
                      "parent_oid": intent.base_oid, "tree_oid": tree,
                      "candidate_sha256": candidate.digest,
                      "mode_evidence": {"path": mode_ref.path, "digest": mode_ref.digest,
                                        "size": mode_ref.size}}
            name, digest = write_evidence(controller, _COMMIT_CATEGORY, record)
            journal.append_branch_record(intent.task_id, "prepared-commit", name, digest, owner=owner)
            _checkpoint("after-prepared-commit")
            record = _prepared_commit(intent, journal, controller, artifacts)
            state = _require_association(
                intent, journal, controller, transaction, prepared.expected_head_ref,
            )
            if state.status != "committed":
                raise HandoverError("lifecycle transaction did not commit")
            _envelopes(intent, binding, plan, inputs, artifacts, controller)
            _source(scheduler, intent.task_id, intent)
            _pre_cas_checkout(intent, binding, tree, prepared.expected_head_ref,
                              worktree_modes, directory_modes)
            _assert_candidate_applied(pinned.descriptor, candidate)
            _assert_staged_tree(binding.root, tree, expected_tree)
            _git(binding.root, "update-ref", "--no-deref", intent.branch_ref, oid, intent.old_ref_oid)
            _checkpoint("after-ref-cas")
            if intent.old_ref_oid == _ZERO_OID:
                if _head_ref(binding.root) != prepared.expected_head_ref:
                    raise HandoverError("target HEAD branch changed after ref CAS")
                _git(binding.root, "symbolic-ref", "HEAD", intent.branch_ref)
            _checkpoint("after-head-switch")
            return _finish(intent, binding, record, plan, inputs, artifacts,
                           scheduler, controller, journal, owner)
        except Exception as error:
            try:
                current_ref = _optional_ref(binding.root, intent.branch_ref)
            except Exception:
                return _block(intent, journal, owner, "ref-changed")
            try:
                prepared_commit = _prepared_commit_receipt(intent, journal, controller)
            except Exception:
                prepared_commit = None
            old_ref = None if intent.old_ref_oid == _ZERO_OID else intent.old_ref_oid
            if current_ref != old_ref:
                if prepared_commit is not None and current_ref == prepared_commit["commit_oid"]:
                    raise HandoverError("handover finalization requires exact recovery") from error
                return _block(intent, journal, owner, "ref-changed")
            if isinstance(error, HandoverError) and "branch OID" in str(error):
                reason = "ref-changed"
            else:
                reason = "delivery-failed"
            return _block(intent, journal, owner, reason)


def _pre_cas_checkout(intent: BranchHandoverIntentV2, binding: TargetBinding,
                      tree: str, expected_head_ref: str,
                      exact_modes: Mapping[str, int],
                      directory_modes: Mapping[str, int]) -> None:
    current = resolve_target(binding.spec, binding.root)
    if (current.common_dir != binding.common_dir
            or (current.common_device, current.common_inode) != (binding.common_device, binding.common_inode)
            or current.branch_oid != binding.branch_oid or current.base_oid != intent.base_oid
            or _head_ref(binding.root) != expected_head_ref):
        raise HandoverError("checkout or branch OID changed before CAS")
    _assert_raw_checkout(binding.root, tree, exact_modes=exact_modes,
                         directory_modes=directory_modes)


def _finish(intent, binding, record, plan, inputs, artifacts, scheduler, controller, journal, owner):
    proof = _proof_bytes(intent, binding, record)
    path = f"branch-proof/{intent.target_id}/{intent.sha256}.json"
    try:
        evidence = artifacts.write_bytes(path, proof)
    except ArtifactExistsError:
        evidence = ArtifactRef(path, hashlib.sha256(proof).hexdigest(), len(proof))
        if artifacts.read_bytes(evidence) != proof:
            raise HandoverError("Git proof artifact differs")
    unsigned = {"schema_version": "fanout-branch-handover-terminal-v2",
                "intent_sha256": intent.sha256, "commit_oid": record["commit_oid"],
                "evidence": {"path": evidence.path, "digest": evidence.digest, "size": evidence.size},
                "candidate_sha256": record["candidate_sha256"]}
    terminal = HandoverTerminalV2(intent.sha256, record["commit_oid"], evidence,
                                  record["candidate_sha256"], hashlib.sha256(canonical_json(unsigned)).hexdigest())
    if journal.branch_handover_state(intent.task_id)[1] is None:
        journal.append_branch_terminal(intent.task_id, terminal, owner=owner)
    _checkpoint("after-journal-terminal")
    scheduler.mark_handover_terminal(intent.task_id, terminal, owner=owner)
    _checkpoint("after-scheduler-cas")
    return terminal


def _proof_bytes(intent, binding, record) -> bytes:
    root = binding.root
    oid = record["commit_oid"]
    current = resolve_target(binding.spec, root)
    if _git(root, "cat-file", "-t", oid) != b"commit":
        raise HandoverError("Git prepared OID is not a commit")
    raw = _git(root, "cat-file", "-p", oid, strip=False)
    header, separator, message = raw.partition(b"\n\n")
    lines = header.split(b"\n")
    trees = [line[5:] for line in lines if line.startswith(b"tree ")]
    parents = [line[7:] for line in lines if line.startswith(b"parent ")]
    if (current.root != binding.root or current.common_dir != binding.common_dir
            or (current.common_device, current.common_inode) != (binding.common_device, binding.common_inode)
            or current.branch_oid != oid
            or _optional_ref(root, intent.branch_ref) != oid
            or _git(root, "rev-parse", "HEAD").decode() != oid
            or _head_ref(root) != intent.branch_ref
            or separator != b"\n\n"
            or trees != [record["tree_oid"].encode("ascii")]
            or parents != [intent.base_oid.encode("ascii")]
            or message != _commit_message(intent, record["candidate_sha256"], binding)):
        raise HandoverError("Git proof does not match exact prepared commit")
    _clean(root, exact_modes=record["worktree_modes"],
           directory_modes=record["directory_modes"])
    return canonical_json({"schema_version": _PROOF_SCHEMA,
                           "intent_sha256": intent.sha256, "target_id": intent.target_id,
                           "branch_ref": intent.branch_ref, "prepared_commit_oid": oid,
                           "commit_oid": oid, "parent_oid": intent.base_oid,
                           "tree_oid": record["tree_oid"],
                           "candidate_sha256": record["candidate_sha256"]})


def verify_git_handover_proof(intent: BranchHandoverIntentV2, terminal: HandoverTerminalV2,
                              *, inputs: RunInputs, artifacts: ArtifactStore,
                              controller: LifecycleController, journal: RunJournal) -> None:
    """Recheck exact prepared OID, final Git state, and dependency-safe bytes."""
    if terminal.intent_sha256 != intent.sha256:
        raise HandoverError("Git terminal differs from exact intent")
    binding = inputs.targets[intent.target_id]
    state = _require_association(intent, journal, controller)
    if state.status != "committed":
        raise HandoverError("lifecycle transaction did not commit")
    record = _prepared_commit(intent, journal, controller, artifacts)
    if record.get("commit_oid") != terminal.commit_oid or record.get("candidate_sha256") != terminal.candidate_sha256:
        raise HandoverError("Git proof differs from prepared commit")
    proof = _proof_bytes(intent, binding, record)
    if terminal.evidence.path != f"branch-proof/{intent.target_id}/{intent.sha256}.json":
        raise HandoverError("Git proof artifact path differs from intent")
    if artifacts.read_bytes(terminal.evidence) != proof:
        raise HandoverError("Git proof artifact differs from actual commit")


def recover_branch_handover(
    intent: BranchHandoverIntentV2, *, plan: FanoutPlanV2, inputs: RunInputs,
    artifacts: ArtifactStore, scheduler: Scheduler, controller: LifecycleController,
    journal: RunJournal, owner: OwnerCapability,
) -> HandoverTerminalV2 | BlockedHandover:
    journal.authorize_owner(owner)
    if (inputs.targets is None or scheduler._journal is not journal
            or scheduler._lifecycle_controller is not controller
            or scheduler.plan != plan or scheduler.inputs != inputs or scheduler.artifacts is not artifacts):
        raise HandoverError("recovery authority differs from handover intent")
    recorded, terminal = journal.branch_handover_state(intent.task_id)
    if recorded != intent:
        raise HandoverError("recovery intent differs from authenticated journal")
    binding = inputs.targets[intent.target_id]
    if intent.task_id in journal.state.branch_blocked:
        return BlockedHandover(*journal.state.branch_blocked[intent.task_id])
    with ExitStack() as locks:
        try:
            locks.enter_context(_handover_lock(binding))
        except Exception:
            return _block(intent, journal, owner, "delivery-failed")
        try:
            _require_association(intent, journal, controller)
        except Exception:
            return _block(intent, journal, owner, "association-missing")
        if terminal is not None:
            try:
                verify_git_handover_proof(intent, terminal, inputs=inputs, artifacts=artifacts,
                                          controller=controller, journal=journal)
            except Exception:
                return _block(intent, journal, owner, "git-proof-invalid")
            scheduler.mark_handover_terminal(intent.task_id, terminal, owner=owner)
            return terminal
        try:
            record = _prepared_commit(intent, journal, controller, artifacts)
        except Exception:
            try:
                current_ref = _optional_ref(binding.root, intent.branch_ref)
            except Exception:
                return _block(intent, journal, owner, "ref-changed")
            try:
                receipt = _prepared_commit_receipt(intent, journal, controller)
            except Exception:
                receipt = None
            old_ref = None if intent.old_ref_oid == _ZERO_OID else intent.old_ref_oid
            reason = ("git-proof-invalid" if receipt is not None and current_ref == receipt["commit_oid"]
                      else "pre-cas-interrupted" if current_ref == old_ref else "ref-changed")
            return _block(intent, journal, owner, reason)
        try:
            current_ref = _optional_ref(binding.root, intent.branch_ref)
        except Exception:
            return _block(intent, journal, owner, "ref-changed")
        if current_ref != record["commit_oid"]:
            old_ref = None if intent.old_ref_oid == _ZERO_OID else intent.old_ref_oid
            return _block(intent, journal, owner,
                          "pre-cas-interrupted" if current_ref == old_ref else "ref-changed")
        try:
            _envelopes(intent, binding, plan, inputs, artifacts, controller)
            _source(scheduler, intent.task_id, intent)
            _assert_raw_checkout(
                binding.root, record["tree_oid"], exact_modes=record["worktree_modes"],
                directory_modes=record["directory_modes"],
            )
            if (_head_ref(binding.root) != intent.branch_ref
                    and _head_ref(binding.root) == _associated_head_ref(intent, journal, controller)
                    and _git(binding.root, "rev-parse", "HEAD").decode() == intent.base_oid):
                _git(binding.root, "symbolic-ref", "HEAD", intent.branch_ref)
            return _finish(intent, binding, record, plan, inputs, artifacts,
                           scheduler, controller, journal, owner)
        except Exception:
            recorded_terminal = journal.branch_handover_state(intent.task_id)[1]
            if recorded_terminal is not None:
                try:
                    verify_git_handover_proof(
                        intent, recorded_terminal, inputs=inputs, artifacts=artifacts,
                        controller=controller, journal=journal,
                    )
                except Exception:
                    pass
                else:
                    raise
            return _block(intent, journal, owner, "git-proof-invalid")
