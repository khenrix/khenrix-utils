"""Materialize sealed Forge quality fixtures without exposing held-out checks.

The caller first verifies the committed casebook pin. Only ``public/<case>``
is copied to an executor workspace; ``held-out/<case>.py`` is invoked later by
the controller in an isolated verifier workspace. This module records fixture
inputs and command specs, never a fabricated live result.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple


ROOT = Path(__file__).resolve().parents[1] / "evals" / "llm-fanout" / "forge-fixtures"
CASEBOOK = ROOT.parent / "quality-cases.json"
_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_SYSTEM_GIT = Path("/usr/bin/git")


class FixtureError(ValueError):
    """A fixture is missing, malformed, or no longer matches its preregistered pin."""


class ForgeBoundaryDescriptor(NamedTuple):
    run_root: Path
    baseline_root: Path
    consumer_root: Path
    protected_roots: tuple[Path, ...]
    executable_paths: tuple[tuple[str, Path], ...]
    policy_sha256: str
    seal: str


_BOUNDARY_SCHEMA = "fanout-forge-boundary-v1"
_BOUNDARY_DIR = ".forge-boundary"
_VERIFIER_BOUNDARY_ENV = "FANOUT_FORGE_VERIFIER_BOUNDARY"


def _account_home() -> Path:
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
        ambient = os.environ.get("HOME")
        if ambient is not None and Path(ambient).resolve(strict=True) != home:
            raise FixtureError("Forge HOME differs from the OS account home")
    except (KeyError, OSError, RuntimeError) as error:
        raise FixtureError("Forge OS account home is unavailable") from error
    return home


def _boundary_paths(run_root: Path | str | None, baseline_root: Path | str | None,
                    consumer_root: Path | str | None) -> tuple[Path, Path, Path]:
    if "AGENTIC_MEMORY_HOME" in os.environ:
        raise FixtureError("Forge AGENTIC_MEMORY_HOME override is not sealed")
    paths = []
    for label, raw in (("campaign", run_root), ("baseline", baseline_root),
                       ("consumer", consumer_root)):
        if raw is None:
            raise FixtureError(f"Forge {label} root is required for seat blindness")
        path = Path(raw)
        if path.is_symlink() or (label != "campaign" and not path.is_dir()):
            raise FixtureError(f"Forge {label} root is not a real directory")
        paths.append(path.resolve(strict=label != "campaign"))
    run, baseline, consumer = paths
    if len(set(paths)) != 3 or any(
        run == protected or protected in run.parents
        for protected in (baseline, consumer, _account_home(), *_protected_roots(ROOT))
    ):
        raise FixtureError("Forge campaign root overlaps a protected root")
    return run, baseline, consumer


def _installed_executables() -> tuple[tuple[str, Path], ...]:
    binaries = []
    for executor in ("claude", "codex", "agy"):
        found = shutil.which(executor)
        if found is None:
            raise FixtureError(f"Forge {executor} executable is unavailable")
        path = Path(found).resolve(strict=True)
        if executor == "agy":
            path = Path(found).parent.parent / "libexec" / "agy-bin"
            path = path.resolve(strict=True)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise FixtureError(f"Forge {executor} executable is not a regular executable")
        binaries.append((executor, path))
    return tuple(binaries)


def _boundary_payload(run: Path, baseline: Path, consumer: Path) -> dict[str, Any]:
    home = _account_home()
    roots = tuple(sorted({*_protected_roots(ROOT), home, baseline, consumer, run}))
    executables = _installed_executables()
    if any(path == root or root in path.parents for _, path in executables
           for root in (baseline, consumer, run, *_protected_roots(ROOT))):
        raise FixtureError("Forge CLI executable is inside a protected campaign root")
    provisional = ForgeBoundaryDescriptor(
        run, baseline, consumer, roots, executables, "", "",
    )
    policy_template = _native_campaign_policy(
        provisional, run / "__SEAT__", run / "__STATE__",
    )
    return {
        "schema_version": _BOUNDARY_SCHEMA,
        "run_root": str(run), "baseline_root": str(baseline),
        "consumer_root": str(consumer),
        "protected_roots": [str(path) for path in roots],
        "executable_paths": [[name, str(path)] for name, path in executables],
        "policy_sha256": hashlib.sha256(policy_template.encode("utf-8")).hexdigest(),
    }


def _private_bytes(path: Path, *, limit: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise FixtureError("Forge boundary seal is unavailable") from error
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or not 0 < info.st_size <= limit):
            raise FixtureError("Forge boundary seal file is unsafe")
        data = os.read(descriptor, limit + 1)
        if len(data) != info.st_size:
            raise FixtureError("Forge boundary seal file changed during read")
        return data
    finally:
        os.close(descriptor)


def _write_private_bytes(path: Path, data: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600)
    except OSError as error:
        raise FixtureError("Forge boundary seal path already exists or is unsafe") from error
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _boundary_descriptor(payload: dict[str, Any], seal: str) -> ForgeBoundaryDescriptor:
    return ForgeBoundaryDescriptor(
        run_root=Path(payload["run_root"]), baseline_root=Path(payload["baseline_root"]),
        consumer_root=Path(payload["consumer_root"]),
        protected_roots=tuple(Path(item) for item in payload["protected_roots"]),
        executable_paths=tuple((name, Path(path)) for name, path in payload["executable_paths"]),
        policy_sha256=payload["policy_sha256"], seal=seal,
    )


def open_blindness_boundary(
    run_root: Path | str | None, baseline_root: Path | str | None,
    consumer_root: Path | str | None,
    *, resume: bool,
) -> ForgeBoundaryDescriptor:
    """Create or reopen the controller-owned policy descriptor before campaign intents."""
    run, baseline, consumer = _boundary_paths(run_root, baseline_root, consumer_root)
    payload = _boundary_payload(run, baseline, consumer)
    directory = run / _BOUNDARY_DIR
    if resume:
        try:
            root_info, directory_info = run.lstat(), directory.lstat()
        except OSError as error:
            raise FixtureError("Forge boundary seal is missing on resume") from error
        if any(not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
               or stat.S_IMODE(info.st_mode) != 0o700 for info in (root_info, directory_info)):
            raise FixtureError("Forge boundary directories are unsafe")
        key = _private_bytes(directory / "key", limit=32)
        if len(key) != 32:
            raise FixtureError("Forge boundary key has wrong length")
        try:
            sealed = json.loads(_private_bytes(directory / "descriptor.json", limit=16 * 1024))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise FixtureError("Forge boundary seal is invalid JSON") from error
        if not isinstance(sealed, dict) or set(sealed) != set(payload) | {"seal"}:
            raise FixtureError("Forge boundary descriptor schema changed")
        actual_seal = sealed.pop("seal")
        expected_seal = hmac.new(key, _canonical(sealed), hashlib.sha256).hexdigest()
        if (not isinstance(actual_seal, str)
                or not hmac.compare_digest(actual_seal, expected_seal)
                or sealed != payload):
            raise FixtureError("Forge boundary seal or protected roots changed")
        return _boundary_descriptor(payload, actual_seal)
    try:
        run.mkdir(mode=0o700, parents=True, exist_ok=False)
        directory.mkdir(mode=0o700, exist_ok=False)
    except OSError as error:
        raise FixtureError("Forge campaign root already exists or cannot be sealed") from error
    key = secrets.token_bytes(32)
    seal = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
    _write_private_bytes(directory / "key", key)
    _write_private_bytes(directory / "descriptor.json", _canonical({**payload, "seal": seal}))
    return _boundary_descriptor(payload, seal)


def _verify_boundary(boundary: ForgeBoundaryDescriptor) -> None:
    if not isinstance(boundary, ForgeBoundaryDescriptor):
        raise FixtureError("Forge boundary descriptor is required")
    reopened = open_blindness_boundary(
        boundary.run_root, boundary.baseline_root, boundary.consumer_root, resume=True,
    )
    if reopened != boundary:
        raise FixtureError("Forge boundary descriptor differs from its sealed disk state")


def _reject_allowed_hardlinks(workspace: Path, state: Path) -> None:
    inspected = 0
    try:
        for root in (workspace, state):
            for path in root.rglob("*"):
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise FixtureError("hard-link alias in Forge seat or session state; executor is blocked")
                inspected += 1
                if inspected > 200_000:
                    raise FixtureError("Forge seat hard-link scan exceeded its bound")
    except OSError as error:
        raise FixtureError("cannot inspect Forge seat hard-link roots") from error


def _seat_state(boundary: ForgeBoundaryDescriptor, workspace: Path) -> Path:
    relative = workspace.relative_to(boundary.run_root)
    parts = relative.parts
    if (len(parts) == 4 and parts[0] == "cases"
            and parts[2] == "seats" and parts[3] in {"claude", "codex", "agy"}):
        role = parts[3]
    elif (len(parts) == 3 and parts[0] == "cases"
          and parts[2] in {"synthesis", "reviewer"}):
        role = parts[2]
    else:
        raise FixtureError("Forge seat workspace is outside the sealed campaign layout")
    if not re.fullmatch(r"forge-[0-9]{2}", parts[1]):
        raise FixtureError("Forge seat case ID is invalid")
    private = boundary.run_root / ".forge-seat-state"
    case_state = private / parts[1]
    state = case_state / role
    for path in (private, case_state, state, state / ".config", state / "tmp"):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise FixtureError("Forge private seat state is unsafe")
    return state


def seat_environment(boundary: ForgeBoundaryDescriptor, workspace: Path,
                     executor_id: str) -> dict[str, str]:
    """Point each CLI's persisted session state at one seat-specific private HOME."""
    _verify_boundary(boundary)
    workspace = Path(workspace).resolve(strict=True)
    state = _seat_state(boundary, workspace)
    values = {
        "HOME": str(state), "XDG_CONFIG_HOME": str(state / ".config"),
        "TMPDIR": str(state / "tmp"),
    }
    if executor_id == "agy":
        values.update(GOOGLE_CLOUD_LOCATION="eu", GOOGLE_CLOUD_REGION="eu")
    return values


