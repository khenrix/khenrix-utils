"""Live Forge certification must fail closed before spending or publishing."""
from __future__ import annotations

import hashlib
import importlib
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
campaign = importlib.import_module("scripts.fanout_forge_campaign")


def _test_boundary(run: Path, baseline: Path, consumer: Path):
    return campaign.fixtures.ForgeBoundaryDescriptor(
        run, baseline, consumer, (),
        (("claude", Path("/usr/bin/true")),
         ("codex", Path("/usr/bin/true")),
         ("agy", Path("/usr/bin/true"))),
        "a" * 64, "b" * 64,
    )


@pytest.fixture
def _allow_hermetic_forge_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only synthetic test paths bypass the unavailable production attestation.
    monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(campaign, "_require_blindness_attestation", lambda **_kwargs: None)
    monkeypatch.setattr(campaign.fixtures, "require_blindness_attestation",
                        lambda **_kwargs: None)
    monkeypatch.setattr(campaign.fixtures, "seat_environment",
                        lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        campaign.live, "_sealed_version_probe",
        lambda _boundary, executor: {
            "claude": "2.1.284", "codex": "0.159.0", "agy": "1.2.13",
        }[executor], raising=False,
    )

    def open_boundary(run, baseline, consumer, *, resume):
        if not resume:
            Path(run).mkdir(mode=0o700, parents=True)
        return _test_boundary(Path(run), Path(baseline), Path(consumer))

    monkeypatch.setattr(campaign.fixtures, "open_blindness_boundary", open_boundary)


@pytest.mark.usefixtures("_allow_hermetic_forge_turns")
def test_missing_independent_baselines_block_before_live_provider_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = []
    monkeypatch.setattr(campaign.fanout, "run_provider", launched.append)

    with pytest.raises(campaign.CampaignError, match="baseline"):
        campaign.run_live_campaign(
            tmp_path / "run", tmp_path / "missing-baselines",
            consumer_root=tmp_path / "consumer",
        )

    assert launched == []
    assert not (tmp_path / "run").exists()


def test_campaign_requires_measured_blindness_before_creating_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        campaign, "_baseline_inputs",
        lambda _root, case_ids: {case_id: {"candidate_sha256": "a" * 64}
                                 for case_id in case_ids},
    )
    measured = []
    monkeypatch.setattr(
        campaign, "measure_case",
        lambda case_id, *_args, **_kwargs: (
            measured.append(case_id) or {"case_id": case_id, "installed_consumer": False}
        ),
    )
    root = tmp_path / "campaign"

    def refuse_boundary(run, baseline, consumer, *, resume):
        assert (run, baseline, consumer, resume) == (
            root, tmp_path / "independent-baselines",
            tmp_path / "installed-consumer", False,
        )
        raise campaign.fixtures.FixtureError("blindness attestation unavailable")

    monkeypatch.setattr(campaign.fixtures, "open_blindness_boundary", refuse_boundary)

    with pytest.raises(
        campaign.CampaignError,
        match="blindness attestation unavailable",
    ):
        campaign.run_live_campaign(
            root, tmp_path / "independent-baselines",
            consumer_root=tmp_path / "installed-consumer",
        )

    assert measured == []
    assert not root.exists()


def test_campaign_requires_installed_consumer_root_before_creating_run(
    tmp_path: Path,
) -> None:
    run = tmp_path / "campaign"
    with pytest.raises(campaign.CampaignError, match="installed consumer root is required"):
        campaign.run_live_campaign(run, tmp_path / "baselines")
    assert not run.exists()


def test_campaign_rejects_unsealed_version_probe_before_run_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "campaign"
    callbacks = []
    providers = []
    monkeypatch.setattr(campaign.fanout, "run_provider", providers.append)

    with pytest.raises(campaign.CampaignError, match="version probe.*unsupported"):
        campaign.run_live_campaign(
            run, tmp_path / "baselines", consumer_root=tmp_path / "consumer",
            version_probe=lambda executor: callbacks.append(executor) or "2.1.281",
        )

    assert callbacks == []
    assert providers == []
    assert not run.exists()


