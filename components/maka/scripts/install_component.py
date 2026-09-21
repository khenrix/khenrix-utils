#!/usr/bin/env python3
"""Install the pinned Maka component without depending on a checkout path."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import platform
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass

PACKAGE_VERSION = "0.2.0-dev.44.20260920"
MANIFEST_NAME = "install-files.txt"
INSTALL_RELATIVE = pathlib.Path(".local/share/khenrix-utils/maka")
WRAPPER_RELATIVE = pathlib.Path(".local/bin/maka")
STATE_RELATIVE = pathlib.Path(".local/state/khenrix-utils/maka")
NEW_MODE_RELATIVE = pathlib.Path(".config/khenrix-utils/maka/maka-auth-mode")
LEGACY_MODE_RELATIVE = pathlib.Path(".config/agentic-setup/maka-auth-mode")
PLACEHOLDERS = (
    "@@ACCOUNT_ROOT@@",
    "@@ACCOUNT_NAME@@",
    "@@MISE_BIN@@",
    "@@MAKA_LAB_ROOT@@",
    "@@MAKA_PACKAGE_ROOT@@",
)


class ComponentInstallError(RuntimeError):
    """The component cannot be installed without violating its local contract."""


@dataclass(frozen=True)
class InstallLayout:
    home: pathlib.Path
    component: pathlib.Path
    wrapper: pathlib.Path
    state: pathlib.Path


def canonical_layout(home: pathlib.Path | None = None) -> InstallLayout:
    account_home = home or pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not account_home.is_absolute():
        raise ComponentInstallError("account home must be absolute")
    return InstallLayout(
        home=account_home,
        component=account_home / INSTALL_RELATIVE,
        wrapper=account_home / WRAPPER_RELATIVE,
        state=account_home / STATE_RELATIVE,
    )


def supported_platform(system: str | None = None, machine: str | None = None) -> str:
    current_system = system or platform.system()
    current_machine = (machine or platform.machine()).lower()
    if current_system == "Darwin" and current_machine in {"arm64", "aarch64"}:
        return "macos-arm64"
    if current_system == "Linux" and current_machine in {"x86_64", "amd64"}:
        return "linux-x64"
    raise ComponentInstallError(
        "managed Maka supports macOS arm64 and Linux x86-64/WSL only"
    )


def resolve_mise(home: pathlib.Path, candidates: tuple[pathlib.Path, ...] | None = None) -> pathlib.Path:
    admitted = candidates or (
        home / ".local/bin/mise",
        pathlib.Path("/opt/homebrew/bin/mise"),
        pathlib.Path("/usr/local/bin/mise"),
    )
    for candidate in admitted:
        try:
            entry = candidate.lstat()
            target = candidate.resolve(strict=True)
            metadata = target.stat()
        except OSError:
            continue
        if not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.getuid()}
            or metadata.st_mode & 0o022
            or not os.access(candidate, os.X_OK)
        ):
            continue
        return candidate
    raise ComponentInstallError("mise is unavailable at an admitted standard path")


def read_manifest(source: pathlib.Path) -> tuple[pathlib.Path, ...]:
    manifest = source / MANIFEST_NAME
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ComponentInstallError("Maka install manifest is unavailable") from error
    result: list[pathlib.Path] = []
    seen: set[str] = set()
    for raw in lines:
        if not raw or raw.startswith("#"):
            continue
        candidate = pathlib.PurePosixPath(raw)
        if candidate.is_absolute() or ".." in candidate.parts or raw in seen:
            raise ComponentInstallError("Maka install manifest is invalid")
        path = pathlib.Path(*candidate.parts)
        source_path = source / path
        try:
            metadata = source_path.lstat()
        except OSError as error:
            raise ComponentInstallError(f"manifest entry is unavailable: {raw}") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ComponentInstallError(f"manifest entry is not a regular file: {raw}")
        if metadata.st_mode & 0o022:
            raise ComponentInstallError(f"manifest entry is writable by another account: {raw}")
        seen.add(raw)
        result.append(path)
    if pathlib.Path(MANIFEST_NAME) not in result:
        raise ComponentInstallError("Maka install manifest must include itself")
    return tuple(result)


def copy_manifest(source: pathlib.Path, staging: pathlib.Path) -> None:
    for relative in read_manifest(source):
        source_path = source / relative
        destination = staging / relative
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination, follow_symlinks=False)
        source_mode = stat.S_IMODE(source_path.stat(follow_symlinks=False).st_mode)
        destination.chmod(0o755 if source_mode & 0o111 else 0o644)


def discover_package_root(mise: pathlib.Path, component: pathlib.Path, environment: dict[str, str]) -> pathlib.Path:
    completed = subprocess.run(
        [str(mise), "-C", str(component), "which", "maka"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        close_fds=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ComponentInstallError("pinned Maka executable is unavailable")
    shim = pathlib.Path(completed.stdout.strip())
    try:
        source = shim.read_text(encoding="utf-8")
    except OSError as error:
        raise ComponentInstallError("pinned Maka shim is unavailable") from error
    match = re.search(r"^# aube-bin-shim v2 target=([^\s]+)$", source, re.MULTILINE)
    if match is None:
        raise ComponentInstallError("pinned Maka shim is unrecognized")
    package = (shim.parent / match.group(1)).resolve().parent.parent
    try:
        descriptor = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ComponentInstallError("pinned Maka package is invalid") from error
    if descriptor.get("name") != "maka-agent" or descriptor.get("version") != PACKAGE_VERSION:
        raise ComponentInstallError("pinned Maka package version is invalid")
    return package


def render_wrapper(
    template: str,
    *,
    layout: InstallLayout,
    account_name: str,
    mise: pathlib.Path,
    package: pathlib.Path,
) -> str:
    values = {
        "@@ACCOUNT_ROOT@@": shlex.quote(str(layout.home)),
        "@@ACCOUNT_NAME@@": shlex.quote(account_name),
        "@@MISE_BIN@@": shlex.quote(str(mise)),
        "@@MAKA_LAB_ROOT@@": shlex.quote(str(layout.component)),
        "@@MAKA_PACKAGE_ROOT@@": shlex.quote(str(package)),
    }
    rendered = template
    for placeholder, value in values.items():
        if rendered.count(placeholder) != 1:
            raise ComponentInstallError(f"wrapper placeholder is missing or duplicated: {placeholder}")
        rendered = rendered.replace(placeholder, value)
    if any(placeholder in rendered for placeholder in PLACEHOLDERS):
        raise ComponentInstallError("wrapper rendering left an unresolved placeholder")
    return rendered


def atomic_write(path: pathlib.Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ComponentInstallError(f"refusing to replace symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        if temporary.exists():
            temporary.unlink()


def clean_environment(layout: InstallLayout, mise: pathlib.Path, account_name: str) -> dict[str, str]:
    return {
        "HOME": str(layout.home),
        "USER": account_name,
        "LOGNAME": account_name,
        "PATH": f"{mise.parent}:/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }


def assert_cutover_safe(layout: InstallLayout) -> None:
    legacy = layout.home / LEGACY_MODE_RELATIVE
    managed = layout.home / NEW_MODE_RELATIVE
    if (legacy.exists() or legacy.is_symlink()) and not (managed.exists() or managed.is_symlink()):
        raise ComponentInstallError(
            "legacy Agentic Setup Maka state exists; run the reviewed Maka migration before activation"
        )


def installation_plan(source: pathlib.Path, *, activate: bool) -> dict[str, object]:
    source = source.resolve(strict=True)
    files = read_manifest(source)
    layout = canonical_layout()
    account = pwd.getpwuid(os.getuid())
    mise = resolve_mise(layout.home)
    legacy = layout.home / LEGACY_MODE_RELATIVE
    managed = layout.home / NEW_MODE_RELATIVE
    requires_migration = (
        (legacy.exists() or legacy.is_symlink())
        and not (managed.exists() or managed.is_symlink())
    )
    return {
        "schema": "khenrix-maka-install-plan-v1",
        "version": PACKAGE_VERSION,
        "platform": supported_platform(),
        "source": str(source),
        "component": str(layout.component),
        "wrapper": str(layout.wrapper) if activate else None,
        "mise": str(mise),
        "account": account.pw_name,
        "portableFiles": len(files),
        "activate": activate,
        "requiresLegacyMigration": requires_migration,
        "copiesCredentials": False,
        "changesNativeMakaProfile": False,
        "actions": [
            "install exact mise lock",
            "copy only install-files.txt entries to the stable component path",
            "verify maka-agent package identity and version",
            *( ["atomically render ~/.local/bin/maka"] if activate else [] ),
        ],
    }


def backup_id() -> str:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{os.getpid()}"


def install(source: pathlib.Path, *, activate: bool) -> pathlib.Path:
    supported_platform()
    source = source.resolve(strict=True)
    layout = canonical_layout()
    account = pwd.getpwuid(os.getuid())
    mise = resolve_mise(layout.home)
    environment = clean_environment(layout, mise, account.pw_name)
    subprocess.run(
        [str(mise), "trust", str(source / "mise.toml")],
        env=environment,
        check=True,
        close_fds=True,
        timeout=30,
    )
    subprocess.run(
        [str(mise), "-C", str(source), "install", "--locked", "--yes"],
        env=environment,
        check=True,
        close_fds=True,
        timeout=900,
    )

    layout.component.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    staging = pathlib.Path(
        tempfile.mkdtemp(prefix=".maka-staging-", dir=layout.component.parent)
    )
    backup = layout.state / "backups" / backup_id()
    prior_component = backup / "component"
    prior_wrapper = backup / "maka"
    moved_previous = False
    installed_new = False
    activated_wrapper = False
    had_previous_wrapper = False
    try:
        copy_manifest(source, staging)
        subprocess.run(
            [str(mise), "trust", str(staging / "mise.toml")],
            env=environment,
            check=True,
            close_fds=True,
            timeout=30,
        )
        if layout.component.exists() or layout.component.is_symlink():
            if layout.component.is_symlink() or not layout.component.is_dir():
                raise ComponentInstallError("managed Maka component path is unsafe")
            backup.mkdir(mode=0o700, parents=True, exist_ok=False)
            os.replace(layout.component, prior_component)
            moved_previous = True
            if layout.wrapper.exists():
                if layout.wrapper.is_symlink() or not layout.wrapper.is_file():
                    raise ComponentInstallError("managed Maka wrapper path is unsafe")
                shutil.copy2(layout.wrapper, prior_wrapper, follow_symlinks=False)
                prior_wrapper.chmod(0o600)
                had_previous_wrapper = True
        os.replace(staging, layout.component)
        installed_new = True

        subprocess.run(
            [str(mise), "trust", str(layout.component / "mise.toml")],
            env=environment,
            check=True,
            close_fds=True,
            timeout=30,
        )
        subprocess.run(
            [str(mise), "-C", str(layout.component), "install", "--locked", "--yes"],
            env=environment,
            check=True,
            close_fds=True,
            timeout=900,
        )
        package = discover_package_root(mise, layout.component, environment)
        if activate:
            assert_cutover_safe(layout)
            if layout.wrapper.exists() and not backup.exists():
                backup.mkdir(mode=0o700, parents=True, exist_ok=False)
            if layout.wrapper.exists() and not had_previous_wrapper:
                if layout.wrapper.is_symlink() or not layout.wrapper.is_file():
                    raise ComponentInstallError("managed Maka wrapper path is unsafe")
                shutil.copy2(layout.wrapper, prior_wrapper, follow_symlinks=False)
                prior_wrapper.chmod(0o600)
                had_previous_wrapper = True
            template = (layout.component / "wrapper/maka.in").read_text(encoding="utf-8")
            wrapper = render_wrapper(
                template,
                layout=layout,
                account_name=account.pw_name,
                mise=mise,
                package=package,
            )
            atomic_write(layout.wrapper, wrapper.encode("utf-8"), 0o755)
            activated_wrapper = True
        layout.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        receipt = {
            "schema": "khenrix-maka-install-v1",
            "version": PACKAGE_VERSION,
            "platform": supported_platform(),
            "component": str(layout.component),
            "wrapper": str(layout.wrapper) if activate else None,
            "backup": backup.name if backup.exists() else None,
        }
        atomic_write(
            layout.state / "install-receipt.json",
            (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            0o600,
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if activated_wrapper:
            if had_previous_wrapper and prior_wrapper.exists():
                atomic_write(layout.wrapper, prior_wrapper.read_bytes(), 0o755)
            elif layout.wrapper.exists() and not layout.wrapper.is_symlink():
                layout.wrapper.unlink()
        if installed_new and layout.component.exists() and not layout.component.is_symlink():
            shutil.rmtree(layout.component)
        if moved_previous and prior_component.exists():
            os.replace(prior_component, layout.component)
        raise
    print(f"Installed Maka {PACKAGE_VERSION} at {layout.component}")
    return layout.component


def rollback(identifier: str) -> None:
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9]+", identifier):
        raise ComponentInstallError("backup id is invalid")
    layout = canonical_layout()
    backup = layout.state / "backups" / identifier
    previous_component = backup / "component"
    previous_wrapper = backup / "maka"
    if not backup.is_dir() or backup.is_symlink():
        raise ComponentInstallError("backup is unavailable")
    if previous_component.exists():
        if previous_component.is_symlink() or not previous_component.is_dir():
            raise ComponentInstallError("backup component is unsafe")
        displaced = backup / "replaced-component"
        if displaced.exists():
            raise ComponentInstallError("backup was already used")
        if layout.component.exists():
            if layout.component.is_symlink() or not layout.component.is_dir():
                raise ComponentInstallError("managed Maka component path is unsafe")
            os.replace(layout.component, displaced)
        os.replace(previous_component, layout.component)
    if previous_wrapper.exists():
        if previous_wrapper.is_symlink() or not previous_wrapper.is_file():
            raise ComponentInstallError("backup wrapper is unsafe")
        atomic_write(layout.wrapper, previous_wrapper.read_bytes(), 0o755)
    elif layout.wrapper.exists():
        if layout.wrapper.is_symlink() or not layout.wrapper.is_file():
            raise ComponentInstallError("managed Maka wrapper path is unsafe")
        layout.wrapper.unlink()
    print(f"Restored Maka backup {identifier}")


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "rollback"))
    parser.add_argument(
        "--source",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--backup-id")
    parser.add_argument(
        "--stage-only",
        action="store_true",
        help="install the component without replacing ~/.local/bin/maka",
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    os.umask(0o077)
    options = parse_args(arguments)
    try:
        if options.command == "rollback":
            if options.backup_id is None:
                raise ComponentInstallError("rollback requires --backup-id")
            rollback(options.backup_id)
        elif options.command == "plan":
            print(
                json.dumps(
                    installation_plan(options.source, activate=not options.stage_only),
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            install(options.source, activate=not options.stage_only)
    except (ComponentInstallError, OSError, subprocess.SubprocessError) as error:
        print(f"Maka component installation failed: {error}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