def require_blindness_attestation(
    *, boundary: ForgeBoundaryDescriptor | None = None,
    seat_workspace: Path | None = None,
) -> None:
    """Fail closed until the sealed native boundary and auth/resume are proven."""
    _verify_boundary(boundary)
    if seat_workspace is not None:
        workspace = Path(seat_workspace).resolve(strict=True)
        _attest_native_boundary(boundary, workspace)
    else:
        with tempfile.TemporaryDirectory(prefix=".forge-precreation-", dir=boundary.run_root) as temporary:
            workspace = Path(temporary) / "seat"
            state = Path(temporary) / "state"
            workspace.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            (workspace / "sentinel").write_bytes(b"seat\n")
            _attest_native_policy(boundary, workspace, state)
    # A shell-capable seat could read ambient ADC even if it were copied into its private HOME.
    # Launch needs a trusted credential broker and measured exact-resume behavior.
    raise FixtureError("Forge blindness boundary lacks live CLI auth and exact-resume attestation")


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n").encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _rel(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise FixtureError("invalid fixture relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise FixtureError("invalid fixture relative path")
    return value


def _manifest(root: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FixtureError("cannot read Forge fixture manifest") from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "cases"}:
        raise FixtureError("Forge fixture manifest schema is invalid")
    cases = value["cases"]
    if value["schema_version"] != 1 or not isinstance(cases, list) or len(cases) != 12:
        raise FixtureError("Forge fixture manifest needs twelve cases")
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) not in (
            {"id", "mode", "owned_paths", "check"},
            {"id", "mode", "owned_paths", "check", "seed"},
        ):
            raise FixtureError("Forge fixture case schema is invalid")
        case_id = case["id"]
        if (not isinstance(case_id, str) or not re.fullmatch(r"forge-[0-9]{2}", case_id)
                or case_id in ids):
            raise FixtureError("invalid or duplicate Forge fixture ID")
        ids.add(case_id)
        if case["mode"] not in {"review", "deep-review", "ingress", "fusion"}:
            raise FixtureError("invalid Forge fixture mode")
        owned = case["owned_paths"]
        if not isinstance(owned, list) or not owned or len(set(owned)) != len(owned):
            raise FixtureError("Forge fixture needs unique owned paths")
        for path in owned:
            _rel(path)
        check = case["check"]
        if (not isinstance(check, dict) or set(check) != {"id", "argv", "timeout"}
                or not isinstance(check["id"], str) or not check["id"]
                or not isinstance(check["argv"], list) or not check["argv"]
                or not all(isinstance(arg, str) and arg for arg in check["argv"])
                or not isinstance(check["timeout"], int) or check["timeout"] < 1):
            raise FixtureError("invalid declared check")
        if "seed" in case:
            _validate_seed(case["seed"])
    return cases


