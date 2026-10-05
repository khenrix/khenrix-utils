"""The Harbor judge never reaches a paid path without two explicit gates."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest


SCRIPT = pathlib.Path(__file__).with_name("harbor-judge.sh")
LIB = SCRIPT.with_name("lib.sh")
COMPONENT = SCRIPT.parent.parent
CONTROLLER_SCRIPT = COMPONENT / "controller/run-harbor-judge-inside.sh"
JUDGE_COMPOSE = COMPONENT / "controller/egress-overlay/docker-compose-harbor-judge.yaml"


class HarborJudgeGateTests(unittest.TestCase):
    def test_rendered_judge_uses_init_without_broadening_its_boundary(self) -> None:
        compose = shutil.which("docker-compose")
        self.assertIsNotNone(compose, "Use the component's pinned mise tools")
        rendered = subprocess.run(
            [compose, "--project-name", "maka-synthetic-cancel", "-f", str(JUDGE_COMPOSE),
             "config", "--format", "json"],
            capture_output=True, text=True, check=True, timeout=10,
            env={
                "PATH": os.environ["PATH"],
                "MAKA_CONTROLLER_IMAGE_ID": "sha256:" + "c" * 64,
                "MAKA_EGRESS_IMAGE_ID": "sha256:" + "d" * 64,
                "MAKA_EVAL_SECRET_VOLUME": "maka-synthetic-secret",
                "MAKA_EVAL_RUNTIME_ROOT": "/synthetic/runtime",
                "MAKA_LAB_ROOT": "/synthetic/lab",
                "MAKA_INSTALL_RECEIPT": "/synthetic/install-receipt.json",
                "MAKA_JUDGE_DECOY": "maka-decoy-synthetic123",
            },
        )
        judge = json.loads(rendered.stdout)["services"]["judge"]
        self.assertIs(judge.get("init"), True)
        self.assertEqual(judge["entrypoint"][0], "/synthetic/runtime/node/bin/node")
        self.assertIs(judge["read_only"], True)
        self.assertEqual(judge["cap_drop"], ["ALL"])
        self.assertEqual(judge["networks"], {"judge": None})

    def test_entrypoint_selects_distinct_runtime_and_completion_bytes(self) -> None:
        entrypoint = COMPONENT / "controller/entrypoint.sh"
        source = entrypoint.read_text(encoding="utf-8")
        selector = source[source.index('runtime_parent="$MAKA_LAB_ROOT/runtime/linux-amd64"'):
                          source.index("manifest_name=RUNTIME_SHA256SUMS")]
        frozen = """maka=0.2.0-dev.62.20260928
