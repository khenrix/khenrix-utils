#!/usr/bin/env python3
"""Deliver a reviewed source-built SDK companion independently of the npm CLI."""

from __future__ import annotations

import argparse
import copy
import ctypes
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import sys
import tempfile
import uuid

from install_component import canonical_layout


class SourceRuntimeError(RuntimeError):
    """Reviewed source or installed companion no longer matches its identity."""


def require(condition, message):
    if not condition:
        raise SourceRuntimeError(message)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def digest_json(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def safe_path(path, *, absent=False):
    path = pathlib.Path(path)
    require(path.is_absolute() and str(path) == os.path.normpath(str(path)), "path must be absolute and normalized")
    for ancestor in reversed((path, *path.parents)):
        try:
            metadata = ancestor.lstat()
        except FileNotFoundError:
            require(absent, "required path is absent")
            continue
        require(not stat.S_ISLNK(metadata.st_mode), "symlink in path")
        require(metadata.st_uid in {0, os.getuid()}, "path has another owner")
        # Root-owned sticky temporary directories protect each user's children.
        sticky = metadata.st_uid == 0 and stat.S_ISDIR(metadata.st_mode) and metadata.st_mode & stat.S_ISVTX
        require(not metadata.st_mode & 0o022 or sticky, "unsafe writable path")
        if ancestor != path:
            require(stat.S_ISDIR(metadata.st_mode), "ancestor is not a directory")
    return path


def file_ref(path):
    path = safe_path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid(), "file must be owned regular payload")
        require(before.st_nlink == 1, "hardlinked payload is not admitted")
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), "file changed during verification")
        require(path.lstat().st_ino == before.st_ino, "file replaced during verification")
        return dict(path=str(path), sha256=digest.hexdigest(), bytes=before.st_size,
                    owner=before.st_uid, mode=stat.S_IMODE(before.st_mode), device=before.st_dev, inode=before.st_ino)
    finally:
        os.close(descriptor)


def remount_reference(reference, transition):
    if transition is None:
        return reference
    require(type(transition) is dict and set(transition) == {"old", "new"} and
            all(type(value) is int and value > 0 for value in transition.values()) and
            transition["old"] != transition["new"], "invalid device transition")
    require(reference["device"] == transition["old"], "nonuniform recorded device")
    return reference | {"device": transition["new"]}


def check_ref(reference, *, device_transition=None):
    require(type(reference) is dict and set(reference) == {"path", "sha256", "bytes", "owner", "mode", "device", "inode"},
            "a full file reference is required")
    require(file_ref(reference["path"]) == remount_reference(reference, device_transition), "file reference drift")


def load_json(path):
    reference = file_ref(path)
    value = json.loads(pathlib.Path(path).read_bytes())
    require(file_ref(path) == reference, "JSON changed during read")
    require(type(value) is dict, "JSON document must be an object")
    return value


def relative_path(raw):
    require(type(raw) is str and raw != "", "invalid relative payload path")
    path = pathlib.PurePosixPath(raw)
    require(not path.is_absolute() and ".." not in path.parts and str(path) == raw and raw != ".", "unsafe payload path")
    require(raw != "runtime-manifest.json", "manifest cannot hash itself")
    return pathlib.Path(*path.parts)