def test_campaign_passes_unresolved_roots_to_boundary_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "real-baseline"
    baseline.mkdir()
    alias = tmp_path / "baseline-alias"
    alias.symlink_to(baseline, target_is_directory=True)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    run = tmp_path / "campaign"
    monkeypatch.setattr(
        campaign, "_baseline_inputs",
        lambda _root, case_ids: {case_id: {"candidate_sha256": "a" * 64}
                                 for case_id in case_ids},
    )

    def reject_alias(run_root, baseline_root, consumer_root, *, resume):
        assert (run_root, baseline_root, consumer_root, resume) == (
            run, alias, consumer, False,
        )
        raise campaign.fixtures.FixtureError("Forge baseline root is a symlink")

    monkeypatch.setattr(campaign.fixtures, "open_blindness_boundary", reject_alias)
    with pytest.raises(campaign.CampaignError, match="symlink"):
        campaign.run_live_campaign(run, alias, consumer_root=consumer)
    assert not run.exists()


def test_turn_requires_measured_blindness_before_provider_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seat = tmp_path / "seat"
    seat.mkdir()
    (tmp_path / "baseline").mkdir()
    (tmp_path / "consumer").mkdir()
    turn_root = tmp_path / "turn"
    launched = []

    def fake_provider(request, *, registry):
        launched.append((request, registry))
        return campaign.fanout.ProviderResult(
            "claude", True, "ok", None, 1, 0.1, None,
            "11111111-1111-4111-8111-111111111111", "answer", b"", b"",
            answer_digest=hashlib.sha256(b"answer").hexdigest(),
        )

    monkeypatch.setattr(campaign.fanout, "run_provider", fake_provider)
    with pytest.raises((campaign.CampaignError, campaign.fixtures.FixtureError),
                       match="boundary|blindness"):
        campaign._turn(
            "forge-01", "claude", seat, "private task", turn_root,
            boundary=_test_boundary(tmp_path, tmp_path / "baseline", tmp_path / "consumer"),
        )

    assert launched == []
    assert not turn_root.exists()