node=24.16.0
compatibility-overlay-bundle=relay-root-virtiofs-v2-eval-openai-onboarding-v2
compatibility-overlays=relay-root-virtiofs-v2,eval-openai-onboarding-v2
relay-agent-sha256=6214ed402d2d8345b20b625739b9a08905116bfdd944775938d5d78054033621
model-fetcher-sha256=427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7
maka-subject-sha256=f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775"""
        for variant in ("", "harbor-judge-sol61-v1", "harbor-judge-sol61-h2-v1", "unknown"):
            completed = subprocess.run(
                ["/bin/bash", "-c", 'set -eu\n' + selector +
                 '\nprintf "%s\\n%s\\n" "$version_root" "$expected_complete"'],
                capture_output=True, text=True,
                env={"PATH": "/usr/bin:/bin", "MAKA_LAB_ROOT": "/synthetic/lab",
                     "MAKA_EVAL_RUNTIME_VARIANT": variant}, timeout=3,
            )
            if variant == "unknown":
                self.assertEqual(completed.returncode, 64)
                continue
            self.assertEqual(completed.returncode, 0, completed.stderr)
            path, completion = completed.stdout.rstrip("\n").split("\n", 1)
            baseline = "/synthetic/lab/runtime/linux-amd64/maka-0.2.0-dev.62.20260928-node-24.16.0-relay-root-virtiofs-v2-eval-openai-onboarding-v2"
            if not variant:
                self.assertEqual(path, baseline)
                self.assertEqual(completion, frozen)
            else:
                suffix = "-undici-h2-close-v1" if variant == "harbor-judge-sol61-h2-v1" else ""
                self.assertEqual(path, baseline + "-harbor-judge-sol61-v1" + suffix)
                expected = frozen.replace(
                    "compatibility-overlay-bundle=relay-root-virtiofs-v2-eval-openai-onboarding-v2",
                    "compatibility-overlay-bundle=relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1",
                ).replace("compatibility-overlays=relay-root-virtiofs-v2,eval-openai-onboarding-v2",
                          "compatibility-overlays=relay-root-virtiofs-v2,eval-openai-onboarding-v2,harbor-judge-sol61-v1").replace(
                    "427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7",
                    "7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5",
                ) + "\nmodel-metadata-sha256=9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7"
                if suffix:
                    expected = expected.replace(
                        "compatibility-overlay-bundle=relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1",
                        "compatibility-overlay-bundle=relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1-undici-h2-close-v1",
                    ).replace(
                        "compatibility-overlays=relay-root-virtiofs-v2,eval-openai-onboarding-v2,harbor-judge-sol61-v1",
                        "compatibility-overlays=relay-root-virtiofs-v2,eval-openai-onboarding-v2,harbor-judge-sol61-v1,undici-h2-close-v1",
                    ) + "\nundici-h2-base-sha256=611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7\nundici-h2-sha256=521fef59ccb84909a3c68d52c15e972f61836e0d386093d5d4f4e64ba7d85087"
                self.assertEqual(completion, expected)

    def test_shell_result_gate_accepts_only_current_model_in_result_and_relay(self) -> None:
        source = CONTROLLER_SCRIPT.read_text(encoding="utf-8")
        gate = source[source.index("expected_boundary=$MAKA_JUDGE_EXPECTED_BOUNDARY\n"):
                      source.index("jq -c .result ")]
        result = {
            "auth_route": "harbor-lab", "requested_model": "gpt-6.1-sol",
            "observed_model": "gpt-6.1-sol", "effort": "xhigh", "tool_calls": 0,
            "tool_boundary_sha256": "a" * 64, "winner": "A",
        }
        relay = {
            "schema": "khenrix-maka-harbor-judge-request-v2", "time_utc_ms": 1,
            "request_marker_sha256": "b" * 64, "prompt_sha256": "c" * 64,
            "requested_model": "gpt-6.1-sol", "observed_model": "gpt-6.1-sol",
            "requested_service_tier": "default", "observed_service_tier": "default",
            "outbound_effort": "xhigh", "tool_count": 0, "http_status": 200,
            "relay_source_sha256": "d" * 64,
            "response_answer_sha256": "f" * 64,
        }
        with tempfile.TemporaryDirectory() as directory:
            envelope = pathlib.Path(directory) / "envelope.json"
            variants = [("current", None, None)] + [
                (f"{area}.{field}", area, field)
                for area in ("result", "relay")
                for field in ("requested_model", "observed_model")
            ]
            for label, area, field in variants:
                with self.subTest(label=label):
                    candidate_result, candidate_relay = dict(result), dict(relay)
                    if field:
                        (candidate_result if area == "result" else candidate_relay)[field] = "gpt-5.6-sol"
                    envelope.write_text(json.dumps({"result": candidate_result, "evidence": {
                        "schema": "khenrix-maka-harbor-judge-evidence-v1",
                        "transcript_sha256": "e" * 64, "relay_receipt": candidate_relay,
                    }}))
                    completed = subprocess.run(
                        ["/bin/bash", "-c", 'set -eu; envelope_file=$1\n' + gate,
                         "bash", str(envelope)], capture_output=True, text=True,
                        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                             "MAKA_JUDGE_EXPECTED_BOUNDARY": "a" * 64}, timeout=3,
                    )
                    self.assertEqual(completed.returncode == 0, area is None, completed.stderr)

    def test_judge_compose_traverses_runtime_without_mounting_real_secret(self) -> None:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "MAKA_EGRESS_IMAGE_ID": "synthetic-egress",
            "MAKA_CONTROLLER_IMAGE_ID": "synthetic-controller",
            "MAKA_EVAL_RUNTIME_ROOT": "/synthetic/runtime",
            "MAKA_LAB_ROOT": "/synthetic/lab",
            "MAKA_INSTALL_RECEIPT": "/synthetic/receipt.json",
            "MAKA_JUDGE_DECOY": "maka-decoy-synthetic",
            "MAKA_EVAL_SECRET_VOLUME": "synthetic-secret",
        }
        completed = subprocess.run(
            ["docker-compose", "-f", str(JUDGE_COMPOSE), "config", "--format", "json"],
            env=environment, text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        config = json.loads(completed.stdout)
        judge = config["services"]["judge"]
        self.assertEqual(judge.get("cap_drop"), ["ALL"])
        self.assertEqual(judge.get("cap_add"), ["DAC_READ_SEARCH"])
        self.assertTrue(judge["read_only"])
        self.assertIn("no-new-privileges:true", judge["security_opt"])
        self.assertTrue(any(item.startswith("/root:") and all(flag in item for flag in
                             ("noexec", "nosuid", "nodev", "mode=0700"))
                            for item in judge["tmpfs"]))
        self.assertEqual(judge["environment"]["MAKA_RUNTIME_HOST_ELECTION_DEADLINE_MS"], "120000")
        self.assertEqual(set(judge["networks"]), {"judge"})
        self.assertTrue(config["networks"]["judge"]["internal"])
        self.assertNotIn("secret", {volume["source"] for volume in judge["volumes"]})
        self.assertNotIn("/run/maka-secret", {volume["target"] for volume in judge["volumes"]})
        self.assertTrue(all(volume["read_only"] for volume in judge["volumes"]))
        self.assertTrue(all(volume.get("bind", {}).get("create_host_path") is False
                            for volume in judge["volumes"] if volume["type"] == "bind"))
        self.assertEqual(config["services"]["maka-eval-mitmproxy"]["volumes"][0]["source"],
                         "secret")

    def run_harbor_cleanup(self, failing_step: str) -> tuple[int, list[str]]:
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("finish() {\n")
        end = source.index("\n}\ntrap finish EXIT", start) + 2
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            trace = root / "cleanup.log"
            shell = """
