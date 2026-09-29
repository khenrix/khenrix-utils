"""Owner-local, fail-closed entry point for a compiled llm-fanout run."""
from __future__ import annotations

import argparse
import dataclasses
import errno
import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
import urllib.parse
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping


_MAX_PACKET_BYTES = 8 * 1024 * 1024
_MAX_HEALTH_BYTES = 64 * 1024
_MAX_CANDIDATE_MANIFEST_BYTES = 64 * 1024 * 1024
_UNCERTAIN_STATUS_LIMIT = 64
_DESCRIPTOR = "run.json"
_BOOTSTRAP = "startup.json"
_FROZEN_V1_FIXTURE_MANIFEST_SHA256 = "23a149cef125c5b9ceedb18335798e36272ccaf65d68a5956a2c526d36f43c27"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_json(raw: bytes) -> object:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda value: _bad_constant(value))


def _bad_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _runtime():
    skill_root = Path(__file__).resolve().parents[1]
    candidates = (skill_root / "lib/fanout", skill_root.parent.parent / "lib/fanout")
    runtime_root = next((path for path in candidates if (path / "__init__.py").is_file()), None)
    if runtime_root is None:
        raise ValueError("bundled fanout runtime is missing")
    spec = importlib.util.spec_from_file_location(
        "_fanout_execute_cli_runtime", runtime_root / "__init__.py",
        submodule_search_locations=[str(runtime_root)],
    )
    if spec is None or spec.loader is None:
        raise ValueError("bundled fanout runtime could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _memory_modules():
    skill_root = Path(__file__).resolve().parents[1]
    candidates = (skill_root / "memory", skill_root.parent.parent.parent / "components/memory")
    source = next((path for path in candidates if (path / "memoryctl.py").is_file()
                   and (path / "memory_exchange.py").is_file()), None)
    if source is None:
        raise ValueError("bundled memory controller is missing")
    if source.is_symlink() or any((source / name).is_symlink()
                                  for name in ("memoryctl.py", "memory_exchange.py")):
        raise ValueError("bundled memory controller is unsafe")

    def load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ValueError("bundled memory module could not be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module

    controller = load("_fanout_execute_memoryctl", source / "memoryctl.py")
    old_controller = sys.modules.get("memoryctl")
    sys.modules["memoryctl"] = controller
    try:
        exchange = load("_fanout_execute_memory_exchange", source / "memory_exchange.py")
    finally:
        if old_controller is None:
            sys.modules.pop("memoryctl", None)
        else:
            sys.modules["memoryctl"] = old_controller
    if exchange.memoryctl is not controller:
        raise ValueError("bundled memory module imported a foreign controller")
    return controller, exchange


def reject_nested(environment: Mapping[str, str]) -> None:
    """Only the invoking CLI may orchestrate; provider children have depth one."""
    marker = environment.get("KHENRIX_NESTED_AGENT", "")
    depth = environment.get("LLM_FANOUT_DEPTH", "0")
    if marker not in {"", "0"} or not depth.isdecimal() or int(depth) != 0:
        raise ValueError("nested fanout execution is forbidden")


def verify_admission(packet: object, runtime, resolver):
    """Recompile every source, draft, and skill byte before considering spend."""
    if not isinstance(packet, dict) or set(packet) != {
        "schema_version", "kind", "admission", "source_markdown", "draft", "compiled",
    } or packet.get("schema_version") not in {"fanout-plan-admission-v1", "fanout-plan-admission-v2"}:
        raise ValueError("fanout-plan-admission-v1 or v2 packet is required")
    version = packet["schema_version"].removeprefix("fanout-plan-admission-")
    kind, admission = packet["kind"], packet["admission"]
    source, draft, recorded = packet["source_markdown"], packet["draft"], packet["compiled"]
    if (kind not in {"question", "bundle"} or not isinstance(admission, dict)
            or not isinstance(source, str) or not isinstance(draft, dict)
            or not isinstance(recorded, dict)):
        raise ValueError("fanout admission fields are invalid")
    try:
        source_bytes = source.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("admitted source must be UTF-8") from error
    if not source_bytes or len(source_bytes) > runtime.TaskPacket.MAX_SOURCE_BYTES:
        raise ValueError("admitted source exceeds the TaskPacket source limit")
    source_path = (recorded.get("plan") or {}).get("source", {}).get("path")
    if not isinstance(source_path, str):
        raise ValueError("compiled source path is missing")
    compiler = (runtime.compile_superpowers_plan if version == "v1" else
                sys.modules[f"{runtime.__name__}.compiler"].compile_superpowers_plan_v2)
    compiled = compiler(
        source_bytes, source_path=source_path,
        draft_bytes=runtime.canonical_json(draft), resolver=resolver,
    )
    if runtime.canonical_json(recorded) != compiled.to_bytes():
        raise ValueError("compiled plan differs from exact source, draft, or skill closure")
    tasks = [task for task in compiled.plan.tasks if task.kind == "work"]
    if kind == "question":
        if (version != "v1" or set(admission) != {"kind", "approval", "quality_tier"}
                or admission["kind"] != "direct-question"
                or admission["approval"] != "invocation-authorized"
                or admission["quality_tier"] not in {"normal", "deep"}
                or len(tasks) != 1 or tasks[0].execution_class != "read-only"
                or any(task.execution_class == "orchestrator-action"
                       for task in compiled.plan.tasks)):
            raise ValueError("direct-question admission is not read-only or invocation-authorized")
    else:
        if (set(admission) != {"kind", "owner_review", "quality_tier"}
                or admission["kind"] != "task-bundle"
                or admission["quality_tier"] not in {"normal", "deep"}):
            raise ValueError("task-bundle admission is invalid")
        original = {
            "schema_version": "fanout-bundle-ingress-" + version,
            "quality_tier": admission["quality_tier"],
            "source_path": source_path, "source_markdown": source, "draft": draft,
        }
        review = admission["owner_review"]
        needs_review = any(task.execution_class in {"repo-write", "orchestrator-action"}
                           for task in tasks)
        if needs_review or review is not None:
            if (not isinstance(review, dict)
                    or set(review) != {"schema_version", "reviewer", "binding_sha256"}
                    or review["schema_version"] != "fanout-owner-review-" + version
                    or not isinstance(review["reviewer"], str)
                    or not review["reviewer"].strip()
                    or review["binding_sha256"] != _sha256(runtime.canonical_json(
                        original if version == "v1" else
                        {"schema_version": "fanout-owner-review-binding-v2", "bundle": original}
                    ))):
                raise ValueError("owner review differs from exact repository-writing bundle")
        if admission["quality_tier"] == "deep" and any(
            task.execution_class != "read-only" for task in tasks
        ):
            raise ValueError("deep bundle admission requires read-only work")
    selected_tier = "standard" if admission["quality_tier"] == "normal" else "deep"
    if compiled.plan.defaults.quality_tier != selected_tier:
        raise ValueError("admission quality tier differs from compiled plan")
    return compiled


def authenticated_memory_preflight(memoryctl, memory_exchange, *,
                                   connection_factory=http.client.HTTPConnection) -> tuple[Path, str]:
    """Require an authenticated, initialized worker before any first-round turn."""
    try:
        problem = memoryctl.install_receipt_problem()
        health = memoryctl.health_document(require_running=True)
        if (problem is not None or health.get("ok") is not True
                or health.get("worker") != "running" or health.get("gateway") != "running"):
            raise ValueError("memory installation or local worker is unhealthy")
        endpoint = memory_exchange._default_endpoint()
        token = memory_exchange._read_gateway_token(memory_exchange._default_token_path())
        memory_exchange.GatewayClient(endpoint, token, timeout=5)
        parsed = urllib.parse.urlsplit(endpoint)
        connection = connection_factory(parsed.hostname, parsed.port, timeout=5)
        try:
            connection.request("GET", "/api/health", headers={
                "Authorization": f"Bearer {token}", "Connection": "close",
            })
            response = connection.getresponse()
            body = response.read(_MAX_HEALTH_BYTES + 1)
            if (response.status != 200 or len(body) > _MAX_HEALTH_BYTES
                    or (response.getheader("Content-Type") or "").split(";", 1)[0].lower()
                    != "application/json"):
                raise ValueError("authenticated memory worker health failed")
            worker = _strict_json(body)
            if (not isinstance(worker, dict) or worker.get("status") != "ok"
                    or worker.get("initialized") is not True):
                raise ValueError("authenticated memory worker is uninitialized")
        finally:
            connection.close()
        executable = memoryctl.installed_controller_root() / "memory_exchange.py"
        return executable, _sha256(executable.read_bytes())
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("authenticated memory preflight failed") from error


def _private_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError("private descriptor directory is unavailable") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("private descriptor directory is unsafe")


def save_private_descriptor(root: Path | str, document: dict[str, object], *,
                            name: str = _DESCRIPTOR) -> None:
    """Write the owner capability outside the run root, once, with fsynced 0600 bytes."""
    root = Path(root)
    _private_dir(root)
    expected_schema = {_DESCRIPTOR: "fanout-execute-private-v1",
                       _BOOTSTRAP: "fanout-execute-startup-v1"}.get(name)
    if (expected_schema is None or not isinstance(document, dict)
            or document.get("schema_version") != expected_schema):
        raise ValueError("private descriptor schema is invalid")
    raw_document = _canonical(document)
    if len(raw_document) > _MAX_PACKET_BYTES:
        raise ValueError("private descriptor exceeds its byte limit")
    wrapped = _canonical({"schema_version": "fanout-private-envelope-v1",
                          "sha256": _sha256(raw_document), "document": document})
    temporary = root / f".{name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(wrapped)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, root / name, follow_symlinks=False)
    finally:
        try:
            os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_private_descriptor(root: Path | str, *, name: str = _DESCRIPTOR) -> dict[str, object]:
    root = Path(root)
    _private_dir(root)
    expected_schema = {_DESCRIPTOR: "fanout-execute-private-v1",
                       _BOOTSTRAP: "fanout-execute-startup-v1"}.get(name)
    if expected_schema is None:
        raise ValueError("private descriptor name is invalid")
    try:
        descriptor = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > _MAX_PACKET_BYTES):
                raise ValueError("private descriptor file is unsafe")
            raw = os.read(descriptor, info.st_size + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ValueError("private descriptor is unavailable or unsafe") from error
    try:
        envelope = _strict_json(raw)
        if (not isinstance(envelope, dict)
                or set(envelope) != {"schema_version", "sha256", "document"}
                or envelope["schema_version"] != "fanout-private-envelope-v1"
                or raw != _canonical(envelope)
                or not isinstance(envelope["document"], dict)
                or envelope["document"].get("schema_version") != expected_schema
                or envelope["sha256"] != _sha256(_canonical(envelope["document"]))):
            raise ValueError("private descriptor changed")
        return envelope["document"]
    except (UnicodeError, TypeError, ValueError) as error:
        raise ValueError("private descriptor changed") from error


def _repair_private_publication(root: Path, name: str) -> bool:
    """Remove only the exact temporary hardlink left by an interrupted publish."""
    target = root / name
    info = target.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 2):
        return False
    prefix = f".{name}."
    candidates = []
    for source in root.iterdir():
        filename = source.name
        if (not filename.startswith(prefix) or not filename.endswith(".tmp")
                or len(filename) != len(prefix) + 32 + 4
                or any(character not in "0123456789abcdef"
                       for character in filename[len(prefix):-4])):
            continue
        sibling = source.lstat()
        if (stat.S_ISREG(sibling.st_mode) and sibling.st_uid == os.getuid()
                and stat.S_IMODE(sibling.st_mode) == 0o600
                and (sibling.st_dev, sibling.st_ino) == (info.st_dev, info.st_ino)):
            candidates.append(source)
    if len(candidates) != 1:
        raise ValueError("private descriptor publication is ambiguous")
    candidates[0].unlink()
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True


def _resolver(runtime, paths):
    roots = []
    for index, source in enumerate(paths):
        source = Path(source)
        if not source.is_absolute() or not source.is_dir() or source.is_symlink():
            raise ValueError("skill root must be an existing absolute directory")
        roots.append(runtime.SkillRoot(f"root-{index}", source, index))
    return runtime.SkillResolver(tuple(roots))


def _separate_paths(repository: Path, run: Path, authority: Path, private: Path) -> None:
    paths = tuple(path.resolve(strict=False) for path in (repository, run, authority, private))
    if any(path != original or original.is_symlink()
           for path, original in zip(paths, (repository, run, authority, private), strict=True)):
        raise ValueError("fanout root paths must be real absolute paths")
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError("repository, run, authority, and private roots must be disjoint")


def _disjoint_target_paths(target_roots: Mapping[str, Path], *storage_roots: Path) -> None:
    if not isinstance(target_roots, Mapping) or not target_roots:
        raise ValueError("target roots are required")
    paths = [Path(path) for path in target_roots.values()] + [Path(path) for path in storage_roots]
    if any(not path.is_absolute() or path.resolve(strict=False) != path or path.is_symlink()
           for path in paths):
        raise ValueError("target and storage roots must be real absolute paths")
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError("target, run, authority, and private roots must be disjoint")


def _separate_target_paths(target_roots: Mapping[str, Path], run: Path,
                           authority: Path, private: Path) -> None:
    """Check every target against each other and future durable storage roots."""
    _disjoint_target_paths(target_roots, run, authority, private)


def _task_seat_id(task_id: str, executor_id: str) -> str:
    return "seat-" + _sha256(f"{task_id}\0{executor_id}".encode("utf-8"))[:24]


def _admission_session_id(run_id: str, task_id: str, executor_id: str) -> str:
    return "admission-" + _sha256(f"{run_id}\0{task_id}\0{executor_id}".encode("utf-8"))[:24]


def _task_skill_bundle(runtime, resolver, plan, task_id: str) -> bytes:
    admission = resolver.admit_plan(
        plan, task_id, seat_id="seed", provider="codex", session_id="seed",
    )
    return runtime.canonical_json({"schema_version": "fanout-seat-skill-bundle-v1",
                                   "skills": admission.manifest_dict()["skills"]})


def _runtime_digest(runtime) -> str:
    root = Path(runtime.__file__).resolve().parent
    files = sorted(root.glob("*.py"))
    if not files:
        raise ValueError("fanout runtime source is missing")
    hasher = hashlib.sha256()
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError("fanout runtime source is unsafe")
        hasher.update(path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _real_memory(runtime):
    memoryctl, exchange = _memory_modules()
    executable = memoryctl.installed_controller_root() / "memory_exchange.py"
    digest = _sha256(executable.read_bytes())
    controller = runtime.MemoryController(executable, executable_sha256=digest)

    def healthy() -> bool:
        current, current_digest = authenticated_memory_preflight(memoryctl, exchange)
        return current == executable and current_digest == digest and controller.preflight()

    class AuthenticatedMemoryExchange(runtime.MemoryCheckpointExchange):
        __slots__ = ("_health",)

        def __init__(self, artifacts, health):
            super().__init__(controller, artifacts)
            self._health = health

        def preflight(self):
            return self._health() and super().preflight()

    return AuthenticatedMemoryExchange, healthy


def _dependency_inputs(runtime, compiled, plan, run_id: str, baseline, registry, bundles,
                       *, profile_digests=None):
    compiler = Path(runtime.__file__).resolve().parent / "compiler.py"
    return runtime.RunInputs(
        run_id=run_id,
        compiled_plan_sha256=_sha256(runtime.canonical_json(plan.to_dict())),
        source_sha256=plan.source.sha256,
        draft_sha256=compiled.draft_sha256,
        compiler_sha256=_sha256(compiler.read_bytes()),
        parser_sha256=_sha256(compiled.parser_version.encode("utf-8")),
        provider_profiles=dict(registry.profile_digests if profile_digests is None
                               else profile_digests),
        skill_manifests={task_id: _sha256(bundle) for task_id, bundle in bundles.items()},
        repo_baseline_sha256=baseline.digest,
        profile_shape="class-tier",
    )


def _registry_descriptors(registry):
    return [profile.to_dict() for profile in registry._profiles.values()]


def _initial_registry(runtime, descriptor, adapter_catalog, inputs):
    recorded = descriptor.get("provider_profile_descriptors")
    if recorded is None:
        if dict(adapter_catalog.profile_digests) == dict(inputs.provider_profiles):
            return adapter_catalog
        fallback = runtime.ProviderRegistry.default(
            version_probe=adapter_catalog._version_probe,
        )
        if dict(fallback.profile_digests) == dict(inputs.provider_profiles):
            return fallback
        raise ValueError("initial executor profile descriptors are unavailable")
    if not isinstance(recorded, list):
        raise ValueError("initial executor profile descriptors are invalid")
    profiles = []
    for item in recorded:
        if not isinstance(item, dict) or not isinstance(item.get("executor_id"), str):
            raise ValueError("initial executor profile descriptor is invalid")
        adapter = adapter_catalog.require(item["executor_id"])
        profiles.append(runtime.ExecutorProfile.from_dict(item, adapter=adapter))
    restored = runtime.ProviderRegistry(
        profiles, version_probe=adapter_catalog._version_probe,
    )
    if dict(restored.profile_digests) != dict(inputs.provider_profiles):
        raise ValueError("initial executor profile descriptors changed binding")
    return restored


def _baseline_prefix(target_id: str | None) -> str:
    if target_id is None:
        return "baseline"
    if not isinstance(target_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", target_id) is None:
        raise ValueError("baseline target id is invalid")
    return f"baseline/{target_id}"


def _store_baseline(runtime, artifacts, baseline, *, target_id: str | None = None):
    prefix = _baseline_prefix(target_id)
    blobs = {}

    def records(items):
        result = []
        for item in items:
            digest = _sha256(item.data)
            ref = blobs.get(digest)
            if ref is None:
                ref = artifacts.write_bytes(f"{prefix}/blobs/{digest}.bin", item.data)
                blobs[digest] = ref
            document = {"path": item.path, "mode": item.mode,
                        "blob": _ref_document(ref)}
            if isinstance(item, runtime.RepositoryEntry):
                document["kind"] = item.kind
            else:
                document.update(stage=item.stage, flags=item.flags)
            result.append(document)
        return result

    manifest = {
        "schema_version": ("fanout-baseline-snapshot-v1" if target_id is None
                           else "fanout-baseline-snapshot-v2"),
        "repository": str(baseline.repository), "head": baseline.head,
        "digest": baseline.digest,
        "limits": {"max_file_bytes": baseline.limits.max_file_bytes,
                   "max_total_bytes": baseline.limits.max_total_bytes},
        "head_entries": records(baseline.head_entries),
        "index_entries": records(baseline.index_entries),
        "entries": records(baseline.entries),
        "directories": [{"path": item.path, "mode": item.mode}
                        for item in baseline.directories],
        "deleted_paths": list(baseline.deleted_paths),
    }
    if target_id is not None:
        manifest["target_id"] = target_id
    return artifacts.write_bytes(f"{prefix}/manifest.json", runtime.canonical_json(manifest))


def _load_baseline(runtime, artifacts, reference: object, repository: Path,
                   *, target_id: str | None = None):
    prefix = _baseline_prefix(target_id)
    if not isinstance(reference, dict) or set(reference) != {"path", "digest", "size"}:
        raise ValueError("baseline snapshot reference is invalid")
    if reference["path"] != f"{prefix}/manifest.json":
        raise ValueError("baseline target manifest reference changed")
    raw = artifacts.read_bytes(runtime.ArtifactRef(**reference))
    manifest = _strict_json(raw)
    fields = {"schema_version", "repository", "head", "digest", "limits",
              "head_entries", "index_entries", "entries", "directories", "deleted_paths"}
    if target_id is not None:
        fields.add("target_id")
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest["schema_version"] != ("fanout-baseline-snapshot-v1" if target_id is None
                                               else "fanout-baseline-snapshot-v2")
            or (target_id is not None and manifest["target_id"] != target_id)
            or manifest["repository"] != str(repository)
            or raw != runtime.canonical_json(manifest)):
        raise ValueError("baseline snapshot manifest changed")

    def records(name, entry_class, expected):
        source = manifest[name]
        if not isinstance(source, list):
            raise ValueError("baseline snapshot entries are invalid")
        result = []
        for item in source:
            if not isinstance(item, dict) or set(item) != expected:
                raise ValueError("baseline snapshot entry changed")
            ref = item["blob"]
            if not isinstance(ref, dict) or set(ref) != {"path", "digest", "size"}:
                raise ValueError("baseline snapshot blob changed")
            if target_id is not None and ref["path"] != f"{prefix}/blobs/{ref['digest']}.bin":
                raise ValueError("baseline target blob reference changed")
            data = artifacts.read_bytes(runtime.ArtifactRef(**ref))
            fields = {key: value for key, value in item.items() if key != "blob"}
            result.append(entry_class(data=data, **fields))
        return tuple(result)

    entry_fields = {"path", "mode", "kind", "blob"}
    index_fields = {"path", "mode", "stage", "flags", "blob"}
    limits = manifest["limits"]
    if not isinstance(limits, dict) or set(limits) != {"max_file_bytes", "max_total_bytes"}:
        raise ValueError("baseline snapshot limits changed")
    if not isinstance(manifest["directories"], list) or not isinstance(manifest["deleted_paths"], list):
        raise ValueError("baseline snapshot directory or deletion list changed")
    baseline = runtime.RepositoryBaseline(
        repository=repository, head=manifest["head"],
        head_entries=records("head_entries", runtime.RepositoryEntry, entry_fields),
        index_entries=records("index_entries", runtime.RepositoryIndexEntry, index_fields),
        entries=records("entries", runtime.RepositoryEntry, entry_fields),
        directories=tuple(runtime.RepositoryDirectory(**item)
                          for item in manifest["directories"]),
        deleted_paths=tuple(manifest["deleted_paths"]),
        limits=runtime.RepoLimits(**limits),
    )
    if baseline.digest != manifest["digest"]:
        raise ValueError("baseline snapshot digest changed")
    return baseline


def _store_target_baseline_record(runtime, artifacts, inputs, baselines,
                                  *, kind: str, writable_ids: set[str]) -> dict[str, object]:
    """Build the target-keyed baseline fragment for a future v2 descriptor."""
    if kind not in {"startup", "private"} or inputs.targets is None:
        raise ValueError("v3 target baseline record kind or inputs are invalid")
    if (set(inputs.targets) != set(baselines) or not isinstance(writable_ids, set)
            or not writable_ids <= set(inputs.targets)):
        raise ValueError("v3 target baseline record is incomplete")
    repo_module = sys.modules[f"{runtime.__name__}.repo"]
    try:
        live = repo_module.revalidate_target_bindings(inputs.targets, writable_ids=writable_ids)
    except runtime.RepositoryValidationError as error:
        raise ValueError("v3 target binding changed before baseline storage") from error
    refs = {}
    for target_id in sorted(inputs.targets):
        baseline = baselines[target_id]
        if baseline != live[target_id] or baseline.digest != inputs.targets[target_id].baseline_sha256:
            raise ValueError(f"target {target_id}: baseline changed before storage")
        refs[target_id] = _ref_document(_store_baseline(
            runtime, artifacts, baseline, target_id=target_id,
        ))
    return {
        "schema_version": f"fanout-execute-{kind}-v2",
        "run_id": inputs.run_id,
        "inputs": inputs.to_dict(),
        "baseline_manifest_refs": refs,
        "writable_target_ids": sorted(writable_ids),
    }


def _load_target_baseline_record(runtime, artifacts, record: object):
    """Authenticate every saved target binding and target-prefixed manifest."""
    if (not isinstance(record, dict)
            or set(record) != {"schema_version", "run_id", "inputs", "baseline_manifest_refs",
                               "writable_target_ids"}
            or record["schema_version"] not in {"fanout-execute-startup-v2",
                                                "fanout-execute-private-v2"}):
        raise ValueError("v3 target baseline record has invalid fields")
    inputs = runtime.RunInputs.from_dict(record["inputs"])
    refs = record["baseline_manifest_refs"]
    writable = record["writable_target_ids"]
    if (inputs.targets is None or record["run_id"] != inputs.run_id
            or not isinstance(refs, dict) or set(refs) != set(inputs.targets)
            or not isinstance(writable, list) or not all(isinstance(value, str) for value in writable)
            or writable != sorted(set(writable))
            or not set(writable) <= set(inputs.targets)):
        raise ValueError("v3 target baseline record changed targets")
    repo_module = sys.modules[f"{runtime.__name__}.repo"]
    try:
        live = repo_module.revalidate_target_bindings(inputs.targets, writable_ids=set(writable))
    except runtime.RepositoryValidationError as error:
        raise ValueError("v3 target binding changed before baseline reopen") from error
    baselines = {}
    for target_id in sorted(inputs.targets):
        baseline = _load_baseline(
            runtime, artifacts, refs[target_id], inputs.targets[target_id].root,
            target_id=target_id,
        )
        if baseline != live[target_id] or baseline.digest != inputs.targets[target_id].baseline_sha256:
            raise ValueError(f"target {target_id}: baseline manifest changed")
        baselines[target_id] = baseline
    return MappingProxyType(baselines)


def _build_preparations(runtime, *, plan, inputs, resolver, bundles, source_markdown,
                        repository, run_root, baseline, controller, artifacts, registry, restore,
                        workspace_evidence=None, plan_revision=1,
                        require_provider_guards=True):
    prepared = {}
    evidence = {}
    compiled_plan = runtime.canonical_json(plan.to_dict())
    for task in plan.tasks:
        if task.kind != "work" or task.execution_class == "orchestrator-action":
            continue
        policy = task.provider_policy or plan.defaults
        seat_list = []
        verifications = []
        task_evidence = {}
        stage_group = run_root / "skill-stage" / _sha256(task.id.encode())[:24]
        if not restore:
            stage_group.mkdir(parents=True, mode=0o700, exist_ok=False)
        for executor in policy.executor_ids:
            seat_id = _task_seat_id(task.id, executor)
            session_id = _admission_session_id(inputs.run_id, task.id, executor)
            if restore:
                recorded = (workspace_evidence or {}).get(task.id, {}).get(executor)
                if not isinstance(recorded, str):
                    raise ValueError("seat workspace evidence is missing")
                workspace = runtime.SeatWorkspace(
                    controller.root / "workspaces" / "seats" / seat_id, seat_id, baseline.digest,
                )
                verification = runtime.resume_seat_workspace(
                    workspace, controller=controller, evidence_digest=recorded,
                )
            else:
                workspace = runtime.create_seat_workspace(
                    baseline, controller.root / "workspaces", seat_id,
                )
                verification = runtime.verify_seat_workspace(
                    baseline, workspace, controller=controller,
                )
            task_evidence[executor] = verification.evidence_digest
            verifications.append(verification)
            admission = resolver.admit_plan(
                plan, task.id, seat_id=seat_id, provider=executor, session_id=session_id,
            )
            stage = stage_group / seat_id
            if restore:
                admission = dataclasses.replace(admission, staged_root=stage / "ready")
            else:
                admission = resolver.stage(admission, stage)
            admission = resolver.verify_engine_delivery(
                admission, tuple(runtime.SkillLoadEvidence.engine(skill, admission)
                                 for skill in admission.skills),
            )
            guard = None
            guard_digest = None
            if executor == "agy" and task.execution_class == "read-only":
                profile = registry.select(executor, "read-only", policy.quality_tier)
                if require_provider_guards:
                    guard = runtime.issue_agy_readonly_guard(
                        controller, verification, profile,
                    )
                else:
                    guard_digest = runtime.agy_readonly_guard_receipt_sha256(
                        controller, verification, profile,
                    )
            seat_list.append(runtime.SeatAssignment.from_admission(
                admission, skill_bundle=bundles[task.id], artifacts=artifacts,
                workspace_verification=verification, agy_guard=guard,
                agy_guard_receipt_sha256=guard_digest,
            ))
        task_bytes = runtime.canonical_json(task.to_dict())
        packet_values = dict(
            run_id=inputs.run_id, task_id=task.id, attempt=1,
            compiled_plan=compiled_plan, compiled_plan_sha256=inputs.compiled_plan_sha256,
            source_markdown=source_markdown,
            task=task_bytes, task_sha256=_sha256(task_bytes),
            skill_bundle=bundles[task.id],
            skill_manifest_sha256=inputs.skill_manifests[task.id],
            dependency_artifacts=(), execution_class=task.execution_class,
            cwd=repository,
        )
        packet = (
            runtime.TaskPacket.for_repo_write(
                workspace_verifications=verifications,
                lifecycle_controller=controller, **packet_values,
            ) if task.execution_class == "repo-write" else runtime.TaskPacket(**packet_values)
        )
        prepared[task.id] = runtime.TaskPreparation(
            packet, tuple(seat_list), runtime.RoundPolicy.from_provider_policy(policy),
            inputs, plan_revision, registry=registry,
        )
        evidence[task.id] = task_evidence
    return prepared, evidence


class RunContext:
    """A reopened owner session with explicit close of every durable file handle."""

    def __init__(self, service, owner, journal, artifacts, scheduler_backend, execution_backend):
        self.service = service
        self.owner = owner
        self.journal = journal
        self.artifacts = artifacts
        self.scheduler_backend = scheduler_backend
        self.execution_backend = execution_backend

    def close(self) -> None:
        self.journal.close()
        self.artifacts.close()
        self.scheduler_backend.close()
        self.execution_backend.close()

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()


def _require_native_seat_boundary(plan) -> None:
    if plan.schema_version == "v2" or any(task.kind == "work" and task.execution_class == "repo-write"
           for task in plan.tasks):
        raise ValueError("v2 or repo-write execution requires a validated native seat boundary")


def _refuse_uncontained_repo_write(plan, provider_runner, runtime) -> None:
    # Direct Python test fixtures can inject a hermetic transport. The owner
    # command dispatcher enforces the native gate independently of this seam.
    if provider_runner is not None and provider_runner is not runtime.run_provider:
        return
    _require_native_seat_boundary(plan)


def _require_turn_budget(plan, budget: int, runtime) -> None:
    runtime.ExecutionBudget(budget)
    required_turns = sum(
        len(policy.executor_ids) * policy.rounds * (policy.retries + 1)
        for task in plan.tasks
        if task.kind == "work" and task.execution_class != "orchestrator-action"
        for policy in (task.provider_policy or plan.defaults,)
    )
    if budget < required_turns:
        raise ValueError("provider turn budget is below the compiled worst-case spend")


@dataclasses.dataclass(frozen=True, slots=True)
class PreparedRunV2:
    """Validated all-target preflight; no durable run or provider turn exists yet."""

    inputs: object
    baselines: Mapping[str, object]
    target_roots: Mapping[str, Path]
    provider_runner: object = None


def prepare_v2_run(packet: Mapping[str, object], *, target_roots: Mapping[str, Path],
                   skill_roots, provider_runner=None) -> PreparedRunV2:
    """Recompile and bind every target before any durable startup or provider spend."""
    reject_nested(os.environ)
    runtime = _runtime()
    resolver = _resolver(runtime, skill_roots)
    compiled = verify_admission(dict(packet), runtime, resolver)
    plan = compiled.plan
    if plan.schema_version != "v2":
        raise ValueError("v2 admission is required for target-bound preparation")
    specs = {target.id: target for target in plan.targets}
    if not isinstance(target_roots, Mapping) or set(target_roots) != set(specs):
        raise ValueError("target roots must match the complete admitted registry")
    _disjoint_target_paths(target_roots)
    repo_module = sys.modules[f"{runtime.__name__}.repo"]
    target_module = sys.modules[f"{runtime.__name__}.targets"]
    try:
        bindings, baselines = repo_module.capture_and_bind_targets(specs, target_roots)
    except runtime.RepositoryValidationError as error:
        raise ValueError(str(error)) from error
    writable_ids = {task.target_id for task in plan.tasks
                    if task.kind == "work" and task.execution_class == "repo-write"}
    try:
        runtime.validate_target_bindings(bindings, writable_ids=writable_ids)
    except runtime.RepositoryValidationError as error:
        raise ValueError(str(error)) from error
    for target_id in sorted(writable_ids):
        binding = bindings[target_id]
        if binding.branch_oid is not None:
            current_ref = target_module._git_local_optional(
                binding.root, "symbolic-ref", "-q", "HEAD",
            )
            if current_ref != binding.spec.branch_ref or binding.branch_oid != binding.base_oid:
                raise ValueError(f"target {target_id}: existing ticket branch must be current HEAD")
        status = repo_module._git(
            binding.root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
        )
        if status:
            raise ValueError(f"target {target_id}: repository writer requires a clean checkout")
    registry = runtime.ProviderRegistry.default()
    bundles = {task.id: _task_skill_bundle(runtime, resolver, plan, task.id)
               for task in plan.tasks if task.kind == "work"
               and task.execution_class != "orchestrator-action"}
    compiler = Path(runtime.__file__).resolve().parent / "compiler.py"
    inputs = runtime.RunInputs(
        run_id="fanout-" + secrets.token_hex(16),
        compiled_plan_sha256=_sha256(runtime.canonical_json(plan.to_dict())),
        source_sha256=plan.source.sha256,
        draft_sha256=compiled.draft_sha256,
        compiler_sha256=_sha256(compiler.read_bytes()),
        parser_sha256=_sha256(compiled.parser_version.encode("utf-8")),
        provider_profiles=dict(registry.profile_digests),
        skill_manifests={task_id: _sha256(bundle) for task_id, bundle in bundles.items()},
        profile_shape="class-tier", targets=bindings,
    )
    return PreparedRunV2(
        inputs=inputs, baselines=baselines,
        target_roots=MappingProxyType({key: bindings[key].root for key in sorted(bindings)}),
        provider_runner=provider_runner,
    )


def prepare_run(packet: dict[str, object], *, repo_root: Path | str, run_root: Path | str,
                authority_root: Path | str, private_root: Path | str, skill_roots,
                budget: int, runtime=None, registry=None, memory_factory=None,
                memory_preflight: Callable[[], bool] | None = None,
                provider_runner=None) -> dict[str, object]:
    """Create a dormant durable run; service.start schedules but never launches seats."""
    reject_nested(os.environ)
    runtime = runtime or _runtime()
    repository, run, authority, private = map(
        Path, (repo_root, run_root, authority_root, private_root),
    )
    if any(not path.is_absolute() for path in (repository, run, authority, private)):
        raise ValueError("fanout roots must be absolute")
    _private_dir(private)
    _separate_paths(repository, run, authority, private)
    if (run.exists() or authority.exists() or (private / _DESCRIPTOR).exists()
            or (private / _BOOTSTRAP).exists()
            or (private / "startup-baseline").exists()):
        raise ValueError("fanout run, authority, and private descriptor must be fresh")
    resolver = _resolver(runtime, skill_roots)
    compiled = verify_admission(packet, runtime, resolver)
    plan = compiled.plan
    if plan.schema_version != "v1":
        raise ValueError("v2 admission requires target-bound preparation")
    registry = registry or runtime.ProviderRegistry.default()
    _require_turn_budget(plan, budget, runtime)
    _refuse_uncontained_repo_write(plan, provider_runner, runtime)
    if memory_factory is None:
        memory_factory, health = _real_memory(runtime)
        memory_preflight = health
    elif memory_preflight is None:
        raise ValueError("memory preflight is required")
    if memory_preflight() is not True:
        raise ValueError("authenticated memory preflight failed")
    baseline = runtime.capture_repository_baseline(repository)
    bundles = {task.id: _task_skill_bundle(runtime, resolver, plan, task.id)
               for task in plan.tasks if task.kind == "work"
               and task.execution_class != "orchestrator-action"}
    run_id = "fanout-" + secrets.token_hex(16)
    inputs = _dependency_inputs(runtime, compiled, plan, run_id, baseline, registry, bundles)
    escrow_owner = runtime.OwnerCapability.from_token(secrets.token_urlsafe(32))
    escrow_controller = runtime.LifecycleCapability(secrets.token_urlsafe(32))
    with runtime.ArtifactStore(private / "startup-baseline") as startup_artifacts:
        startup_baseline_ref = _store_baseline(runtime, startup_artifacts, baseline)
    bootstrap = {
        "schema_version": "fanout-execute-startup-v1", "run_id": run_id,
        "run_root": str(run), "repo_root": str(repository),
        "authority_root": str(authority),
        "skill_roots": [str(Path(path)) for path in skill_roots],
        "admission_packet": packet, "inputs": inputs.to_dict(),
        "owner_token": escrow_owner.export_token(),
        "controller_token": escrow_controller.export_token(),
        "provider_profile_descriptors": _registry_descriptors(registry),
        "budget": budget, "runtime_sha256": _runtime_digest(runtime),
        "baseline_manifest_ref": _ref_document(startup_baseline_ref),
    }
    save_private_descriptor(private, bootstrap, name=_BOOTSTRAP)
    anchor = runtime.LocalAnchorAuthority.bootstrap(
        authority, run_root=run, repo_root=repository,
    )
    journal, owner = runtime.RunJournal.create(
        run, inputs, anchor_store=anchor, owner_capability=escrow_owner,
    )
    artifacts = runtime.ArtifactStore(run / "artifacts")
    scheduler_backend = runtime.FileSchedulerBackend.create(
        run, run_id=run_id, owner=owner,
    )
    execution_backend = runtime.FileExecutionBackend.create(
        run, run_id=run_id, owner=owner,
    )
    try:
        baseline_ref = _store_baseline(runtime, artifacts, baseline)
        memory = memory_factory(artifacts, memory_preflight)
        lifecycle = runtime.create_lifecycle_controller(
            run / "lifecycle", capability=escrow_controller,
        )
        preparations, evidence = _build_preparations(
            runtime, plan=plan, inputs=inputs, resolver=resolver, bundles=bundles,
            source_markdown=packet["source_markdown"].encode("utf-8"),
            repository=repository, run_root=run, baseline=baseline,
            controller=lifecycle, artifacts=artifacts, registry=registry,
            restore=False,
        )
        scheduler = runtime.Scheduler.create(
            plan, inputs, scheduler_backend, artifacts, owner=owner, anchor_store=anchor,
        )
        coordinator = runtime.CollaborationCoordinator(
            artifacts=artifacts, journal=journal, owner=owner, memory=memory,
            registry=registry, provider_runner=provider_runner or runtime.run_provider,
            lifecycle_controller=lifecycle, repository_baseline=baseline,
            slot_root=run / "slots",
        )
        service = runtime.ExecutionService(
            plan=plan, inputs=inputs, preparations=preparations,
            provider_profile_digests=inputs.provider_profiles,
            budget=runtime.ExecutionBudget(budget), scheduler=scheduler,
            journal=journal, coordinator=coordinator, artifacts=artifacts,
            backend=execution_backend, memory_preflight=memory.preflight,
            baseline=baseline, lifecycle_controller=lifecycle,
        )
        descriptor = {
            "schema_version": "fanout-execute-private-v1", "run_id": run_id,
            "run_root": str(run), "repo_root": str(repository),
            "authority_root": str(authority),
            "skill_roots": [str(Path(path)) for path in skill_roots],
            "admission_packet": packet, "inputs": inputs.to_dict(),
            "owner_token": owner.export_token(),
            "controller_token": lifecycle.capability.export_token(),
            "provider_profile_descriptors": _registry_descriptors(registry),
            "workspace_evidence": evidence, "budget": budget,
            "runtime_sha256": _runtime_digest(runtime),
            "baseline_manifest_ref": _ref_document(baseline_ref),
        }
        save_private_descriptor(private, descriptor)
        return service.start(owner=owner).to_dict()
    finally:
        journal.close()
        artifacts.close()
        scheduler_backend.close()
        execution_backend.close()


def recover_start(private_root: Path | str, *, runtime=None, registry=None) -> dict[str, object]:
    """Finish a pre-spend startup after a lost final descriptor write.

    The escrow is durable before either run root exists. Recovery authenticates every
    completed store and re-verifies pristine seat workspaces; a partial store fails
    closed and can never be mistaken for provider work that should be retried.
    """
    reject_nested(os.environ)
    runtime = runtime or _runtime()
    private = Path(private_root)
    _private_dir(private)
    if (private / _DESCRIPTOR).exists():
        if not _repair_private_publication(private, _DESCRIPTOR):
            raise ValueError("startup has already published its final descriptor")
        published = load_private_descriptor(private)
        return {"run_id": published["run_id"], "recovered": True,
                "descriptor_link_repaired": True}
    bootstrap = load_private_descriptor(private, name=_BOOTSTRAP)
    expected_fields = {
        "schema_version", "run_id", "run_root", "repo_root", "authority_root",
        "skill_roots", "admission_packet", "inputs", "owner_token",
        "controller_token", "provider_profile_descriptors", "budget",
        "runtime_sha256", "baseline_manifest_ref",
    }
    if set(bootstrap) != expected_fields or bootstrap["runtime_sha256"] != _runtime_digest(runtime):
        raise ValueError("startup escrow changed runtime or fields")
    repository = Path(bootstrap["repo_root"])
    run = Path(bootstrap["run_root"])
    authority = Path(bootstrap["authority_root"])
    _separate_paths(repository, run, authority, private)
    resolver = _resolver(runtime, bootstrap["skill_roots"])
    compiled = verify_admission(bootstrap["admission_packet"], runtime, resolver)
    plan = compiled.plan
    registry = registry or runtime.ProviderRegistry.default()
    inputs = runtime.RunInputs.from_dict(bootstrap["inputs"])
    runtime.ExecutionBudget(bootstrap["budget"])
    if inputs.run_id != bootstrap["run_id"]:
        raise ValueError("startup run identity changed")
    bundles = {task.id: _task_skill_bundle(runtime, resolver, plan, task.id)
               for task in plan.tasks if task.kind == "work"
               and task.execution_class != "orchestrator-action"}
    if not (private / "startup-baseline").is_dir():
        raise ValueError("startup baseline escrow is missing")
    with runtime.ArtifactStore(private / "startup-baseline") as startup_artifacts:
        baseline = _load_baseline(
            runtime, startup_artifacts, bootstrap["baseline_manifest_ref"], repository,
        )
    expected = _dependency_inputs(
        runtime, compiled, plan, inputs.run_id, baseline, registry, bundles,
    )
    if expected.digest != inputs.digest:
        raise ValueError("startup escrow differs from admitted inputs")
    owner = runtime.OwnerCapability.from_token(bootstrap["owner_token"])
    anchor = runtime.LocalAnchorAuthority(
        authority, run_root=run, repo_root=repository,
    )
    journal = runtime.RunJournal.resume(run, inputs, owner, anchor_store=anchor)
    try:
        if not (run / "artifacts").is_dir():
            raise ValueError("startup run artifacts are incomplete")
        with runtime.ArtifactStore(run / "artifacts") as artifacts:
            private_ref = bootstrap["baseline_manifest_ref"]
            if not isinstance(private_ref, dict) or set(private_ref) != {"path", "digest", "size"}:
                raise ValueError("startup baseline reference changed")
            run_ref = runtime.ArtifactRef(
                "baseline/manifest.json", private_ref["digest"], private_ref["size"],
            )
            run_baseline = _load_baseline(runtime, artifacts, _ref_document(run_ref), repository)
            if run_baseline != baseline:
                raise ValueError("run baseline differs from private startup escrow")
            lifecycle = runtime.resume_lifecycle_controller(
                run / "lifecycle",
                runtime.LifecycleCapability(bootstrap["controller_token"]),
            )
            evidence = {}
            for task in plan.tasks:
                if task.kind != "work" or task.execution_class == "orchestrator-action":
                    continue
                policy = task.provider_policy or plan.defaults
                task_evidence = {}
                for executor in policy.executor_ids:
                    seat_id = _task_seat_id(task.id, executor)
                    workspace = runtime.SeatWorkspace(
                        lifecycle.root / "workspaces" / "seats" / seat_id,
                        seat_id, baseline.digest,
                    )
                    verification = runtime.verify_seat_workspace(
                        baseline, workspace, controller=lifecycle,
                    )
                    task_evidence[executor] = verification.evidence_digest
                evidence[task.id] = task_evidence
            _build_preparations(
                runtime, plan=plan, inputs=inputs, resolver=resolver, bundles=bundles,
                source_markdown=bootstrap["admission_packet"]["source_markdown"].encode("utf-8"),
                repository=repository, run_root=run, baseline=baseline,
                controller=lifecycle, artifacts=artifacts, registry=registry,
                restore=True, workspace_evidence=evidence,
            )
            scheduler_backend = runtime.FileSchedulerBackend.resume(
                run, run_id=inputs.run_id, owner=owner,
            )
            execution_backend = runtime.FileExecutionBackend.resume(
                run, run_id=inputs.run_id, owner=owner,
            )
            try:
                if execution_backend.read() is not None:
                    raise ValueError("startup recovery found an initialized execution")
                runtime.resume_scheduler_from_amendments(
                    initial_plan=plan, journal=journal, backend=scheduler_backend,
                    artifacts=artifacts, owner=owner, anchor_store=anchor,
                )
            finally:
                scheduler_backend.close()
                execution_backend.close()
    finally:
        journal.close()
    descriptor = dict(bootstrap)
    descriptor["schema_version"] = "fanout-execute-private-v1"
    descriptor["baseline_manifest_ref"] = _ref_document(run_ref)
    descriptor["workspace_evidence"] = evidence
    save_private_descriptor(private, descriptor)
    return {"run_id": inputs.run_id, "recovered": True, "provider_turns": 0}


def _recovered_workspace_evidence(runtime, controller, plan, baseline):
    evidence_module = sys.modules.get(runtime.__name__ + ".controller")
    if evidence_module is None:
        raise ValueError("lifecycle evidence reader is unavailable")
    receipts = controller.root / "receipts"
    candidates = {}
    for source in sorted(receipts.iterdir()):
        if not source.name.endswith(".json") or len(source.name) != 69:
            continue
        digest = source.name[:-5]
        try:
            payload = evidence_module.read_evidence(
                controller, "seat-workspace", source.name, digest,
            )
        except runtime.LifecycleError:
            continue
        seat_id = payload.get("seat_id")
        if payload.get("baseline_digest") == baseline.digest and isinstance(seat_id, str):
            candidates.setdefault(seat_id, []).append(digest)
    evidence = {}
    for task in plan.tasks:
        if task.kind != "work" or task.execution_class == "orchestrator-action":
            continue
        policy = task.provider_policy or plan.defaults
        task_evidence = {}
        for executor in policy.executor_ids:
            seat_id = _task_seat_id(task.id, executor)
            workspace = runtime.SeatWorkspace(
                controller.root / "workspaces" / "seats" / seat_id,
                seat_id, baseline.digest,
            )
            valid = []
            for digest in candidates.get(seat_id, ()):
                try:
                    runtime.resume_seat_workspace(
                        workspace, controller=controller, evidence_digest=digest,
                    )
                except runtime.RepositoryError:
                    continue
                valid.append(digest)
            if len(valid) != 1:
                raise ValueError("amended seat workspace evidence is missing or ambiguous")
            task_evidence[executor] = valid[0]
        evidence[task.id] = task_evidence
    return evidence


def _accepted_plan_inputs(runtime, journal, artifacts, backend, initial_plan, initial_inputs):
    amendments = journal.state.amendments
    if not amendments:
        return initial_plan, initial_inputs, 1
    runtime.ExecutionService.recovery_documents(journal, artifacts)
    accepted = [revision for revision, record in amendments.items()
                if record.phase == "plan-amendment-accepted"]
    if not accepted:
        return initial_plan, initial_inputs, 1
    latest = max(accepted)
    documents = runtime.ExecutionService.recovery_documents(
        journal, artifacts, revision=latest,
    )
    if documents.inputs.run_id != initial_inputs.run_id or (
        documents.inputs.repo_baseline_sha256 != initial_inputs.repo_baseline_sha256
    ):
        raise ValueError("accepted amendment changed immutable run identity or baseline")
    record = backend.read()
    if not isinstance(record, runtime.ExecutionRecord):
        raise ValueError("accepted amendment lacks an execution record")
    snapshot = record.snapshot
    if snapshot.plan_revision == latest:
        if (snapshot.plan_sha256, snapshot.inputs_digest) != (
            documents.binding[2], documents.binding[3],
        ):
            raise ValueError("accepted amendment execution binding changed")
        return documents.plan, documents.inputs, latest
    if snapshot.plan_revision != latest - 1 or latest != max(amendments):
        raise ValueError("accepted amendment execution revision changed")
    if latest == 2:
        predecessor_plan, predecessor_inputs = initial_plan, initial_inputs
    else:
        predecessor = runtime.ExecutionService.recovery_documents(
            journal, artifacts, revision=latest - 1,
        )
        if amendments[latest - 1].phase != "plan-amendment-accepted":
            raise ValueError("accepted amendment predecessor is not settled")
        predecessor_plan, predecessor_inputs = predecessor.plan, predecessor.inputs
    if (snapshot.plan_sha256, snapshot.inputs_digest) != documents.binding[:2] or (
        predecessor_inputs.compiled_plan_sha256, predecessor_inputs.digest
    ) != documents.binding[:2]:
        raise ValueError("accepted amendment predecessor binding changed")
    return predecessor_plan, predecessor_inputs, latest - 1


def _accepted_registry(runtime, artifacts, registry, inputs, *, adapter_catalog=None):
    if dict(registry.profile_digests) == dict(inputs.provider_profiles):
        return registry
    catalog = adapter_catalog or registry
    profiles = []
    for key, digest in sorted(inputs.provider_profiles.items()):
        descriptor = runtime.ExecutionService._read_amendment_document(
            artifacts, "executor-profiles", digest,
        )
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("executor_id"), str):
            raise ValueError("accepted amendment executor profile is invalid")
        adapter = catalog.require(descriptor["executor_id"])
        profile = runtime.ExecutorProfile.from_dict(descriptor, adapter=adapter)
        if profile.binding_key != key or profile.digest != digest:
            raise ValueError("accepted amendment executor profile changed binding")
        profiles.append(profile)
    return runtime.ProviderRegistry(profiles, version_probe=catalog._version_probe)


def recover_amendment(opened: RunContext, private_root: Path | str, *,
                      runtime, adapter_catalog=None) -> dict[str, object]:
    """Replay only an authenticated pending amendment under the private owner."""
    service, owner = opened.service, opened.owner
    documents = runtime.ExecutionService.recovery_documents(
        opened.journal, opened.artifacts,
    )
    journal_record = opened.journal.state.amendments.get(documents.revision)
    if journal_record is None or journal_record.phase not in {
        "plan-amendment-intent", "plan-amendment-accepted",
    }:
        raise ValueError("no pending amendment requires recovery")
    if (journal_record.phase == "plan-amendment-accepted"
            and service.inputs.digest == documents.inputs.digest):
        raise ValueError("accepted amendment already reached execution authority")
    replacement, new_inputs = documents.plan, documents.inputs
    if (new_inputs.run_id != service.inputs.run_id
            or new_inputs.repo_baseline_sha256 != service.baseline.digest):
        raise ValueError("pending amendment changed immutable run identity or baseline")
    catalog = adapter_catalog or service.coordinator.registry
    selected_registry = (
        service.load_amendment_registry(owner=owner, adapter_catalog=catalog)
        if dict(new_inputs.provider_profiles) != dict(service.inputs.provider_profiles)
        else service.coordinator.registry
    )
    descriptor = load_private_descriptor(private_root)
    resolver = _resolver(runtime, descriptor["skill_roots"])
    bundles = {task.id: _task_skill_bundle(runtime, resolver, replacement, task.id)
               for task in replacement.tasks if task.kind == "work"
               and task.execution_class != "orchestrator-action"}
    if new_inputs.skill_manifests != {task_id: _sha256(bundle)
                                      for task_id, bundle in bundles.items()}:
        raise ValueError("pending amendment skill closure changed")
    evidence = _recovered_workspace_evidence(
        runtime, service.lifecycle_controller, replacement, service.baseline,
    )
    preparations, _ = _build_preparations(
        runtime, plan=replacement, inputs=new_inputs, resolver=resolver, bundles=bundles,
        source_markdown=descriptor["admission_packet"]["source_markdown"].encode("utf-8"),
        repository=service.baseline.repository, run_root=opened.journal.root,
        baseline=service.baseline, controller=service.lifecycle_controller,
        artifacts=opened.artifacts, registry=selected_registry, restore=True,
        workspace_evidence=evidence, plan_revision=documents.revision,
    )
    execution_module = sys.modules.get(runtime.__name__ + ".execute")
    if execution_module is None:
        raise ValueError("amendment impact calculator is unavailable")
    impact = execution_module._affected_amendment(
        service.plan, replacement, service.inputs, new_inputs, documents.revision,
    )
    affected = {task_id: preparations[task_id] for task_id in impact.affected_task_ids
                if task_id in preparations}
    recovered = service.recover_pending_amendment(
        affected, registry=selected_registry, adapter_catalog=catalog, owner=owner,
    )
    result = service.status().to_dict()
    if result["plan_revision"] != recovered.plan_revision:
        raise ValueError("recovered amendment revision changed")
    return result


@dataclasses.dataclass(frozen=True, slots=True)
class HistoricalRunStatus:
    schema_version: str
    run_id: str
    journal_seq: int
    scheduler_revision: int
    amendments: Mapping[int, str]
    handovers: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "run_id": self.run_id,
            "journal_seq": self.journal_seq,
            "scheduler_revision": self.scheduler_revision,
            "amendments": {str(key): value for key, value in self.amendments.items()},
            "handovers": dict(self.handovers), "read_only": True,
        }


