"""Disposable macOS file-boundary oracle; it does not certify model access."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.lib import fanout  # noqa: E402


_PROBE = 'if [ "$1" = read ]; then IFS= read -r x < "$2"; else printf probe > "$2"; fi'


def _command(workspace: Path, operation: str, path: Path) -> fanout.ProcessCommand:
    return fanout.ProcessCommand(
        argv=("/bin/sh", "-c", _PROBE, "sh", operation, str(path)),
        stdin=b"", cwd=workspace, timeout=10,
    )


def _fixture(root: Path):
    repository = root / "repository"
    repository.mkdir(mode=0o700)
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    (repository / "fixture.txt").write_text("disposable fixture\n")
    subprocess.run(("git", "-C", str(repository), "add", "-A"), check=True)
    subprocess.run((
        "git", "-C", str(repository), "-c", "user.name=Native Canary",
        "-c", "user.email=canary@example.invalid", "commit", "-qm", "fixture",
    ), check=True)
    baseline = fanout.capture_repository_baseline(repository)
    controller = fanout.create_lifecycle_controller(root / "controller")
    workspace = fanout.create_seat_workspace(
        baseline, controller.root / "workspaces", "diagnostic-seat",
    )
    verification = fanout.verify_seat_workspace(
        baseline, workspace, controller=controller,
    )
    other = root / "other-target"
    other.mkdir(mode=0o700)
    (other / "secret.txt").write_text("other target sentinel\n")
    owner = root / "owner"
    owner.mkdir(mode=0o700)
    (owner / "run.json").write_text("controller sentinel\n")
    adapter = fanout.CodexAdapter()
    profile = fanout.ExecutorProfile(
        "codex", adapter, adapter.capabilities, "repo-write", "standard",
        "diagnostic-only", "low", "diagnostic-only", ("/bin/sh",),
        ("/bin/sh", "--resume", "{session_id}"), 10, 10,
    )
    request = fanout.ProviderRequest(
        "codex", "diagnostic only", cwd=workspace.root,
        execution_class="repo-write", profile=profile,
        run_id="native-canary", task_id="diagnostic",
        target_id="disposable-target", seat_id="diagnostic-seat",
        inputs_digest="a" * 64,
        artifact_store=fanout.ArtifactStore(controller.root / "artifacts"),
        artifact_prefix="native-canary",
    )
    command = _command(workspace.root, "write", workspace.root / "allowed.txt")
    boundary = fanout.issue_native_boundary(
        controller, verification, run_id=request.run_id, task_id=request.task_id,
        target_id=request.target_id, inputs_digest=request.inputs_digest,
        request=request, command=command, other_target_roots=(other,),
        denied_owner_roots=(repository, owner),
    )
    return controller, verification, request, boundary, other, owner


def _run(root: Path) -> dict[str, object]:
    controller, verification, request, boundary, other, owner = _fixture(root)
    oracles = {
        "other": other / "secret.txt", "owner": owner / "run.json",
        "controller": controller.root / "controller.json",
        "profile": boundary.profile_path,
    }
    allowed = request.cwd / "allowed.txt"
    receipt = fanout.run_native_boundary_probe(
        boundary, request, _command(request.cwd, "write", allowed),
        controller=controller, verification=verification,
        oracle_paths=oracles,
    )
    if (receipt.exit_code != 0 or not receipt.allowed_workspace_write
            or receipt.denied_paths != tuple(str(path) for path in oracles.values())
            or allowed.read_text() != "probe"):
        raise RuntimeError("native seat workspace write failed")
    return {
        "native_file_boundary": "passed",
        "denied_operations": len(receipt.denied_paths) * 2,
        "workspace_write": "passed",
        "credential_blind_model_route": "unproven",
        "provider_production_launch": "closed",
        "profile_sha256": receipt.profile_sha256,
    }


def main() -> int:
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        print("macOS sandbox-exec is required", file=sys.stderr)
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="fanout-native-canary-", dir="/private/tmp") as raw:
            print(json.dumps(_run(Path(raw)), sort_keys=True))
    except Exception as error:
        print(f"native canary failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