set -euo pipefail
probe_writer_pid=
writer_pid=
request_fifo=/synthetic/request.fifo
probe_fifo=/synthetic/probe.fifo
staged_receipt=/synthetic/install-receipt.json
request_dir=/synthetic/request
secret_volume_created=true
secret_volume=synthetic-volume
evidence_dir=$EVIDENCE_DIR
record() {
  printf '%s\\n' "$1" >> "$TRACE"
  test "$FAILING_STEP" != "$1"
}
rm() { record request-files; }
rmdir() { record request-dir; }
cleanup_secret_volume() { record secret-volume; }
finalize_evidence() { record evidence; }
""" + source[start:end] + """
trap finish EXIT
exit 23
"""
            completed = subprocess.run(
                ["/bin/bash", "-c", shell], text=True, capture_output=True,
                env={"PATH": "/usr/bin:/bin", "HOME": "/tmp",
                     "EVIDENCE_DIR": str(evidence), "TRACE": str(trace),
                     "FAILING_STEP": failing_step},
                timeout=3,
            )
            return completed.returncode, trace.read_text().splitlines()

    def test_harbor_cleanup_prioritizes_secret_volume_and_survives_failures(self) -> None:
        expected = ["secret-volume", "request-files", "request-dir", "evidence"]
        for failing_step in ("request-files", "secret-volume"):
            with self.subTest(failing_step=failing_step):
                status, steps = self.run_harbor_cleanup(failing_step)
                self.assertEqual(status, 70)
                self.assertEqual(steps, expected)
        status, steps = self.run_harbor_cleanup("none")
        self.assertEqual(status, 23)
        self.assertEqual(steps, expected)

    def test_missing_live_flag_stops_before_any_setup(self) -> None:
        completed = subprocess.run(
            ["/bin/bash", str(SCRIPT), "-"],
            input="{}", text=True, capture_output=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
            timeout=3,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("explicit live", completed.stderr.lower())

    def test_missing_paid_run_selector_stops_before_any_setup(self) -> None:
        environment = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
        environment.pop("MAKA_JUDGE_PAID_RUN", None)
        completed = subprocess.run(
            ["/bin/bash", str(SCRIPT), "--live", "-"],
            input="{}", text=True, capture_output=True,
            env=environment, timeout=3,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("paid run", completed.stderr.lower())

    def test_boundary_probe_is_nonpaid_but_requires_the_installed_component(self) -> None:
        completed = subprocess.run(
            ["/bin/bash", str(SCRIPT), "--boundary"],
            input="", text=True, capture_output=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}, timeout=3,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("private installed", completed.stderr.lower())
        self.assertNotIn("paid run", completed.stderr.lower())

    def test_core_limit_preflight_preserves_judge_request_stdin(self) -> None:
        shell = """