def inspect_historical_run(private_root: Path | str, *, runtime=None) -> HistoricalRunStatus:
    """Authenticate saved v1-plan status without recompiling or mutating its run."""
    runtime = runtime or _runtime()
    runstate = sys.modules[f"{runtime.__name__}.runstate"]
    storage = sys.modules[f"{runtime.__name__}.storage"]
    source = Path(private_root)
    frozen = (source / "descriptor.json").is_file()
    if frozen:
        manifest_bytes = _read_file(str(source / "manifest.json"), limit=16_384)
        if _sha256(manifest_bytes) != _FROZEN_V1_FIXTURE_MANIFEST_SHA256:
            raise runtime.RunStateError("historical frozen fixture changed")
        manifest = _strict_json(manifest_bytes)
        if (not isinstance(manifest, dict)
                or set(manifest) != {"schema_version", "runtime_sha256", "compiler_sha256", "files"}
                or manifest["schema_version"] != "fanout-v1-frozen-fixture-v1"
                or not isinstance(manifest["files"], dict)
                or set(manifest["files"]) != {
                    "descriptor.json", "inputs.json", "owner.json", "events.jsonl",
                    "snapshot.json", "scheduler-meta.json", "scheduler-record.json",
                }
                or any(not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                       for digest in manifest["files"].values())
                or any(_sha256(_read_file(str(source / name), limit=32 * 1024 * 1024)) != digest
                       for name, digest in manifest["files"].items())):
            raise runtime.RunStateError("historical frozen fixture changed")
        envelope = _strict_json(_read_file(str(source / "descriptor.json")))
        if (not isinstance(envelope, dict)
                or set(envelope) != {"schema_version", "sha256", "document"}
                or envelope["schema_version"] != "fanout-private-envelope-v1"
                or envelope["sha256"] != _sha256(_canonical(envelope["document"]))):
            raise runtime.RunStateError("historical descriptor changed")
        descriptor = envelope["document"]
        records_root = source
        names = {"inputs": "inputs.json", "owner": "owner.json", "events": "events.jsonl",
                 "snapshot": "snapshot.json", "scheduler_meta": "scheduler-meta.json",
                 "scheduler_record": "scheduler-record.json"}
    else:
        descriptor = load_private_descriptor(source)
        records_root = Path(descriptor["run_root"])
        names = {"inputs": "inputs.json", "owner": "owner.json", "events": "events.jsonl",
                 "snapshot": "snapshot.json", "scheduler_meta": ".fanout-scheduler.meta.json",
                 "scheduler_record": ".fanout-scheduler.record.json"}
        if not (records_root / "snapshot.json").exists():
            del names["snapshot"]
    if (not isinstance(descriptor, dict)
            or descriptor.get("schema_version") != "fanout-execute-private-v1"
            or descriptor.get("runtime_sha256") == _runtime_digest(runtime)):
        raise runtime.RunStateError("historical status requires a changed v1 runtime")
    if frozen and manifest["runtime_sha256"] != descriptor["runtime_sha256"]:
        raise runtime.RunStateError("historical frozen runtime binding changed")
    try:
        raw = {key: _read_file(str(records_root / name), limit=32 * 1024 * 1024)
               for key, name in names.items()}
        parsed = {key: _strict_json(value) for key, value in raw.items() if key != "events"}
        if any(raw[key] != _canonical(parsed[key]) for key in parsed):
            raise ValueError("historical record is not canonical")
        inputs = runtime.RunInputs.from_dict(parsed["inputs"])
        if (inputs.to_dict() != descriptor["inputs"]
                or inputs.run_id != descriptor["run_id"]
                or inputs.targets is not None
                or (frozen and manifest["compiler_sha256"] != inputs.compiler_sha256)
                or descriptor["admission_packet"]["compiled"]["plan"]["schema_version"] != "v1"):
            raise ValueError("historical input binding changed")
        owner = parsed["owner"]
        capability = runtime.OwnerCapability.from_token(descriptor["owner_token"])
        key = capability._mac_key()
        unsigned_owner = {name: value for name, value in owner.items() if name != "mac"}
        if (owner["inputs_digest"] != inputs.digest
                or owner["capability_verifier"] != runstate._owner_verifier(
                    owner["capability_salt"], capability)
                or not hmac.compare_digest(
                    owner["mac"], runstate._mac(key, b"owner", unsigned_owner),
                )):
            raise ValueError("historical owner authentication failed")
        events, offset, tail = runstate._parse_events(raw["events"], runtime.RunLimits(), key)
        if tail or offset != len(raw["events"]):
            raise ValueError("historical journal has an incomplete tail")
        state = runstate._replay(events, inputs.digest, inputs.run_id)
        if "snapshot" in parsed:
            runstate._verify_snapshot(
                parsed["snapshot"], raw["events"], events, inputs.digest, inputs.run_id,
                key, owner["anchor_store_id"], owner["anchor_key"],
                (owner["journal_device"], owner["journal_inode"]),
                events[-1]["authority_revision"] if events else 1,
            )
        meta = parsed["scheduler_meta"]
        unsigned_meta = {name: value for name, value in meta.items() if name != "owner_mac"}
        expected_mac = hmac.digest(
            key, b"fanout-file-backend-v1" + runtime.canonical_json(unsigned_meta), "sha256",
        ).hex()
        if (not hmac.compare_digest(meta["owner_mac"], expected_mac)
                or meta["run_id"] != inputs.run_id
                or meta["run_root"] != descriptor["run_root"]):
            raise ValueError("historical scheduler authentication failed")
        scheduler = parsed["scheduler_record"]
        if (scheduler["snapshot_sha256"] != _sha256(_canonical(scheduler["snapshot"]))
                or scheduler["revision"] != scheduler["snapshot"]["backend_revision"]):
            raise ValueError("historical scheduler snapshot changed")
        snapshot = storage._snapshot_from_dict(scheduler["snapshot"])
        if (snapshot.run_id != inputs.run_id or snapshot.inputs_digest != inputs.digest
                or snapshot.backend_identity != meta["backend_identity"]
                or snapshot.backend_key != meta["backend_key"]):
            raise ValueError("historical scheduler binding changed")
        if not frozen:
            anchor = runtime.LocalAnchorAuthority(
                descriptor["authority_root"], run_root=descriptor["run_root"],
                repo_root=descriptor["repo_root"],
            )
            inspection = runtime.RunJournal.inspect(
                records_root, inputs, capability, anchor_store=anchor,
            )
            if inspection.state.to_dict() != state.to_dict():
                raise ValueError("historical authority differs from journal")
            scheduler_authority = sys.modules[f"{runtime.__name__}.scheduler_authority"]
            authority_key = scheduler_authority._authority_key(
                capability, inputs.run_id, snapshot.backend_identity, snapshot.backend_key,
            )
            _, authority_record = scheduler_authority._authority_read(
                anchor, anchor.identity, authority_key, inputs.run_id,
                snapshot.backend_identity, snapshot.backend_key, capability,
            )
            binding = scheduler_authority._snapshot_binding(snapshot)
            if binding not in (authority_record["committed"], authority_record["pending"]):
                raise ValueError("historical scheduler differs from authority")
        return HistoricalRunStatus(
            inputs.to_dict()["schema_version"], inputs.run_id, state.seq,
            snapshot.backend_revision,
            MappingProxyType({revision: amendment.phase
                              for revision, amendment in state.amendments.items()}),
            MappingProxyType({task_id: handover.phase
                              for task_id, handover in state.handovers.items()}),
        )
    except (KeyError, TypeError, ValueError, runtime.RunStateError) as error:
        raise runtime.RunStateError("historical run authentication failed") from error


