"""Bounded Harbor ballots stay in process memory and a one-use FIFO."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import time
import unittest


HELPER = pathlib.Path(__file__).with_name("harbor_judge_input.py")
PYTHON = "/usr/bin/python3"


def ballot() -> bytes:
    return json.dumps({
        "panel_id": "harbor", "criterion": "Which answer is correct?",
        "answer_a": "secret answer A", "answer_b": "secret answer B",
        "expected_boundary_sha256": "a" * 64,
    }).encode()


class HarborJudgeInputTests(unittest.TestCase):
    def test_source_digest_matches_installer_manifest_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "install-files.txt").write_text(
                "install-files.txt\npayload.txt\n", encoding="utf-8"
            )
            (root / "payload.txt").write_bytes(b"installed source\n")
            digest = hashlib.sha256()
            for name in ("install-files.txt", "payload.txt"):
                digest.update(name.encode() + b"\0")
                digest.update((root / name).read_bytes())
                digest.update(b"\0")
            completed = subprocess.run(
                [PYTHON, str(HELPER), "source-digest", str(root)],
                text=True, capture_output=True, timeout=3,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), digest.hexdigest())

    def test_synthetic_fifo_probe_round_trips_without_a_ballot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = pathlib.Path(directory, "probe.fifo")
            os.mkfifo(fifo, 0o600)
            with subprocess.Popen(
                [PYTHON, str(HELPER), "probe-write", str(fifo)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as writer:
                reader = subprocess.run(
                    [PYTHON, str(HELPER), "probe-read", str(fifo)],
                    capture_output=True, timeout=5,
                )
                self.assertEqual(reader.returncode, 0, reader.stderr.decode())
                self.assertEqual(reader.stdout, b"maka-fifo-probe-v1\n")
                self.assertEqual(writer.wait(timeout=5), 0)
            self.assertEqual([entry.name for entry in pathlib.Path(directory).iterdir()],
                             ["probe.fifo"])

    def test_fragmented_pipe_is_read_to_eof_before_validation(self) -> None:
        payload = ballot()
        with subprocess.Popen(
            [PYTHON, str(HELPER), "capture"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as child:
            assert child.stdin is not None
            child.stdin.write(payload[:8])
            child.stdin.flush()
            time.sleep(0.03)
            child.stdin.write(payload[8:])
            child.stdin.close()
            assert child.stdout is not None and child.stderr is not None
            output = child.stdout.read()
            error = child.stderr.read()
            self.assertEqual(child.wait(timeout=3), 0, error.decode())
            self.assertEqual(base64.b64decode(output, validate=True), payload)

    def test_oversized_request_is_rejected_before_any_fifo_delivery(self) -> None:
        payload = ballot() + b" " * 131073
        completed = subprocess.run(
            [PYTHON, str(HELPER), "capture"], input=payload,
            capture_output=True, timeout=3,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"too large", completed.stderr.lower())

    def test_fifo_delivery_leaves_no_regular_answer_archive(self) -> None:
        payload = ballot()
        captured = subprocess.run(
            [PYTHON, str(HELPER), "capture"], input=payload,
            capture_output=True, check=True, timeout=3,
        ).stdout
        digest = subprocess.run(
            [PYTHON, str(HELPER), "digest"], input=captured,
            capture_output=True, check=True, timeout=3,
        ).stdout.strip()
        self.assertEqual(digest.decode(), hashlib.sha256(payload).hexdigest())
        boundary = subprocess.run(
            [PYTHON, str(HELPER), "boundary"], input=captured,
            capture_output=True, check=True, timeout=3,
        ).stdout.strip()
        self.assertEqual(boundary, b"a" * 64)
        with tempfile.TemporaryDirectory() as directory:
            fifo = pathlib.Path(directory, "judge-request.fifo")
            os.mkfifo(fifo, 0o600)
            with subprocess.Popen(
                [PYTHON, str(HELPER), "deliver", str(fifo)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as writer:
                assert writer.stdin is not None
                writer.stdin.write(captured)
                writer.stdin.close()
                with fifo.open("rb") as stream:
                    self.assertEqual(stream.read(), payload)
                self.assertEqual(writer.wait(timeout=3), 0)
            self.assertEqual([entry.name for entry in pathlib.Path(directory).iterdir()],
                             ["judge-request.fifo"])
            self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))


if __name__ == "__main__":
    unittest.main()