def _validate_seed(seed: object) -> None:
    if not isinstance(seed, dict) or not seed or set(seed) - {
        "staged_binary", "untracked_note", "symlink",
    }:
        raise FixtureError("invalid Forge fixture seed")
    binary = seed.get("staged_binary")
    if binary is not None:
        if not isinstance(binary, dict) or set(binary) != {"path", "base_hex", "staged_hex"}:
            raise FixtureError("invalid staged binary seed")
        _rel(binary["path"])
        for key in ("base_hex", "staged_hex"):
            if not isinstance(binary[key], str):
                raise FixtureError("invalid staged binary bytes")
            try:
                bytes.fromhex(binary[key])
            except ValueError as error:
                raise FixtureError("invalid staged binary bytes") from error
    note = seed.get("untracked_note")
    if note is not None:
        if not isinstance(note, dict) or set(note) != {"path", "text"}:
            raise FixtureError("invalid untracked note seed")
        _rel(note["path"])
        if not isinstance(note["text"], str):
            raise FixtureError("invalid untracked note text")
    link = seed.get("symlink")
    if link is not None:
        if not isinstance(link, dict) or set(link) != {"path", "target"}:
            raise FixtureError("invalid symlink seed")
        _rel(link["path"])
        if (not isinstance(link["target"], str) or not link["target"]
                or "/" in link["target"] or "\\" in link["target"]
                or link["target"] in {".", ".."}):
            raise FixtureError("symlink seed must point to a sibling file")


def _tree_digest(source: Path, case_id: str, seed: object) -> str:
    if not source.is_dir() or source.is_symlink():
        raise FixtureError(f"missing public source for {case_id}")
    files = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise FixtureError(f"public source symlink is not allowed: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise FixtureError(f"special fixture file is not allowed: {path}")
        relative = _rel(path.relative_to(source).as_posix())
        if any(part in {".git", "held-out"} for part in relative.split("/")):
            raise FixtureError("private or repository-control path in public source")
        files.append({
            "path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": stat.S_IMODE(path.stat().st_mode),
        })
    if not files:
        raise FixtureError(f"empty public source for {case_id}")
    return _digest({"files": files, "seed": seed})


def _source_tree(root: Path, case_id: str, seed: object) -> str:
    return _tree_digest(root / "public" / case_id, case_id, seed)


def descriptors(root: Path = ROOT) -> dict[str, dict[str, Any]]:
    """Recompute exact scorer descriptors from public files and private oracle bytes."""
    root = Path(root)
    probe = root / "held-out" / "probe.py"
    if not probe.is_file() or probe.is_symlink():
        raise FixtureError("missing held-out candidate probe")
    probe_sha256 = hashlib.sha256(probe.read_bytes()).hexdigest()
    result = {}
    for case in _manifest(root):
        case_id = case["id"]
        oracle = root / "held-out" / f"{case_id}.py"
        if not oracle.is_file() or oracle.is_symlink():
            raise FixtureError(f"missing held-out check for {case_id}")
        oracle_sha256 = hashlib.sha256(oracle.read_bytes()).hexdigest()
        source_sha256 = _source_tree(root, case_id, case.get("seed"))
        declared_checks = [{"id": case["check"]["id"],
                            "spec_sha256": _digest(case["check"])}]
        fixture_sha256 = _digest({
            "case_id": case_id, "mode": case["mode"],
            "source_tree_sha256": source_sha256,
            "owned_paths": case["owned_paths"],
            "declared_checks": declared_checks,
            "held_out_sha256": oracle_sha256,
            "candidate_probe_sha256": probe_sha256,
        })
        result[case_id] = {
            "case_id": case_id, "fixture_manifest_sha256": fixture_sha256,
            "source_tree_sha256": source_sha256,
            "owned_paths": case["owned_paths"], "declared_checks": declared_checks,
        }
    return result


def manifest_sha256(root: Path = ROOT) -> str:
    cases = descriptors(root)
    return _digest([cases[case_id] for case_id in sorted(cases)])


def verify_pin(casebook: Path, root: Path = ROOT) -> dict[str, dict[str, Any]]:
    """Refuse source, check, or casebook drift before any live provider call."""
    try:
        book = json.loads(Path(casebook).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FixtureError("cannot read Forge fixture casebook") from error
    if (not isinstance(book, dict) or book.get("schema_version") != 1
            or not isinstance(book.get("cases"), list)
            or book.get("cases_sha256") != _digest(book["cases"])):
        raise FixtureError("Forge fixture casebook seal is invalid")
    cases = _manifest(Path(root))
    forge = {case["id"]: case["mode"] for case in book["cases"] if case["kind"] == "forge"}
    if forge != {case["id"]: case["mode"] for case in cases}:
        raise FixtureError("Forge fixture cases differ from casebook")
    actual = descriptors(root)
    if book.get("forge_fixture_manifest_sha256") != _digest(
        [actual[case_id] for case_id in sorted(actual)]
    ):
        raise FixtureError("Forge fixture pin does not match source and held-out checks")
    return actual


def _trusted_git() -> str:
    try:
        info = _SYSTEM_GIT.lstat()
    except OSError as error:
        raise FixtureError("trusted system Git is unavailable") from error
    unsafe = stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & unsafe or not os.access(_SYSTEM_GIT, os.X_OK)):
        raise FixtureError("trusted system Git is unsafe")
    return str(_SYSTEM_GIT)


def _git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin", "HOME": "/var/empty", "XDG_CONFIG_HOME": "/var/empty",
        "LC_ALL": "C", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TEMPLATE_DIR": os.devnull, "GIT_TERMINAL_PROMPT": "0",
    }


def _git(workspace: Path, *args: str) -> None:
    subprocess.run([_trusted_git(), "-C", str(workspace), *args], env=_git_environment(),
                   check=True, capture_output=True, timeout=30)


