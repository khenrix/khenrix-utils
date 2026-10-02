"""Owner-held machine route admission; this module makes no provider calls."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .container_transfer_support import canonical_json


DATA_CLASSES = {"non-sensitive": 0, "protected": 1}
AUTH_FAMILIES = {"vertex-adc", "openai-keychain", "openai-1password",
                 "claude-subscription", "codex-subscription", "agy-subscription"}
_EXECUTION_CLASSES = {"read-only", "repo-write"}
_MAX_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_PROJECT = re.compile(r"[a-z][a-z0-9_-]{2,63}\Z")
_OPENAI_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+|:-]{0,127}\Z")
_KEYCHAIN_REF = re.compile("keychain:" + _ACCOUNT.pattern)
_OP_REF = re.compile(r"op://[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
_SUPPORTED_MODELS = {
    "claude": ("claude-opus-5-5", "claude-opus-5-5", "claude-opus-5-5"),
    "codex": ("gpt-6.1-sol", "gpt-6.1-sol", "gpt-6.1-sol"),
    "agy": ("gemini-3.8-flash-high", "gemini-3.8-flash", "gemini-3.8-flash"),
}
_VERTEX_ORIGINS = {
    "eu": "https://aiplatform.eu.rep.googleapis.com",
    "europe-west1": "https://europe-west1-aiplatform.googleapis.com",
    "europe-west4": "https://europe-west4-aiplatform.googleapis.com",
}
_SUBSCRIPTION = {
    "claude": ("claude-subscription", "https://api.anthropic.com"),
    "codex": ("codex-subscription", "https://api.openai.com"),
    "agy": ("agy-subscription", "https://generativelanguage.googleapis.com"),
}
_PROFILE_FIELDS = frozenset({
    "route_id", "provider", "auth_family", "api_family", "origin", "project",
    "location", "requested_model", "wire_model", "effective_model", "credential_ref",
    "max_data_classification", "execution_classes", "transform_version",
})
_POLICY_FIELDS = _PROFILE_FIELDS - {"credential_ref"}


class RouteRejected(ValueError):
    """A machine route is absent, unsafe, or outside its owner-held policy."""


@dataclass(frozen=True, slots=True)
class RouteProfile:
    route_id: str
    provider: str
    auth_family: str
    api_family: str
    origin: str
    project: str | None
    location: str | None
    requested_model: str
    wire_model: str
    effective_model: str
    credential_ref: str = field(repr=False)
    max_data_classification: str
    execution_classes: tuple[str, ...]
    transform_version: int


@dataclass(frozen=True, slots=True)
class MachineRoutes:
    machine_id: str
    policy_digest: str
    manifest_digest: str
    routes: tuple[RouteProfile, ...]


@dataclass(frozen=True, slots=True)
class RouteAdmission:
    machine_id: str
    policy_digest: str
    manifest_digest: str
    profile: RouteProfile

    @property
    def digest(self) -> str:
        descriptor = asdict(self)
        descriptor["profile"].pop("credential_ref")
        return hashlib.sha256(canonical_json(descriptor)).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedOutbound:
    origin: str
    path: str
    body: bytes
    effective_model: str
    route_digest: str


def _require_keys(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or value.keys() != expected:
        raise RouteRejected(f"{label} fields are invalid")
    return value


def _identifier(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise RouteRejected(f"{label} is invalid")
    return value


def _duplicate_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RouteRejected("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise RouteRejected("non-finite JSON number")


def _open_parent(path: Path) -> int:
    if path.anchor != "/" or not path.name or ".." in path.parts:
        raise RouteRejected("route file path is invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open("/", flags)
    try:
        for part in path.parts[1:-1]:
            child_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _read_owned_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        parent_fd = _open_parent(path)
        try:
            before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or not 0 < before.st_size <= _MAX_BYTES):
                raise RouteRejected("route file ownership, mode, or size is invalid")
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
            try:
                opened = os.fstat(fd)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                        or stat.S_IMODE(opened.st_mode) != 0o600
                        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                            opened.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_size,
                                                    before.st_mtime_ns, before.st_ctime_ns)):
                    raise RouteRejected("route file changed or is unsafe")
                chunks: list[bytes] = []
                remaining = opened.st_size
                while remaining:
                    chunk = os.read(fd, min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                after = os.fstat(fd)
                if (len(raw) != opened.st_size or len(raw) > _MAX_BYTES
                        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                            after.st_ctime_ns) != (opened.st_dev, opened.st_ino,
                                                   opened.st_size, opened.st_mtime_ns,
                                                   opened.st_ctime_ns)):
                    raise RouteRejected("route file changed or exceeds byte budget")
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)
    except OSError as error:
        raise RouteRejected("route file cannot be read safely") from error
    try:
        value = json.loads(raw, object_pairs_hook=_duplicate_key,
                           parse_constant=_invalid_constant)
        if type(value) is not dict or canonical_json(value) != raw:
            raise RouteRejected("route JSON is not canonical")
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise RouteRejected("route JSON is invalid or noncanonical") from error
    return value, raw


def _validate_profile(value: Any) -> RouteProfile:
    item = _require_keys(value, _PROFILE_FIELDS, "route")
    route_id = _identifier(item["route_id"], _IDENTIFIER, "route ID")
    provider = item["provider"]
    if type(provider) is not str or provider not in _SUBSCRIPTION:
        raise RouteRejected("route provider is unsupported")
    family = item["auth_family"]
    if type(family) is not str or family not in AUTH_FAMILIES:
        raise RouteRejected("route auth family is unsupported")
    if (type(item["max_data_classification"]) is not str
            or item["max_data_classification"] not in DATA_CLASSES):
        raise RouteRejected("route data class is invalid")
    classes = item["execution_classes"]
    if (type(classes) is not list or not classes
            or any(type(name) is not str or name not in _EXECUTION_CLASSES for name in classes)
            or len(classes) != len(set(classes))):
        raise RouteRejected("route execution classes are invalid")
    if type(item["transform_version"]) is not int or item["transform_version"] != 1:
        raise RouteRejected("route transform version is unsupported")
    models = tuple(item[name] for name in ("requested_model", "wire_model", "effective_model"))
    if models != _SUPPORTED_MODELS[provider]:
        raise RouteRejected("route provider or model differs")
    origin = item["origin"]
    api_family = item["api_family"]
    project = item["project"]
    location = item["location"]
    credential_ref = item["credential_ref"]
    if family == "vertex-adc":
        expected_api = {"claude": "anthropic-vertex-messages",
                        "agy": "google-vertex-generate-content"}.get(provider)
        if (expected_api is None or api_family != expected_api
                or type(location) is not str or location not in _VERTEX_ORIGINS
                or origin != _VERTEX_ORIGINS[location]
                or project is None or credential_ref != "gcloud-adc"):
            raise RouteRejected("Vertex route origin, project, or credential differs")
        _identifier(project, _PROJECT, "route project")
    elif family in {"openai-keychain", "openai-1password"}:
        if (provider != "codex" or api_family != "openai-responses"
                or origin != "https://api.openai.com" or location is not None
                or project is None):
            raise RouteRejected("OpenAI route origin or project differs")
        _identifier(project, _OPENAI_PROJECT, "route project")
        if family == "openai-keychain":
            if type(credential_ref) is not str or not _KEYCHAIN_REF.fullmatch(credential_ref):
                raise RouteRejected("Keychain selector is invalid")
        elif (type(credential_ref) is not str or not _OP_REF.fullmatch(credential_ref)
              or credential_ref.split("/")[2] in {"Private", "Personal"}):
            raise RouteRejected("1Password selector is invalid")
    else:
        expected_family, expected_origin = _SUBSCRIPTION[provider]
        if (family != expected_family or api_family != expected_family
                or origin != expected_origin or project is not None or location is not None
                or credential_ref != f"adapter:{provider}" or classes != ["read-only"]):
            raise RouteRejected("subscription route authority or origin is unsupported")
    return RouteProfile(route_id=route_id, provider=provider, auth_family=family,
                        api_family=api_family, origin=origin, project=project,
                        location=location, requested_model=item["requested_model"],
                        wire_model=item["wire_model"], effective_model=item["effective_model"],
                        credential_ref=credential_ref,
                        max_data_classification=item["max_data_classification"],
                        execution_classes=tuple(classes),
                        transform_version=item["transform_version"])


def read_machine_routes(path: Path, policy_path: Path) -> MachineRoutes:
    """Read an exact owner-held manifest and bind every route to policy bytes."""
    policy, policy_raw = _read_owned_json(Path(policy_path))
    manifest, manifest_raw = _read_owned_json(Path(path))
    _require_keys(policy, frozenset({"schema", "machine_id", "routes"}), "policy")
    _require_keys(manifest, frozenset({"schema", "machine_id", "policy_digest", "routes"}),
                  "manifest")
    if policy["schema"] != "fanout-machine-policy-v1":
        raise RouteRejected("policy schema is unsupported")
    if manifest["schema"] != "fanout-machine-routes-v1":
        raise RouteRejected("manifest schema is unsupported")
    machine_id = _identifier(policy["machine_id"], _IDENTIFIER, "machine ID")
    if manifest["machine_id"] != machine_id:
        raise RouteRejected("policy machine differs")
    policy_digest = hashlib.sha256(policy_raw).hexdigest()
    if (type(manifest["policy_digest"]) is not str
            or _SHA256.fullmatch(manifest["policy_digest"]) is None
            or manifest["policy_digest"] != policy_digest):
        raise RouteRejected("policy digest differs")
    policy_routes = policy["routes"]
    manifest_routes = manifest["routes"]
    if (type(policy_routes) is not list or type(manifest_routes) is not list
            or not policy_routes or not manifest_routes or len(manifest_routes) > len(policy_routes)):
        raise RouteRejected("policy routes are invalid")
    descriptors = []
    policy_ids = []
    for value in policy_routes:
        item = _require_keys(value, _POLICY_FIELDS, "policy route")
        policy_ids.append(_identifier(item["route_id"], _IDENTIFIER, "policy route ID"))
        descriptors.append(canonical_json(item))
    if len(descriptors) != len(set(descriptors)) or len(policy_ids) != len(set(policy_ids)):
        raise RouteRejected("policy route is duplicated")
    routes = tuple(_validate_profile(value) for value in manifest_routes)
    if len({route.route_id for route in routes}) != len(routes):
        raise RouteRejected("route ID is duplicated")
    for route in routes:
        descriptor = asdict(route)
        descriptor.pop("credential_ref")
        descriptor["execution_classes"] = list(route.execution_classes)
        if descriptors.count(canonical_json(descriptor)) != 1:
            raise RouteRejected("route differs from policy")
    return MachineRoutes(machine_id, policy_digest,
                         hashlib.sha256(manifest_raw).hexdigest(), routes)


def select_route(manifest: MachineRoutes, *, route_id: str, provider: str,
                 requested_model: str, execution_class: str,
                 data_classification: str | None = None) -> RouteAdmission:
    """Select exactly one admitted route; no fallback or certificate is issued."""
    classification = "protected" if data_classification is None else data_classification
    if type(classification) is not str or classification not in DATA_CLASSES:
        raise RouteRejected("data class is invalid")
    matches = [item for item in manifest.routes if item.route_id == route_id]
    if len(matches) != 1:
        raise RouteRejected("route is unavailable")
    route = matches[0]
    if route.provider != provider or route.requested_model != requested_model:
        raise RouteRejected("route provider or model differs")
    if DATA_CLASSES[classification] > DATA_CLASSES[route.max_data_classification]:
        raise RouteRejected("data class exceeds route policy")
    if execution_class not in route.execution_classes:
        raise RouteRejected("execution class is unavailable")
    return RouteAdmission(manifest.machine_id, manifest.policy_digest,
                          manifest.manifest_digest, route)