def test_receipt_is_hash_bound_and_excludes_raw_provider_text(tmp_path: Path) -> None:
    receipt = campaign.write_receipt(
        tmp_path / "receipt.json",
        casebook_sha256="a" * 64,
        fixture_manifest_sha256="b" * 64,
        rows=[{"case_id": "forge-01", "candidate_final_artifact_sha256": "c" * 64}],
        blockers=["missing-installed-consumer-handoff"],
    )

    raw = (tmp_path / "receipt.json").read_bytes()
    assert b"private provider answer" not in raw
    assert receipt["status"] == "blocked"
    assert receipt["receipt_sha256"] == hashlib.sha256(
        campaign.fanout.canonical_json({k: v for k, v in receipt.items() if k != "receipt_sha256"})
    ).hexdigest()
    assert campaign.verify_receipt(tmp_path / "receipt.json") == receipt

    changed = json.loads(raw)
    changed["cases"][0]["candidate_final_artifact_sha256"] = "d" * 64
    (tmp_path / "receipt.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(campaign.CampaignError, match="digest"):
        campaign.verify_receipt(tmp_path / "receipt.json")


def test_receipt_refuses_raw_answers_and_never_marks_partial_rows_green(tmp_path: Path) -> None:
    with pytest.raises(campaign.CampaignError, match="unexpected|raw"):
        campaign.write_receipt(
            tmp_path / "leaked.json", casebook_sha256="a" * 64,
            fixture_manifest_sha256="b" * 64,
            rows=[{"case_id": "forge-01", "answer": "private provider answer"}],
            blockers=[],
        )
    assert not (tmp_path / "leaked.json").exists()

    receipt = campaign.write_receipt(
        tmp_path / "partial.json", casebook_sha256="a" * 64,
        fixture_manifest_sha256="b" * 64,
        rows=[{"case_id": "forge-01", "candidate_final_artifact_sha256": "c" * 64}],
        blockers=[],
    )
    assert receipt["status"] == "blocked"


@pytest.mark.parametrize(("field", "value"), [
    ("source_contributions", ["private provider answer"]),
    ("owned_paths", ["private provider answer"]),
    ("declared_checks", [{"id": "private provider answer", "spec_sha256": "c" * 64}]),
    ("candidate_delivered", "private provider answer"),
])
def test_receipt_rejects_raw_nested_row_values(
    tmp_path: Path, field: str, value: object,
) -> None:
    path = tmp_path / "leaked.json"
    with pytest.raises(campaign.CampaignError, match="unexpected|raw"):
        campaign.write_receipt(
            path, casebook_sha256="a" * 64,
            fixture_manifest_sha256="b" * 64,
            rows=[{"case_id": "forge-01", field: value}], blockers=["partial-evidence"],
        )
    assert not path.exists()


def test_self_hashed_receipt_rejects_raw_nested_row_values(tmp_path: Path) -> None:
    path = tmp_path / "forged.json"
    forged = {
        "schema_version": "fanout-forge-campaign-v1", "status": "blocked",
        "casebook_sha256": "a" * 64, "fixture_manifest_sha256": "b" * 64,
        "cases": [{"case_id": "forge-01", "source_contributions": ["private provider answer"]}],
        "blockers": ["partial-evidence"],
    }
    forged["receipt_sha256"] = campaign._digest(forged)
    path.write_text(json.dumps(forged), encoding="utf-8")

    with pytest.raises(campaign.CampaignError, match="invalid shape"):
        campaign.verify_receipt(path)


def test_complete_synthetic_rows_cannot_become_measured_evidence(tmp_path: Path) -> None:
    row = {key: "a" * 64 if key.endswith("_sha256") else True
           for key in campaign._ROW_FIELDS}
    row.update(source_contributions=["b" * 64, "c" * 64])
    descriptors = campaign.fixtures.verify_pin(campaign.CASEBOOK)
    rows = [{**row, "case_id": case_id,
             "owned_paths": descriptor["owned_paths"],
             "declared_checks": descriptor["declared_checks"]}
            for case_id, descriptor in sorted(descriptors.items())]

    receipt = campaign.write_receipt(
        tmp_path / "receipt.json", casebook_sha256="e" * 64,
        fixture_manifest_sha256="f" * 64, rows=rows, blockers=[],
    )

    assert receipt["status"] == "blocked"
    assert receipt["blockers"]


def test_self_hashed_measured_receipt_without_run_provenance_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forged.json"
    forged = {
        "schema_version": "fanout-forge-campaign-v1", "status": "measured",
        "casebook_sha256": "a" * 64, "fixture_manifest_sha256": "b" * 64,
        "cases": [], "blockers": [],
    }
    forged["receipt_sha256"] = campaign._digest(forged)
    path.write_text(json.dumps(forged), encoding="utf-8")

    with pytest.raises(campaign.CampaignError, match="unauthenticated|measured|provenance"):
        campaign.verify_receipt(path)


def test_workspace_digest_binds_binary_symlink_and_refuses_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "source.py").write_text("one\n", encoding="utf-8")
    (workspace / "binary.bin").write_bytes(b"\x00\xff")
    (workspace / "current.bin").symlink_to("binary.bin")
    first = campaign.workspace_digest(workspace)

    (workspace / "binary.bin").write_bytes(b"\x00\xfe")
    assert campaign.workspace_digest(workspace) != first
    (workspace / "current.bin").unlink()
    (workspace / "current.bin").symlink_to("../outside")
    with pytest.raises(campaign.CampaignError, match="symlink"):
        campaign.workspace_digest(workspace)


def test_workspace_digest_binds_staged_index_without_file_change(tmp_path: Path) -> None:
    workspace = tmp_path / "forge-10"
    campaign.fixtures.materialize("forge-10", workspace)
    before = campaign.workspace_digest(workspace)

    subprocess.run(["git", "-C", str(workspace), "add", "notes/local.txt"], check=True)

    assert campaign.workspace_digest(workspace) != before


def test_workspace_digest_never_executes_candidate_git_hooks(tmp_path: Path) -> None:
    workspace = tmp_path / "forge-10"
    campaign.fixtures.materialize("forge-10", workspace)
    marker = tmp_path / "controller-hook-ran"
    hook = workspace / ".git" / "fsmonitor"
    hook.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\n", encoding="utf-8")
    hook.chmod(0o700)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "core.fsmonitor", str(hook)],
        check=True,
    )

    assert campaign.workspace_digest(workspace)
    assert not marker.exists(), "controller executed candidate-controlled Git config"


@pytest.mark.parametrize("metadata", ("config", "auxiliary_ref", "object"))
def test_workspace_digest_binds_all_git_control_bytes(
    tmp_path: Path, metadata: str,
) -> None:
    workspace = tmp_path / "forge-10"
    campaign.fixtures.materialize("forge-10", workspace)
    before = campaign.workspace_digest(workspace)
    git = workspace / ".git"
    if metadata == "config":
        path = git / "config"
        path.write_bytes(path.read_bytes() + b"\n[forge]\n\tchanged = true\n")
    elif metadata == "auxiliary_ref":
        path = git / "refs" / "tags" / "unrelated"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a" * 40 + "\n", encoding="ascii")
    else:
        head = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        path = git / "objects" / head[:2] / head[2:]
        path.unlink()
        path.write_bytes(b"replaced object bytes")

    assert campaign.workspace_digest(workspace) != before