colima() { /bin/cat >/dev/null; }
source "$1"
assert_maka_daemon_core_limits >/dev/null
/bin/cat
"""
        completed = subprocess.run(
            ["/bin/bash", "-c", shell, "bash", str(LIB)],
            input="judge request", text=True, capture_output=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
            timeout=3,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "judge request")

    def test_stale_guest_source_is_rejected_before_request_capture(self) -> None:
        shell = """
colima() { printf '%064d\n' 0; }
source "$1"
assert_maka_guest_source_parity
"""
        completed = subprocess.run(
            ["/bin/bash", "-c", shell, "bash", str(LIB)],
            input="judge request", text=True, capture_output=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp",
                 "MAKA_LAB_ROOT": str(COMPONENT)},
            timeout=5,
        )
        self.assertEqual(completed.returncode, 78)
        self.assertIn("guest mount differs", completed.stderr)

    def test_matching_guest_source_preserves_request_stdin(self) -> None:
        shell = """
colima() {
  /bin/cat >/dev/null
  /usr/bin/python3 "$MAKA_LAB_ROOT/scripts/harbor_judge_input.py" source-digest "$MAKA_LAB_ROOT"
}
source "$1"
assert_maka_guest_source_parity
/bin/cat
"""
        completed = subprocess.run(
            ["/bin/bash", "-c", shell, "bash", str(LIB)],
            input="judge request", text=True, capture_output=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp",
                 "MAKA_LAB_ROOT": str(COMPONENT)},
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "judge request")

    def test_guest_dns_preflight_fails_before_request_capture(self) -> None:
        shell = """
colima() {
  /bin/cat >/dev/null
  if test "$FAKE_DNS" = ok; then
    printf '203.0.113.1 STREAM api.openai.com\\n'
  else
    return 2
  fi
}
source "$1"
assert_maka_guest_provider_dns
/bin/cat
"""
        for mode, expected_status in (("ok", 0), ("fail", 78)):
            with self.subTest(mode=mode):
                completed = subprocess.run(
                    ["/bin/bash", "-c", shell, "bash", str(LIB)],
                    input="uncaptured judge request", text=True, capture_output=True,
                    env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "FAKE_DNS": mode},
                    timeout=3,
                )
                self.assertEqual(completed.returncode, expected_status, completed.stderr)
                if mode == "ok":
                    self.assertEqual(completed.stdout, "uncaptured judge request")
                else:
                    self.assertIn("guest DNS", completed.stderr)
                    self.assertNotIn("uncaptured judge request", completed.stdout)

    def test_staged_receipt_is_private_and_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "install-receipt.json"
            source.write_bytes(b'{"schema":"khenrix-maka-install-v2"}\n')
            source.chmod(0o600)
            private = root / "request"
            private.mkdir(mode=0o700)
            staged = private / source.name
            completed = subprocess.run(
                ["/bin/bash", "-c",
                 'set -e; source "$1"; stage_maka_install_receipt "$2" "$3"',
                 "bash", str(LIB), str(source), str(staged)],
                text=True, capture_output=True,
                env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}, timeout=3,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(staged.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)

    def test_guest_probe_writer_preserves_uncaptured_request_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            commands = root / "colima-args"
            colima = root / "colima"
            colima.write_text("""#!/bin/sh