def verify_manifest(reference, *, physical_root=None, device_transition=None):
    check_ref(reference, device_transition=device_transition)
    document = load_json(reference["path"])
    logical_root = safe_path(document["artifact_root"], absent=physical_root is not None)
    root = safe_path(physical_root) if physical_root is not None else logical_root
    require(pathlib.Path(reference["path"]) == root / "runtime-manifest.json", "manifest must be at artifact root")
    require(type(document.get("schema")) is str and type(document.get("files")) is dict and document["files"], "invalid runtime manifest")
    expected = {"runtime-manifest.json"}
    directories = {"."}
    for raw, entry in document["files"].items():
        relative = relative_path(raw)
        require(entry.get("path") == str(logical_root / relative), "manifest payload path does not match root")
        check_ref(entry | {"path": str(root / relative)}, device_transition=device_transition)
        expected.add(raw)
        directories.update(str(parent) for parent in relative.parents)
    found = set()
    for directory, child_dirs, names in os.walk(root, followlinks=False):
        for name in child_dirs:
            child = pathlib.Path(directory) / name
            safe_path(child)
            require(str(child.relative_to(root)) in directories, "unexpected directory in closure")
        for name in names:
            child = pathlib.Path(directory) / name
            require(stat.S_ISREG(child.lstat().st_mode), "non-regular payload in closure")
            found.add(str(child.relative_to(root)))
    require(found == expected, "payload closure has missing or extra files")
    payload_refs = {entry["path"]: entry for entry in document["files"].values()}
    require(type(document.get("entrypoints")) is dict and document["entrypoints"], "runtime entrypoints missing")
    require(all(path in payload_refs for path in document["entrypoints"].values()), "entrypoint outside closure")
    require(document.get("worker") in list(payload_refs.values()), "worker identity outside closure")
    for group in ("packaging_repairs", "source_repairs"):
        repairs = document.get(group, [])
        require(type(repairs) is list, "repair collection must be a list")
        for repair in repairs:
            require(type(repair) is dict and repair.get("staged") in list(payload_refs.values()),
                    "repair outside closure")
    statistics = document.get("statistics", {})
    require(statistics.get("files") == len(document["files"]) and
            statistics.get("bytes") == sum(entry["bytes"] for entry in document["files"].values()) and
            statistics.get("symlinks") == 0, "closure statistics mismatch")
    check_ref(reference, device_transition=device_transition)
    return document


def paths(component, state):
    component = safe_path(component)
    state = safe_path(state, absent=True)
    require(component != state and component not in state.parents and state not in component.parents, "state and component must be separate")
    slot = safe_path(component / "runtime/source-candidate", absent=True)
    receipt = safe_path(state / "source-runtime/receipt.json", absent=True)
    return slot, receipt


def installed_state(component, state):
    slot, receipt = paths(component, state)
    require(slot.exists() == receipt.exists(), "partial companion installation")
    if not receipt.exists():
        return None
    require(stat.S_IMODE(receipt.stat().st_mode) == 0o600, "receipt must have mode 0600")
    record = load_json(receipt)
    require(record.get("schema") == "source-runtime-receipt-v1" and record.get("slot") == str(slot), "invalid companion receipt")
    require(record.get("installed_manifest", {}).get("path") == str(slot / "runtime-manifest.json"), "receipt manifest outside companion")
    verify_manifest(record["installed_manifest"])
    return dict(receipt=file_ref(receipt), installed_manifest=record["installed_manifest"])


def make_plan(reference, component, state):
    require_no_transaction(state)
    document = verify_manifest(reference)
    slot, receipt = paths(component, state)
    source = pathlib.Path(document["artifact_root"])
    for destination in (slot, pathlib.Path(state)):
        require(source != destination and source not in destination.parents and destination not in source.parents,
                "source candidate overlaps delivery state")
    return dict(schema="source-runtime-plan-v1", component_root=str(component), state_root=str(state),
                slot=str(slot), receipt=str(receipt), source_manifest=reference,
                source_schema=document["schema"], source_commit=document.get("commit"), capacity=document.get("capacity"),
                statistics=document["statistics"], previous=installed_state(component, state))