@pytest.mark.usefixtures("_allow_hermetic_forge_turns")
def test_each_case_dispatches_three_blind_turns_and_exact_resumes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = campaign.quality.load_casebook(campaign.CASEBOOK)
    wrapped = []
    turns = []

    def wrapper(case_id, workspace, argv, *, boundary):
        wrapped.append((case_id, workspace, tuple(argv)))
        return ["/usr/bin/sandbox-exec", "-p", "checked-policy", *argv]

    def fake_provider(request, *, registry):
        command = registry.require(request.executor_id).build_command(request)
        assert command.argv[:3] == ("/usr/bin/sandbox-exec", "-p", "checked-policy")
        turns.append((request.executor_id, request.resume, request.session_id, request.prompt))
        source = request.cwd / "src" / "transfer.py"
        if request.prompt.startswith("Synthesize"):
            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    "with ledger_lock:\n        with account_lock:",
                    "with account_lock:\n        with ledger_lock:",
                ) + "\n    return True\n", encoding="utf-8",
            )
            answer = json.dumps({"used_sources": ["claude", "codex", "agy"],
                                 "notes": "Unified lock order and retained two peer findings."})
        elif request.prompt.startswith("Review fused"):
            answer = '{"blockers":[]}'
        else:
            text = source.read_text(encoding="utf-8")
            if request.resume and request.executor_id == "claude":
                text = text.replace(
                    "with ledger_lock:\n        with account_lock:",
                    "with account_lock:\n        with ledger_lock:",
                )
            if request.resume and request.executor_id == "codex":
                text += "\n    return True\n"
            source.write_text(
                text + f"\n# {request.executor_id} round {1 + int(request.resume)}\n",
                encoding="utf-8",
            )
            answer = f"private {request.executor_id} answer {1 + int(request.resume)}"
        session = request.session_id or (
            "11111111-1111-4111-8111-111111111111" if request.executor_id == "claude"
            else f"{request.executor_id}-exact-session"
        )
        return campaign.fanout.ProviderResult(
            request.executor_id, True, "ok", None, 1, 0.1, None, session,
            answer, b"", b"", answer_digest=hashlib.sha256(answer.encode()).hexdigest(),
        )

    monkeypatch.setattr(campaign.fixtures, "executor_argv", wrapper)
    monkeypatch.setattr(campaign.fanout, "run_provider", fake_provider)
    case_root = tmp_path / "forge-01"
    first = campaign.dispatch_case(
        "forge-01", case_root, book,
        boundary=_test_boundary(tmp_path, tmp_path / "baseline", tmp_path / "consumer"),
    )

    assert len(turns) == len(wrapped) == 6
    assert all(not resume and "private" not in prompt for _, resume, _, prompt in turns[:3])
    assert all(resume and "private" in prompt for _, resume, _, prompt in turns[3:])
    assert [item[2] for item in turns[3:]] == [
        "11111111-1111-4111-8111-111111111111", "codex-exact-session",
        "agy-exact-session",
    ]
    assert len(first) == 3
    assert all(item[0] == "forge-01" for item in wrapped)

    again = campaign.dispatch_case(
        "forge-01", case_root, book,
        boundary=_test_boundary(tmp_path, tmp_path / "baseline", tmp_path / "consumer"),
    )
    assert again == first
    assert len(turns) == len(wrapped) == 6

    fused = campaign.fuse_case(
        "forge-01", case_root, book, first,
        boundary=_test_boundary(tmp_path, tmp_path / "baseline", tmp_path / "consumer"),
    )
    assert len(turns) == len(wrapped) == 8
    assert fused["candidate_sha256"] not in first.values()
    assert fused["source_contributions"] == [first["claude"], first["codex"]]
    assert fused["review_sha256"] == hashlib.sha256(b'{"blockers":[]}').hexdigest()
    assert (case_root / "caller" / "src" / "transfer.py").read_text(
        encoding="utf-8"
    ).count("with ledger_lock:\n        with account_lock:") == 1


