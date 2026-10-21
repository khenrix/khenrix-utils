#!/usr/bin/env python3
"""Execute unchanged answer-only fixture patches in disposable copied directories.

Subprocess timeouts and a minimal environment bound ordinary fixture runs. This is
not an OS sandbox: use only retained answers authorized for local execution.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evals/khenrix-quality/fixtures/shared-id"
DISCLOSURE = "Verification: not executed (answer-only)."
COMMAND = "python3 -m unittest -v"


def _run(argv, cwd, *, input=None):
    environment = {"PATH": os.defpath, "HOME": str(cwd), "LANG": "C.UTF-8",
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(argv, cwd=cwd, env=environment, input=input,
                            text=True, capture_output=True, timeout=20)
    return {"command": argv, "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}


def _patch(answer):
    # The extraction preserves every emitted diff byte, including hunk counts.
    fence = re.compile(r"^```([^`\r\n]*)\r?\n(.*?)^```[ \t]*\r?$",
                       re.MULTILINE | re.DOTALL)
    blocks = list(fence.finditer(answer))
    diffs = [block for block in blocks if block.group(1) in {"diff", "patch"}]
    if len(diffs) != 1 or len(re.findall(r"^```", answer, re.MULTILINE)) != 2 * len(blocks):
        raise ValueError("expected exactly one fenced unified diff; ambiguous code fences")
    command_blocks = [block for block in blocks if block is not diffs[0]]
    if any(block.group(2).strip() != COMMAND for block in command_blocks):
        raise ValueError("additional code fence must contain only the exact test command")
    patch = diffs[0].group(2)
    prose = fence.sub("", answer)
    if re.search(r"^(?:diff --git |--- |\+\+\+ |@@ )", prose, re.MULTILINE):
        raise ValueError("additional unfenced diff makes the answer ambiguous")
    has_command = (bool(command_blocks) or f"`{COMMAND}`" in prose
                   or re.search(r"^[ \t]*" + re.escape(COMMAND) + r"[ \t]*\r?$",
                                prose, re.MULTILINE))
    if DISCLOSURE not in prose or not has_command:
        raise ValueError("missing exact test command or explicit answer-only verification disclosure")
    if re.search(r"\b(?:I|we)\s+(?:ran|executed|tested)\b|\btests?\s+(?:all\s+)?pass(?:ed)?\b|\b(?:all\s+)?tests?\s+(?:were|was)\s+(?:run|executed)\b",
                 prose, re.IGNORECASE):
        raise ValueError("execution claim contradicts answer-only prompt")
    allowed = {p.name for p in FIXTURE.glob("*.py")}
    headers = re.findall(r"^(?:---|\+\+\+) ([^\r\n]+)\r?$", patch, re.MULTILINE)
    if not headers or len(headers) % 2:
        raise ValueError("missing paired unified diff file headers")
    for header in headers:
        # Timestamps, quoting, absolute paths, /dev/null, renames and additions are
        # outside this fixed existing-file fixture contract.
        name = header[2:] if header.startswith(("a/", "b/")) else header
        path = PurePosixPath(name)
        if name not in allowed or path.is_absolute() or ".." in path.parts:
            raise ValueError("patch path escapes or changes an additional file")
    if re.search(r"^(?:new file mode|deleted file mode|old mode|new mode|rename |copy |GIT binary patch|Binary files)", patch, re.MULTILINE):
        raise ValueError("patch must modify existing text files only")
    return patch


def _test_statements(source):
    """Retain original test statements in order while allowing added coverage.

    Setup can grow or change representation; replaying the untouched original suite
    against patched production separately proves its original fixture still works.
    """
    tree = ast.parse(source)
    return {(cls.name, node.name): [ast.dump(statement, include_attributes=False)
                                   for statement in node.body]
            for cls in tree.body if isinstance(cls, ast.ClassDef)
            for node in cls.body if isinstance(node, ast.FunctionDef)
            and node.name.startswith("test")}


def _preserves_test_statements(original, patched):
    for key, statements in original.items():
        additions = iter(patched.get(key, []))
        if not all(any(candidate == statement for candidate in additions)
                   for statement in statements):
            return False
    return True


# Independent contract checks also guard behavior not present in the initial tests.
CONTRACT = '''import unittest
from id_utils import canonical_id
from customers import find_customer
from orders import import_order

class Contract(unittest.TestCase):
    def test_contract(self):
        customers = {"strasse": {"name": "Street"}, "alice": {"name": "Alice"}}
        self.assertEqual(canonical_id(" Straße "), "strasse")
        self.assertEqual(find_customer(customers, " Straße "), {"name": "Street"})
        self.assertEqual(import_order({"customer_id": " Straße ", "quantity": 2}, customers),
                         {"customer_id": "strasse", "customer_name": "Street", "quantity": 2})
        for value in (None, 1, [], {}):
            for call in (lambda: canonical_id(value), lambda: find_customer(customers, value),
                         lambda: import_order({"customer_id": value, "quantity": 1}, customers)):
                with self.assertRaises(TypeError): call()
        for call in (lambda: canonical_id(" "), lambda: find_customer(customers, " "),
                     lambda: import_order({"customer_id": " ", "quantity": 1}, customers)):
            with self.assertRaises(ValueError): call()
        self.assertEqual(find_customer(customers, " ALICE "), {"name": "Alice"})
        self.assertEqual(import_order({"customer_id": " ALICE ", "quantity": 1}, customers)["customer_id"], "alice")
        for quantity in (0, -1, "2"):
            with self.assertRaises(ValueError):
                import_order({"customer_id": "alice", "quantity": quantity}, customers)
        for call in (lambda: find_customer(customers, "missing"),
                     lambda: import_order({"customer_id": "missing", "quantity": 1}, customers)):
            with self.assertRaises(KeyError): call()
'''


# Unittest's text summary cannot distinguish a regression from broken setup.
# Record failures in actual test bodies so a helper-only control can prove cause.
RED_PROBE = '''import json
from pathlib import Path
import sys
import unittest

class RegressionResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        self.method = getattr(getattr(test, test._testMethodName), "__code__", None)

    def record(self, test, error):
        traceback = error[2]
        while traceback is not None:
            if traceback.tb_frame.f_code is getattr(self, "method", None):
                self.test_body_failures.add(test.id())
                break
            traceback = traceback.tb_next

    def addFailure(self, test, error):
        self.record(test, error)
        super().addFailure(test, error)

    def addError(self, test, error):
        self.record(test, error)
        super().addError(test, error)

    def addSubTest(self, test, subtest, error):
        if error is not None:
            self.record(test, error)
        super().addSubTest(test, subtest, error)

    def addSuccess(self, test):
        self.successful_tests.add(test.id())
        super().addSuccess(test)

    def startTestRun(self):
        self.test_body_failures = set()
        self.successful_tests = set()

suite = unittest.defaultTestLoader.discover(".")
result = unittest.TextTestRunner(verbosity=2, resultclass=RegressionResult).run(suite)
Path("red-probe.json").write_text(json.dumps({
    "tests_run": result.testsRun,
    "test_body_failures": sorted(result.test_body_failures),
    "successful_tests": sorted(result.successful_tests)}))
sys.exit(not result.wasSuccessful())
'''


def _regression_run(copied):
    evidence = copied / "red-probe.json"
    evidence.unlink(missing_ok=True)
    result = _run([sys.executable, "-c", RED_PROBE], copied)
    try:
        result["regression"] = json.loads(evidence.read_text())
    except (OSError, json.JSONDecodeError):
        result["regression"] = {}
        result["evidence_error"] = "missing or invalid RED evidence"
    return result


def check_answer(answer: str, fixture: Path = FIXTURE) -> dict:
    """Return auditable strict-apply, original, RED, GREEN and contract records."""
    result = {"accepted": False}
    try:
        patch = _patch(answer)
        result["patch_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="quality-patch-") as temporary:
            copied = Path(temporary) / "fixture"
            shutil.copytree(fixture, copied, ignore=shutil.ignore_patterns("__pycache__"))
            original_sources = {p.name: p.read_bytes() for p in copied.glob("*.py")}
            result["original"] = _run([sys.executable, "-m", "unittest", "-v"], copied)
            if result["original"]["returncode"]:
                raise ValueError("original fixture tests do not pass")
            for phase, args in (("apply_check", ["git", "apply", "--check", "-"]),
                                ("apply", ["git", "apply", "-"])):
                result[phase] = _run(args, copied, input=patch)
                if result[phase]["returncode"]:
                    raise ValueError("unchanged emitted patch failed strict " + phase)
            original_tests = _test_statements(original_sources["test_ids.py"].decode())
            new_tests = _test_statements((copied / "test_ids.py").read_text())
            if not _preserves_test_statements(original_tests, new_tests):
                raise ValueError("original test statement changed or removed")
            result["green"] = _run([sys.executable, "-m", "unittest", "-v"], copied)
            if result["green"]["returncode"]:
                raise ValueError("patched test suite fails GREEN")
            patched_sources = {p.name: p.read_bytes() for p in copied.glob("*.py")}
            (copied / "test_ids.py").write_bytes(original_sources["test_ids.py"])
            result["retained_original"] = _run([sys.executable, "-m", "unittest", "-v"], copied)
            if result["retained_original"]["returncode"]:
                raise ValueError("untouched original test suite fails against patched production")
            (copied / "test_ids.py").write_bytes(patched_sources["test_ids.py"])
            for name, data in original_sources.items():
                if name != "test_ids.py":
                    (copied / name).write_bytes(data)
            result["red"] = _regression_run(copied)
            failures = set(result["red"]["regression"].get("test_body_failures", []))
            if result["red"]["returncode"] == 0 or not failures:
                raise ValueError("new tests do not expose the original implementation bug")
            # Correct only the known original defect, without candidate additions.
            # A matching failed test must now pass; unrelated import/setup errors
            # cannot certify RED, while actual caller KeyError remains valid.
            (copied / "id_utils.py").write_bytes(
                original_sources["id_utils.py"].replace(b".lower()", b".casefold()", 1))
            result["red_control"] = _regression_run(copied)
            successes = set(result["red_control"]["regression"].get("successful_tests", []))
            result["red"]["bug_exposing_tests"] = sorted(failures & successes)
            if not result["red"]["bug_exposing_tests"]:
                raise ValueError("new tests do not expose the original implementation bug")
            for name, data in patched_sources.items():
                (copied / name).write_bytes(data)
            (copied / "acceptance_contract.py").write_text(CONTRACT)
            result["contract"] = _run([sys.executable, "-m", "unittest", "-v", "acceptance_contract"], copied)
            if result["contract"]["returncode"]:
                raise ValueError("patched helper or public callers violate retained contract")
        result.update(accepted=True, reason="strict unchanged patch passed original, RED, GREEN and contract checks")
    except (ValueError, SyntaxError, OSError, subprocess.TimeoutExpired) as error:
        result["reason"] = str(error)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("answer", nargs="?", type=Path, help="retained raw answer.md (never rewritten)")
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    fixture_options = parser.add_mutually_exclusive_group()
    fixture_options.add_argument("--calibration", action="store_true", help="emit semantic judge calibration fixtures; no model calls")
    fixture_options.add_argument("--holdouts", action="store_true", help="emit the separate focused holdout eval specification")
    args = parser.parse_args(argv)
    if args.calibration or args.holdouts:
        name = "calibration.json" if args.calibration else "holdouts.json"
        print((ROOT / "evals/khenrix-quality/fixtures" / name).read_text())
        return 0
    if not args.answer:
        parser.error("answer is required unless --calibration or --holdouts is used")
    result = check_answer(args.answer.read_bytes().decode("utf-8"), args.fixture)
    print(json.dumps(result, indent=2))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