def exchange(left, right):
    """Swap two complete directories atomically; fail closed without native support."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        operation = libc.renamex_np
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = operation(os.fsencode(left), os.fsencode(right), 2)
    else:
        operation = getattr(libc, "renameat2", None)
        require(operation is not None, "atomic directory exchange is unavailable")
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = operation(-100, os.fsencode(left), -100, os.fsencode(right), 2)
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def write_private(path, value):
    with open(path, "xb") as stream:
        os.chmod(path, 0o600)
        stream.write(json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


def copy_payload(reference, destination, *, device_transition=None):
    check_ref(reference, device_transition=device_transition)
    expected = remount_reference(reference, device_transition)
    source = os.open(reference["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(source, "rb") as incoming:
        before = os.fstat(incoming.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
                (before.st_dev, before.st_ino, before.st_size, before.st_uid, stat.S_IMODE(before.st_mode)) ==
                tuple(expected[key] for key in ("device", "inode", "bytes", "owner", "mode")),
                "source identity changed before copy")
        parent = os.open(safe_path(destination.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            target = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
            with os.fdopen(target, "wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                after = os.fstat(incoming.fileno())
                identity = lambda metadata: (metadata.st_dev, metadata.st_ino, metadata.st_size,
                                             metadata.st_uid, metadata.st_mode, metadata.st_nlink,
                                             metadata.st_mtime_ns, metadata.st_ctime_ns)
                require(identity(before) == identity(after), "source changed during copy")
                check_ref(reference, device_transition=device_transition)
                os.fchmod(outgoing.fileno(), reference["mode"])
                installed = file_ref(destination)
                created = os.fstat(outgoing.fileno())
                require((installed["device"], installed["inode"]) == (created.st_dev, created.st_ino),
                        "destination replaced during copy")
                require(all(installed[key] == reference[key] for key in ("sha256", "bytes", "owner", "mode")),
                        "staged copy differs from reviewed payload")
                return installed
        finally:
            os.close(parent)


def staged_manifest(document, reference, staging, slot, *, device_transition=None):
    result = copy.deepcopy(document)
    result["source_schema"] = result["schema"]
    result["schema"] = "source-runtime-manifest-v1"
    result["source_manifest"] = reference
    result["artifact_root"] = str(slot)
    result["installed"] = True
    mappings = {}
    for raw, original in document["files"].items():
        destination = staging / relative_path(raw)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        installed = copy_payload(original, destination, device_transition=device_transition)
        installed["path"] = str(slot / raw)
        result["files"][raw] = installed
        mappings[original["path"]] = installed
    result["worker"] = mappings[document["worker"]["path"]]
    result["entrypoints"] = {key: mappings[path]["path"] for key, path in document["entrypoints"].items()}
    for group in ("packaging_repairs", "source_repairs"):
        for repair in result.get(group, []):
            repair["staged"] = mappings[repair["staged"]["path"]]
    return result


def lock_state(state):
    directory = pathlib.Path(state) / "source-runtime"
    safe_path(directory, absent=True)
    missing = []
    ancestor = directory
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(directory.stat().st_uid == os.getuid() and stat.S_IMODE(directory.stat().st_mode) == 0o700, "companion state must be owner-only")
    lock = directory / "lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    metadata = os.fstat(descriptor)
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600, "unsafe companion lock")
    os.fsync(descriptor)
    sync_directory(directory)
    for created in missing:
        sync_directory(created.parent)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return os.fdopen(descriptor, "r+")


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_tree(root):
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            descriptor = os.open(pathlib.Path(directory) / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        sync_directory(directory)


def sync_move(left, right):
    sync_directory(pathlib.Path(left).parent)
    if pathlib.Path(left).parent != pathlib.Path(right).parent:
        sync_directory(pathlib.Path(right).parent)


def move_new(left, right):
    """Rename without replacing a concurrently created destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        operation = libc.renamex_np
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = operation(os.fsencode(left), os.fsencode(right), 4)
    else:
        operation = getattr(libc, "renameat2", None)
        require(operation is not None, "exclusive rename is unavailable")
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = operation(-100, os.fsencode(left), -100, os.fsencode(right), 1)
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    sync_move(left, right)


def matches(reference, path, *, device_transition=None):
    if reference is None:
        return not pathlib.Path(path).exists() and not pathlib.Path(path).is_symlink()
    if not pathlib.Path(path).exists():
        return False
    return file_ref(path) | {"path": reference["path"]} == remount_reference(reference, device_transition)