def resume_run(private_root: Path | str, **kwargs):
    """Resume only runs built by the currently installed exact runtime."""
    runtime = kwargs.get("runtime") or _runtime()
    source = Path(private_root)
    if (source / "descriptor.json").is_file():
        raise runtime.RunStateError("restore the original runtime to resume a frozen historical run")
    descriptor = load_private_descriptor(source)
    if descriptor.get("runtime_sha256") != _runtime_digest(runtime):
        raise runtime.RunStateError("restore the original runtime to resume this run")
    return open_run(private_root, for_dispatch=True, **kwargs)


def open_run(private_root: Path | str, *, runtime=None, registry=None, memory_factory=None,
             memory_preflight: Callable[[], bool] | None = None,
             provider_runner=None, for_dispatch: bool = False) -> RunContext:
    """Rebuild the same service from private bytes and every authenticated store."""
    reject_nested(os.environ)
    runtime = runtime or _runtime()
    descriptor = load_private_descriptor(private_root)
    if _runtime_digest(runtime) != descriptor.get("runtime_sha256"):
        raise ValueError("fanout runtime source changed before cold reopen")
    repository = Path(descriptor["repo_root"])
    run = Path(descriptor["run_root"])
    authority = Path(descriptor["authority_root"])
    private = Path(private_root)
    _separate_paths(repository, run, authority, private)
    resolver = _resolver(runtime, descriptor["skill_roots"])
    compiled = verify_admission(descriptor["admission_packet"], runtime, resolver)
    initial_plan = compiled.plan
    if for_dispatch:
        _require_native_seat_boundary(initial_plan)
    adapter_catalog = registry or runtime.ProviderRegistry.default()
    initial_inputs = runtime.RunInputs.from_dict(descriptor["inputs"])
    registry = _initial_registry(runtime, descriptor, adapter_catalog, initial_inputs)
    initial_bundles = {task.id: _task_skill_bundle(runtime, resolver, initial_plan, task.id)
               for task in initial_plan.tasks if task.kind == "work"
               and task.execution_class != "orchestrator-action"}
    if memory_factory is None:
        memory_factory, health = _real_memory(runtime)
        memory_preflight = health
    elif memory_preflight is None:
        raise ValueError("memory preflight is required")
    owner = runtime.OwnerCapability.from_token(descriptor["owner_token"])
    lifecycle = runtime.resume_lifecycle_controller(
        run / "lifecycle", runtime.LifecycleCapability(descriptor["controller_token"]),
    )
    anchor = runtime.LocalAnchorAuthority(
        authority, run_root=run, repo_root=repository,
    )
    inspection = None
    if for_dispatch:
        inspection = runtime.RunJournal.inspect(
            run, initial_inputs, owner, anchor_store=anchor,
        )
        if inspection.pending_authority:
            raise ValueError("pending journal authority needs explicit status recovery before resume")
        if inspection.state.amendments:
            with runtime.ArtifactStore.open_existing(run / "artifacts") as preview_artifacts:
                for revision in sorted(inspection.state.amendments):
                    documents = runtime.ExecutionService.recovery_documents(
                        inspection, preview_artifacts, revision=revision,
                    )
                    _require_native_seat_boundary(documents.plan)
    resume_options = {"anchor_store": anchor}
    if inspection is not None:
        resume_options["expected_inspection"] = inspection
    journal = runtime.RunJournal.resume(run, initial_inputs, owner, **resume_options)
    artifacts = runtime.ArtifactStore(run / "artifacts")
    scheduler_backend = runtime.FileSchedulerBackend.resume(
        run, run_id=initial_inputs.run_id, owner=owner,
    )
    execution_backend = runtime.FileExecutionBackend.resume(
        run, run_id=initial_inputs.run_id, owner=owner,
    )
    try:
        baseline = _load_baseline(
            runtime, artifacts, descriptor["baseline_manifest_ref"], repository,
        )
        expected_initial = _dependency_inputs(
            runtime, compiled, initial_plan, descriptor["run_id"], baseline,
            registry, initial_bundles,
            profile_digests=initial_inputs.provider_profiles,
        )
        if initial_inputs.digest != expected_initial.digest:
            raise ValueError("run inputs changed before cold reopen")
        plan, inputs, plan_revision = _accepted_plan_inputs(
            runtime, journal, artifacts, execution_backend, initial_plan, initial_inputs,
        )
        if for_dispatch:
            _require_native_seat_boundary(plan)
        registry = _accepted_registry(
            runtime, artifacts, registry, inputs, adapter_catalog=adapter_catalog,
        )
        bundles = {task.id: _task_skill_bundle(runtime, resolver, plan, task.id)
                   for task in plan.tasks if task.kind == "work"
                   and task.execution_class != "orchestrator-action"}
        if (inputs.compiled_plan_sha256 != _sha256(runtime.canonical_json(plan.to_dict()))
                or inputs.repo_baseline_sha256 != baseline.digest
                or inputs.skill_manifests != {task_id: _sha256(bundle)
                                               for task_id, bundle in bundles.items()}):
            raise ValueError("accepted amendment plan, baseline, or skill closure changed")
        memory = memory_factory(artifacts, memory_preflight)
        workspace_evidence = (
            descriptor["workspace_evidence"] if plan_revision == 1 else
            _recovered_workspace_evidence(runtime, lifecycle, plan, baseline)
        )
        preparations, _ = _build_preparations(
            runtime, plan=plan, inputs=inputs, resolver=resolver, bundles=bundles,
            source_markdown=descriptor["admission_packet"]["source_markdown"].encode("utf-8"),
            repository=repository, run_root=run, baseline=baseline,
            controller=lifecycle, artifacts=artifacts, registry=registry,
            restore=True, workspace_evidence=workspace_evidence,
            plan_revision=plan_revision,
            require_provider_guards=for_dispatch,
        )
        scheduler = runtime.resume_scheduler_from_amendments(
            initial_plan=initial_plan, journal=journal, backend=scheduler_backend,
            artifacts=artifacts, owner=owner, anchor_store=anchor,
        )
        coordinator = runtime.CollaborationCoordinator(
            artifacts=artifacts, journal=journal, owner=owner, memory=memory,
            registry=registry, provider_runner=provider_runner or runtime.run_provider,
            lifecycle_controller=lifecycle, repository_baseline=baseline,
            slot_root=run / "slots",
        )
        service = runtime.ExecutionService(
            plan=plan, inputs=inputs, preparations=preparations,
            provider_profile_digests=inputs.provider_profiles,
            budget=runtime.ExecutionBudget(descriptor["budget"]),
            scheduler=scheduler, journal=journal, coordinator=coordinator,
            artifacts=artifacts, backend=execution_backend,
            memory_preflight=memory.preflight, baseline=baseline,
            lifecycle_controller=lifecycle,
        )
        return RunContext(service, owner, journal, artifacts,
                          scheduler_backend, execution_backend)
    except BaseException:
        journal.close()
        artifacts.close()
        scheduler_backend.close()
        execution_backend.close()
        raise


