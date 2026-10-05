"""Hermetic Council campaign tests: final answers, private evidence, exact resume."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import fanout_council_campaign as campaign
from shared.lib.fanout import quality


BOOK = quality.load_casebook(campaign.CASEBOOK)
CASES = [case for case in BOOK["cases"] if case["kind"] == "council"]


def _delivery(case: dict, role: str) -> dict[str, object]:
    answer = f"private {role} final answer for {case['id']}"
    digest = hashlib.sha256(answer.encode()).hexdigest()
    result: dict[str, object] = {
        "answer": answer, "artifact_sha256": digest,
        "assertions": [True] * len(case["assertions"]),
        "installed_consumer": True,
        "source_kind": f"installed-{'llm-council' if role == 'baseline' else 'llm-fanout-execute'}-final",
        "delivery_receipt_sha256": "a" * 64,
    }
    if role == "candidate":
        result.update({
            "synthesis_sources": ["b" * 64, "c" * 64],
            "verification_receipt_sha256": "d" * 64,
            "verified_final_sha256": digest,
            "blocker_regressions": [],
        })
    return result


def _judger(calls: list[tuple[str, str, str, str, str]]):
    def judge(judge_id, request):
        calls.append((judge_id, request.case_id, request.panel_id,
                      request.order, request.answer_a))
        models = {"claude": "claude-opus-5-5", "codex": "gpt-6.1-sol",
                  "maka": "gpt-6.1-sol"}
        return {
            "winner": "tie", "auth_route": (
                "api-key-relay" if request.panel_id == "interactive" else "harbor-lab"
            ) if judge_id == "maka" else f"{judge_id}-native",
            "requested_model": models[judge_id], "observed_model": models[judge_id],
            "effort": "xhigh", "tool_boundary_sha256": "e" * 64,
            "tool_calls": 0,
        }
    return judge


class CouncilCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def deliveries(self) -> dict[tuple[str, str], dict[str, object]]:
        return {(case["id"], role): _delivery(case, role)
                for case in CASES for role in ("baseline", "candidate")}

    def test_prospective_orders_and_delivery_boundaries_cannot_be_reassigned(self):
        from shared.lib.fanout.artifacts import ArtifactStore
        from shared.lib.fanout.controller import create_lifecycle_controller
        controller = create_lifecycle_controller(self.root / "controller")
        normal = [f"normal-{i:02}" for i in range(1, 31)]
        plan = {"schema": "fanout-quality-observations-v1",
                "casebook_sha256": campaign._digest(BOOK),
                "orders": [{"case_id": case, "nonce": f"{i:032x}",
                            "first": "enabled" if i < 6 else "disabled"}
                           for i, case in enumerate(normal[:12])],
                "timers": normal}
        with ArtifactStore(self.root / "observations") as store:
            ref = store.write_json("plan.json", plan)
            authority = campaign.preregister_quality_observations(controller, store, ref, BOOK)
            start = campaign.start_quality_delivery(controller, store, authority, BOOK,
                                                     "normal-01", "baseline")
            with self.assertRaisesRegex(campaign.CouncilCampaignError, "already started"):
                campaign.start_quality_delivery(controller, store, authority, BOOK,
                                                 "normal-01", "baseline")
            terminal = campaign.finish_quality_delivery(controller, start, "a" * 64)
            self.assertGreaterEqual(terminal["elapsed_ns"], 0)
            with self.assertRaisesRegex(campaign.CouncilCampaignError, "already frozen"):
                campaign.preregister_quality_observations(controller, store, ref, BOOK)

    def test_live_gate_refuses_self_asserted_deliveries_before_judging(self) -> None:
        called = []
        with self.assertRaisesRegex(campaign.CouncilCampaignError,
                                    "authenticated installed.*unavailable"):
            campaign.run_live_campaign(
                self.root / "run", self.root / "baseline", self.root / "candidate",
                authorized=True, judge=_judger(called),
            )
        self.assertEqual(called, [])
        self.assertFalse((self.root / "run").exists())

    def test_live_requires_opt_in_before_creating_evidence(self) -> None:
        with self.assertRaisesRegex(campaign.CouncilCampaignError,
                                    "explicit paid authorization"):
            campaign.run_live_campaign(self.root / "run", self.root / "baseline",
                                       self.root / "candidate")
        self.assertFalse((self.root / "run").exists())

    def test_ballot_request_repr_does_not_expose_private_answers_or_prompt(self) -> None:
        request = campaign.BallotRequest(
            "normal-01", "interactive", "maka", "candidate-first",
            "private sealed prompt", "private candidate answer", "private baseline answer",
        )
        rendered = repr(request)
        self.assertNotIn("private sealed prompt", rendered)
        self.assertNotIn("private candidate answer", rendered)
        self.assertNotIn("private baseline answer", rendered)

    def test_hermetic_campaign_records_two_panels_without_public_answers(self) -> None:
        calls: list[tuple[str, str, str, str, str]] = []
        deliveries = self.deliveries()
        receipt = campaign.run_measured_campaign(
            self.root / "run", self.root / "baseline", self.root / "candidate",
            delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
            judge=_judger(calls),
        )
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["blockers"], ["unauthenticated-installed-delivery"])
        self.assertEqual(receipt["distinct_cases"], 60)
        self.assertEqual(len(calls), 60 * 2 * 3 * 2)
        self.assertEqual({call[2] for call in calls}, {"interactive", "harbor"})
        first = [call for call in calls if call[1] == "normal-01" and call[2] == "interactive"]
        self.assertEqual([(call[0], call[3]) for call in first], [
            (judge, order) for judge in ("claude", "codex", "maka")
            for order in ("candidate-first", "baseline-first")
        ])
        self.assertEqual(first[0][4], deliveries[("normal-01", "candidate")]["answer"])
        self.assertEqual(first[1][4], deliveries[("normal-01", "baseline")]["answer"])
        public = (self.root / "run" / "receipt.json").read_text()
        self.assertNotIn("private baseline final answer", public)
        self.assertNotIn("private candidate final answer", public)
        self.assertNotIn("prompt", public)
        private = self.root / "run" / "cases" / "normal-01.json"
        self.assertIn("private candidate final answer", private.read_text())
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(campaign.verify_receipt(self.root / "run" / "receipt.json"), receipt)

    def test_resume_skips_settled_ballots_and_rejects_changed_final_answer(self) -> None:
        deliveries = self.deliveries()
        first_calls = []
        root = self.root / "run"
        first = campaign.run_measured_campaign(
            root, self.root / "baseline", self.root / "candidate",
            delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
            judge=_judger(first_calls),
        )
        replay_calls = []
        self.assertEqual(campaign.run_measured_campaign(
            root, self.root / "baseline", self.root / "candidate", resume=True,
            delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
            judge=_judger(replay_calls),
        ), first)
        self.assertEqual(replay_calls, [])
        deliveries[("normal-01", "candidate")] = {
            **deliveries[("normal-01", "candidate")], "answer": "changed final answer",
        }
        with self.assertRaisesRegex(campaign.CouncilCampaignError,
                                    "final answer.*changed|digest"):
            campaign.run_measured_campaign(
                root, self.root / "baseline", self.root / "candidate", resume=True,
                delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
                judge=_judger(replay_calls),
            )
        self.assertEqual(replay_calls, [])

    def test_candidate_seat_draft_cannot_be_marked_as_final(self) -> None:
        deliveries = self.deliveries()
        candidate = deliveries[("normal-01", "candidate")]
        candidate["synthesis_sources"] = [candidate["artifact_sha256"], "c" * 64]
        calls = []
        with self.assertRaisesRegex(campaign.CouncilCampaignError,
                                    "synthesized final answer"):
            campaign.run_measured_campaign(
                self.root / "run", self.root / "baseline", self.root / "candidate",
                delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
                judge=_judger(calls),
            )
        self.assertEqual(calls, [])

    def test_unhashable_candidate_evidence_is_rejected_as_invalid_delivery(self) -> None:
        for field in ("synthesis_sources", "blocker_regressions"):
            with self.subTest(field=field):
                deliveries = self.deliveries()
                deliveries[("normal-01", "candidate")][field] = [["nested"]]
                calls = []
                with self.assertRaisesRegex(campaign.CouncilCampaignError,
                                            "verified synthesized final answer"):
                    campaign.run_measured_campaign(
                        self.root / field, self.root / "baseline", self.root / "candidate",
                        delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
                        judge=_judger(calls),
                    )
                self.assertEqual(calls, [])

    def test_tampered_private_record_fails_closed_on_resume(self) -> None:
        deliveries = self.deliveries()
        root = self.root / "run"
        campaign.run_measured_campaign(
            root, self.root / "baseline", self.root / "candidate",
            delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
            judge=_judger([]),
        )
        path = root / "cases" / "normal-01.json"
        record = json.loads(path.read_text())
        record["panels"][0]["ballots"][0]["winner"] = "A"
        path.write_text(json.dumps(record))
        calls = []
        with self.assertRaisesRegex(campaign.CouncilCampaignError,
                                    "private case digest changed"):
            campaign.run_measured_campaign(
                root, self.root / "baseline", self.root / "candidate", resume=True,
                delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
                judge=_judger(calls),
            )
        self.assertEqual(calls, [])

    def test_run_root_rejects_symlinked_parent_before_writing(self) -> None:
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        deliveries = self.deliveries()
        calls = []
        with self.assertRaisesRegex(campaign.CouncilCampaignError,
                                    "canonical|symlink"):
            campaign.run_measured_campaign(
                alias / "run", self.root / "baseline", self.root / "candidate",
                delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
                judge=_judger(calls),
            )
        self.assertEqual(calls, [])
        self.assertFalse((real / "run").exists())

    def test_resume_does_not_repeat_a_settled_ballot_after_interruption(self) -> None:
        deliveries = self.deliveries()
        root = self.root / "run"
        first_calls = []
        judge = _judger(first_calls)

        def interrupted(judge_id, request):
            if len(first_calls) == 1:
                raise RuntimeError("synthetic interruption")
            return judge(judge_id, request)

        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            campaign.run_measured_campaign(
                root, self.root / "baseline", self.root / "candidate",
                delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
                judge=interrupted,
            )
        self.assertEqual(len(first_calls), 1)
        resumed_calls = []
        campaign.run_measured_campaign(
            root, self.root / "baseline", self.root / "candidate", resume=True,
            delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
            judge=_judger(resumed_calls),
        )
        self.assertEqual(len(resumed_calls), 60 * 2 * 3 * 2 - 1)
        self.assertNotIn(first_calls[0], resumed_calls)

    def test_missing_candidate_counts_as_loss_in_both_panels_without_judging(self) -> None:
        deliveries = self.deliveries()
        deliveries[("normal-01", "candidate")] = None
        calls = []
        try:
            receipt = campaign.run_measured_campaign(
                self.root / "run", self.root / "baseline", self.root / "candidate",
                delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
                judge=_judger(calls),
            )
        except campaign.CouncilCampaignError as error:
            self.fail(f"missing candidate should count as a loss: {error}")
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["distinct_cases"], 60)
        self.assertEqual(len(calls), 59 * 2 * 3 * 2)
        self.assertNotIn("normal-01", {call[1] for call in calls})
        self.assertIsNone(receipt["cases"][0]["candidate_final_sha256"])
        for panel in receipt["panels"]:
            self.assertEqual(panel["distinct_cases"], 60)
            self.assertEqual(panel["paired_cases"], 60)
            self.assertEqual(panel["candidate_failed_deliveries"], 1)
            self.assertEqual(panel["baseline_wins"], 1)
            self.assertEqual(panel["paired_losses"], 1)
            self.assertEqual(panel["status"], "red")
        self.assertEqual(campaign.verify_receipt(self.root / "run" / "receipt.json"),
                         receipt)

    def test_explicit_failed_candidate_counts_as_loss(self) -> None:
        deliveries = self.deliveries()
        deliveries[("deep-01", "candidate")] = {
            "delivered": False, "failure_receipt_sha256": "f" * 64,
        }
        try:
            receipt = campaign.run_measured_campaign(
                self.root / "run", self.root / "baseline", self.root / "candidate",
                delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
                judge=_judger([]),
            )
        except campaign.CouncilCampaignError as error:
            self.fail(f"failed candidate should count as a loss: {error}")
        self.assertIn("panels", receipt)
        for panel in receipt["panels"]:
            self.assertEqual(panel["strata"]["deep"]["paired_losses"], 1)
            self.assertEqual(panel["candidate_failed_deliveries"], 1)

    def test_panel_scores_count_judge_wins_abstentions_and_assertion_loss(self) -> None:
        deliveries = self.deliveries()
        deliveries[("normal-01", "candidate")]["assertions"] = [False]
        calls = []
        tie_judger = _judger(calls)

        def judge(judge_id, request):
            ballot = tie_judger(judge_id, request)
            if request.case_id == "normal-02":
                ballot["winner"] = "A" if request.order == "candidate-first" else "B"
            elif request.case_id == "normal-03":
                ballot["winner"] = "B" if request.order == "candidate-first" else "A"
            elif request.case_id == "normal-04":
                ballot["winner"] = "A"
            return ballot

        receipt = campaign.run_measured_campaign(
            self.root / "run", self.root / "baseline", self.root / "candidate",
            delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
            judge=judge,
        )
        self.assertEqual(receipt["status"], "blocked")
        self.assertIn("panels", receipt)
        for panel in receipt["panels"]:
            self.assertEqual(panel["baseline_assertions"], 90)
            self.assertEqual(panel["candidate_assertions"], 89)
            self.assertEqual(panel["paired_losses"], 1)
            self.assertEqual(panel["candidate_wins"], 1)
            self.assertEqual(panel["baseline_wins"], 1)
            self.assertEqual(panel["ties"], 57)
            self.assertEqual(panel["abstentions"], 1)
            self.assertEqual(panel["decisive_cases"], 2)
            self.assertEqual(panel["status"], "red")
        self.assertEqual(campaign.verify_receipt(self.root / "run" / "receipt.json"),
                         receipt)

    def test_panel_score_retains_assessed_security_regression(self) -> None:
        deliveries = self.deliveries()
        deliveries[("normal-01", "candidate")]["blocker_regressions"] = ["security"]
        try:
            receipt = campaign.run_measured_campaign(
                self.root / "run", self.root / "baseline", self.root / "candidate",
                delivery_reader=lambda case_id, role: deliveries[(case_id, role)],
                judge=_judger([]),
            )
        except campaign.CouncilCampaignError as error:
            self.fail(f"assessed security regression should be scored: {error}")
        for panel in receipt["panels"]:
            self.assertEqual(panel["blocker_regressions"], 1)
            self.assertEqual(panel["status"], "red")


class GuardedCouncilProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.stage = self.root / "stage"
        self.work = self.stage / "engine-work"
        source_files = {
            "shared/lib/council/__init__.py": b"",
            "shared/lib/council/engine.py": b"# reviewed engine fixture\n",
            "shared/lib/council/agy_guard.py": b"# guarded agy fixture\n",
            "shared/lib/council/agy_readonly_hook.py": b"# deny hook fixture\n",
            "shared/skills/llm-council/scripts/fanout.py": b"# unchanged facade fixture\n",
            "shared/skills/llm-council/SKILL.md": (
                b"# Council\n\n## 4. Synthesize the best answer\n"
                b"Use all valid seat answers and emit the exact header.\n\n"
                b"## Failure handling\nReject an inconclusive panel.\n"
            ),
        }
        for relative, content in source_files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.expected_files = {
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in source_files.items()
        }

    def stage_bundle(self) -> dict[str, object]:
        return campaign.stage_guarded_council_bundle(
            self.source, self.stage, self.expected_files,
        )

    def engine_manifest(self, *, altered_result: bool = False) -> Path:
        answers = {
            "claude": "A" * 4_200 + " VERBATIM-END",
            "codex": "Codex independently answers the question.",
            "agy": "Agy independently answers the question.",
        }
        providers = []
        for name, answer in answers.items():
            path = self.work / f"{name}.result.txt"
            path.write_text(answer + (" changed" if altered_result and name == "claude" else ""))
            path.chmod(0o600)
            providers.append({
                "name": name, "valid": True, "reason": "ok",
                "result_file": str(path),
                "result_sha256": hashlib.sha256(answer.encode()).hexdigest(),
            })
        manifest = {
            "schema": 1, "workdir": str(self.work), "prompt_sha256": "a" * 64,
            "config": {"mode": "normal", "read_only": True,
                       "providers": ["claude", "codex", "agy"]},
            "summary": {"requested": 3, "valid": 3,
                        "failed": 0, "degraded": False,
                        "seats_attempted": 3, "seats_responded": 3,
                        "header": "**Council: 3 of 3 seats responded.**"},
            "providers": providers,
        }
        path = self.work / "manifest.json"
        path.write_text(json.dumps(manifest))
        path.chmod(0o600)
        return path

    def synthesizer(self, body: str | None = None) -> tuple[Path, str]:
        script = self.root / "claude-fixture"
        if body is None:
            body = (
                "import json,sys\n"
                "argv=sys.argv[1:]\n"
                "required=['--print','--output-format','json','--strict-mcp-config',"
                "'--safe-mode','--restricted','--disable-slash-commands',"
                "'--permission-prompts','none','--no-session-persistence']\n"
                "assert all(part in argv for part in required)\n"
                "assert argv[argv.index('--tools')+1] == ''\n"
                "assert argv[argv.index('--model')+1] == 'claude-opus-5-5'\n"
                "assert argv[argv.index('--effort')+1] == 'max'\n"
                "prompt=sys.stdin.read()\n"
                "assert 'A'*4200 in prompt and 'VERBATIM-END' in prompt\n"
                "assert 'Use all valid seat answers' in prompt\n"
                "result='**Council: 3 of 3 seats responded.**\\n\\nA careful final answer.'\n"
                "print(json.dumps({'type':'result','subtype':'success',"
                "'is_error':False,'result':result}))\n"
            )
        script.write_text("#!/usr/bin/env python3\n" + body)
        script.chmod(0o700)
        return script, hashlib.sha256(script.read_bytes()).hexdigest()

    def test_stage_rejects_source_drift_and_copies_exact_guarded_bundle(self) -> None:
        receipt = self.stage_bundle()
        self.assertEqual(receipt["protocol"], "guarded-council-protocol-v1")
        self.assertEqual((self.stage / "bundle/lib/council/engine.py").read_bytes(),
                         b"# reviewed engine fixture\n")
        self.assertEqual((self.stage / "bundle/skills/llm-council/scripts/fanout.py").read_bytes(),
                         b"# unchanged facade fixture\n")
        self.assertEqual((self.stage / "bundle-receipt.json").stat().st_mode & 0o777, 0o600)
        (self.source / "shared/lib/council/engine.py").write_text("changed\n")
        with self.assertRaisesRegex(campaign.CouncilCampaignError, "source.*digest"):
            campaign.stage_guarded_council_bundle(
                self.source, self.root / "second-stage", self.expected_files,
            )

    def test_synthesis_witness_binds_actual_child_stdout_and_full_seat_answers(self) -> None:
        self.stage_bundle()
        manifest = self.engine_manifest()
        binary, binary_sha256 = self.synthesizer()
        witness_path = self.root / "witness.json"
        witness = campaign.capture_guarded_council_synthesis(
            self.stage, manifest, "How should this be handled?", "normal",
            binary, binary_sha256, witness_path,
        )
        self.assertEqual(witness["protocol"], "guarded-council-protocol-v1")
        self.assertEqual(witness["status"], "provisional-native-isolation-unproven")
        self.assertEqual(witness["engine_origin"], "unattested-staged-workdir")
        self.assertIn("synth_prompt_sha256", witness)
        self.assertEqual(witness["synth_argv"][witness["synth_argv"].index("--tools") + 1], "")
        self.assertEqual(witness["answer"],
                         "**Council: 3 of 3 seats responded.**\n\nA careful final answer.")
        self.assertEqual(witness["artifact_sha256"],
                         hashlib.sha256(witness["answer"].encode()).hexdigest())
        self.assertEqual(witness["seat_result_sha256"]["claude"],
                         hashlib.sha256(("A" * 4_200 + " VERBATIM-END").encode()).hexdigest())
        self.assertEqual(witness["synth_stdout_sha256"],
                         hashlib.sha256(witness["synth_stdout"].encode()).hexdigest())
        self.assertEqual(witness_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("installed_consumer", witness)
        self.assertNotIn("assertions", witness)

    def test_changed_engine_result_is_rejected_before_synthesis(self) -> None:
        self.stage_bundle()
        manifest = self.engine_manifest(altered_result=True)
        with self.assertRaisesRegex(campaign.CouncilCampaignError, "result.*digest"):
            campaign.capture_guarded_council_synthesis(
                self.stage, manifest, "How should this be handled?", "normal",
                self.root / "missing-claude", "0" * 64, self.root / "witness.json",
            )
        self.assertFalse((self.root / "witness.json").exists())

    def test_synthesizer_error_envelope_cannot_become_a_delivery(self) -> None:
        self.stage_bundle()
        manifest = self.engine_manifest()
        binary, binary_sha256 = self.synthesizer(
            "import json\nprint(json.dumps({'type':'result','subtype':'error',"
            "'is_error':True,'result':'**Council: 3 of 3 seats responded.**\\n\\nwrong'}))\n"
        )
        with self.assertRaisesRegex(campaign.CouncilCampaignError, "synthesizer.*success"):
            campaign.capture_guarded_council_synthesis(
                self.stage, manifest, "How should this be handled?", "normal",
                binary, binary_sha256, self.root / "witness.json",
            )
        self.assertFalse((self.root / "witness.json").exists())

    def test_manifest_must_be_an_object_and_header_must_match_panel(self) -> None:
        self.stage_bundle()
        manifest = self.engine_manifest()
        manifest.write_text("[]")
        with self.assertRaises(campaign.CouncilCampaignError):
            campaign.capture_guarded_council_synthesis(
                self.stage, manifest, "Question?", "normal", self.root / "missing",
                "0" * 64, self.root / "witness.json",
            )
        data = json.loads(self.engine_manifest().read_text())
        data["summary"]["header"] += "  Model fallback: invented."
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(campaign.CouncilCampaignError, "summary header"):
            campaign.capture_guarded_council_synthesis(
                self.stage, manifest, "Question?", "normal", self.root / "missing",
                "0" * 64, self.root / "witness.json",
            )
        self.assertFalse((self.root / "witness.json").exists())

    def test_large_pinned_synthesizer_binary_is_hashable(self) -> None:
        binary = self.root / "large-claude-fixture"
        binary.write_bytes(b"#!fake\n" + b"x" * 2_000_000)
        binary.chmod(0o700)
        self.assertEqual(campaign._executable_digest(binary),
                         hashlib.sha256(binary.read_bytes()).hexdigest())

    def test_manifest_outside_disposable_stage_is_rejected(self) -> None:
        self.stage_bundle()
        manifest = self.engine_manifest()
        external = self.root / "external"
        external.mkdir(mode=0o700)
        outside = external / "manifest.json"
        outside.write_bytes(manifest.read_bytes())
        outside.chmod(0o600)
        with self.assertRaisesRegex(campaign.CouncilCampaignError, "manifest path"):
            campaign.capture_guarded_council_synthesis(
                self.stage, outside, "Question?", "normal", self.root / "missing",
                "0" * 64, self.root / "witness.json",
            )


if __name__ == "__main__":
    unittest.main()
