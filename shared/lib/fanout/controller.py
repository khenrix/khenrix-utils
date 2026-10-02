"""Private controller-root capabilities for Task 14 verifier and lifecycle evidence."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

from .artifacts import canonical_json
from .errors import LifecycleError


_SCHEMA = "fanout-lifecycle-controller-v1"
_EVIDENCE_SCHEMA = "fanout-lifecycle-evidence-v1"
_RECORD = "controller.json"
_DIRECTORIES = ("receipts", "transactions", "verifier", "locks", "quarantine")
_READ_LIMIT = 64 * 1024


class LifecycleCapability:
    """Opaque recovery capability for one exclusively created controller root."""

    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        if not isinstance(secret, str) or len(secret) < 32:
            raise LifecycleError("controller capability is malformed")
        try:
            secret.encode("ascii")
        except UnicodeEncodeError as error:
            raise LifecycleError("controller capability is malformed") from error
        self._secret = secret

    def export_token(self) -> str:
        """Return the token only for owner-controlled recovery storage."""
        return self._secret

    def __repr__(self) -> str:
        return "LifecycleCapability(<redacted>)"

    __str__ = __repr__


class LifecycleController:
    """An authenticated in-memory handle to one exclusively created run root."""

    __slots__ = ("root", "controller_id", "_secret", "_root_identity")

    def __init__(self, *_: object, **__: object) -> None:
        raise LifecycleError("lifecycle controllers are issued by create_lifecycle_controller")

    @classmethod
    def _issue(
        cls,
        root: Path,
        controller_id: str,
        secret: str,
        root_identity: tuple[int, int],
    ) -> "LifecycleController":
        result = object.__new__(cls)
        object.__setattr__(result, "root", root)
        object.__setattr__(result, "controller_id", controller_id)
        object.__setattr__(result, "_secret", secret)
        object.__setattr__(result, "_root_identity", root_identity)
        return result

    @property
    def capability(self) -> LifecycleCapability:
        return LifecycleCapability(self._secret)


def create_lifecycle_controller(
    root: Path | str, *, capability: LifecycleCapability | None = None,
) -> LifecycleController:
    """Create an exclusive controller, optionally using a pre-escrowed capability."""
    if capability is not None and not isinstance(capability, LifecycleCapability):
        raise LifecycleError("controller capability is invalid")
    requested = Path(root)
    if not requested.is_absolute():
        requested = requested.absolute()
    parent = requested.parent.resolve()
    try:
        parent_info = parent.lstat()
    except OSError as error:
        raise LifecycleError("controller root parent is unavailable") from error
    if parent.is_symlink() or not stat.S_ISDIR(parent_info.st_mode):
        raise LifecycleError("controller root parent must be a real directory")
    path = parent / requested.name
    try:
        os.mkdir(path, mode=0o700)
    except FileExistsError as error:
        raise LifecycleError("controller root must be created exclusively") from error
    except OSError as error:
        raise LifecycleError("controller root cannot be created") from error
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise LifecycleError("controller root parent cannot be synced") from error
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    try:
        root_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise LifecycleError("controller root cannot be opened safely") from error
    try:
        information = os.fstat(root_fd)
        identity = (information.st_dev, information.st_ino)
        secret = capability.export_token() if capability is not None else secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        controller_id = hashlib.sha256(
            f"{identity[0]}:{identity[1]}:{salt}".encode("ascii")
        ).hexdigest()
        unsigned = {
            "schema_version": _SCHEMA,
            "controller_id": controller_id,
            "root_device": identity[0],
            "root_inode": identity[1],
            "salt": salt,
        }
        record = dict(unsigned)
        record["verifier"] = _mac(secret, b"controller", unsigned)
        _write_new(root_fd, _RECORD, canonical_json(record))
        for name in _DIRECTORIES:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
        os.fsync(root_fd)
        return LifecycleController._issue(path.resolve(), controller_id, secret, identity)
    finally:
        os.close(root_fd)


def resume_lifecycle_controller(root: Path | str, capability: LifecycleCapability) -> LifecycleController:
    """Reopen an existing controller root with its owner-held recovery capability."""
    if not isinstance(capability, LifecycleCapability):
        raise LifecycleError("controller recovery capability is required")
    path = Path(root)
    if not path.is_absolute():
        path = path.absolute()
    try:
        root_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise LifecycleError("controller root cannot be opened safely") from error
    try:
        information = os.fstat(root_fd)
        record = _read_record(root_fd)
        identity = (information.st_dev, information.st_ino)
        _validate_record(record, capability._secret, identity)
        for name in _DIRECTORIES:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            os.close(child)
        return LifecycleController._issue(path.resolve(), record["controller_id"], capability._secret, identity)
    finally:
        os.close(root_fd)


def assert_controller(value: LifecycleController) -> None:
    """Recheck the durable root record before any privileged operation."""
    if not isinstance(value, LifecycleController):
        raise LifecycleError("an authenticated lifecycle controller is required")
    try:
        root_fd = os.open(value.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise LifecycleError("controller root cannot be opened safely") from error
    try:
        information = os.fstat(root_fd)
        identity = (information.st_dev, information.st_ino)
        if identity != value._root_identity:
            raise LifecycleError("controller root identity changed")
        record = _read_record(root_fd)
        _validate_record(record, value._secret, identity)
        if record["controller_id"] != value.controller_id:
            raise LifecycleError("controller identity changed")
    finally:
        os.close(root_fd)


def controller_directory(controller: LifecycleController, name: str) -> Path:
    """Return one fixed, authenticated private subdirectory beneath a controller root."""
    if name not in _DIRECTORIES:
        raise LifecycleError("controller directory is invalid")
    assert_controller(controller)
    path = controller.root / name
    try:
        information = path.lstat()
    except OSError as error:
        raise LifecycleError("controller directory is unavailable") from error
    if path.is_symlink() or not stat.S_ISDIR(information.st_mode):
        raise LifecycleError("controller directory is unsafe")
    return path


def write_evidence(controller: LifecycleController, category: str, payload: dict[str, object]) -> tuple[str, str]:
    """Durably write one controller-authenticated canonical evidence record."""
    if not isinstance(category, str) or not category or "/" in category or "\x00" in category:
        raise LifecycleError("controller evidence category is invalid")
    try:
        category.encode("ascii")
    except UnicodeEncodeError as error:
        raise LifecycleError("controller evidence category is invalid") from error
    assert_controller(controller)
    unsigned: dict[str, object] = {
        "schema_version": _EVIDENCE_SCHEMA,
        "controller_id": controller.controller_id,
        "category": category,
        "payload": payload,
    }
    record = dict(unsigned)
    record["mac"] = _mac(controller._secret, b"evidence:" + category.encode("ascii"), unsigned)
    data = canonical_json(record)
    digest = hashlib.sha256(data).hexdigest()
    name = f"{digest}.json"
    receipts = controller_directory(controller, "receipts")
    try:
        receipts_fd = os.open(receipts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise LifecycleError("controller evidence directory is unavailable") from error
    try:
        _write_new(receipts_fd, name, data)
    except LifecycleError as error:
        if not _evidence_matches(receipts_fd, name, data):
            raise error
    finally:
        os.close(receipts_fd)
    return name, digest


def read_evidence(controller: LifecycleController, category: str, name: str, digest: str) -> dict[str, object]:
    """Read and authenticate one exact durable evidence record."""
    if (
        not isinstance(name, str)
        or not name.endswith(".json")
        or len(name) != 69
        or any(character not in "0123456789abcdef" for character in name[:-5])
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise LifecycleError("controller evidence reference is invalid")
    assert_controller(controller)
    receipts = controller_directory(controller, "receipts")
    try:
        receipts_fd = os.open(receipts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise LifecycleError("controller evidence directory is unavailable") from error
    try:
        data = _read_named(receipts_fd, name)
    finally:
        os.close(receipts_fd)
    if hashlib.sha256(data).hexdigest() != digest or name != f"{digest}.json":
        raise LifecycleError("controller evidence digest changed")
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LifecycleError("controller evidence is malformed") from error
    if not isinstance(record, dict) or canonical_json(record) != data:
        raise LifecycleError("controller evidence is noncanonical")
    expected = {"schema_version", "controller_id", "category", "payload", "mac"}
    if set(record) != expected or record.get("schema_version") != _EVIDENCE_SCHEMA:
        raise LifecycleError("controller evidence is malformed")
    if record.get("controller_id") != controller.controller_id or record.get("category") != category:
        raise LifecycleError("controller evidence belongs to another controller")
    payload = record.get("payload")
    mac = record.get("mac")
    if not isinstance(payload, dict) or not isinstance(mac, str):
        raise LifecycleError("controller evidence is malformed")
    unsigned = {key: value for key, value in record.items() if key != "mac"}
    expected_mac = _mac(controller._secret, b"evidence:" + category.encode("ascii"), unsigned)
    if not hmac.compare_digest(mac, expected_mac):
        raise LifecycleError("controller evidence authentication failed")
    return payload


def _mac(secret: str, domain: bytes, value: object) -> str:
    return hmac.digest(secret.encode("ascii"), domain + canonical_json(value), "sha256").hex()


def _write_new(root_fd: int, name: str, data: bytes) -> None:
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
    except OSError as error:
        raise LifecycleError("controller record cannot be created") from error
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short controller record write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(root_fd)


def _read_record(root_fd: int) -> dict[str, object]:
    try:
        fd = os.open(_RECORD, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as error:
        raise LifecycleError("controller record is unavailable") from error
    try:
        data = bytearray()
        while len(data) <= _READ_LIMIT:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > _READ_LIMIT:
            raise LifecycleError("controller record exceeds its limit")
    finally:
        os.close(fd)
    try:
        value = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LifecycleError("controller record is malformed") from error
    if not isinstance(value, dict) or canonical_json(value) != bytes(data):
        raise LifecycleError("controller record is noncanonical")
    return value


def _read_named(root_fd: int, name: str) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as error:
        raise LifecycleError("controller evidence is unavailable") from error
    try:
        data = bytearray()
        while len(data) <= _READ_LIMIT:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > _READ_LIMIT:
            raise LifecycleError("controller evidence exceeds its limit")
        return bytes(data)
    finally:
        os.close(fd)


def _evidence_matches(root_fd: int, name: str, expected: bytes) -> bool:
    try:
        return _read_named(root_fd, name) == expected
    except LifecycleError:
        return False


def _validate_record(record: dict[str, object], secret: str, identity: tuple[int, int]) -> None:
    expected = {"schema_version", "controller_id", "root_device", "root_inode", "salt", "verifier"}
    if set(record) != expected or record.get("schema_version") != _SCHEMA:
        raise LifecycleError("controller record is malformed")
    controller_id = record.get("controller_id")
    salt = record.get("salt")
    verifier = record.get("verifier")
    if (
        not isinstance(controller_id, str)
        or len(controller_id) != 64
        or not isinstance(salt, str)
        or not isinstance(verifier, str)
        or record.get("root_device") != identity[0]
        or record.get("root_inode") != identity[1]
    ):
        raise LifecycleError("controller record is malformed")
    unsigned = {name: value for name, value in record.items() if name != "verifier"}
    if not hmac.compare_digest(verifier, _mac(secret, b"controller", unsigned)):
        raise LifecycleError("controller capability is invalid")