def test_fusion_rejects_a_claimed_source_absent_from_delivered_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = campaign.quality.load_casebook(campaign.CASEBOOK)
    root = tmp_path / "case"
    caller = root / "caller"
    campaign.fixtures.materialize("forge-01", caller)
    campaign._write_new(root / "baseline.json", {
        "workspace_sha256": campaign.workspace_digest(caller),
    })
    original = (caller / "src" / "transfer.py").read_text(encoding="utf-8")
    sources = {}
    for executor, change in (
        ("claude", "\nCLAUDE_CHANGE = 1\n"),
        ("codex", "\nCODEX_CHANGE = 2\n"),
        ("agy", "\n# comment only\n"),
    ):
        seat = root / "seats" / executor
        shutil.copytree(caller, seat)
        (seat / "src" / "transfer.py").write_text(original + change, encoding="utf-8")
        sources[executor] = campaign.workspace_digest(seat)

    def synthesize(_case_id, _executor, workspace, prompt, _turn_root, **_kwargs):
        if prompt.startswith("Review fused"):
            answer = '{"blockers":[]}'
        else:
            (workspace / "src" / "transfer.py").write_text(
                original + "\nCLAUDE_CHANGE = 1\nFUSED_CHANGE = 3\n", encoding="utf-8",
            )
            answer = json.dumps({"used_sources": ["claude", "codex"], "notes": "Used both."})
        return {"answer": answer, "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                "workspace_sha256": campaign.workspace_digest(workspace)}

    monkeypatch.setattr(campaign, "_turn", synthesize)
    with pytest.raises(campaign.CampaignError, match="contribution"):
        campaign.fuse_case(
            "forge-01", root, book, sources,
            boundary=_test_boundary(tmp_path, tmp_path / "baseline", tmp_path / "consumer"),
        )


def test_uncertain_turn_intent_blocks_replay_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seat = tmp_path / "seat"
    seat.mkdir()
    turn_root = tmp_path / "turn"
    turn_root.mkdir()
    (turn_root / "intent.json").write_text("{}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(campaign.fanout, "run_provider", calls.append)

    with pytest.raises(campaign.CampaignError, match="uncertain provider spend"):
        campaign._turn(
            "forge-01", "claude", seat, "task", turn_root,
            boundary=_test_boundary(tmp_path, tmp_path / "baseline", tmp_path / "consumer"),
        )

    assert calls == []


def test_settled_turn_rechecks_native_blindness_before_reusing_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seat = tmp_path / "seat"
    seat.mkdir()
    turn = tmp_path / "turn"
    boundary = _test_boundary(tmp_path, tmp_path / "baseline", tmp_path / "consumer")
    profile = campaign.live.ForgeLiveRegistry("forge-01", seat, boundary=boundary).select(
        "claude", "repo-write", "standard",
    )
    session_id = "11111111-1111-4111-8111-111111111111"
    identity = {
        "case_id": "forge-01", "executor_id": "claude", "round": 2,
        "prompt_sha256": hashlib.sha256(b"task").hexdigest(),
        "profile_sha256": profile.digest, "session_id": session_id,
        "execution_class": "repo-write",
    }
    campaign._write_new(turn / "intent.json", identity)
    campaign._write_new(turn / "terminal.json", {
        "identity_sha256": campaign._digest(identity), "valid": True,
        "session_id": session_id, "answer": "answer",
        "answer_sha256": hashlib.sha256(b"answer").hexdigest(),
        "workspace_sha256": campaign.workspace_digest(seat),
    })
    providers = []
    monkeypatch.setattr(campaign.fanout, "run_provider", providers.append)

    def refuse_boundary(**_kwargs):
        raise campaign.CampaignError("native blindness no longer attested")

    monkeypatch.setattr(campaign, "_require_blindness_attestation", refuse_boundary)
    with pytest.raises(campaign.CampaignError, match="native blindness"):
        campaign._turn(
            "forge-01", "claude", seat, "task", turn,
            boundary=boundary, session_id=session_id,
        )
    assert providers == []


def test_settled_turn_rejects_workspace_changed_after_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seat = tmp_path / "seat"
    seat.mkdir()
    draft = seat / "draft.txt"
    draft.write_text("before", encoding="utf-8")
    turn = tmp_path / "turn"
    boundary = _test_boundary(tmp_path, tmp_path / "baseline", tmp_path / "consumer")
    profile = campaign.live.ForgeLiveRegistry("forge-01", seat, boundary=boundary).select(
        "claude", "repo-write", "standard",
    )
    session_id = "11111111-1111-4111-8111-111111111111"
    identity = {
        "case_id": "forge-01", "executor_id": "claude", "round": 2,
        "prompt_sha256": hashlib.sha256(b"task").hexdigest(),
        "profile_sha256": profile.digest, "session_id": session_id,
        "execution_class": "repo-write",
    }
    campaign._write_new(turn / "intent.json", identity)
    campaign._write_new(turn / "terminal.json", {
        "identity_sha256": campaign._digest(identity), "valid": True,
        "session_id": session_id, "answer": "answer",
        "answer_sha256": hashlib.sha256(b"answer").hexdigest(),
        "workspace_sha256": campaign.workspace_digest(seat),
    })
    draft.write_text("after", encoding="utf-8")
    monkeypatch.setattr(campaign, "_require_blindness_attestation", lambda **_kwargs: None)
    providers = []
    monkeypatch.setattr(campaign.fanout, "run_provider", providers.append)

    with pytest.raises(campaign.CampaignError, match="settled.*workspace changed"):
        campaign._turn(
            "forge-01", "claude", seat, "task", turn,
            boundary=boundary, session_id=session_id,
        )
    assert providers == []


def test_settled_resume_rejects_a_changed_session_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign, "_require_blindness_attestation", lambda **_kwargs: None)
    seat = tmp_path / "seat"
    seat.mkdir()
    turn = tmp_path / "turn"
    boundary = _test_boundary(tmp_path, tmp_path / "baseline", tmp_path / "consumer")
    profile = campaign.live.ForgeLiveRegistry("forge-01", seat, boundary=boundary).select(
        "claude", "repo-write", "standard",
    )
    identity = {
        "case_id": "forge-01", "executor_id": "claude", "round": 2,
        "prompt_sha256": hashlib.sha256(b"task").hexdigest(),
        "profile_sha256": profile.digest,
        "session_id": "11111111-1111-4111-8111-111111111111",
        "execution_class": "repo-write",
    }
    answer = "answer"
    campaign._write_new(turn / "intent.json", identity)
    campaign._write_new(turn / "terminal.json", {
        "identity_sha256": campaign._digest(identity), "valid": True,
        "session_id": "22222222-2222-4222-8222-222222222222",
        "answer": answer, "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
        "workspace_sha256": campaign.workspace_digest(seat),
    })

    with pytest.raises(campaign.CampaignError, match="session"):
        campaign._turn(
            "forge-01", "claude", seat, "task", turn,
            boundary=boundary,
            session_id="11111111-1111-4111-8111-111111111111",
        )


@pytest.mark.usefixtures("_allow_hermetic_forge_turns")
def test_hand_authored_baseline_binding_cannot_authenticate_installed_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = tmp_path / "forge-01"
    candidate = case / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "source.py").write_text("result = 1\n", encoding="utf-8")
    legacy = b'{"run_id":"independent-legacy-run","phase":"delivered"}\n'
    (case / "legacy-receipt.json").write_bytes(legacy)
    binding = {
        "schema_version": "forge-legacy-binding-v1", "case_id": "forge-01",
        "source_kind": "installed-llm-forge", "installed_consumer": True,
        "candidate_sha256": campaign.workspace_digest(candidate),
        "legacy_receipt_sha256": hashlib.sha256(legacy).hexdigest(),
    }
    (case / "binding.json").write_text(json.dumps(binding), encoding="utf-8")

    with pytest.raises(campaign.CampaignError, match="baseline.*provenance"):
        campaign.read_baseline(tmp_path, "forge-01")
    launched = []
    monkeypatch.setattr(campaign.fanout, "run_provider", launched.append)
    with pytest.raises(campaign.CampaignError, match="baseline.*provenance"):
        campaign.run_live_campaign(
            tmp_path / "run", tmp_path, consumer_root=tmp_path / "consumer",
        )
    assert launched == []
    assert not (tmp_path / "run").exists()