def _seed_dirty_workspace(workspace: Path, seed: dict[str, Any]) -> None:
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "config", "user.name", "Forge Fixture")
    _git(workspace, "config", "user.email", "forge-fixture@example.invalid")
    binary = seed.get("staged_binary")
    if binary is not None:
        path = workspace / binary["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes.fromhex(binary["base_hex"]))
    link = seed.get("symlink")
    if link is not None:
        path = workspace / link["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(link["target"])
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "fixture baseline")
    if binary is not None:
        path.write_bytes(bytes.fromhex(binary["staged_hex"]))
        _git(workspace, "add", binary["path"])
    note = seed.get("untracked_note")
    if note is not None:
        path = workspace / note["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note["text"], encoding="utf-8")


def materialize(case_id: str, workspace: Path, root: Path = ROOT) -> None:
    """Copy only one public fixture and its declared caller-owned dirty baseline."""
    verify_pin(CASEBOOK, root)
    cases = {case["id"]: case for case in _manifest(Path(root))}
    if case_id not in cases:
        raise FixtureError(f"unknown Forge fixture {case_id}")
    source = Path(root) / "public" / case_id
    seed = cases[case_id].get("seed")
    expected_tree = _source_tree(Path(root), case_id, seed)
    workspace = Path(workspace)
    if workspace.exists() or workspace.is_symlink():
        raise FixtureError("Forge fixture workspace must be new")
    shutil.copytree(source, workspace, symlinks=True)
    if _tree_digest(workspace, case_id, seed) != expected_tree:
        raise FixtureError("materialized source differs from pinned public source")
    if seed is not None:
        _seed_dirty_workspace(workspace, seed)


def _protected_roots(root: Path) -> tuple[Path, ...]:
    checkout = root.resolve().parents[2]
    git = _trusted_git()
    environment = _git_environment()
    common = subprocess.run(
        [git, "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        env=environment, capture_output=True, text=True, timeout=15,
    )
    worktrees = subprocess.run(
        [git, "-C", str(checkout), "worktree", "list", "--porcelain"],
        env=environment, capture_output=True, text=True, timeout=15,
    )
    if common.returncode or worktrees.returncode:
        raise FixtureError("cannot discover all Forge source and Git object roots")
    roots = {Path(common.stdout.strip()).resolve()}
    roots.update(Path(line.removeprefix("worktree ")).resolve()
                 for line in worktrees.stdout.splitlines() if line.startswith("worktree "))
    if checkout not in roots or not all(path.is_dir() for path in roots):
        raise FixtureError("Forge checkout or Git object root is not a registered directory")
    if (roots.intersection({Path("/"), _account_home()})
            or (roots and (Path(common.stdout.strip()) / "objects" / "info" / "alternates").exists())):
        raise FixtureError("Forge source roots are too broad or use Git object alternates")
    return tuple(sorted(roots))


def _sandbox_policy(roots: tuple[Path, ...], private: tuple[Path, ...] = ()) -> str:
    rules = []
    for root in (*roots, *private):
        literal = json.dumps(str(root))
        rules.append(f"(deny file-read* (subpath {literal}))")
        rules.append(f"(deny file-write* (subpath {literal}))")
    return "(version 1) (allow default) " + " ".join(rules) + " (deny file-link)"


def _native_campaign_policy(boundary: ForgeBoundaryDescriptor, workspace: Path,
                            state: Path) -> str:
    # Path-denying protected trees alone leaks a hard-linked file through an
    # alias elsewhere. Deny reads everywhere except immutable OS runtime files,
    # exact CLI executables, and this seat's two writable trees.
    runtime_roots = tuple(Path(path) for path in (
        "/System", "/usr/bin", "/usr/lib", "/usr/share",
        "/Library/Apple", "/Library/Developer/CommandLineTools",
        "/private/var/db/dyld", "/private/var/db/timezone",
        "/private/preboot/Cryptexes", "/private/etc/ssl",
    ))
    runtime_files = tuple(Path(path) for path in (
        "/", "/dev/null", "/dev/random", "/dev/urandom", "/dev/zero",
        "/private/etc/passwd", "/private/etc/services", "/private/etc/protocols",
        "/etc/hosts", "/private/etc/hosts",
    ))
    roots = (*runtime_roots, workspace, state)
    files = (*runtime_files, *(path for _, path in boundary.executable_paths))
    readable = [*(f"(subpath {json.dumps(str(path))})" for path in roots),
                *(f"(literal {json.dumps(str(path))})" for path in files)]
    ancestors = {parent for path in (*roots, *files) for parent in path.parents}
    ancestors.update(Path(path) for path in ("/bin", "/etc", "/sbin", "/tmp", "/var"))
    metadata = [f"(literal {json.dumps(str(path))})" for path in sorted(ancestors)]
    read_filter = "(require-any " + " ".join(readable) + ")"
    metadata_filter = "(require-any " + " ".join((*readable, *metadata)) + ")"
    rules = [
        "(version 1)", "(allow default)",
        "(deny file-read* (require-not " + metadata_filter + "))",
        "(deny file-read-data (require-not " + read_filter + "))",
        "(deny file-write*)",
    ]
    for allowed in (workspace, state):
        literal = json.dumps(str(allowed))
        rules.extend((f"(allow file-read* (subpath {literal}))",
                      f"(allow file-write* (subpath {literal}))"))
    rules.append('(allow file-write* (literal "/dev/null"))')
    for _, executable in boundary.executable_paths:
        rules.append(f"(allow file-read* (literal {json.dumps(str(executable))}))")
    rules.extend(("(deny file-link)",
                  '(deny network-outbound (remote ip "localhost:*"))'))
    return " ".join(rules)


def _is_campaign_verifier(workspace: Path) -> bool:
    return (workspace.name == "workspace"
            and workspace.parent.name.startswith("forge-fresh-verifier-")
            and workspace.parent.parent.name == "verifiers")


def _verifier_policy(boundary: ForgeBoundaryDescriptor,
                     workspace: Path) -> tuple[str, Path]:
    _verify_boundary(boundary)
    workspace = Path(workspace)
    if (not workspace.is_absolute() or workspace.is_symlink()
            or workspace.resolve(strict=True) != workspace or not workspace.is_dir()
            or not _is_campaign_verifier(workspace)
            or workspace.parent.parent != boundary.run_root / "verifiers"):
        raise FixtureError("Forge verifier workspace is outside its sealed campaign layout")
    parent_info = workspace.parent.lstat()
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid()
            or stat.S_IMODE(parent_info.st_mode) != 0o700):
        raise FixtureError("Forge verifier state parent is unsafe")
    state = workspace.parent / "state"
    for path in (state, state / ".config", state / "tmp"):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise FixtureError("Forge verifier private state is unsafe")
    return _attest_native_policy(boundary, workspace, state), state


def _verifier_boundary_from_environment() -> ForgeBoundaryDescriptor | None:
    encoded = os.environ.get(_VERIFIER_BOUNDARY_ENV)
    if encoded is None:
        return None
    try:
        values = json.loads(encoded)
        if (not isinstance(values, list) or len(values) != 4
                or any(not isinstance(value, str) or not value for value in values)):
            raise ValueError("invalid verifier boundary")
        boundary = open_blindness_boundary(*values[:3], resume=True)
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        raise FixtureError("sealed Forge verifier boundary evidence is invalid") from error
    if not hmac.compare_digest(boundary.seal, values[3]):
        raise FixtureError("sealed Forge verifier boundary evidence changed")
    return boundary


def _require_unprotected_verifier_python(boundary: ForgeBoundaryDescriptor) -> None:
    python = Path(sys.executable).resolve(strict=True)
    if any(python == root or root in python.parents for root in boundary.protected_roots):
        raise FixtureError("Forge verifier Python runtime is inside a protected root")


def _native_campaign_probe(policy: str, source: str, *args: str) -> int:
    _trusted_git()
    result = subprocess.run(
        [str(_SANDBOX_EXEC), "-p", policy, "/usr/bin/python3", "-I", "-c", source, *args],
        env=_git_environment(), capture_output=True, text=True, timeout=20,
    )
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        raise FixtureError("sandbox-exec cannot apply in this process")
    return result.returncode


def _native_peer_state_paths(boundary: ForgeBoundaryDescriptor,
                             state: Path) -> tuple[Path, ...]:
    private = boundary.run_root / ".forge-seat-state"
    if state.parent.parent != private:
        return ()
    peers = [state.parent / name for name in ("claude", "codex", "agy", "synthesis", "reviewer")
             if name != state.name]
    peers.extend(private / f"forge-{number:02d}" for number in range(1, 13)
                 if state.parent.name != f"forge-{number:02d}")
    return tuple(peers)


def _attest_native_boundary(boundary: ForgeBoundaryDescriptor, workspace: Path) -> str:
    _verify_boundary(boundary)
    workspace = Path(workspace)
    if (not workspace.is_absolute() or workspace.is_symlink()
            or workspace.resolve(strict=True) != workspace or not workspace.is_dir()):
        raise FixtureError("Forge seat workspace must be a canonical directory")
    state = _seat_state(boundary, workspace)
    return _attest_native_policy(boundary, workspace, state)


def _attest_native_policy(boundary: ForgeBoundaryDescriptor, workspace: Path,
                          state: Path) -> str:
    if not _SANDBOX_EXEC.is_file() or not os.access(_SANDBOX_EXEC, os.X_OK):
        raise FixtureError("macOS sandbox-exec is unavailable; Forge executor is blocked")
    _reject_allowed_hardlinks(workspace, state)
    policy = _native_campaign_policy(boundary, workspace, state)
    read = "import sys;open(sys.argv[1],'rb').read(1)"
    denied = ("import sys\ntry: open(sys.argv[1],'rb').read(1)\n"
              "except PermissionError: sys.exit(17)\nsys.exit(0)")
    denied_dir = ("import os,sys\ntry: os.listdir(sys.argv[1])\n"
                  "except PermissionError: sys.exit(17)\nsys.exit(0)")
    own_files = [path for path in workspace.rglob("*") if path.is_file() and not path.is_symlink()]
    if not own_files or _native_campaign_probe(policy, read, str(own_files[0])) != 0:
        raise FixtureError("sandbox cannot read the Forge seat workspace")
    state_sentinel = state / ".boundary-probe"
    try:
        descriptor = os.open(state_sentinel,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    except OSError as error:
        raise FixtureError("Forge private session probe path is unsafe") from error
    else:
        with os.fdopen(descriptor, "wb") as output:
            output.write(b"seat\n")
    try:
        if _private_bytes(state_sentinel, limit=5) != b"seat\n":
            raise FixtureError("Forge private session probe path is unsafe")
    except FixtureError as error:
        raise FixtureError("Forge private session probe path is unsafe") from error
    if _native_campaign_probe(policy, read, str(state_sentinel)) != 0:
        raise FixtureError("sandbox cannot read its private session state")
    write_stage = (
        "import os,pathlib,subprocess,sys,tempfile\n"
        "env=os.environ.copy()\n"
        "with tempfile.TemporaryDirectory(prefix='.forge-boundary-',dir=sys.argv[1]) as repo:\n"
        "    pathlib.Path(repo,'probe.txt').write_text('probe\\n',encoding='utf-8')\n"
        "    subprocess.run(['/usr/bin/git','init','-q',repo],env=env,check=True,capture_output=True)\n"
        "    subprocess.run(['/usr/bin/git','-C',repo,'add','--','probe.txt'],"
        "env=env,check=True,capture_output=True)\n"
        "with tempfile.TemporaryDirectory(prefix='.forge-boundary-',dir=sys.argv[2]):\n"
        "    pass\n"
    )
    if _native_campaign_probe(policy, write_stage, str(workspace), str(state)) != 0:
        raise FixtureError("sandbox cannot write and stage in the Forge seat workspace or session state")
    for root in boundary.protected_roots:
        if _native_campaign_probe(policy, denied_dir, str(root)) != 17:
            raise FixtureError("sandbox did not deny a Forge protected root")
    descriptor_file = boundary.run_root / _BOUNDARY_DIR / "descriptor.json"
    if _native_campaign_probe(policy, denied, str(descriptor_file)) != 17:
        raise FixtureError("sandbox did not deny the Forge boundary seal")
    sibling = (workspace.parent / "codex" if workspace.name != "codex"
               else workspace.parent / "claude")
    if sibling.is_dir() and _native_campaign_probe(policy, denied_dir, str(sibling)) != 17:
        raise FixtureError("sandbox did not deny a Forge sibling seat")
    for private in _seat_private_paths(workspace):
        if private.is_dir() and _native_campaign_probe(policy, denied_dir, str(private)) != 17:
            raise FixtureError("sandbox did not deny a Forge caller, peer, or authority root")
        if private.is_file() and _native_campaign_probe(policy, denied, str(private)) != 17:
            raise FixtureError("sandbox did not deny a Forge caller, peer, or authority file")
    for peer_state in _native_peer_state_paths(boundary, state):
        if peer_state.is_dir() and _native_campaign_probe(policy, denied_dir, str(peer_state)) != 17:
            raise FixtureError("sandbox did not deny a Forge peer session state")
    oracle_root = ROOT / "held-out"
    if any(oracle_root == root or root in oracle_root.parents
           for root in boundary.protected_roots):
        oracle_files = [oracle_root / "probe.py"]
        relative = workspace.relative_to(boundary.run_root).parts
        if len(relative) > 1 and re.fullmatch(r"forge-[0-9]{2}", relative[1]):
            oracle_files.append(oracle_root / f"{relative[1]}.py")
        for oracle in oracle_files:
            if not oracle.is_file() or _native_campaign_probe(policy, denied, str(oracle)) != 17:
                raise FixtureError("sandbox did not deny a Forge held-out oracle")
    with tempfile.TemporaryDirectory(prefix="forge-boundary-alias-") as temporary:
        alias = Path(temporary) / "alias"
        alias.symlink_to(descriptor_file)
        if _native_campaign_probe(policy, denied, str(alias)) != 17:
            raise FixtureError("sandbox did not deny a Forge protected symlink alias")
        alias.unlink()
        link = ("import os,sys\ntry: os.link(sys.argv[1],sys.argv[2])\n"
                "except PermissionError: sys.exit(17)\nsys.exit(0)")
        if (_native_campaign_probe(policy, link, str(descriptor_file), str(alias)) != 17
                or alias.exists()):
            raise FixtureError("sandbox did not deny Forge protected hard-link creation")
        outside = Path(temporary) / "outside"
        outside.write_bytes(b"outside\n")
        write_open = ("import os,sys\ntry: fd=os.open(sys.argv[1],os.O_WRONLY)\n"
                      "except PermissionError: sys.exit(17)\n"
                      "os.close(fd)\nsys.exit(0)")
        if (_native_campaign_probe(policy, write_open, str(outside)) != 17
                or outside.read_bytes() != b"outside\n"):
            raise FixtureError("sandbox did not deny out-of-seat file writes")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = str(listener.getsockname()[1])
        connect = ("import socket,sys\ntry: socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=2)\n"
                   "except PermissionError: sys.exit(17)\n"
                   "except OSError: sys.exit(18)\nsys.exit(0)")
        if _native_campaign_probe(policy, connect, port) != 17:
            raise FixtureError("sandbox did not deny local memory-network access")
    return policy


def _seat_private_paths(workspace: Path) -> tuple[Path, ...]:
    executors = ("claude", "codex", "agy")
    if workspace.parent.name == "seats" and workspace.name in executors:
        case = workspace.parent.parent
        private = [workspace.parent / peer for peer in executors if peer != workspace.name]
        private.extend(case / name for name in (
            "caller", "turns", "synthesis", "reviewer", "baseline.json",
        ))
    elif workspace.name in {"synthesis", "reviewer"} and re.fullmatch(
        r"forge-[0-9]{2}", workspace.parent.name,
    ):
        case = workspace.parent
        private = [case / name for name in (
            "seats", "caller", "turns", "baseline.json",
            "reviewer" if workspace.name == "synthesis" else "synthesis",
        )]
    else:
        return ()
    private.extend(case.parent / f"forge-{number:02d}" for number in range(1, 13)
                   if case.name != f"forge-{number:02d}")
    return tuple(private)


def _sandbox_probe(policy: str, source: str, *args: str) -> int:
    _trusted_git()
    result = subprocess.run(
        [str(_SANDBOX_EXEC), "-p", policy, sys.executable, "-I", "-c", source, *args],
        env=_git_environment(), capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        raise FixtureError("sandbox-exec cannot apply in this process")
    return result.returncode


def _reject_hardlinked_git_objects(roots: tuple[Path, ...]) -> None:
    seen: set[Path] = set()
    try:
        for root in roots:
            try:
                git_mode = (root / ".git").lstat().st_mode
            except FileNotFoundError:
                stores = [root / "objects"]
            else:
                if stat.S_ISDIR(git_mode):
                    stores = [root / ".git" / "objects"]
                elif stat.S_ISREG(git_mode):
                    stores = []
                else:
                    raise FixtureError("Forge worktree Git control path is not a file or directory")
            for store in stores:
                try:
                    mode = store.lstat().st_mode
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(mode):
                    raise FixtureError("Forge Git object store is not a directory")
                resolved = store.resolve(strict=True)
                if resolved in seen:
                    continue
                seen.add(resolved)
                pending = [store]
                while pending:
                    with os.scandir(pending.pop()) as entries:
                        for entry in entries:
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                pending.append(Path(entry.path))
                            elif not stat.S_ISREG(info.st_mode):
                                raise FixtureError("Forge Git object store has a special entry")
                            elif info.st_nlink != 1:
                                raise FixtureError("hard-link alias in Forge Git object store; executor is blocked")
    except OSError as error:
        raise FixtureError("cannot inspect Forge Git object stores") from error
    if not seen:
        raise FixtureError("cannot discover Forge Git object stores")


def _reject_hardlinked_sibling_files(workspace: Path) -> None:
    if workspace.parent.name != "seats":
        return
    for peer in ("claude", "codex", "agy"):
        if peer == workspace.name:
            continue
        sibling = workspace.parent / peer
        if not sibling.exists():
            continue
        if sibling.is_symlink() or not sibling.is_dir():
            raise FixtureError("Forge sibling seat is not a directory")
        for path in sibling.rglob("*"):
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise FixtureError("hard-link alias in Forge sibling seat; executor is blocked")


def _validate_boundary(case_id: str, workspace: Path, root: Path) -> str:
    cases = {case["id"]: case for case in _manifest(root)}
    if case_id not in cases:
        raise FixtureError(f"unknown Forge fixture {case_id}")
    if not _SANDBOX_EXEC.is_file() or not os.access(_SANDBOX_EXEC, os.X_OK):
        raise FixtureError("macOS sandbox-exec is unavailable; Forge executor is blocked")
    roots = _protected_roots(root)
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir() or any(
        workspace == protected or protected in workspace.parents for protected in roots
    ):
        raise FixtureError("Forge executor workspace must be outside the source checkout")
    _reject_hardlinked_git_objects(roots)
    _reject_hardlinked_sibling_files(workspace)
    checkout = root.resolve().parents[2]
    hidden = root / "held-out" / f"{case_id}.py"
    private_files = [hidden, CASEBOOK, checkout / "tests" / "test_fanout_forge_fixtures.py"]
    private_files.extend((root / "held-out").glob("*.py"))
    for path in private_files:
        if path.is_file() and path.stat().st_nlink != 1:
            raise FixtureError("Forge private fixture has a hard-link alias; executor is blocked")
    policy = _sandbox_policy(roots, _seat_private_paths(workspace))
    read = "import sys;open(sys.argv[1],'rb').read(1)"
    denied = "import sys\ntry: open(sys.argv[1],'rb').read(1)\nexcept PermissionError: sys.exit(17)\nsys.exit(0)"
    write = "import sys\ntry: open(sys.argv[1],'rb+').close()\nexcept PermissionError: sys.exit(17)\nsys.exit(0)"
    allowed = workspace / cases[case_id]["owned_paths"][0]
    if _sandbox_probe(policy, read, str(allowed)) != 0:
        raise FixtureError("sandbox cannot read the Forge executor workspace")
    write_stage = (
        "import os,pathlib,subprocess,sys,tempfile\n"
        "env=os.environ.copy()\n"
        "with tempfile.TemporaryDirectory(prefix='.forge-sandbox-write-',dir=sys.argv[1]) as repo:\n"
        "    pathlib.Path(repo,'probe.txt').write_text('probe\\n',encoding='utf-8')\n"
        "    subprocess.run(['/usr/bin/git','init','-q',repo],env=env,check=True,capture_output=True)\n"
        "    subprocess.run(['/usr/bin/git','-C',repo,'add','--','probe.txt'],"
        "env=env,check=True,capture_output=True)\n"
        "    subprocess.run(['/usr/bin/git','-C',repo,'ls-files','--error-unmatch','probe.txt'],"
        "env=env,check=True,capture_output=True)\n"
    )
    if _sandbox_probe(policy, write_stage, str(workspace)) != 0:
        raise FixtureError("sandbox cannot write and stage in the Forge executor workspace")
    if workspace.parent.name == "seats":
        for peer in ("claude", "codex", "agy"):
            if peer == workspace.name:
                continue
            sibling_source = workspace.parent / peer / cases[case_id]["owned_paths"][0]
            if sibling_source.is_file() and _sandbox_probe(
                policy, denied, str(sibling_source)
            ) != 17:
                raise FixtureError("sandbox did not deny Forge sibling seat read")
    if _sandbox_probe(policy, denied, str(hidden)) != 17:
        raise FixtureError("sandbox did not deny Forge private fixture read")
    if _sandbox_probe(policy, write, str(hidden)) != 17:
        raise FixtureError("sandbox did not deny Forge private fixture write")
    with tempfile.TemporaryDirectory(prefix="forge-sandbox-probe-") as temporary:
        alias = Path(temporary) / "alias.py"
        alias.symlink_to(hidden)
        if _sandbox_probe(policy, denied, str(alias)) != 17:
            raise FixtureError("sandbox did not deny Forge private symlink read")
        alias.unlink()
        link = ("import os,sys\ntry: os.link(sys.argv[1],sys.argv[2])\n"
                "except PermissionError: sys.exit(17)\nsys.exit(0)")
        if _sandbox_probe(policy, link, str(hidden), str(alias)) != 17 or alias.exists():
            raise FixtureError("sandbox did not deny Forge private hard-link creation")
    return policy


def executor_argv(case_id: str, workspace: Path, argv: list[str],
                  root: Path = ROOT, *,
                  boundary: ForgeBoundaryDescriptor | None = None) -> list[str]:
    """Return a sealed native wrapper for a live Forge executor seat."""
    if not isinstance(boundary, ForgeBoundaryDescriptor):
        raise FixtureError("sealed Forge boundary is required for a live executor wrapper")
    verify_pin(CASEBOOK, root)
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
        raise FixtureError("Forge executor argv must be a non-empty argument list")
    workspace = Path(workspace)
    try:
        relative = workspace.relative_to(boundary.run_root)
    except ValueError as error:
        raise FixtureError("Forge executor is outside its sealed campaign root") from error
    if len(relative.parts) < 2 or relative.parts[:2] != ("cases", case_id):
        raise FixtureError("Forge executor case differs from the sealed campaign seat")
    policy = _attest_native_boundary(boundary, workspace)
    return [str(_SANDBOX_EXEC), "-p", policy, *argv]


def _declared_check_executable(name: str, workspace: Path, path_value: str | None) -> str:
    if not path_value or "/" in name or "\\" in name:
        raise FixtureError("Forge declared check executable or PATH is unsafe")
    workspace = Path(workspace).resolve(strict=True)
    protected = [workspace]
    if (workspace.name == "workspace" and workspace.parent.name.startswith("forge-fresh-verifier-")
            and workspace.parent.parent.name == "verifiers"):
        protected.append(workspace.parents[2])
    for raw in path_value.split(os.pathsep):
        if not raw or not Path(raw).is_absolute():
            raise FixtureError("Forge declared check PATH has an empty or relative component")
        try:
            directory = Path(raw).resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise FixtureError("Forge declared check PATH cannot be resolved") from error
        if any(directory == root or root in directory.parents for root in protected):
            raise FixtureError("Forge declared check PATH includes a candidate-owned directory")
    found = shutil.which(name, path=path_value)
    if found is None:
        raise FixtureError("Forge declared check executable is unavailable")
    try:
        executable = Path(found).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise FixtureError("Forge declared check executable cannot be resolved") from error
    if (not executable.is_file() or not os.access(executable, os.X_OK)
            or any(executable == root or root in executable.parents for root in protected)):
        raise FixtureError("Forge declared check executable is candidate-owned or unsafe")
    return str(executable)


def run_declared_check(case_id: str, workspace: Path,
                       root: Path = ROOT, *,
                       boundary: ForgeBoundaryDescriptor | None = None) -> subprocess.CompletedProcess[str]:
    """Run the public check supplied to every seat by the fixture bundle."""
    workspace = Path(workspace)
    if boundary is None and _is_campaign_verifier(workspace):
        raise FixtureError("sealed Forge verifier boundary is required")
    verify_pin(CASEBOOK, root)
    cases = {case["id"]: case for case in _manifest(Path(root))}
    if case_id not in cases:
        raise FixtureError(f"unknown Forge fixture {case_id}")
    check = cases[case_id]["check"]
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
    executable = _declared_check_executable(check["argv"][0], workspace, env.get("PATH"))
    argv = [executable, *check["argv"][1:]]
    if boundary is not None:
        policy, state = _verifier_policy(boundary, workspace)
        if any(Path(executable) == root or root in Path(executable).parents
               for root in boundary.protected_roots):
            raise FixtureError("Forge declared check executable is inside a protected root")
        argv = [str(_SANDBOX_EXEC), "-p", policy, *argv]
        env.update(HOME=str(state), XDG_CONFIG_HOME=str(state / ".config"),
                   TMPDIR=str(state / "tmp"))
    return subprocess.run(argv, cwd=workspace,
                          capture_output=True, text=True,
                          timeout=check["timeout"], env=env)


def ingress_packet(case_id: str, root: Path = ROOT) -> dict[str, Any]:
    """Return a Forge-style bundle; owner review must still arrive out of band."""
    verify_pin(CASEBOOK, root)
    cases = {case["id"]: case for case in _manifest(Path(root))}
    if case_id not in {"forge-09", "forge-10"} or case_id not in cases:
        raise FixtureError("Forge fixture has no ingress bundle")
    case = cases[case_id]
    task_text = (Path(root) / "public" / case_id / "TASK.md").read_text(encoding="utf-8")
    source = "\n".join([
        f"# {case_id} Forge Ingress Implementation Plan", "",
        "**Goal:** Complete the declared synthetic change.", "",
        "**Architecture:** One owned repository task.", "",
        "**Tech Stack:** Python 3.11+ standard library.", "",
        f"**Spec:** `forge-fixtures/{case_id}/TASK.md`", "",
        "## Global Constraints", "", "- Preserve owned paths and declared checks.", "",
        "### Task 1: Update source", "", "**Files:**",
        *[f"- Modify: `{path}`" for path in case["owned_paths"]], "",
        "- [ ] **Step 1: Implement and verify**", "  **Depends on:** none", "",
        task_text, "",
    ])
    check = case["check"]
    declared = {
        "argv": check["argv"], "cwd": "", "env_allowlist": [],
        "timeout": check["timeout"], "accepted_exit_codes": [0],
        "expected_artifacts": [],
    }
    draft = {
        "schema_version": "fanout-draft-v1",
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "defaults": {"executor_ids": ["claude", "codex", "agy"], "rounds": 2,
                     "timeout": 120, "retries": 0, "minimum_success": 2},
        "tasks": [{
            "id": "change", "kind": "work", "parent_id": None,
            "title": "Update source", "objective": task_text.strip(),
            "source_step_ids": ["Task 1/Step 1"], "depends_on": [],
            "execution_class": "repo-write", "required_skills": ["khenrix-quality"],
            "none_reason": None, "owned_paths": case["owned_paths"],
            "acceptance": ["The declared and held-out checks pass on the synthesized candidate."],
            "checks": [declared], "provider_policy": None,
        }],
    }
    return {
        "schema_version": "fanout-bundle-ingress-v1", "quality_tier": "normal",
        "source_path": f"forge-fixtures/{case_id}/TASK.md",
        "source_markdown": source, "draft": draft,
    }


def run_held_out(case_id: str, workspace: Path, root: Path = ROOT, *,
                 boundary: ForgeBoundaryDescriptor | None = None) -> subprocess.CompletedProcess[str]:
    """Run a controller oracle against a fresh verifier copy, not the caller seat."""
    workspace = Path(workspace)
    if boundary is None and _is_campaign_verifier(workspace):
        raise FixtureError("sealed Forge verifier boundary is required")
    verify_pin(CASEBOOK, root)
    if case_id not in {case["id"] for case in _manifest(Path(root))}:
        raise FixtureError(f"unknown Forge fixture {case_id}")
    oracle = Path(root) / "held-out" / f"{case_id}.py"
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
    if boundary is not None:
        _require_unprotected_verifier_python(boundary)
        _verifier_policy(boundary, workspace)
        env[_VERIFIER_BOUNDARY_ENV] = json.dumps([
            str(boundary.run_root), str(boundary.baseline_root),
            str(boundary.consumer_root), boundary.seal,
        ], separators=(",", ":"))
        return subprocess.run([sys.executable, str(oracle), str(workspace)],
                              capture_output=True, text=True, timeout=60, env=env)
    with tempfile.TemporaryDirectory(prefix="forge-fresh-verifier-") as temporary:
        verifier = Path(temporary) / "verifier"
        shutil.copytree(workspace, verifier, symlinks=True)
        return subprocess.run([sys.executable, str(oracle), str(verifier)],
                              capture_output=True, text=True, timeout=60, env=env)


def probe_observation(case_id: str, workspace: Path, root: Path = ROOT) -> dict[str, Any]:
    """Measure candidate behavior in a sandboxed child; oracle assertions stay outside."""
    root = Path(root)
    workspace = Path(workspace)
    boundary = _verifier_boundary_from_environment()
    if boundary is None and _is_campaign_verifier(workspace):
        raise FixtureError("sealed Forge verifier boundary is required")
    verify_pin(CASEBOOK, root)
    if boundary is None:
        policy = _validate_boundary(case_id, workspace, root)
        state = None
    else:
        _require_unprotected_verifier_python(boundary)
        policy, state = _verifier_policy(boundary, workspace)
    code = (root / "held-out" / "probe.py").read_text(encoding="utf-8")
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_TEMPLATE_DIR=os.devnull)
    if state is not None:
        env.update(HOME=str(state), XDG_CONFIG_HOME=str(state / ".config"),
                   TMPDIR=str(state / "tmp"))
    run = subprocess.run(
        [str(_SANDBOX_EXEC), "-p", policy, sys.executable, "-", case_id,
         str(workspace.resolve())],
        input=code, capture_output=True, text=True, timeout=45, env=env,
        cwd=workspace,
    )
    if run.returncode != 0:
        raise FixtureError(f"sandboxed candidate probe failed: {run.stderr[-2000:]}")
    try:
        value = json.loads(run.stdout)
    except json.JSONDecodeError as error:
        raise FixtureError("sandboxed candidate probe returned invalid JSON") from error
    if not isinstance(value, dict):
        raise FixtureError("sandboxed candidate probe returned a non-object")
    return value
