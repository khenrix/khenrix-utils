import json
import tempfile
import unittest
from pathlib import Path

from harbor_judge_audit import summarize


class HarborJudgeAuditTests(unittest.TestCase):
    def test_summary_reports_only_fixed_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = [
                {"ruleId": "openai_connect"},
                {"ruleId": "openai_websocket_disabled"},
                {"ruleId": "openai_authorized", "host": "SECRET_SHOULD_NOT_APPEAR"},
                {"ruleId": "credential_boundary", "normalizedPath": "SECRET_SHOULD_NOT_APPEAR"},
                {"ruleId": "judge_boundary", "normalizedPath": "SECRET_SHOULD_NOT_APPEAR"},
                {"ruleId": "judge_response", "normalizedPath":
                 "status=200;encoding=gzip;reason=created-tier",
                 "host": "SECRET_SHOULD_NOT_APPEAR"},
                {"ruleId": "authority_mismatch"},
                {"ruleId": "raw_tunnel"},
                {"ruleId": "policy_error"},
                {"ruleId": "unrelated_rule", "host": "SECRET_SHOULD_NOT_APPEAR"},
            ]
            (root / "hits.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n")
            (root / "relay-judge-receipts").mkdir()
            (root / "relay-judge-receipts" / "receipt.json").write_text("{}")

            result = summarize(root)

            self.assertEqual(result, {
                "schema": "khenrix-maka-harbor-judge-audit-v1",
                "state": "ok",
                "total_events": 10,
                "openai_connects": 1,
                "websocket_probes": 1,
                "authorized": 1,
                "request_rejected": 2,
                "credential_rejected": 1,
                "judge_rejected": 1,
                "response_rejected": 1,
                "response_rejections": [{"upstream_status": 200,
                                         "content_encoding": "gzip",
                                         "reason": "created-tier"}],
                "authority_mismatch": 1,
                "raw_tunnel": 1,
                "policy_error": 1,
                "other_events": 1,
                "receipt_count": 1,
            })
            self.assertNotIn("SECRET_SHOULD_NOT_APPEAR", json.dumps(result))

    def test_untrusted_response_diagnostic_is_never_repeated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "hits.jsonl").write_text(json.dumps({
                "ruleId": "judge_response",
                "normalizedPath": "status=200;encoding=SECRET_HEADER;reason=SECRET_BODY",
            }) + "\n")
            result = summarize(root)
            self.assertEqual(result["state"], "unavailable")
            self.assertNotIn("SECRET_", json.dumps(result))

    def test_touched_but_empty_audit_is_distinct_from_a_proxy_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "hits.jsonl").touch()
            result = summarize(root)
            self.assertEqual(result["state"], "ok")
            self.assertEqual(result["total_events"], 0)
            self.assertEqual(result["openai_connects"], 0)

    def test_missing_or_malformed_audit_never_exposes_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(summarize(root)["state"], "not_recorded")
            (root / "hits.jsonl").write_text("SECRET_SHOULD_NOT_APPEAR\n")
            result = summarize(root)
            self.assertEqual(result["state"], "unavailable")
            self.assertNotIn("SECRET_SHOULD_NOT_APPEAR", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
