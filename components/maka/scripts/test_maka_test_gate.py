"""The broad regression route requires an explicit, unpatched Harbor baseline."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


RUNNER = pathlib.Path(__file__).with_name("test.sh")


class TestGateTests(unittest.TestCase):
    def test_missing_or_current_baseline_refuses_before_any_test_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            launched = root / "launched"
            binaries = root / "bin"
            binaries.mkdir()
            for name in ("mise", "node"):
                executable = binaries / name
                executable.write_text(
                    f"#!/bin/sh\n: > {str(launched)!r}\nexit 83\n", encoding="utf-8"
                )
                executable.chmod(0o700)
            python = binaries / "python"
            python.symlink_to(sys.executable)
            current = root / "current package"
            current.mkdir()
            (current / "package.json").write_text(json.dumps({
                "name": "maka-agent", "version": "0.2.0-dev.70.20261005",
            }))
            drifted = root / "drifted package"
            drifted.mkdir()
            (drifted / "package.json").write_text(json.dumps({
                "name": "maka-agent", "version": "0.2.0-dev.62.20260928",
            }))
            for relative in (
                "node_modules/@maka/eval/harbor/relay_agent.py",
                "node_modules/@maka/runtime/dist/model-fetcher.js",
                "node_modules/@maka/eval/dist/maka-subject.js",
                "node_modules/@maka/core/dist/model-metadata.js",
                "node_modules/@maka/runtime/node_modules/undici/lib/dispatcher/client-h2.js",
            ):
                target = drifted / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"unreviewed source drift\n")
            linked = root / "linked package"
            linked.symlink_to(drifted, target_is_directory=True)
            for arguments in (
                [], ["--broad"], ["--baseline-package-root", str(current)],
                ["--baseline-package-root", str(drifted)],
                ["--baseline-package-root", str(linked)],
            ):
                with self.subTest(arguments=arguments):
                    completed = subprocess.run(
                        ["/bin/bash", str(RUNNER), *arguments],
                        env={**os.environ, "PATH": f"{binaries}:/usr/bin:/bin"},
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(completed.returncode, 78, completed.stderr)
                    self.assertIn("unpatched .62 baseline", completed.stderr)
                    self.assertFalse(launched.exists(), "test/discovery started before baseline validation")


if __name__ == "__main__":
    unittest.main()