def test_declared_and_held_out_checks_use_fresh_workspace_outside_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seat = tmp_path / "seat"
    campaign.fixtures.materialize("forge-01", seat)
    boundary = _test_boundary(tmp_path / "campaign", tmp_path / "baseline",
                              tmp_path / "consumer")
    monkeypatch.setattr(campaign.fixtures, "_verify_boundary", lambda _boundary: None)
    seen = []

    def checked(case_id, workspace, *, boundary):
        seen.append((case_id, workspace))
        assert workspace != seat
        assert (workspace / "src" / "transfer.py").stat().st_ino != (
            seat / "src" / "transfer.py"
        ).stat().st_ino
        return subprocess.CompletedProcess(["check"], 0, "", "")

    monkeypatch.setattr(campaign.fixtures, "run_declared_check", checked)
    monkeypatch.setattr(campaign.fixtures, "run_held_out", checked)
    result = campaign.verify_fresh("forge-01", seat, boundary.run_root / "verifiers",
                                   boundary=boundary)

    assert result["fresh_verifier"] is True
    assert result["held_out_detected"] is True
    assert result["verified_candidate_sha256"] == campaign.workspace_digest(seat)
    assert len(seen) == 2


def test_fresh_campaign_verifier_refuses_missing_boundary_before_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seat = tmp_path / "seat"
    campaign.fixtures.materialize("forge-01", seat)
    called = []
    monkeypatch.setattr(campaign.fixtures, "run_declared_check",
                        lambda *_args, **_kwargs: (
                            called.append("declared") or subprocess.CompletedProcess([], 0, "", "")
                        ))
    monkeypatch.setattr(campaign.fixtures, "run_held_out",
                        lambda *_args, **_kwargs: (
                            called.append("held-out") or subprocess.CompletedProcess([], 0, "", "")
                        ))

    with pytest.raises(campaign.CampaignError, match="sealed.*boundary"):
        campaign.verify_fresh("forge-01", seat, tmp_path / "verifiers")

    assert called == []
    assert not (tmp_path / "verifiers").exists()


