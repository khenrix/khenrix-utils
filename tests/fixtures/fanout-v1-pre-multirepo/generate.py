"""Freeze disposable v1 runtime bytes before the multi-repository changes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(os.environ.get("FANOUT_FIXTURE_SOURCE_ROOT", Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(ROOT / "tests"))
from test_fanout_execute_skill import (  # noqa: E402
    _NeverMemory, _question_packet, _registry, _repository, fanout,
)


def main() -> None:
    source = ROOT / "shared/skills/llm-fanout-execute/scripts/execute.py"
    spec = importlib.util.spec_from_file_location("frozen_v1_execute", source)
    assert spec is not None and spec.loader is not None
    execute = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = execute
    spec.loader.exec_module(execute)
    execute.secrets.token_urlsafe = lambda _length: "test-only-owner-material-00000000"
    destination = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="fanout-v1-freeze-", dir="/private/tmp") as temporary:
        root = Path(temporary)
        packet, _ = _question_packet(root, executors=("claude", "codex"))
        repo = _repository(root)
        private, run, authority = (root / name for name in ("private", "run", "authority"))
        private.mkdir(mode=0o700)
        execute.prepare_run(
            packet, repo_root=repo, run_root=run, authority_root=authority,
            private_root=private, skill_roots=(root / "skills",), budget=12,
            runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
            memory_preflight=lambda: True,
            provider_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("frozen fixture must not dispatch")),
        )
        descriptor = execute.load_private_descriptor(private)
        owner = fanout.OwnerCapability.from_token(descriptor["owner_token"])
        anchor = fanout.LocalAnchorAuthority(
            authority, run_root=run, repo_root=repo,
        )
        inputs = fanout.RunInputs.from_dict(descriptor["inputs"])
        journal = fanout.RunJournal.resume(run, inputs, owner, anchor_store=anchor)
        try:
            for phase in ("reconciliation-pending", "reconciliation-verifying", "completed"):
                journal.append(phase, task_id="answer", owner=owner)
            amendment = dict(
                revision=2, old_plan_sha256=inputs.compiled_plan_sha256,
                old_inputs_digest=inputs.digest, new_plan_sha256="a" * 64,
                new_inputs_digest="b" * 64, old_profiles_sha256="c" * 64,
                new_profiles_sha256="d" * 64, owner=owner,
            )
            journal.append_amendment("plan-amendment-intent", **amendment)
            journal.append_amendment("plan-amendment-accepted", **amendment)
            handover = dict(
                task_id="answer", transaction_root=str(root / "handover"),
                transaction_sha256="e" * 64, owner=owner,
            )
            journal.append("handover-intent", **handover)
            journal.append("handover-complete", disposition_sha256="f" * 64, **handover)
            journal.snapshot(owner)
        finally:
            journal.close()
        sources = {
            "descriptor.json": private / "run.json",
            "inputs.json": run / "inputs.json",
            "owner.json": run / "owner.json",
            "events.jsonl": run / "events.jsonl",
            "snapshot.json": run / "snapshot.json",
            "scheduler-meta.json": run / ".fanout-scheduler.meta.json",
            "scheduler-record.json": run / ".fanout-scheduler.record.json",
        }
        for name, path in sources.items():
            shutil.copyfile(path, destination / name)
        manifest = {
            "schema_version": "fanout-v1-frozen-fixture-v1",
            "runtime_sha256": execute._runtime_digest(fanout),
            "compiler_sha256": hashlib.sha256(
                (ROOT / "shared/lib/fanout/compiler.py").read_bytes()).hexdigest(),
            "files": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                      for name, path in sources.items()},
        }
        (destination / "manifest.json").write_bytes(fanout.canonical_json(manifest))


if __name__ == "__main__":
    main()