def tree_matches(reference, root, *, device_transition=None):
    if reference is None:
        return not pathlib.Path(root).exists() and not pathlib.Path(root).is_symlink()
    path = pathlib.Path(root) / "runtime-manifest.json"
    if not matches(reference, path, device_transition=device_transition):
        return False
    verify_manifest(reference | {"path": str(path)}, physical_root=root, device_transition=device_transition)
    return True


def bound_tree_matches(bound, root):
    return tree_matches(bound["installed_manifest"] if bound else None, root,
                        device_transition=bound.get("device_transition") if bound else None)


def require_no_transaction(state):
    journal = pathlib.Path(state) / "source-runtime/transaction.json"
    require(not journal.exists() and not journal.is_symlink(), "pending source transaction requires explicit recover")


def state_matches(bound, slot, receipt):
    return bound_tree_matches(bound, slot) and matches(bound["receipt"] if bound else None, receipt)


def publish_receipt(pending, receipt, before, after):
    require(matches(before, receipt), "active receipt changed before publication")
    if before is None:
        move_new(pending, receipt)
    else:
        exchange(pending, receipt)
        sync_move(pending, receipt)
        if not matches(before, pending):
            # Put a raced receipt back only while the active inode is still ours.
            if matches(after, receipt):
                exchange(pending, receipt)
                sync_move(pending, receipt)
            raise SourceRuntimeError("concurrent receipt replacement preserved")
    require(matches(after, receipt), "published receipt changed")


def verify_transaction_source(transaction):
    if transaction["kind"] == "remount-retire":
        document = verify_manifest(transaction["source_manifest"])
        require(document.get("remount_retire_plan_sha256") == transaction["reviewed_plan_sha256"],
                "source snapshot differs from reviewed remount plan")


def finish_transaction(transaction, journal):
    verify_transaction_source(transaction)
    slot, receipt = pathlib.Path(transaction["slot"]), pathlib.Path(transaction["receipt"])
    before, after = transaction["before"], transaction["after"]
    if transaction["kind"] == "apply" and before:
        displaced = pathlib.Path(transaction["tree_exchange"])
        archive = pathlib.Path(transaction["tree_archive"])
        if tree_matches(before["installed_manifest"], displaced):
            move_new(displaced, archive)
        pending = pathlib.Path(transaction["receipt_exchange"])
        archived_receipt = pathlib.Path(transaction["receipt_archive"])
        if matches(before["receipt"], pending):
            move_new(pending, archived_receipt)
    if before:
        require(bound_tree_matches(before, transaction["tree_archive"]), "archived tree drift")
        require(matches(before["receipt"], transaction["receipt_archive"]), "archived receipt drift")
    require(state_matches(after, slot, receipt), "committed source state drift")
    verify_transaction_source(transaction)
    journal.unlink()
    sync_directory(journal.parent)


def recover_transaction(transaction, journal):
    verify_transaction_source(transaction)
    slot, receipt = pathlib.Path(transaction["slot"]), pathlib.Path(transaction["receipt"])
    before, after = transaction["before"], transaction["after"]
    if state_matches(after, slot, receipt):
        finish_transaction(transaction, journal)
        return dict(status="recovered", outcome="committed")
    after_tree = after["installed_manifest"] if after else None
    if not bound_tree_matches(before, slot):
        require(tree_matches(after_tree, slot), "unknown tree blocks recovery")
        if before is None:
            move_new(slot, pathlib.Path(transaction["tree_archive"]))
        else:
            candidates = [pathlib.Path(transaction[key]) for key in ("tree_exchange", "tree_archive")]
            candidates = list(dict.fromkeys(candidates))
            found = [path for path in candidates if bound_tree_matches(before, path)]
            require(len(found) == 1, "previous bound tree is unavailable")
            if slot.exists():
                exchange(found[0], slot)
                sync_move(found[0], slot)
            else:
                move_new(found[0], slot)
    before_receipt = before["receipt"] if before else None
    after_receipt = after["receipt"] if after else None
    if not matches(before_receipt, receipt):
        require(matches(after_receipt, receipt), "concurrent receipt retained; recovery requires reconciliation")
        if before is None:
            move_new(receipt, pathlib.Path(transaction["receipt_archive"]))
        else:
            candidates = list(dict.fromkeys(pathlib.Path(transaction[key]) for key in ("receipt_exchange", "receipt_archive")))
            found = [path for path in candidates if matches(before_receipt, path)]
            require(len(found) == 1, "previous bound receipt is unavailable")
            if receipt.exists():
                exchange(found[0], receipt)
                sync_move(found[0], receipt)
            else:
                move_new(found[0], receipt)
    require(state_matches(before, slot, receipt), "recovered source state drift")
    verify_transaction_source(transaction)
    journal.unlink()
    sync_directory(journal.parent)
    return dict(status="recovered", outcome="restored")