printf '%s\\n' "$@" > "$COLIMA_ARGS"
/bin/cat >/dev/null
""")
            colima.chmod(0o755)
            shell = """
source "$1"
maka_guest_fifo_write probe-write "$2" &
wait "$!"
/bin/cat
"""
            completed = subprocess.run(
                ["/bin/bash", "-c", shell, "bash", str(LIB), "/guest/probe.fifo"],
                input="uncaptured judge request", text=True, capture_output=True,
                env={"PATH": f"{root}:/usr/bin:/bin", "HOME": "/tmp",
                     "MAKA_LAB_ROOT": str(COMPONENT), "COLIMA_ARGS": str(commands)},
                timeout=3,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "uncaptured judge request")
            self.assertEqual(commands.read_text().splitlines(), [
                "ssh", "--profile", "maka-amd64", "--", "python3",
                str(COMPONENT / "scripts/harbor_judge_input.py"),
                "probe-write", "/guest/probe.fifo",
            ])

    def test_guest_request_writer_streams_validated_base64_over_ssh_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            commands = root / "colima-args"
            colima = root / "colima"
            colima.write_text("""#!/bin/sh
printf '%s\\n' "$@" > "$COLIMA_ARGS"
/usr/bin/python3 "$MAKA_LAB_ROOT/scripts/harbor_judge_input.py" digest
""")
            colima.chmod(0o755)
            payload = json.dumps({
                "panel_id": "harbor", "criterion": "Synthetic transport check",
                "answer_a": "A", "answer_b": "B",
                "expected_boundary_sha256": "a" * 64,
            }).encode()
            shell = """
source "$1"
maka_guest_fifo_write deliver "$2"
"""
            completed = subprocess.run(
                ["/bin/bash", "-c", shell, "bash", str(LIB), "/guest/request.fifo"],
                input=base64.b64encode(payload), capture_output=True,
                env={"PATH": f"{root}:/usr/bin:/bin", "HOME": "/tmp",
                     "MAKA_LAB_ROOT": str(COMPONENT), "COLIMA_ARGS": str(commands)},
                timeout=3,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stdout.strip().decode(), hashlib.sha256(payload).hexdigest())
            self.assertEqual(commands.read_text().splitlines(), [
                "ssh", "--profile", "maka-amd64", "--", "python3",
                str(COMPONENT / "scripts/harbor_judge_input.py"),
                "deliver", "/guest/request.fifo",
            ])

    def test_missing_guest_writer_cannot_block_controller_fifo_open_forever(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            docker_run_marker = root / "docker-run"
            docker = fake_bin / "docker"
            docker.write_text("""#!/bin/sh
if test "$1" = image && test "$2" = inspect; then
  case "$3" in
    maka-eval-egress-proxy:*) printf '%s\\n' egress-id; exit 0 ;;
    maka-lab-controller:*) printf '%s\\n' controller-id; exit 0 ;;
  esac
fi
if test "$1" = compose; then
  case " $* " in *' run '*) /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}' > "$DOCKER_RUN_MARKER" ;; esac
  exit 0
fi
exit 1
""")
            docker.chmod(0o755)
            opener = fake_bin / "opener"
            opener.write_text("""#!/usr/bin/python3
import signal
import subprocess
import sys
import time

child = subprocess.Popen(["/usr/bin/python3", *sys.argv[1:]])
try:
    time.sleep(0.5)
    child.send_signal(signal.SIGALRM)
    raise SystemExit(child.wait(timeout=1))
finally:
    if child.poll() is None:
        child.kill()
        child.wait()
