#!/usr/bin/env python3
"""Read-only checks for the portable Maka component and any installed copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import pwd
import re
import sys

from install_component import (
    PACKAGE_INTEGRITY,
    PACKAGE_NAME,
    PACKAGE_VERSION,
    SOURCE_COMMIT,
    InstallLayout,
    ComponentInstallError,
    canonical_layout,
    clean_environment,
    discover_package_root,
    read_manifest,
    render_wrapper,
    resolve_mise,
    supported_platform,
)

EXPECTED_INTEGRITY = PACKAGE_INTEGRITY
EXPECTED_COMMIT = SOURCE_COMMIT
EXCLUDED_DIRECTORY_NAMES = {"__pycache__", "evidence", "runtime", "trials"}
EXPECTED_ATTRIBUTION_HASHES = {
    "third_party/apache-maka/LICENSE": "ebd45d2cb43f6d451345b872b944b937e6b444fa34edb81b743b2330f4dfa927",
    "third_party/apache-maka/NOTICE": "4cce021a96be5a16e86083c0020b788b4ca1b3ed24185ff3a4a51e53419045f0",
    "third_party/apache-maka/DISCLAIMER-WIP": "67268b9e9381fe3fda6bc56c484ae8b50ef7dad4c6f1ee7beec021673dfd830c",
}


class DoctorError(RuntimeError):
    """The portable or installed Maka component does not match its pin."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DoctorError(message)


def portable_files(source: pathlib.Path) -> set[pathlib.Path]:
    result: set[pathlib.Path] = set()
    for candidate in source.rglob("*"):
        relative = candidate.relative_to(source)
        if any(part in EXCLUDED_DIRECTORY_NAMES for part in relative.parts):
            continue
        if relative.parts[:2] == ("controller", ".build"):
            continue
        if candidate.is_symlink():
            raise DoctorError(f"portable component contains a symlink: {relative}")
        if candidate.is_file() and candidate.name != ".DS_Store" and candidate.suffix not in {".pyc", ".pyo"}:
            result.add(relative)
    return result


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_install_receipt(layout: InstallLayout) -> dict[str, object]:
    state = layout.state
    receipt_path = state / "install-receipt.json"
    try:
        state_metadata = state.lstat()
        receipt_metadata = receipt_path.lstat()
    except OSError as error:
        raise DoctorError("installed component lacks its final install receipt") from error
    require(
        state_metadata.st_uid == os.getuid()
        and state_metadata.st_mode & 0o777 == 0o700
        and state.is_dir()
        and not state.is_symlink(),
        "install receipt directory is not owner-only",
    )
    require(
        receipt_metadata.st_uid == os.getuid()
        and receipt_metadata.st_mode & 0o777 == 0o600
        and receipt_path.is_file()
        and not receipt_path.is_symlink(),
        "install receipt is not an owner-only regular file",
    )
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise DoctorError(f"install receipt contains a duplicate field: {key}")
            result[key] = value
        return result

    try:
        receipt = json.loads(
            receipt_path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DoctorError("install receipt is invalid JSON") from error
    require(isinstance(receipt, dict), "install receipt must be an object")
    allowed = {
        "schema",
        "package",
        "version",
        "integrity",
        "source_commit",
        "platform",
        "component",
        "wrapper",
        "backup",
    }
    require(set(receipt) == allowed, "install receipt fields are invalid")
    require(receipt.get("schema") == "khenrix-maka-install-v1", "install receipt schema mismatch")
    require(receipt.get("package") == PACKAGE_NAME, "install receipt package mismatch")
    require(receipt.get("version") == PACKAGE_VERSION, "install receipt version mismatch")
    require(receipt.get("integrity") == EXPECTED_INTEGRITY, "install receipt integrity mismatch")
    require(receipt.get("source_commit") == EXPECTED_COMMIT, "install receipt source commit mismatch")
    require(receipt.get("platform") == supported_platform(), "install receipt platform mismatch")
    require(receipt.get("component") == str(layout.component), "install receipt component path mismatch")
    require(
        receipt.get("wrapper") == str(layout.wrapper),
        "install receipt wrapper path mismatch",
    )
    backup = receipt.get("backup")
    require(
        backup is None
        or (
            isinstance(backup, str)
            and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9]+", backup) is not None
        ),
        "install receipt backup id is invalid",
    )
    return receipt