def _read_file(path: str, *, limit: int = _MAX_PACKET_BYTES) -> bytes:
    source = Path(path)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("input must be a bounded regular file")
        raw = os.read(descriptor, info.st_size + 1)
        if len(raw) != info.st_size:
            raise ValueError("input changed while reading")
        return raw
    finally:
        os.close(descriptor)


def _read_ref(path: str, runtime):
    value = _strict_json(_read_file(path, limit=16_384))
    if not isinstance(value, dict) or set(value) != {"path", "digest", "size"}:
        raise ValueError("artifact reference file is invalid")
    return runtime.ArtifactRef(value["path"], value["digest"], value["size"])


def _ref_document(ref) -> dict[str, object]:
    return {"path": ref.path, "digest": ref.digest, "size": ref.size}


def _result_document(result) -> dict[str, object]:
    return {"task_id": result.task_id, "plan_revision": result.plan_revision,
            "artifact": _ref_document(result.artifact)}


def _action_approval(path: str, *, action: str, run_id: str, task_id: str,
                     evidence: object) -> None:
    review = _strict_json(_read_file(path, limit=16_384))
    if (not isinstance(review, dict)
            or set(review) != {"schema_version", "action", "run_id", "task_id",
                              "evidence_sha256", "reviewer"}
            or review["schema_version"] != "fanout-owner-action-v1"
            or review["action"] != action or review["run_id"] != run_id
            or review["task_id"] != task_id
            or not isinstance(review["reviewer"], str) or not review["reviewer"].strip()
            or review["evidence_sha256"] != _sha256(_canonical(evidence))):
        raise ValueError("owner action approval differs from exact evidence")


