from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components" / "skills"))
import maka_smoke  # noqa: E402
import skillctl  # noqa: E402

from test_skill_delivery import fixture as delivery_fixture  # noqa: E402


def test_default_runtime_db_matches_managed_platform_profiles(tmp_path: Path) -> None:
    assert maka_smoke.default_runtime_db(tmp_path, platform="darwin") == (
        tmp_path / "Library" / "Application Support" / "Maka" / "workspaces" / "default" / "runtime.sqlite"
    )
    assert maka_smoke.default_runtime_db(
        tmp_path,
        platform="linux",
        environment={"XDG_CONFIG_HOME": "/tmp/ignored-by-managed-launcher"},
    ) == tmp_path / ".config" / "Maka" / "workspaces" / "default" / "runtime.sqlite"


def initialize_runtime_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE session_metadata (
                session_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE message_admissions (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                skill_invocation_json TEXT NOT NULL
            );
            CREATE TABLE core_root_turn_admissions (
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                admitted_at INTEGER NOT NULL,
                record_json TEXT NOT NULL
            );
            CREATE TABLE core_agent_run_events (
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                record_json TEXT NOT NULL
            );
            """
        )


def fake_maka(tmp_path: Path) -> Path:
    binary = tmp_path / "maka"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sqlite3, sys, time, uuid\n"
        "if sys.argv[1:2] == ['--version']:\n"
        "    print('Maka test')\n"
        "    raise SystemExit(0)\n"
        "assert '--yolo' not in sys.argv\n"
        "with open(os.environ['KHENRIX_MAKA_SMOKE_ARGV_LOG'], 'a') as log:\n"
        "    log.write('\\t'.join(sys.argv[1:]) + '\\n')\n"
        "prompt = sys.argv[2]\n"
        "cwd = sys.argv[sys.argv.index('--cwd') + 1]\n"
        "db = sqlite3.connect(os.environ['KHENRIX_FAKE_MAKA_DB'])\n"
        "rows = db.execute('SELECT session_id,payload_json FROM session_metadata').fetchall()\n"
        "existing = [sid for sid,payload in rows if json.loads(payload)['cwd'] == cwd]\n"
        "if '--continue' in sys.argv:\n"
        "    assert len(existing) == 1\n"
        "    session_id = existing[0]\n"
        "else:\n"
        "    assert not existing\n"
        "    session_id = str(uuid.uuid4())\n"
        "    now = int(time.time() * 1000)\n"
        "    db.execute('INSERT INTO session_metadata VALUES (?,?,?)', (session_id, json.dumps({'cwd': cwd, 'createdAt': now}), now))\n"
        "if '/skill:' in prompt:\n"
        "    skill = prompt.split('/skill:', 1)[1].split()[0]\n"
        "    receipt = {'invocation':'explicit','request':skill,'success':True,'ref':'user:agents:'+skill,'id':skill,'name':skill,'scope':'user','source':'agents','truncated':False}\n"
        "    invocation = {'loaded':[{'id':skill,'name':skill}],'failed':[],'receipts':[receipt]}\n"
        "    turn_id = str(uuid.uuid4())\n"
        "    admission = {'skillInvocation':invocation}\n"
        "    db.execute('INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)', (session_id,turn_id,int(time.time()*1000),json.dumps(admission)))\n"
        "elif '--continue' not in sys.argv:\n"
        "    skill = 'khenrix-writing' if 'Humanize' in prompt else 'khenrix-quality'\n"
        "    event_id = str(uuid.uuid4())\n"
        "    data = {'invocation':'model_tool','success':True,'skillRef':'user:agents:'+skill,'skillId':skill,'skillName':skill,'skillScope':'user','skillSource':'agents','truncated':False,'shadowCandidateCount':0,'shadowHitAt1':False,'shadowHitAt5':False,'shadowHitAt20':False}\n"
        "    record = {'type':'skill_loaded','data':data}\n"
        "    sequence = db.execute('SELECT count(*)+1 FROM core_agent_run_events WHERE session_id=?',(session_id,)).fetchone()[0]\n"
        "    db.execute('INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)',(session_id,sequence,event_id,'skill_loaded',json.dumps(record)))\n"
        "db.commit(); db.close()\n"
        "if 'three things on my plate' in prompt: print('Open the pull request and read the description.')\n"
        "elif 'Stop ADHD mode' in prompt: print('ADHD mode is now off.')\n"
        "else: print('bounded model output')\n"
    )
    binary.chmod(0o755)
    return binary


def test_smoke_runs_bounded_cases_and_writes_hash_bound_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, home, config = delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    binary = fake_maka(tmp_path)
    log = tmp_path / "argv.log"
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(log))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    manifest = ROOT / "components" / "skills" / "maka-smoke.json"

    result = maka_smoke.main(
        [
            "--repo-root",
            str(repo),
            "--home",
            str(home),
            "--manifest",
            str(manifest),
            "--maka-bin",
            str(binary),
            "--runtime-db",
            str(runtime_db),
        ]
    )

    assert result == 0
    invocations = log.read_text().splitlines()
    declared = json.loads(manifest.read_text())
    assert len(invocations) == len(declared["cases"]) + sum(
        len(flow["steps"]) for flow in declared["flows"]
    )
    assert all("--yolo" not in line for line in invocations)
    assert all("--cwd" in line and "--max-steps" in line and "--timeout" in line for line in invocations)
    receipt = json.loads((config.state_dir / "maka-smoke-receipt.json").read_text())
    assert receipt["input_hash"].startswith("sha256:")
    assert receipt["runner_hash"].startswith("sha256:")
    assert receipt["bounded"] == {"temporary_cwd_per_case": True, "yolo": False}
    assert {(case["skill"], case["route"]) for case in receipt["cases"]} == {
        ("khenrix-quality", "explicit"),
        ("khenrix-quality", "natural"),
        ("khenrix-writing", "explicit"),
        ("khenrix-writing", "natural"),
    }
    assert all(case["event_evidence"]["evidence_hash"].startswith("sha256:") for case in receipt["cases"])
    assert {
        case["event_evidence"]["kind"]
        for case in receipt["cases"]
        if case["route"] == "explicit"
    } == {"core_root_turn_admission"}
    natural_prompts = "\n".join(
        case["prompt"] for case in declared["cases"] if case["route"] == "natural"
    )
    assert {
        "i-have-adhd",
        "no-ai-slop",
        "ponytail",
        "ponytail-review",
        "ponytail-audit",
        "ponytail-debt",
        "ponytail-gain",
        "ponytail-help",
        "humanizer",
    } <= set(natural_prompts.replace(",", "").replace(".", "").split())
    flow = receipt["flows"][0]
    assert [step["continued"] for step in flow["steps"]] == [False, True, True]
    assert flow["activation_evidence"]["invocation"] == "explicit"
    assert flow["steps"][1]["behavior_evidence"]["first_line_actionable"] is True
    assert flow["steps"][2]["behavior_evidence"] == {
        "check": "normal_mode_ack",
        "line_count": 1,
        "state_acknowledged": True,
    }


def test_flow_checks_behavior_without_prompting_for_the_expected_answer() -> None:
    action = {
        "id": "action-first-follow-up",
        "prompt": "I have three things on my plate: review a pull request, answer email, and prepare lunch.",
        "check": "adhd_action_first",
    }
    evidence = maka_smoke.verify_flow_output(
        action, "Open the pull request and read the description.\nThen decide whether to review it."
    )
    assert evidence["first_line_actionable"] is True
    with pytest.raises(maka_smoke.SmokeError, match="concrete action"):
        maka_smoke.verify_flow_output(action, "Those tasks sound difficult. Here are some options.")

    opt_out = {
        "id": "normal-mode-opt-out",
        "prompt": "Normal mode. Stop ADHD mode. Confirm briefly.",
        "check": "normal_mode_ack",
    }
    with pytest.raises(maka_smoke.SmokeError, match="echoed"):
        maka_smoke.verify_flow_output(opt_out, opt_out["prompt"])
    with pytest.raises(maka_smoke.SmokeError, match="bounded state"):
        maka_smoke.verify_flow_output(opt_out, "Understood. I can help with anything else.")


def test_dry_run_executes_no_model_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, home, config = delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    binary = fake_maka(tmp_path)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(log))

    result = maka_smoke.main(
        [
            "--repo-root",
            str(repo),
            "--home",
            str(home),
            "--manifest",
            str(ROOT / "components" / "skills" / "maka-smoke.json"),
            "--maka-bin",
            str(binary),
            "--dry-run",
        ]
    )
    assert result == 0
    assert not log.exists()
    assert not (config.state_dir / "maka-smoke-receipt.json").exists()


def test_smoke_rejects_non_mapping_nested_install_receipt(tmp_path: Path) -> None:
    _, _, config = delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    receipt_path = config.state_dir / skillctl.RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text())
    receipt["skills"] = []
    receipt_path.write_text(json.dumps(receipt))

    manifest = maka_smoke.load_cases(ROOT / "components" / "skills" / "maka-smoke.json")
    with pytest.raises(skillctl.DeliveryError, match="skills must be an object"):
        maka_smoke.installed_closure(config, manifest)


def test_manifest_requires_explicit_and_natural_case_per_skill(tmp_path: Path) -> None:
    manifest = tmp_path / "cases.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "id": "only-one",
                        "route": "explicit",
                        "skill": "khenrix-quality",
                        "prompt": "/skill:khenrix-quality test",
                        "timeout_seconds": 10,
                        "max_steps": 1,
                    }
                ],
            }
        )
    )
    with pytest.raises(maka_smoke.SmokeError, match="cover each skill"):
        maka_smoke.load_cases(manifest)


def test_natural_case_fails_when_model_tool_event_is_absent(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    now = 2_000
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO session_metadata VALUES (?,?,?)",
            ("session-1", json.dumps({"cwd": str(cwd.resolve()), "createdAt": now}), now),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        session_id = maka_smoke.locate_session(connection, cwd, now)
        with pytest.raises(maka_smoke.SmokeError, match="model_tool"):
            maka_smoke.verify_natural_event(connection, session_id, "khenrix-quality")


def test_explicit_case_rejects_wrong_source(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    invocation = {
        "loaded": [{"id": "khenrix-quality", "name": "khenrix-quality"}],
        "failed": [],
        "receipts": [
            {
                "invocation": "explicit",
                "success": True,
                "id": "khenrix-quality",
                "ref": "custom:khenrix-quality",
                "source": "custom",
            }
        ],
    }
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO message_admissions(session_id,skill_invocation_json) VALUES (?,?)",
            ("session-1", json.dumps(invocation)),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="explicit admission"):
            maka_smoke.verify_explicit_event(connection, "session-1", "khenrix-quality")


def test_explicit_case_reads_canonical_root_turn_admission(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    skill = "khenrix-quality"
    receipt = {
        "invocation": "explicit",
        "request": skill,
        "success": True,
        "ref": f"user:agents:{skill}",
        "id": skill,
        "name": skill,
        "scope": "user",
        "source": "agents",
        "truncated": False,
    }
    record = {
        "skillInvocation": {
            "loaded": [{"id": skill, "name": skill}],
            "failed": [],
            "receipts": [receipt],
        }
    }
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)",
            ("session-1", "turn-1", 1, json.dumps(record)),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        evidence = maka_smoke.verify_explicit_event(connection, "session-1", skill)
    assert evidence["kind"] == "core_root_turn_admission"
    assert evidence["turn_id"] == "turn-1"


def test_explicit_case_rejects_truncated_skill_instructions(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    skill = "khenrix-quality"
    receipt = {
        "invocation": "explicit",
        "request": skill,
        "success": True,
        "ref": f"user:agents:{skill}",
        "id": skill,
        "name": skill,
        "scope": "user",
        "source": "agents",
        "truncated": True,
    }
    invocation = {
        "loaded": [{"id": skill, "name": skill}],
        "failed": [],
        "receipts": [receipt],
    }
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO message_admissions(session_id,skill_invocation_json) VALUES (?,?)",
            ("session-1", json.dumps(invocation)),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="truncated instructions"):
            maka_smoke.verify_explicit_event(connection, "session-1", skill)


def add_natural_event(runtime_db: Path, data: dict[str, object]) -> None:
    record = {"type": "skill_loaded", "data": data}
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
            ("session-1", 1, "event-1", "skill_loaded", json.dumps(record)),
        )


def natural_event_data(**overrides: object) -> dict[str, object]:
    skill = "khenrix-quality"
    data: dict[str, object] = {
        "invocation": "model_tool",
        "success": True,
        "skillRef": f"user:agents:{skill}",
        "skillId": skill,
        "skillName": skill,
        "skillScope": "user",
        "skillSource": "agents",
        "truncated": False,
        "shadowCandidateCount": 0,
        "shadowHitAt1": False,
        "shadowHitAt5": False,
        "shadowHitAt20": False,
    }
    data.update(overrides)
    return data


def test_natural_case_rejects_truncated_skill_instructions(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    add_natural_event(runtime_db, natural_event_data(truncated=True))
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="truncated instructions"):
            maka_smoke.verify_natural_event(connection, "session-1", "khenrix-quality")


def test_natural_case_rejects_non_top_shadow_candidate(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    add_natural_event(
        runtime_db,
        natural_event_data(
            shadowCandidateCount=2,
            shadowRank=2,
            shadowHitAt1=False,
            shadowHitAt5=True,
            shadowHitAt20=True,
        ),
    )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="first-ranked"):
            maka_smoke.verify_natural_event(connection, "session-1", "khenrix-quality")


def test_natural_case_accepts_first_ranked_shadow_candidate(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    add_natural_event(
        runtime_db,
        natural_event_data(
            shadowCandidateCount=3,
            shadowRank=1,
            shadowHitAt1=True,
            shadowHitAt5=True,
            shadowHitAt20=True,
        ),
    )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        evidence = maka_smoke.verify_natural_event(
            connection, "session-1", "khenrix-quality"
        )
    assert evidence["skill_id"] == "khenrix-quality"
