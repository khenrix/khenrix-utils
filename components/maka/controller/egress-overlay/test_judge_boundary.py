"""Pure contract checks for the pinned Harbor judge proxy path."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import egress_filter as policy


MARKER = "maka:fanout-judge-0123456789abcdef0123456789abcdef"
PROMPT = f"Judge receipt nonce: {MARKER}\nCompare two final answers."


def body(prompt: str = PROMPT, **changes: object) -> bytes:
    request = {
        "model": "gpt-6.1-sol",
        "reasoning": {"effort": "xhigh", "summary": "auto"},
        "stream": True,
        "store": False,
        "parallel_tool_calls": True,
        "include": ["reasoning.encrypted_content"],
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": "system"}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
        "tools": [{"type": "function", "name": "Read", "parameters": {}}],
        "tool_choice": "auto",
        "prompt_cache_key": "maka:judge-session",
    }
    request.update(changes)
    return json.dumps(request).encode()


class HarborJudgeBoundaryTests(unittest.TestCase):
    def test_coding_body_keeps_the_frozen_model_while_judge_uses_current_model(self) -> None:
        headers = SimpleNamespace(get_all=lambda name: ["application/json"] if name == "content-type" else [])
        for judge_only, model, accepted in (
            (False, "gpt-5.6-sol", True), (False, "gpt-6.1-sol", False),
            (True, "gpt-6.1-sol", True), (True, "gpt-5.6-sol", False),
        ):
            with self.subTest(judge_only=judge_only, model=model), patch.object(policy, "JUDGE_ONLY", judge_only):
                self.assertEqual(policy.valid_openai_body(SimpleNamespace(
                    headers=headers, raw_content=body(model=model))), accepted)

    def test_judge_rewrite_keeps_content_length_equal_to_outbound_body(self) -> None:
        candidate = json.loads(body())
        candidate["input"][0] = {"role": "developer", "content": "synthetic system"}
        candidate.pop("tools")
        candidate.pop("tool_choice")
        raw = json.dumps(candidate, separators=(",", ":")).encode()

        if policy.http is not None:
            request = policy.http.Request.make(
                "POST", "https://api.openai.com/v1/responses", raw,
                {"Content-Type": "application/json"},
            )
        else:
            class Headers(dict):
                def get_all(self, name: str) -> list[str]:
                    return [self[name]] if name in self else []

                def set_all(self, name: str, values: list[str]) -> None:
                    if values:
                        self[name] = values[0]
                    else:
                        self.pop(name, None)

            class Request:
                def __init__(self) -> None:
                    self.headers = Headers({
                        "content-type": "application/json",
                        "content-length": str(len(raw)),
                    })
                    self.raw_content = raw
                    self.scheme = "https"
                    self.method = "POST"
                    self.path = "/v1/responses"
                    self.query = ()

                @property
                def content(self) -> bytes:
                    return self.raw_content

                @content.setter
                def content(self, value: bytes) -> None:
                    self.raw_content = value
                    self.headers["content-length"] = str(len(value))

            request = Request()

        flow = SimpleNamespace(request=request, response=None, metadata={})
        with tempfile.TemporaryDirectory() as temporary:
            with (patch.object(policy, "JUDGE_ONLY", True),
                  patch.object(policy, "_OPENAI_API_KEY", "synthetic-key"),
                  patch.object(policy, "_OPENAI_WEBSOCKET_PROBE_COUNT", 1),
                  patch.object(policy, "_OPENAI_REQUEST_COUNT", 0),
                  patch.object(policy, "AUDIT_PATH", Path(temporary) / "hits.jsonl")):
                policy.inject_openai_authorization(flow, policy.OPENAI_API_ENDPOINT)

        self.assertIsNone(flow.response)
        self.assertEqual(request.headers["content-length"], str(len(request.raw_content)))

    def test_response_rejection_audits_only_fixed_diagnostics(self) -> None:
        created = {"type": "response.created", "response": {
            "id": "SECRET_RESPONSE_ID", "model": "gpt-6.1-sol"}}
        payload = (f"data: {json.dumps(created)}\n\ndata: [DONE]\n\n"
                   "SECRET_BODY https://SECRET_URL/ Bearer SECRET_KEY").encode()
        headers = SimpleNamespace(get_all=lambda _name: ["gzip-SECRET_HEADER-SECRET_KEY"])
        upstream = SimpleNamespace(status_code=200, raw_content=payload, headers=headers)
        flow = SimpleNamespace(response=upstream, metadata={"maka_judge_evidence": {}},
                               request=SimpleNamespace(url="https://SECRET_URL/"))
        fake_http = SimpleNamespace(Response=SimpleNamespace(make=lambda status, body, values:
            SimpleNamespace(status_code=status, raw_content=body, headers=values)))
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "hits.jsonl"
            with (patch.object(policy, "JUDGE_ONLY", True),
                  patch.object(policy, "AUDIT_PATH", audit),
                  patch.object(policy, "http", fake_http)):
                policy.response(flow)
            self.assertEqual(flow.response.status_code, 502)
            record = json.loads(audit.read_text())
            self.assertEqual(record["ruleId"], "judge_response")
            self.assertEqual(record["normalizedPath"],
                             "status=200;encoding=other;reason=created-tier")
            self.assertNotIn("SECRET_", audit.read_text())
            self.assertFalse((audit.parent / "relay-judge-receipts").exists())

    def test_upstream_error_audit_normalizes_status_and_encoding(self) -> None:
        headers = SimpleNamespace(get_all=lambda _name: [" GZip "])
        upstream = SimpleNamespace(status_code=429, raw_content=b"SECRET_BODY", headers=headers)
        flow = SimpleNamespace(response=upstream, metadata={"maka_judge_evidence": {}})
        fake_http = SimpleNamespace(Response=SimpleNamespace(make=lambda status, body, values:
            SimpleNamespace(status_code=status, raw_content=body, headers=values)))
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "hits.jsonl"
            with (patch.object(policy, "JUDGE_ONLY", True),
                  patch.object(policy, "AUDIT_PATH", audit),
                  patch.object(policy, "http", fake_http)):
                policy.response(flow)
            self.assertEqual(flow.response.status_code, 502)
            record = json.loads(audit.read_text())
            self.assertEqual(record["normalizedPath"],
                             "status=429;encoding=gzip;reason=upstream-status")
            self.assertNotIn("SECRET_BODY", audit.read_text())

    def test_response_diagnostic_keeps_non_200_and_missing_tier_fail_closed(self) -> None:
        created = {"type": "response.created", "response": {
            "id": "resp_synthetic", "model": "gpt-6.1-sol"}}
        completed = {"type": "response.completed", "response": {
            "id": "resp_synthetic", "model": "gpt-6.1-sol",
            "output": [{"type": "message"}], "status": "completed"}}
        payload = (f"data: {json.dumps(created)}\n\n"
                   f"data: {json.dumps(completed)}\n\n").encode()
        self.assertEqual(policy.judge_response_evidence_with_reason(429, payload),
                         (None, "upstream-status"))
        self.assertEqual(policy.judge_response_evidence_with_reason(200, payload),
                         (None, "created-tier"))
        self.assertIsNone(policy.judge_response_evidence(200, payload))

    def test_only_allowed_judge_openai_connect_marks_proxy_entry(self) -> None:
        events = []
        flow = SimpleNamespace(request=SimpleNamespace(), response=None)
        with (patch.object(policy, "JUDGE_ONLY", True),
              patch.object(policy, "mitm_url", SimpleNamespace(unparse=lambda *_: "https://api.openai.com/")),
              patch.object(policy, "append_audit", side_effect=lambda *args: events.append(args)),
              patch.object(policy, "validate_connect_identity", return_value=policy.OPENAI_API_ENDPOINT),
              patch.object(policy, "apply_http_policy")):
            policy.http_connect(flow)
        self.assertEqual(events, [("openai_connect", "", "allowed")])

        for judge_only, target, rejected in [
            (False, policy.OPENAI_API_ENDPOINT, False),
            (True, policy.Endpoint("example.com", 443), False),
            (True, policy.OPENAI_API_ENDPOINT, True),
        ]:
            with self.subTest(judge_only=judge_only, target=target, rejected=rejected):
                events.clear()
                flow.response = None
                def apply(candidate, _url):
                    if rejected:
                        candidate.response = object()
                with (patch.object(policy, "JUDGE_ONLY", judge_only),
                      patch.object(policy, "mitm_url", SimpleNamespace(unparse=lambda *_: "https://example.com/")),
                      patch.object(policy, "append_audit", side_effect=lambda *args: events.append(args)),
                      patch.object(policy, "validate_connect_identity", return_value=target),
                      patch.object(policy, "apply_http_policy", side_effect=apply)):
                    policy.http_connect(flow)
                self.assertEqual(events, [])

    def test_marked_judge_request_removes_all_provider_tools(self) -> None:
        rewritten, evidence = policy.prepare_judge_request(body())
        transmitted = json.loads(rewritten)
        self.assertNotIn("tools", transmitted)
        self.assertEqual(transmitted["tool_choice"], "none")
        self.assertEqual(evidence["request_marker_sha256"],
                         hashlib.sha256(MARKER.encode()).hexdigest())
        self.assertEqual(evidence["prompt_sha256"],
                         hashlib.sha256(PROMPT.encode()).hexdigest())
        self.assertEqual(evidence["requested_model"], "gpt-6.1-sol")
        self.assertEqual(evidence["outbound_effort"], "xhigh")
        self.assertEqual(evidence["tool_count"], 0)

    def test_pinned_adapter_developer_string_still_keeps_one_bounded_user_prompt(self) -> None:
        request = json.loads(body())
        request["input"] = [
            {"role": "developer", "content": "Pinned Runtime Host instructions"},
            {"role": "user", "content": [{"type": "input_text", "text": PROMPT}]},
        ]
        rewritten = policy.prepare_judge_request(json.dumps(request).encode())
        self.assertIsNotNone(rewritten)
        self.assertEqual(json.loads(rewritten[0])["tool_choice"], "none")
        request["input"][1]["content"] = PROMPT
        self.assertIsNone(policy.prepare_judge_request(json.dumps(request).encode()))
        request["input"][1]["content"] = [{"type": "input_text", "text": PROMPT}]
        request["input"][0]["content"] = "x" * (48 * 1024 + 1)
        self.assertIsNone(policy.prepare_judge_request(json.dumps(request).encode()))

    def test_other_or_ambiguous_requests_cannot_enter_judge_route(self) -> None:
        cases = [
            body("ordinary request"),
            body(PROMPT, input=[
                {"role": "user", "content": [{"type": "input_text", "text": PROMPT}]},
                {"role": "user", "content": [{"type": "input_text", "text": PROMPT}]},
            ]),
            body(PROMPT, model="gpt-5.6-sol"),
            body(PROMPT, model="gpt-6-sol"),
            body(PROMPT, reasoning={"effort": "high", "summary": "auto"}),
            body(PROMPT, input=[{"role": "tool", "content": [{"type": "input_text", "text": PROMPT}]}]),
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate[:60]):
                self.assertIsNone(policy.prepare_judge_request(candidate))

    def test_response_attests_the_provider_model_and_default_tier(self) -> None:
        created = {"type": "response.created", "response": {
            "id": "resp_1", "model": "gpt-6.1-sol", "service_tier": "default"}}
        completed = {"type": "response.completed", "response": {
            "id": "resp_1", "model": "gpt-6.1-sol", "service_tier": "default",
            "output": [{"type": "message"}]}}
        payload = (f"data: {json.dumps(created)}\n\n"
                   f"data: {json.dumps(completed)}\n\n"
                   "data: [DONE]\n\n").encode()
        self.assertEqual(policy.judge_response_evidence(200, payload), {
            "observed_model": "gpt-6.1-sol", "observed_service_tier": "default"})
        self.assertIsNone(policy.judge_response_evidence(
            200, f"data: {json.dumps(created)}\n\n".encode()))
        created["response"]["model"] = "gpt-5.6-sol"
        self.assertIsNone(policy.judge_response_evidence(
            200, (f"data: {json.dumps(created)}\n\n"
                  f"data: {json.dumps(completed)}\n\n").encode()))
        created["response"]["model"] = "gpt-6.1-sol"
        completed["response"]["id"] = "resp_other"
        self.assertIsNone(policy.judge_response_evidence(
            200, (f"data: {json.dumps(created)}\n\n"
                  f"data: {json.dumps(completed)}\n\n").encode()))
        completed["response"]["id"] = "resp_1"
        completed["response"]["output"] = [{"type": "function_call"}]
        self.assertIsNone(policy.judge_response_evidence(
            200, (f"data: {json.dumps(created)}\n\n"
                  f"data: {json.dumps(completed)}\n\n").encode()))
        self.assertIsNone(policy.judge_response_evidence(500, payload))


if __name__ == "__main__":
    unittest.main()