def _private_write_or_verify(root: Path, name: str, data: bytes) -> Path:
    _private_dir(root)
    path = root / name
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise ValueError("private answer file could not be created") from error
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size != len(data) or os.read(descriptor, len(data) + 1) != data):
                raise ValueError("private answer file changed")
        finally:
            os.close(descriptor)
        return path
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return path


def _inspect_answers(opened: RunContext, task_id: str, private_root: Path) -> dict[str, object]:
    service = opened.service
    task = service._task(task_id)
    if task.execution_class != "read-only":
        raise ValueError("answer inspection requires read-only work")
    policy = task.provider_policy or service.plan.defaults
    state = service._execution_state(task_id)
    if (len(state.barriers) != policy.rounds
            or service.scheduler.task_phase(task_id) not in {"reconciliation-pending", "completed"}):
        raise ValueError("final answer barrier is not complete")
    barrier = service._restore_barrier(state.barriers[-1], service._packet_for(task))
    seats = []
    for terminal in barrier.valid_terminals:
        ref = terminal.answer_ref
        if ref is None:
            raise ValueError("final seat answer is unavailable")
        answer = opened.artifacts.read_bytes(ref).decode("utf-8")
        seats.append({
            "seat_id": terminal.seat_id, "executor_id": terminal.executor_id,
            "answer": answer, "answer_ref": _ref_document(ref),
            "requested_model": terminal.requested_model,
            "observed_model": terminal.observed_model,
            "profile_sha256": terminal.profile_sha256,
        })
    if len(seats) < policy.minimum_success:
        raise ValueError("final answer barrier lacks minimum successful seats")
    document = {"schema_version": "fanout-private-final-answers-v1",
                "run_id": service.inputs.run_id, "task_id": task_id,
                "barrier_sha256": state.barriers[-1].digest, "seats": seats}
    raw = _canonical(document)
    path = _private_write_or_verify(
        private_root, "answers-" + _sha256(task_id.encode())[:24] + ".json", raw,
    )
    return {"task_id": task_id, "private_answers_file": str(path),
            "sha256": _sha256(raw), "seat_count": len(seats)}