def execute_transaction(transaction, journal):
    slot, receipt = pathlib.Path(transaction["slot"]), pathlib.Path(transaction["receipt"])
    before, after = transaction["before"], transaction["after"]
    verify_transaction_source(transaction)
    require(state_matches(before, slot, receipt), "source state changed before cutover")
    write_private(journal, transaction)
    sync_directory(journal.parent)
    try:
        verify_transaction_source(transaction)
        require(state_matches(before, slot, receipt), "source state changed before exchange")
        other = pathlib.Path(transaction["tree_exchange"])
        if before and after:
            exchange(other, slot)
            sync_move(other, slot)
            require(bound_tree_matches(before, other), "exchanged previous tree drift")
        elif after:
            move_new(other, slot)
        else:
            move_new(slot, other)
            require(bound_tree_matches(before, other), "retired tree drift")
        require(matches(before["receipt"] if before else None, receipt), "active receipt changed during cutover")
        verify_transaction_source(transaction)
        require(tree_matches(after["installed_manifest"] if after else None, slot), "installed tree drift before publication")
        if after:
            publish_receipt(pathlib.Path(transaction["receipt_exchange"]), receipt,
                            before["receipt"] if before else None, after["receipt"])
        else:
            move_new(receipt, pathlib.Path(transaction["receipt_exchange"]))
            require(matches(before["receipt"], transaction["receipt_exchange"]), "retired receipt drift")
        finish_transaction(transaction, journal)
    except BaseException:
        if journal.exists():
            try:
                recover_transaction(transaction, journal)
            except (SourceRuntimeError, OSError):
                # Preserve every bound tree and any concurrent receipt for explicit recovery.
                pass
        raise


def new_backup(receipt):
    backup = receipt.parent / "backups" / uuid.uuid4().hex
    backup.mkdir(parents=True, mode=0o700)
    sync_directory(backup)
    sync_directory(backup.parent)
    sync_directory(backup.parent.parent)
    return backup


def make_remount_retire_plan(component, state, source_snapshot):
    """Review one uniform device renumbering; never change the frozen evidence."""
    require_no_transaction(state)
    slot, receipt = paths(component, state)
    current_receipt = file_ref(receipt)
    require(current_receipt["mode"] == 0o600, "receipt must have mode 0600")
    record = load_json(receipt)
    require(record.get("schema") == "source-runtime-receipt-v1" and record.get("slot") == str(slot), "invalid companion receipt")
    installed = record["installed_manifest"]
    require(installed["path"] == str(slot / "runtime-manifest.json"), "receipt manifest outside companion")
    transition = dict(old=installed["device"], new=file_ref(installed["path"])["device"])
    document = verify_manifest(installed, device_transition=transition)
    reference = record["source_manifest"]
    require(document.get("source_manifest") == reference, "source provenance differs from receipt")
    source = verify_manifest(reference, device_transition=transition)
    snapshot = safe_path(source_snapshot, absent=True)
    require(not snapshot.exists(), "source snapshot destination exists")
    parent = safe_path(snapshot.parent)
    require(parent.stat().st_uid == os.getuid() and stat.S_IMODE(parent.stat().st_mode) == 0o700,
            "source snapshot parent must be owner-only")
    source_root = pathlib.Path(source["artifact_root"])
    for left, right in ((source_root, component), (source_root, state), (snapshot, source_root),
                        (snapshot, component), (snapshot, state)):
        left, right = pathlib.Path(left), pathlib.Path(right)
        require(left != right and left not in right.parents and right not in left.parents,
                "source snapshot or source overlaps delivery state")
    require(file_ref(receipt) == current_receipt, "receipt changed during remount review")
    return dict(schema="source-runtime-remount-retire-plan-v1", component_root=str(component), state_root=str(state),
                slot=str(slot), receipt=str(receipt), source_manifest=reference, source_snapshot=str(snapshot),
                device_transition=transition, source_statistics=source["statistics"],
                installed_statistics=document["statistics"],
                previous=dict(receipt=current_receipt, installed_manifest=installed, device_transition=transition))