def inspect_component(source: pathlib.Path) -> dict[str, object]:
    source = source.resolve(strict=True)
    files = read_manifest(source)
    require(set(files) == portable_files(source), "install manifest does not cover the portable component exactly")
    provenance = json.loads((source / "provenance.json").read_text(encoding="utf-8"))
    maka = provenance.get("maka", {})
    require(maka.get("version") == PACKAGE_VERSION, "provenance version mismatch")
    require(maka.get("integrity") == EXPECTED_INTEGRITY, "npm integrity mismatch")
    require(maka.get("apacheCommit") == EXPECTED_COMMIT, "Apache release commit mismatch")
    lock = (source / "mise.lock").read_text(encoding="utf-8")
    require(f'version = "{PACKAGE_VERSION}"' in lock, "mise lock version mismatch")
    require('platforms.macos-arm64' in lock, "mise lock lacks macOS arm64")
    require('platforms.linux-x64' in lock, "mise lock lacks Linux x64")
    npm_lock = (
        source
        / ".mise/locks/npm-maka-agent"
        / PACKAGE_VERSION
        / "aube-lock.yaml"
    ).read_text(encoding="utf-8")
    require(EXPECTED_INTEGRITY in npm_lock, "npm aube lock integrity mismatch")
    for relative in (*EXPECTED_ATTRIBUTION_HASHES, "third_party/README.md"):
        require((source / relative).is_file(), f"third-party attribution missing: {relative}")
    recorded_hashes = provenance.get("thirdParty", {}).get("apacheMaka", {}).get("files", {})
    require(recorded_hashes == EXPECTED_ATTRIBUTION_HASHES, "third-party provenance mismatch")
    for relative, expected in EXPECTED_ATTRIBUTION_HASHES.items():
        require(sha256(source / relative) == expected, f"third-party attribution hash mismatch: {relative}")
    template = (source / "wrapper/maka.in").read_text(encoding="utf-8")
    for placeholder in (
        "@@ACCOUNT_ROOT@@",
        "@@ACCOUNT_NAME@@",
        "@@MISE_BIN@@",
        "@@MAKA_LAB_ROOT@@",
        "@@MAKA_PACKAGE_ROOT@@",
    ):
        require(template.count(placeholder) == 1, f"wrapper placeholder invalid: {placeholder}")
    require("dev.khenrix.maka-openai-relay" in template, "neutral relay service missing")
    require("--thinking xhigh" in template, "headless xhigh default missing")

    layout = canonical_layout()
    account = pwd.getpwuid(os.getuid())
    mise = resolve_mise(layout.home)
    environment = clean_environment(layout, mise, account.pw_name)
    package = discover_package_root(mise, source, environment)
    installed_matches: bool | None = None
    install_receipt = "absent"
    if layout.component.exists():
        require(layout.component.is_dir() and not layout.component.is_symlink(), "installed component path is unsafe")
        installed_matches = all(
            (layout.component / relative).is_file()
            and (layout.component / relative).read_bytes() == (source / relative).read_bytes()
            for relative in files
        )
        require(installed_matches, "installed component drift detected")
        inspect_install_receipt(layout)
        install_receipt = "current"
    elif (layout.state / "install-receipt.json").exists() or (layout.state / "install-receipt.json").is_symlink():
        raise DoctorError("install receipt exists without an installed component")
    wrapper_status = "absent"
    if layout.wrapper.exists():
        require(layout.wrapper.is_file() and not layout.wrapper.is_symlink(), "installed wrapper path is unsafe")
        wrapper = layout.wrapper.read_text(encoding="utf-8")
        require("@@" not in wrapper, "installed wrapper has unresolved placeholders")
        if install_receipt == "current":
            expected_wrapper = render_wrapper(
                template,
                layout=layout,
                account_name=account.pw_name,
                mise=mise,
                package=package,
            )
            require(wrapper == expected_wrapper, "managed wrapper drift detected")
            wrapper_status = "khenrix-managed"
        elif str(layout.component) in wrapper and PACKAGE_VERSION in wrapper:
            wrapper_status = "khenrix-managed-unattested"
        else:
            wrapper_status = "other-owner"
    elif install_receipt == "current":
        raise DoctorError("managed wrapper drift detected: wrapper is missing")
    return {
        "schema": "khenrix-maka-component-doctor-v1",
        "ok": True,
        "version": PACKAGE_VERSION,
        "platform": supported_platform(),
        "portableFiles": len(files),
        "packageRoot": str(package),
        "installedComponent": str(layout.component),
        "installedMatchesSource": installed_matches,
        "installReceipt": install_receipt,
        "wrapper": wrapper_status,
        "authChecked": False,
        "liveServiceChecked": False,
    }


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parent.parent,
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    options = parse_args(arguments)
    try:
        print(json.dumps(inspect_component(options.source), indent=2, sort_keys=True))
    except (DoctorError, ComponentInstallError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Maka component doctor failed: {error}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