def _inspect_candidates(opened: RunContext, task_id: str,
                        private_root: Path, runtime) -> dict[str, object]:
    service = opened.service
    task = service._task(task_id)
    if task.execution_class != "repo-write":
        raise ValueError("candidate inspection requires repo-write work")
    policy = task.provider_policy or service.plan.defaults
    state = service._execution_state(task_id)
    if (len(state.barriers) != policy.rounds
            or service.scheduler.task_phase(task_id) not in {"reconciliation-pending", "completed"}):
        raise ValueError("final candidate barrier is not complete")
    candidates = service.collect(task_id, owner=opened.owner)
    barrier = service._restore_barrier(state.barriers[-1], service._packet_for(task))
    if barrier.status != "round-complete" or len(candidates) < policy.minimum_success:
        raise ValueError("final candidate barrier lacks minimum successful seats")
    sources = dict(barrier.candidate_sources)
    if len(sources) != len(candidates) or set(sources) != {item.seat_id for item in candidates}:
        raise ValueError("final candidate sources differ from collected seats")

    verified = []
    for item in candidates:
        ref = sources[item.seat_id]
        if ref.size > _MAX_CANDIDATE_MANIFEST_BYTES:
            raise ValueError("final candidate manifest exceeds byte limit")
        raw = opened.artifacts.read_bytes(ref)
        candidate = runtime.CandidateBundle.from_manifest(raw)
        if candidate.digest != ref.digest or raw != item.candidate.manifest_bytes:
            raise ValueError("final candidate artifact changed")
        verified.append((item.seat_id, ref, raw))

    exported = []
    for seat_id, ref, raw in verified:
        name = "candidate-" + _sha256(
            f"{service.inputs.run_id}\0{task_id}\0{seat_id}".encode(),
        ) + ".json"
        path = _private_write_or_verify(private_root, name, raw)
        exported.append({
            "seat_id": seat_id, "candidate_ref": _ref_document(ref),
            "candidate_sha256": ref.digest, "private_manifest_file": str(path),
            "private_manifest_sha256": _sha256(raw),
        })
    document = {
        "schema_version": "fanout-private-final-candidates-v1",
        "run_id": service.inputs.run_id, "task_id": task_id,
        "barrier_sha256": state.barriers[-1].digest, "candidates": exported,
    }
    raw_index = _canonical(document)
    index_path = _private_write_or_verify(
        private_root, "candidates-" + _sha256(task_id.encode()) + ".json", raw_index,
    )
    return {"private_candidates_file": str(index_path), "sha256": _sha256(raw_index),
            "candidate_count": len(exported)}