def apply_remount_retire_plan(plan, approved_sha256):
    require(digest_json(plan) == approved_sha256, "reviewed plan digest mismatch")
    require(plan.get("schema") == "source-runtime-remount-retire-plan-v1", "invalid remount retirement plan")
    component, state = pathlib.Path(plan["component_root"]), pathlib.Path(plan["state_root"])
    snapshot = pathlib.Path(plan["source_snapshot"])
    with lock_state(state):
        require(make_remount_retire_plan(component, state, snapshot) == plan, "reviewed remount plan is stale")
        slot, receipt = paths(component, state)
        backup = new_backup(receipt)
        write_private(backup / "remount-retire-plan.json", plan)
        staging = backup / "source-snapshot"
        staging.mkdir(mode=0o700)
        source = verify_manifest(plan["source_manifest"], device_transition=plan["device_transition"])
        document = staged_manifest(source, plan["source_manifest"], staging, snapshot,
                                   device_transition=plan["device_transition"])
        document["schema"] = source["schema"]
        for key in ("source_schema", "installed"):
            if key in source:
                document[key] = source[key]
            else:
                document.pop(key, None)
        document["remount_retire_plan_sha256"] = approved_sha256
        write_private(staging / "runtime-manifest.json", document)
        sync_tree(staging)
        source_reference = file_ref(staging / "runtime-manifest.json") | {"path": str(snapshot / "runtime-manifest.json")}
        require(tree_matches(source_reference, staging), "source snapshot drift")
        require(make_remount_retire_plan(component, state, snapshot) == plan, "reviewed remount plan is stale")
        # Publish a new immutable source; cutover still checks the exact old receipt/tree.
        move_new(staging, snapshot)
        verify_manifest(source_reference)
        write_private(backup / "source-reference.json", source_reference)
        sync_directory(backup)
        transaction = dict(schema="source-runtime-transaction-v1", kind="remount-retire", slot=str(slot), receipt=str(receipt),
                           reviewed_plan_sha256=approved_sha256, source_manifest=source_reference,
                           before=plan["previous"], after=None, tree_exchange=str(backup / "rolled-back-candidate"),
                           tree_archive=str(backup / "rolled-back-candidate"),
                           receipt_exchange=str(backup / "rolled-back-receipt.json"),
                           receipt_archive=str(backup / "rolled-back-receipt.json"))
        execute_transaction(transaction, receipt.parent / "transaction.json")
        return dict(status="retired", installed=False, backup=str(backup), source_manifest=source_reference,
                    source_reference=str(backup / "source-reference.json"))