""")
            opener.chmod(0o755)
            fifo = root / "request.fifo"
            os.mkfifo(fifo, 0o600)
            receipt = root / "install-receipt.json"
            receipt.write_text("{}")
            environment = {
                "PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": "/tmp",
                "OPENAI_API_KEY": "maka-decoy-test", "MAKA_LAB_ROOT": str(root),
                "MAKA_JUDGE_REQUEST_FILE": str(fifo),
                "MAKA_JUDGE_EXPECTED_BOUNDARY": "a" * 64,
                "MAKA_EVAL_SECRET_VOLUME": "synthetic-volume",
                "MAKA_EVIDENCE_DIR": str(root),
                "MAKA_EVAL_RUNTIME_ROOT": str(root / "runtime"),
                "MAKA_EVAL_MAKA_BUNDLE_PATH": str(root / "runtime/maka-agent"),
                "MAKA_INSTALL_RECEIPT": str(receipt),
                "MAKA_CONTROLLER_IMAGE_ID": "controller-id",
                "MAKA_EGRESS_IMAGE_ID": "egress-id",
                "MAKA_EVAL_HARBOR_PYTHON": str(opener),
                "DOCKER_RUN_MARKER": str(docker_run_marker),
            }
            started = time.monotonic()
            completed = subprocess.run(
                ["/bin/bash", str(CONTROLLER_SCRIPT)], cwd=root,
                env=environment, stdin=subprocess.DEVNULL,
                text=True, capture_output=True, timeout=2,
            )
            self.assertEqual(completed.returncode, 78, completed.stderr)
            self.assertIn("FIFO writer unavailable", completed.stderr)
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(docker_run_marker.exists())

            payload = b"synthetic FIFO request"
            writer = subprocess.Popen([
                "/usr/bin/python3", "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).open('wb').write(sys.argv[2].encode())",
                str(fifo), payload.decode(),
            ])
            try:
                completed = subprocess.run(
                    ["/bin/bash", str(CONTROLLER_SCRIPT)], cwd=root,
                    env={**environment, "MAKA_EVAL_HARBOR_PYTHON": "/usr/bin/python3"},
                    stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=3,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(writer.wait(timeout=3), 0)
                self.assertEqual(docker_run_marker.read_text().strip(),
                                 hashlib.sha256(payload).hexdigest())
            finally:
                if writer.poll() is None:
                    writer.kill()
                    writer.wait(timeout=3)

    def test_background_writer_pid_is_the_colima_process_for_cleanup(self) -> None:
        for mode in ("probe-write", "deliver"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                colima = root / "colima"
                colima.write_text("""#!/usr/bin/python3
import os
import pathlib
import signal
import sys

pathlib.Path(os.environ["FAKE_COLIMA_PID"]).write_text(str(os.getpid()))
signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
signal.pause()
""")
                colima.chmod(0o755)
                pid_file = root / "colima-pid"
                shell = """
source "$1"
if test "$2" = deliver; then
  printf '%s' c3ludGhldGlj | maka_guest_fifo_write deliver /guest/request.fifo &
else
  maka_guest_fifo_write probe-write /guest/probe.fifo &
fi
writer_pid=$!
printf '%s\\n' "$writer_pid"
wait "$writer_pid"
"""
                process = subprocess.Popen(
                    ["/bin/bash", "-c", shell, "bash", str(LIB), mode],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env={"PATH": f"{root}:/usr/bin:/bin", "HOME": "/tmp",
                         "MAKA_LAB_ROOT": str(COMPONENT), "FAKE_COLIMA_PID": str(pid_file)},
                )
                try:
                    for _ in range(100):
                        if pid_file.exists():
                            break
                        time.sleep(0.01)
                    self.assertTrue(pid_file.exists(), "fake Colima did not start")
                    assert process.stdout is not None
                    writer_pid = int(process.stdout.readline().strip())
                    self.assertEqual(writer_pid, int(pid_file.read_text()))
                    os.kill(writer_pid, signal.SIGTERM)
                    _stdout, stderr = process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 0, stderr)
                finally:
                    if pid_file.exists():
                        try:
                            os.kill(int(pid_file.read_text()), signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