def _unresolved_uncertain_status(opened: RunContext) -> dict[str, object]:
    state = opened.journal.state
    unresolved = sorted(
        key for key, phase in state.seat_phases.items()
        if phase == "uncertain-attempt" and key not in state.uncertainty_resolutions
    )
    visible = unresolved[:_UNCERTAIN_STATUS_LIMIT]
    return {
        "unresolved_uncertain": [
            {"task_id": task_id, "seat_id": seat_id, "attempt": attempt, "round": round_number}
            for task_id, seat_id, attempt, round_number in visible
        ],
        "unresolved_uncertain_total": len(unresolved),
        "unresolved_uncertain_remaining": len(unresolved) - len(visible),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str):
        subcommand = commands.add_parser(name, help=help_text)
        subcommand.add_argument("--private-root", required=True)
        return subcommand

    start = command("start", "admit and schedule one new run without launching seats")
    start.add_argument("--admission-file", required=True)
    start.add_argument("--repo-root", required=True)
    start.add_argument("--run-root", required=True)
    start.add_argument("--authority-root", required=True)
    start.add_argument("--skill-root", action="append", required=True)
    start.add_argument("--max-turns", type=int, required=True)
    command("recover-start", "rebuild a missing final descriptor from private pre-spend escrow")
    command("status", "read safe durable task metadata")
    command("resume", "advance a started run from its durable barriers")
    uncertain = command("recover-uncertain", "exclude one uncertain source turn by owner action")
    uncertain.add_argument("--task-id", required=True)
    uncertain.add_argument("--seat-id", required=True)
    uncertain.add_argument("--attempt", type=int, required=True)
    uncertain.add_argument("--round", type=int, required=True)
    command("recover-amendment", "replay one authenticated pending amendment after authority interruption")
    command("abandon-amendment", "abandon only an old-authority amendment intent")
    collect = command("collect", "list exact candidate manifests without changing the caller")
    collect.add_argument("--task-id", required=True)
    inspect = command("inspect-answers", "write final-seat answers to an owner-only evidence file")
    inspect.add_argument("--task-id", required=True)
    inspect_candidates = command(
        "inspect-candidates", "write final-seat candidate manifests to owner-only evidence files",
    )
    inspect_candidates.add_argument("--task-id", required=True)
    synthesis = command("synthesize-answer", "verify and submit a new read-only answer")
    synthesis.add_argument("--task-id", required=True)
    synthesis.add_argument("--source-seat", action="append", required=True)
    synthesis.add_argument("--synthesizer-id", required=True)
    synthesis.add_argument("--answer-file", required=True)
    selection = command("select-answer", "submit an exact final-seat answer")
    selection.add_argument("--task-id", required=True)
    selection.add_argument("--seat-id", required=True)
    selection.add_argument("--answer-ref-file")
    repository = command("synthesize-repository", "verify and submit a fresh repo synthesis")
    repository.add_argument("--task-id", required=True)
    repository.add_argument("--candidate-file", required=True)
    handover = command("handover", "transactionally deliver a verified repository result")
    handover.add_argument("--task-id", required=True)
    handover.add_argument("--candidate-ref-file", required=True)
    handover.add_argument("--owner-approval-file", required=True)
    recover_handover = command("recover-handover", "resolve an exact pending handover transaction")
    recover_handover.add_argument("--task-id", required=True)
    recover_handover.add_argument("--candidate-ref-file", required=True)
    recover_handover.add_argument("--transaction-root", required=True)
    recover_handover.add_argument("--owner-approval-file", required=True)
    action = command("action-complete", "complete an orchestrator-only action barrier")
    action.add_argument("--task-id", required=True)
    action.add_argument("--artifact-ref-file", required=True)
    action.add_argument("--owner-approval-file", required=True)
    return parser


