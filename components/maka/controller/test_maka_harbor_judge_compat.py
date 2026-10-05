#!/usr/bin/env python3
"""Offline licensed-payload checks; these do not certify a live judge."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import apply_maka_compat as relay
import apply_maka_hosted_onboarding_compat as v2
import test_maka_hosted_onboarding_compat as onboarding_test


def main() -> None:
    package = Path(sys.argv[1]).resolve(strict=True)
    # The frozen v2 patch must still yield exactly its original discovery row.
    fetcher = v2.model_fetcher_patched_bytes((package / v2.MODEL_FETCHER_PATH).read_bytes())
    metadata_path = Path("node_modules/@maka/core/dist/model-metadata.js")
    metadata = (package / metadata_path).read_bytes()
    assert v2.digest(fetcher) == "427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7"
    assert v2.digest(metadata) == "454ae64aa3a2dee4fe93d30f3d547e361b6b30f2c26987c69f0a8ab6981cd2cf"

    import apply_maka_harbor_judge_compat as compat

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime = root / "maka-0.2.0-dev.62.20260928-node-24.16.0-relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1"
        staged = runtime / "maka-agent"
        shutil.copytree(package, staged, symlinks=True)
        relay.apply(staged)
        v2.apply(staged)
        subject = (staged / v2.MAKA_SUBJECT_PATH).read_bytes()
        relay_bytes = (staged / relay.RELAY_PATH).read_bytes()
        onboarding_test.run_fetch_trap(package, fetcher, root)
        node = shutil.which("node")
        assert node is not None
        path_script = Path(__file__).with_name("assert-eval-runtime-path.mjs")
        baseline = subprocess.run([node, str(path_script), str(runtime), str(staged)],
                                  text=True, capture_output=True, timeout=30)
        assert baseline.returncode == 0, baseline.stderr
        assert json.loads(baseline.stdout)["modelFetcherSha256"] == "427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7"
        compat.apply(staged)
        assert (staged / v2.MAKA_SUBJECT_PATH).read_bytes() == subject
        assert v2.digest(subject) == "f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775"
        assert (staged / relay.RELAY_PATH).read_bytes() == relay_bytes
        assert v2.digest(relay_bytes) == "6214ed402d2d8345b20b625739b9a08905116bfdd944775938d5d78054033621"
        assert v2.digest((staged / v2.MODEL_FETCHER_PATH).read_bytes()) == "7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5"
        assert v2.digest((staged / metadata_path).read_bytes()) == "9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7"
        onboarding_test.run_fetch_trap(staged, (staged / v2.MODEL_FETCHER_PATH).read_bytes(), root,
                                      expected_models=[{"id": "gpt-5.6-sol"}, {"id": "gpt-6.1-sol"}])
        checked = subprocess.run([node, "--input-type=module", "-"], input=f'''
import assert from 'node:assert/strict';
import {{ lookupModelMetadata, openAiAdapterApiProtocol }} from {json.dumps((staged / metadata_path).as_uri())};
assert.deepEqual(lookupModelMetadata('openai', 'gpt-6.1-sol'), {{
  displayName: 'GPT-6.1 Sol',
  capabilities: {{ reasoning: true, functionCalling: true, vision: true }},
  contextWindow: 1050000, maxOutputTokens: 128000,
  thinkingOptions: {{ efforts: ['low', 'medium', 'high', 'xhigh', 'max'] }},
}});
assert.equal(openAiAdapterApiProtocol('gpt-6.1-sol', 'openai'), 'openai-responses');
assert.deepEqual(lookupModelMetadata('openai-codex', 'gpt-6.1-sol'), {{}});
assert.ok(Object.keys(lookupModelMetadata('openai', 'gpt-5.6-sol')).length > 0);
''', text=True, capture_output=True, timeout=30)
        assert checked.returncode == 0, checked.stderr
        # Reuse the frozen offline capture harness without changing its 5.6 bytes.
        # The actual licensed SDK sends only to a loopback WS trap / fetch trap.
        capture_source = Path(__file__).with_name("provider-request-contract.mjs").read_text()
        capture_source = capture_source.replace("'gpt-5.6-sol'", "'gpt-6.1-sol'").replace(
            "synthetic contract prompt",
            "Judge receipt nonce: maka:fanout-judge-0123456789abcdef0123456789abcdef\\nCompare A and B.",
        )
        capture_script = root / "judge-provider-contract.mjs"
        capture_script.write_text(capture_source)
        capture_path = root / "judge-request.json"
        captured = subprocess.run([node, str(capture_script), str(staged), str(capture_path)],
                                  text=True, capture_output=True, timeout=30)
        assert captured.returncode == 0, captured.stderr
        sys.path.insert(0, str(Path(__file__).parent / "egress-overlay"))
        import egress_filter
        for body in json.loads(capture_path.read_text())["fallbackBodies"]:
            assert body["model"] == "gpt-6.1-sol"
            assert body["reasoning"] == {"effort": "xhigh", "summary": "auto"}
            prepared = egress_filter.prepare_judge_request(json.dumps(body).encode())
            assert prepared is not None
            outbound, evidence = prepared
            rewritten = json.loads(outbound)
            assert rewritten["model"] == "gpt-6.1-sol"
            assert rewritten["service_tier"] == "default"
            assert "tools" not in rewritten and rewritten["tool_choice"] == "none"
            assert evidence["requested_model"] == "gpt-6.1-sol" and evidence["tool_count"] == 0
        projector = subprocess.run([
            node, "--test", "--test-name-pattern=pinned RuntimeEvent",
            str(Path(__file__).with_name("test_harbor_judge.mjs")),
        ], env={**os.environ, "KHENRIX_MAKA_RUNTIME_ROOT": str(runtime)},
            text=True, capture_output=True, timeout=30)
        assert projector.returncode == 0, projector.stderr + projector.stdout
        assert "skipped 0" in projector.stdout
        proof = subprocess.run([node, str(path_script), str(runtime), str(staged), compat.OVERLAY_ID],
                               text=True, capture_output=True, timeout=30)
        assert proof.returncode == 0, proof.stderr
        evidence = json.loads(proof.stdout)
        assert evidence["modelFetcherSha256"] == "7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5"
        assert evidence["modelMetadataSha256"] == "9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7"
        assert evidence["compatibilityVariant"] == "harbor-judge-sol61-v1"
        old_path = subprocess.run([node, str(path_script), str(runtime), str(staged)],
                                  text=True, capture_output=True, timeout=30)
        assert old_path.returncode != 0

        import apply_undici_h2_close_compat as h2
        h2_original = (staged / h2.TARGET).read_bytes()
        assert v2.digest(h2_original) == "611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7"
        active_runtime = root / (runtime.name + "-undici-h2-close-v2")
        active_staged = active_runtime / "maka-agent"
        shutil.copytree(staged, active_staged, symlinks=True)
        h2.apply(active_staged)
        assert (staged / h2.TARGET).read_bytes() == h2_original
        assert (active_staged / relay.RELAY_PATH).read_bytes() == relay_bytes
        assert (active_staged / v2.MAKA_SUBJECT_PATH).read_bytes() == subject
        active_proof = subprocess.run([
            node, str(path_script), str(active_runtime), str(active_staged), "harbor-judge-sol61-h2-v1",
        ], text=True, capture_output=True, timeout=30)
        assert active_proof.returncode == 0, active_proof.stderr
        active_evidence = json.loads(active_proof.stdout)
        assert active_evidence["compatibilityVariant"] == "harbor-judge-sol61-h2-v1"
        assert active_evidence["modelFetcherSha256"] == "7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5"
        assert active_evidence["modelMetadataSha256"] == "9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7"
        assert active_evidence["undiciH2BaseSha256"] == "611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7"
        assert active_evidence["undiciH2Sha256"] == "32c2b747c1c1c9c7405e60dc70145f67f9b8f96db0b6264f55e1f0400a375dc4"
        target = active_staged / h2.TARGET
        patched_h2 = target.read_bytes()
        target.write_bytes(patched_h2 + b"\n// drift\n")
        drift_proof = subprocess.run([
            node, str(path_script), str(active_runtime), str(active_staged), "harbor-judge-sol61-h2-v1",
        ], text=True, capture_output=True, timeout=30)
        assert drift_proof.returncode != 0 and "Undici" in drift_proof.stderr
        target.write_bytes(patched_h2)
        missing_variant = subprocess.run([
            node, str(path_script), str(active_runtime), str(active_staged), "harbor-judge-sol61-v1",
        ], text=True, capture_output=True, timeout=30)
        assert missing_variant.returncode != 0

        for damaged in (v2.MODEL_FETCHER_PATH, metadata_path):
            candidate = root / damaged.name
            for relative, content in ((v2.MODEL_FETCHER_PATH, fetcher), (metadata_path, metadata)):
                target = candidate / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            target = candidate / damaged
            target.write_bytes(target.read_bytes() + b"\n// drift\n")
            before = [(candidate / relative).read_bytes() for relative in (v2.MODEL_FETCHER_PATH, metadata_path)]
            try:
                compat.apply(candidate)
            except RuntimeError as error:
                assert "hash does not match" in str(error)
            else:
                raise AssertionError("judge overlay accepted unpinned input")
            assert [(candidate / relative).read_bytes() for relative in (v2.MODEL_FETCHER_PATH, metadata_path)] == before
            target.unlink()
            target.symlink_to(staged / damaged)
            try:
                compat.apply(candidate)
            except RuntimeError as error:
                assert "not a regular file" in str(error)
            else:
                raise AssertionError("judge overlay accepted a symlink")


if __name__ == "__main__":
    main()