def apply_plan(plan, approved_sha256):
    require(digest_json(plan) == approved_sha256, "reviewed plan digest mismatch")
    require(plan.get("schema") == "source-runtime-plan-v1", "invalid source runtime plan")
    component, state = pathlib.Path(plan["component_root"]), pathlib.Path(plan["state_root"])
    require(make_plan(plan["source_manifest"], component, state) == plan, "reviewed plan is stale")
    slot, receipt = paths(component, state)
    with lock_state(state):
        require(make_plan(plan["source_manifest"], component, state) == plan, "reviewed plan is stale")
        document = verify_manifest(plan["source_manifest"])
        slot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = pathlib.Path(tempfile.mkdtemp(prefix=".source-candidate-", dir=slot.parent))
        backup = new_backup(receipt)
        manifest = staged_manifest(document, plan["source_manifest"], staging, slot)
        write_private(staging / "runtime-manifest.json", manifest)
        sync_tree(staging)
        sync_directory(staging.parent)
        sync_directory(staging.parent.parent)
        installed_reference = file_ref(staging / "runtime-manifest.json") | {"path": str(slot / "runtime-manifest.json")}
        before = plan["previous"]
        record = dict(schema="source-runtime-receipt-v1", slot=str(slot), source_manifest=plan["source_manifest"],
                      installed_manifest=installed_reference, reviewed_plan_sha256=approved_sha256,
                      backup_id=backup.name, previous=before,
                      backup_receipt=before["receipt"] | {"path": str(backup / "receipt.json")} if before else None)
        pending = backup / "new-receipt.json"
        write_private(pending, record)
        sync_directory(backup)
        after = dict(installed_manifest=installed_reference, receipt=file_ref(pending) | {"path": str(receipt)})
        require(tree_matches(installed_reference, staging), "staged tree drift before transaction")
        require(make_plan(plan["source_manifest"], component, state) == plan, "reviewed plan is stale")
        transaction = dict(schema="source-runtime-transaction-v1", kind="apply", slot=str(slot), receipt=str(receipt),
                           before=before, after=after, tree_exchange=str(staging), tree_archive=str(backup / "candidate"),
                           receipt_exchange=str(pending), receipt_archive=str(backup / "receipt.json"))
        execute_transaction(transaction, receipt.parent / "transaction.json")
        return record


def doctor(component, state):
    require_no_transaction(state)
    current = installed_state(component, state)
    require(current is not None, "source companion is not installed")
    return dict(status="verified", **current)


def rollback(component, state, receipt_sha256, *, retire=False):
    slot, receipt = paths(component, state)
    with lock_state(state):
        require_no_transaction(state)
        current = installed_state(component, state)
        require(current is not None and current["receipt"]["sha256"] == receipt_sha256, "rollback receipt digest mismatch")
        record = load_json(receipt)
        require(re.fullmatch(r"[0-9a-f]{32}", record.get("backup_id", "")) is not None, "invalid backup id")
        backup = safe_path(receipt.parent / "backups" / record["backup_id"])
        if record["previous"] is None or retire:
            if retire:
                backup = new_backup(receipt)
            transaction = dict(schema="source-runtime-transaction-v1", kind="retire", slot=str(slot), receipt=str(receipt),
                               before=current, after=None, tree_exchange=str(backup / "rolled-back-candidate"),
                               tree_archive=str(backup / "rolled-back-candidate"),
                               receipt_exchange=str(backup / "rolled-back-receipt.json"),
                               receipt_archive=str(backup / "rolled-back-receipt.json"))
            execute_transaction(transaction, receipt.parent / "transaction.json")
            return dict(status="retired" if retire else "rolled_back", installed=False)
        old_receipt = safe_path(backup / "receipt.json")
        require(record.get("backup_receipt", {}).get("path") == str(old_receipt), "backup receipt outside backup")
        check_ref(record["backup_receipt"])
        require(file_ref(old_receipt)["sha256"] == record["previous"]["receipt"]["sha256"], "backup receipt drift")
        previous = load_json(old_receipt)
        old_manifest = previous["installed_manifest"]
        # Backup keeps the prior inodes, while its frozen document names the stable slot.
        candidate = safe_path(backup / "candidate")
        verify_manifest(old_manifest | {"path": str(candidate / "runtime-manifest.json")}, physical_root=candidate)
        after = dict(installed_manifest=old_manifest, receipt=record["backup_receipt"] | {"path": str(receipt)})
        transaction = dict(schema="source-runtime-transaction-v1", kind="rollback", slot=str(slot), receipt=str(receipt),
                           before=current, after=after, tree_exchange=str(candidate), tree_archive=str(candidate),
                           receipt_exchange=str(old_receipt), receipt_archive=str(old_receipt))
        execute_transaction(transaction, receipt.parent / "transaction.json")
        return dict(status="rolled_back", installed=True, installed_manifest=old_manifest)