def main(argv: list[str] | None = None, *, runtime=None, registry=None,
         memory_factory=None, memory_preflight=None, provider_runner=None) -> int:
    args = _parser().parse_args(argv)
    try:
        reject_nested(os.environ)
        runtime = runtime or _runtime()
        common = dict(runtime=runtime, registry=registry, memory_factory=memory_factory,
                      memory_preflight=memory_preflight, provider_runner=provider_runner)
        if args.command == "start":
            packet = _strict_json(_read_file(args.admission_file))
            skill_roots = tuple(Path(path) for path in args.skill_root)
            admitted = verify_admission(packet, runtime, _resolver(runtime, skill_roots))
            _require_turn_budget(admitted.plan, args.max_turns, runtime)
            _require_native_seat_boundary(admitted.plan)
            status = prepare_run(
                packet, repo_root=Path(args.repo_root), run_root=Path(args.run_root),
                authority_root=Path(args.authority_root), private_root=Path(args.private_root),
                skill_roots=skill_roots,
                budget=args.max_turns, **common,
            )
            print(_canonical(status).decode("utf-8"), end="")
            return 0
        if args.command == "recover-start":
            result = recover_start(args.private_root, runtime=runtime, registry=registry)
            print(_canonical(result).decode("utf-8"), end="")
            return 0
        if args.command in {"status", "resume"}:
            private_root = Path(args.private_root)
            if (private_root / "descriptor.json").is_file():
                if args.command == "resume":
                    raise runtime.RunStateError("restore the original runtime to resume this run")
                print(_canonical(inspect_historical_run(private_root, runtime=runtime).to_dict()).decode("utf-8"), end="")
                return 0
            if (private_root / _DESCRIPTOR).is_file():
                descriptor = load_private_descriptor(private_root)
                if descriptor.get("runtime_sha256") != _runtime_digest(runtime):
                    if args.command == "resume":
                        raise runtime.RunStateError("restore the original runtime to resume this run")
                    print(_canonical(inspect_historical_run(private_root, runtime=runtime).to_dict()).decode("utf-8"), end="")
                    return 0
        with open_run(args.private_root, for_dispatch=args.command == "resume", **common) as opened:
            service, owner = opened.service, opened.owner
            if args.command == "status":
                result = {**service.status().to_dict(), **_unresolved_uncertain_status(opened)}
            elif args.command == "resume":
                status = service.status()
                result = (service.start(owner=owner) if status.authority_state == "resolved"
                          and status.execution_revision == 0 else
                          service.resume(owner=owner)).to_dict()
            elif args.command == "recover-uncertain":
                opened.journal.resolve_uncertain(
                    owner, task_id=args.task_id, seat_id=args.seat_id,
                    attempt=args.attempt, round=args.round, disposition="exclude",
                )
                result = service.status().to_dict()
            elif args.command == "recover-amendment":
                result = recover_amendment(
                    opened, args.private_root, runtime=runtime, adapter_catalog=registry,
                )
            elif args.command == "abandon-amendment":
                service.abandon_pending_amendment(owner=owner)
                result = service.status().to_dict()
            elif args.command == "collect":
                task = next(task for task in service.plan.tasks if task.id == args.task_id)
                policy = task.provider_policy or service.plan.defaults
                candidates = service.collect(args.task_id, owner=owner)
                result = {"task_id": args.task_id, "candidates": []}
                for item in candidates:
                    path = runtime.source_candidate_artifact_path(
                        service.inputs.run_id, args.task_id, 1, policy.rounds, item.seat_id,
                    )
                    ref = runtime.ArtifactRef(
                        path, item.candidate.digest, len(item.candidate.manifest_bytes),
                    )
                    if opened.artifacts.read_bytes(ref) != item.candidate.manifest_bytes:
                        raise ValueError("collected candidate artifact changed")
                    result["candidates"].append({"seat_id": item.seat_id,
                                                 "candidate": _ref_document(ref)})
            elif args.command == "inspect-answers":
                result = _inspect_answers(opened, args.task_id, Path(args.private_root))
            elif args.command == "inspect-candidates":
                result = _inspect_candidates(opened, args.task_id, Path(args.private_root), runtime)
            elif args.command == "synthesize-answer":
                answer = _read_file(args.answer_file, limit=3 * 1024 * 1024)
                verified = service.verify_read_only_synthesis(
                    args.task_id, source_seat_ids=args.source_seat,
                    synthesizer_id=args.synthesizer_id, answer=answer, owner=owner,
                )
                if not verified.valid:
                    raise ValueError("reconciled answer failed fresh verification")
                result = _result_document(service.submit(args.task_id, verified, owner=owner))
            elif args.command == "select-answer":
                task = next(task for task in service.plan.tasks if task.id == args.task_id)
                if task.checks:
                    evidence = service.verify_read_only_selection(
                        args.task_id, args.seat_id, owner=owner,
                    )
                    if not evidence.valid:
                        raise ValueError("selected answer failed fresh verification")
                else:
                    if args.answer_ref_file is None:
                        raise ValueError("unchecked selection needs an exact answer reference file")
                    evidence = _read_ref(args.answer_ref_file, runtime)
                    state = service._execution_state(args.task_id)
                    policy = task.provider_policy or service.plan.defaults
                    if len(state.barriers) != policy.rounds:
                        raise ValueError("selected answer has no final provider barrier")
                    barrier = service._restore_barrier(state.barriers[-1],
                                                       service._packet_for(task))
                    selected = next((terminal for terminal in barrier.valid_terminals
                                     if terminal.seat_id == args.seat_id), None)
                    if selected is None or selected.answer_ref != evidence:
                        raise ValueError("selected answer differs from the named final seat")
                result = _result_document(service.submit(args.task_id, evidence, owner=owner))
            elif args.command == "synthesize-repository":
                candidate = runtime.CandidateBundle.from_manifest(
                    _read_file(args.candidate_file, limit=_MAX_CANDIDATE_MANIFEST_BYTES),
                )
                verified = service.verify_repo_synthesis(args.task_id, candidate, owner=owner)
                if not verified.valid:
                    raise ValueError("repository synthesis failed fresh verification")
                result = _result_document(service.submit(args.task_id, verified, owner=owner))
            elif args.command in {"handover", "recover-handover"}:
                ref = _read_ref(args.candidate_ref_file, runtime)
                action = args.command
                evidence = {"candidate": _ref_document(ref)}
                if action == "recover-handover":
                    evidence["transaction_root"] = args.transaction_root
                _action_approval(args.owner_approval_file, action=action,
                                 run_id=service.inputs.run_id, task_id=args.task_id,
                                 evidence=evidence)
                verified = runtime.load_candidate_verification(
                    service.lifecycle_controller, opened.artifacts,
                    task_id=args.task_id, candidate_ref=ref,
                )
                outcome = (
                    service.handover(args.task_id, verified, owner=owner)
                    if action == "handover" else
                    service.recover_handover(
                        args.task_id, args.transaction_root, verified, owner=owner,
                    )
                )
                result = {"task_id": args.task_id, "status": outcome.status,
                          "candidate_sha256": outcome.candidate_digest,
                          "transaction_root": str(outcome.transaction_root)}
            elif args.command == "action-complete":
                ref = _read_ref(args.artifact_ref_file, runtime)
                _action_approval(args.owner_approval_file, action="action-complete",
                                 run_id=service.inputs.run_id, task_id=args.task_id,
                                 evidence={"artifact": _ref_document(ref)})
                result = _result_document(service.action_complete(args.task_id, ref, owner=owner))
            else:
                raise ValueError("unsupported fanout command")
        print(_canonical(result).decode("utf-8"), end="")
        return 0
    except Exception as error:
        print(f"fanout execute: {error.__class__.__name__}: {str(error)[:300]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