def test_installed_consumer_handoff_must_bind_the_exact_verified_fusion(
    tmp_path: Path,
) -> None:
    candidate = "c" * 64
    verifier = "e" * 64
    sources = ["a" * 64, "b" * 64]
    assert campaign.read_consumer(tmp_path, "forge-01", candidate, verifier, sources) is False

    case = tmp_path / "forge-01"
    case.mkdir()
    consumer_receipt = b'{"run_id":"installed-consumer-run","phase":"delivered"}\n'
    (case / "consumer-receipt.json").write_bytes(consumer_receipt)
    handoff = {
        "schema_version": "fanout-installed-handoff-v1", "case_id": "forge-01",
        "source_kind": "installed-llm-fanout-execute", "delivered": True,
        "candidate_sha256": candidate, "verifier_receipt_sha256": verifier,
        "source_contributions": sources,
        "consumer_receipt_sha256": hashlib.sha256(consumer_receipt).hexdigest(),
    }
    (case / "handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    with pytest.raises(campaign.CampaignError, match="consumer.*provenance"):
        campaign.read_consumer(tmp_path, "forge-01", candidate, verifier, sources)

    handoff["candidate_sha256"] = "d" * 64
    (case / "handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    with pytest.raises(campaign.CampaignError, match="handoff.*digest"):
        campaign.read_consumer(tmp_path, "forge-01", candidate, verifier, sources)


@pytest.mark.usefixtures("_allow_hermetic_forge_turns")
def test_campaign_passes_one_sealed_boundary_to_every_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = object()
    run_root = tmp_path / "campaign"
    baseline_root = tmp_path / "baselines"
    consumer_root = tmp_path / "consumer"

    def open_boundary(run, baseline, consumer, *, resume):
        assert (run, baseline, consumer, resume) == (
            run_root, baseline_root, consumer_root, False,
        )
        assert not run_root.exists()
        run_root.mkdir()
        return boundary

    monkeypatch.setattr(campaign.fixtures, "open_blindness_boundary", open_boundary,
                        raising=False)
    monkeypatch.setattr(
        campaign, "_baseline_inputs",
        lambda _root, case_ids: {case_id: {"candidate_sha256": "a" * 64}
                                 for case_id in case_ids},
    )
    received = []

    def measure(case_id, *_args, boundary=None, **_kwargs):
        received.append((case_id, boundary))
        return {"case_id": case_id, "installed_consumer": False}

    monkeypatch.setattr(campaign, "measure_case", measure)
    campaign.run_live_campaign(run_root, baseline_root, consumer_root=consumer_root)

    assert received == [
        (f"forge-{number:02d}", boundary) for number in range(1, 13)
    ]


@pytest.mark.usefixtures("_allow_hermetic_forge_turns")
def test_campaign_visits_all_twelve_and_withholds_final_receipt_for_missing_handoffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    visited = []
    monkeypatch.setattr(
        campaign, "_baseline_inputs",
        lambda _root, case_ids: {case_id: {"candidate_sha256": "a" * 64}
                                 for case_id in case_ids},
    )

    def measure(case_id, *_args, **_kwargs):
        visited.append(case_id)
        return {"case_id": case_id, "installed_consumer": False}

    monkeypatch.setattr(campaign, "measure_case", measure)
    run_root = tmp_path / "campaign"
    receipt = campaign.run_live_campaign(
        run_root, tmp_path / "baseline", consumer_root=tmp_path / "consumer",
    )

    assert visited == [f"forge-{number:02d}" for number in range(1, 13)]
    assert receipt["status"] == "blocked"
    assert receipt["blockers"] == [
        "missing-installed-consumer-handoff", "unauthenticated-run-evidence",
    ]
    assert not (run_root / "receipt.json").exists()
    assert campaign.verify_receipt(next(run_root.glob("blocked-*.json"))) == receipt


@pytest.mark.usefixtures("_allow_hermetic_forge_turns")
def test_campaign_cannot_publish_a_final_receipt_from_unattested_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        campaign, "_baseline_inputs",
        lambda _root, case_ids: {case_id: {"candidate_sha256": "a" * 64}
                                 for case_id in case_ids},
    )
    monkeypatch.setattr(
        campaign, "measure_case",
        lambda case_id, *_args, **_kwargs: {"case_id": case_id, "installed_consumer": True},
    )
    monkeypatch.setattr(campaign.quality, "_score_forge", lambda *_args: {"status": "green"})
    run_root = tmp_path / "campaign"

    receipt = campaign.run_live_campaign(
        run_root, tmp_path / "baseline", consumer_root=tmp_path / "consumer",
    )

    assert receipt["status"] == "blocked"
    assert "unauthenticated-run-evidence" in receipt["blockers"]
    assert not (run_root / "receipt.json").exists()


@pytest.mark.usefixtures("_allow_hermetic_forge_turns")
def test_campaign_resume_rejects_changed_helper_source_before_any_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "fixture-helper.py"
    helper.write_bytes(Path(campaign.fixtures.__file__).read_bytes())
    monkeypatch.setattr(campaign.fixtures, "__file__", str(helper))
    monkeypatch.setattr(
        campaign, "_baseline_inputs",
        lambda _root, case_ids: {case_id: {"candidate_sha256": "a" * 64}
                                 for case_id in case_ids},
    )
    visits = []
    monkeypatch.setattr(campaign, "measure_case", lambda case_id, *_args, **_kwargs: (
        visits.append(case_id) or {"case_id": case_id, "installed_consumer": False}
    ))
    run_root = tmp_path / "campaign"
    campaign.run_live_campaign(
        run_root, tmp_path / "baselines", consumer_root=tmp_path / "consumer",
    )
    assert len(visits) == 12

    helper.write_bytes(helper.read_bytes() + b"\n# changed after start\n")
    with pytest.raises(campaign.CampaignError, match="resume inputs changed"):
        campaign.run_live_campaign(
            run_root, tmp_path / "baselines", consumer_root=tmp_path / "consumer",
            resume=True,
        )
    assert len(visits) == 12


def test_cli_requires_explicit_live_opt_in(tmp_path: Path) -> None:
    assert campaign.main([
        "--run-root", str(tmp_path / "campaign"),
        "--baseline-root", str(tmp_path / "baseline"),
    ]) == 2
    assert not (tmp_path / "campaign").exists()


def test_cli_live_stays_blocked_by_unmeasured_blindness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_attestation = tmp_path / "self-attestation.json"
    fake_attestation.write_text('{"blind":true}', encoding="utf-8")
    monkeypatch.setenv("FORGE_BLINDNESS_ATTESTATION", str(fake_attestation))
    root = tmp_path / "campaign"
    launched = []
    monkeypatch.setattr(campaign.fanout, "run_provider", launched.append)

    assert campaign.main([
        "--live", "--run-root", str(root),
        "--baseline-root", str(tmp_path / "baselines"),
        "--consumer-root", str(tmp_path / "consumer"),
    ]) == 1
    assert "independent baseline is missing" in capsys.readouterr().err
    assert launched == []
    assert not root.exists()