def recover(component, state, transaction_sha256):
    slot, receipt = paths(component, state)
    with lock_state(state):
        journal = receipt.parent / "transaction.json"
        journal_reference = file_ref(journal)
        require(journal_reference["mode"] == 0o600 and journal_reference["sha256"] == transaction_sha256, "reviewed transaction digest mismatch")
        transaction = load_json(journal)
        require(transaction.get("schema") == "source-runtime-transaction-v1" and
                transaction.get("kind") in {"apply", "rollback", "retire", "remount-retire"} and
                transaction.get("slot") == str(slot) and transaction.get("receipt") == str(receipt), "invalid transaction binding")
        for bound in (transaction["before"], transaction["after"]):
            if bound:
                require("device_transition" not in bound or
                        (transaction["kind"] == "remount-retire" and bound is transaction["before"] and
                         transaction["after"] is None), "device transition outside remount retirement")
                require(bound["receipt"]["path"] == str(receipt) and
                        bound["installed_manifest"]["path"] == str(slot / "runtime-manifest.json"), "transaction state outside companion")
        for key in ("tree_exchange", "tree_archive", "receipt_exchange", "receipt_archive"):
            path = safe_path(transaction[key], absent=True)
            staging = key == "tree_exchange" and path.parent == slot.parent and path.name.startswith(".source-candidate-")
            backed_up = path.parent.parent == receipt.parent / "backups" and re.fullmatch(r"[0-9a-f]{32}", path.parent.name)
            require(staging or backed_up, "transaction path outside bounded storage")
        return recover_transaction(transaction, journal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "doctor", "rollback", "retire", "recover",
                                            "remount-retire-plan", "remount-retire-apply"))
    layout = canonical_layout()
    parser.add_argument("--component-root", type=pathlib.Path, default=layout.component)
    parser.add_argument("--state-root", type=pathlib.Path, default=layout.state)
    parser.add_argument("--input-ref", type=pathlib.Path, help="owner-only JSON containing the full source manifest file reference")
    parser.add_argument("--plan", type=pathlib.Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--receipt-sha256")
    parser.add_argument("--transaction-sha256")
    parser.add_argument("--source-snapshot", type=pathlib.Path, help="new private source snapshot outside component and state")
    args = parser.parse_args()
    try:
        if args.command == "remount-retire-plan":
            require(args.source_snapshot is not None, "remount retirement plan requires --source-snapshot")
            result = make_remount_retire_plan(args.component_root, args.state_root, args.source_snapshot)
        elif args.command == "remount-retire-apply":
            require(args.plan is not None and args.plan_sha256 is not None, "remount retirement apply requires --plan and --plan-sha256")
            result = apply_remount_retire_plan(load_json(args.plan), args.plan_sha256)
        elif args.command == "plan":
            require(args.input_ref is not None, "plan requires --input-ref")
            result = make_plan(load_json(args.input_ref), args.component_root, args.state_root)
        elif args.command == "apply":
            require(args.plan is not None and args.plan_sha256 is not None, "apply requires --plan and --plan-sha256")
            result = apply_plan(load_json(args.plan), args.plan_sha256)
        elif args.command == "doctor":
            result = doctor(args.component_root, args.state_root)
        elif args.command == "recover":
            require(args.transaction_sha256 is not None, "recover requires --transaction-sha256")
            result = recover(args.component_root, args.state_root, args.transaction_sha256)
        else:
            require(args.receipt_sha256 is not None, "rollback requires --receipt-sha256")
            result = rollback(args.component_root, args.state_root, args.receipt_sha256, retire=args.command == "retire")
        sys.stdout.buffer.write(json_bytes(result))
        return 0
    except (SourceRuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"source runtime: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
