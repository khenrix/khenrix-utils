"""Execute unchanged candidate diffs rather than judging patch-shaped prose."""
import difflib
import re
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import eval_patch_acceptance as checker

FIXTURE = ROOT / "evals/khenrix-quality/fixtures/shared-id"


def candidate(*, fix=True, tests=True):
    patches = []
    for name in ("id_utils.py", "test_ids.py"):
        before = (FIXTURE / name).read_text()
        after = before
        if name == "id_utils.py" and fix:
            after = before.replace(".lower()", ".casefold()")
        if name == "test_ids.py" and tests:
            after += '''

class UnicodeTests(unittest.TestCase):
    def test_helper(self):
        self.assertEqual(canonical_id(" Straße "), "strasse")

    def test_customer(self):
        self.assertEqual(find_customer({"strasse": {"name": "S"}}, " Straße "), {"name": "S"})

    def test_order(self):
        self.assertEqual(import_order({"customer_id": " Straße ", "quantity": 1},
                                    {"strasse": {"name": "S"}})["customer_id"], "strasse")
'''
        patches.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                          fromfile="a/" + name, tofile="b/" + name))
    return '```diff\n' + ''.join(patches) + '```\n`python3 -m unittest -v`\nVerification: not executed (answer-only).\n'


def test_unchanged_patch_has_red_and_green_evidence():
    result = checker.check_answer(candidate(), FIXTURE)
    assert result["accepted"], result
    assert result["red"]["returncode"] != 0
    assert result["green"]["returncode"] == 0
    assert result["original"]["returncode"] == 0


@pytest.mark.parametrize("command", [
    "```bash\npython3 -m unittest -v\n```",
    "```sh\npython3 -m unittest -v\n```",
    "```\npython3 -m unittest -v\n```",
    "python3 -m unittest -v",
])
@pytest.mark.parametrize("command_first", [False, True])
def test_accepts_exact_command_outside_diff_without_inline_backticks(command, command_first):
    answer = candidate().replace("`python3 -m unittest -v`\n", "")
    answer = command + "\n" + answer if command_first else answer + command + "\n"
    result = checker.check_answer(answer, FIXTURE)
    assert result["accepted"], result
    assert result["apply_check"]["returncode"] == 0
    assert result["retained_original"]["returncode"] == 0
    assert result["red"]["bug_exposing_tests"]
    assert result["contract"]["returncode"] == 0


def test_command_fence_preserves_every_diff_byte():
    patch = ("--- a/id_utils.py\r\n+++ b/id_utils.py\r\n"
             "@@ -1 +1 @@\r\n-before\r\n+after  \r\n")
    answer = ("```diff\r\n" + patch + "```\r\n"
              "```bash\r\npython3 -m unittest -v\r\n```\r\n"
              "Verification: not executed (answer-only).\r\n")
    assert checker._patch(answer) == patch


@pytest.mark.parametrize("extra", [
    "```diff\n--- a/id_utils.py\n+++ b/id_utils.py\n```\n",
    "```python\nprint('extra code')\n```\n",
    "```bash\npython3 -m unittest -v\necho done\n```\n",
    "```bash\necho before; python3 -m unittest -v\n```\n",
    "```bash\npython3 -m unittest -v\n",
    "```\n",
])
def test_command_fence_does_not_allow_additional_or_unbalanced_code(extra):
    answer = candidate().replace("`python3 -m unittest -v`",
        "```bash\npython3 -m unittest -v\n```") + extra
    result = checker.check_answer(answer, FIXTURE)
    assert not result["accepted"], result
    assert "apply" not in result


@pytest.mark.parametrize("command", [
    "python3 -m unittest",
    "python3 -m unittest -v --failfast",
    "echo python3 -m unittest -v",
    "# python3 -m unittest -v",
])
def test_rejects_nonexact_command_fence(command):
    answer = candidate().replace("`python3 -m unittest -v`",
                                  "```bash\n" + command + "\n```")
    assert not checker.check_answer(answer, FIXTURE)["accepted"]


def test_rejects_missing_command_even_when_diff_contains_it():
    answer = candidate().replace("`python3 -m unittest -v`\n", "")
    answer = answer.replace("+    result =", "+    # python3 -m unittest -v\n+    result =")
    result = checker.check_answer(answer, FIXTURE)
    assert not result["accepted"]
    assert "missing exact test command" in result["reason"]


@pytest.mark.parametrize("damage", ["counts", "escape", "extra", "ambiguous", "unfenced-extra", "false-claim", "missing-claim"])
def test_rejects_unapplyable_or_ambiguous_raw_answer(damage):
    answer = candidate()
    if damage == "counts":
        answer = re.sub(r"@@ -(\d+),(\d+)", r"@@ -\1,9999", answer, count=1)
    elif damage == "escape":
        answer = answer.replace("a/id_utils.py", "a/../id_utils.py").replace("b/id_utils.py", "b/../id_utils.py")
    elif damage == "extra":
        answer = answer.replace("a/id_utils.py", "a/extra.py").replace("b/id_utils.py", "b/extra.py")
    elif damage == "ambiguous":
        answer += '```diff\n--- a/id_utils.py\n+++ b/id_utils.py\n```\n'
    elif damage == "unfenced-extra":
        answer += "--- a/extra.py\n+++ b/extra.py\n@@ -1 +1 @@\n-x\n+y\n"
    elif damage == "false-claim":
        answer += "All tests passed when I ran them.\n"
    else:
        answer = answer.replace("Verification: not executed (answer-only).", "")
    assert not checker.check_answer(answer, FIXTURE)["accepted"]


def test_rejects_no_fix_and_no_regression_and_deleted_original_test():
    assert not checker.check_answer(candidate(fix=False), FIXTURE)["accepted"]
    assert not checker.check_answer(candidate(tests=False), FIXTURE)["accepted"]
    before = (FIXTURE / "test_ids.py").read_text()
    removed = before.replace('        self.assertEqual(canonical_id("  ALICE  "), "alice")', '        pass')
    patch = ''.join(difflib.unified_diff(before.splitlines(True), removed.splitlines(True),
                                        fromfile="a/test_ids.py", tofile="b/test_ids.py"))
    answer = '```diff\n' + patch + '```\n`python3 -m unittest -v`\nVerification: not executed (answer-only).\n'
    result = checker.check_answer(answer, FIXTURE)
    assert not result["accepted"]
    assert "original test" in result["reason"]


def test_acceptance_policy_changes_stale_instrument_quality_criteria_and_tuneup(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts/lib"))
    import checks
    (tmp_path / "capabilities.toml").write_text("")
    policy = tmp_path / "scripts/eval_patch_acceptance.py"
    policy.parent.mkdir(parents=True)
    policy.write_text("# initial acceptance policy\n")
    before = [checks.instrument_manifest(tmp_path),
              checks.criteria_manifest(tmp_path, "khenrix-quality"),
              checks.delivered_behavior_manifest(tmp_path, "skill-tuneup")]
    policy.write_text("# changed acceptance policy\n")
    after = [checks.instrument_manifest(tmp_path),
             checks.criteria_manifest(tmp_path, "khenrix-quality"),
             checks.delivered_behavior_manifest(tmp_path, "skill-tuneup")]
    assert all(old != new for old, new in zip(before, after))


def test_accepts_new_assertions_in_existing_methods_and_extended_setup():
    """Useful tests may grow in place; new method names are not a coverage contract."""
    patches = []
    for name in ("id_utils.py", "test_ids.py"):
        before = (FIXTURE / name).read_text()
        if name == "id_utils.py":
            after = before.replace(".lower()", ".casefold()")
        else:
            after = before.replace('self.customers = {"alice": {"name": "Alice"}}',
                'self.customers = {"alice": {"name": "Alice"}, "strasse": {"name": "S"}}')
            after = after.replace('    def test_canonical_rejects_invalid_input(self):',
                '        self.assertEqual(canonical_id(" Straße "), "strasse")\n\n'
                '    def test_canonical_rejects_invalid_input(self):')
            after = after.replace('    def test_import_order_ascii(self):',
                '        self.assertEqual(find_customer(self.customers, " Straße "), {"name": "S"})\n\n'
                '    def test_import_order_ascii(self):')
            after = after.replace('    def test_lookup_failures(self):',
                '        self.assertEqual(import_order({"customer_id": " Straße ", "quantity": 1},\n'
                '                                      self.customers)["customer_id"], "strasse")\n\n'
                '    def test_lookup_failures(self):')
        patches.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                          fromfile="a/" + name, tofile="b/" + name))
    answer = '```diff\n' + ''.join(patches) + '```\n`python3 -m unittest -v`\nVerification: not executed (answer-only).\n'
    result = checker.check_answer(answer, FIXTURE)
    assert result["accepted"], result
    assert result["retained_original"]["returncode"] == 0
    assert result["red"]["returncode"] != 0


APPENDIX_A_ANSWER = '''```diff
--- a/id_utils.py
+++ b/id_utils.py
@@ -1,7 +1,9 @@
 def canonical_id(value):
     if not isinstance(value, str):
         raise TypeError("ID must be a string")
-    result = value.strip().lower()
+    result = value.strip().casefold()
     if not result:
         raise ValueError("ID must not be blank")
     return result
+
+REGRESSION_READY = True
--- a/test_ids.py
+++ b/test_ids.py
@@ -42,3 +42,8 @@
\x20
 if __name__ == "__main__":
     unittest.main()
+
+class SetupOnlyTests(unittest.TestCase):
+    def test_marker(self):
+        from id_utils import REGRESSION_READY
+        self.assertTrue(REGRESSION_READY)
```
`python3 -m unittest -v`
Verification: not executed (answer-only).
'''


def test_rejects_exact_review_import_only_red():
    result = checker.check_answer(APPENDIX_A_ANSWER, FIXTURE)
    assert not result["accepted"], result
    assert result["green"]["returncode"] == 0
    assert result["retained_original"]["returncode"] == 0
    assert "ImportError" in result["red"]["stderr"]
    assert "original implementation bug" in result["reason"]


def extra_test_candidate(test_source, *, production_suffix=""):
    patches = []
    for name in ("id_utils.py", "test_ids.py"):
        before = (FIXTURE / name).read_text()
        after = (before.replace(".lower()", ".casefold()") + production_suffix
                 if name == "id_utils.py" else before + "\n\n" + test_source)
        patches.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                          fromfile="a/" + name, tofile="b/" + name))
    return ('```diff\n' + ''.join(patches) + '```\n`python3 -m unittest -v`\n'
            'Verification: not executed (answer-only).\n')


@pytest.mark.parametrize("call", [
    'find_customer({"strasse": {"name": "S"}}, " Straße ")',
    'import_order({"customer_id": " Straße ", "quantity": 1}, {"strasse": {"name": "S"}})',
])
def test_accepts_public_caller_regression_with_only_keyerror_red(call):
    answer = extra_test_candidate(
        'class UnicodeCallerTests(unittest.TestCase):\n'
        '    def test_unicode(self):\n'
        f'        self.assertIsNotNone({call})\n')
    result = checker.check_answer(answer, FIXTURE)
    assert result["accepted"], result
    assert "KeyError: 'straße'" in result["red"]["stderr"]
    assert "FAILED (errors=1)" in result["red"]["stderr"]
    assert result["contract"]["returncode"] == 0


@pytest.mark.parametrize("test_source", [
    'from id_utils import REGRESSION_READY\n',
    'def setUpModule():\n    from id_utils import REGRESSION_READY\n',
    'class SetupTests(unittest.TestCase):\n'
    '    def setUp(self):\n        from id_utils import REGRESSION_READY\n'
    '    def test_marker(self):\n        self.assertTrue(True)\n',
    'class SetupTests(unittest.TestCase):\n'
    '    @classmethod\n    def setUpClass(cls):\n        from id_utils import REGRESSION_READY\n'
    '    def test_marker(self):\n        self.assertTrue(True)\n',
    'class SetupTests(unittest.TestCase):\n'
    '    def setUp(self):\n        self.assertEqual(canonical_id("Straße"), "strasse")\n'
    '    def test_marker(self):\n        self.assertTrue(True)\n',
    'class SkippedTests(unittest.TestCase):\n'
    '    @unittest.skip("not executed")\n'
    '    def test_unicode(self):\n        self.assertEqual(canonical_id("Straße"), "strasse")\n',
    'class MarkerTests(unittest.TestCase):\n'
    '    def test_marker(self):\n'
    '        self.assertEqual(canonical_id("ALICE"), "alice")\n'
    '        self.assertTrue(getattr(__import__("id_utils"), "REGRESSION_READY", False))\n',
    'class UnicodeMarkerTests(unittest.TestCase):\n'
    '    def test_marker(self):\n'
    '        canonical_id("Straße")\n'
    '        self.assertTrue(getattr(__import__("id_utils"), "REGRESSION_READY", False))\n',
    'class ImportTests(unittest.TestCase):\n'
    '    def test_unicode_then_import(self):\n'
    '        canonical_id("Straße")\n'
    '        from id_utils import REGRESSION_READY\n',
    'class TeardownTests(unittest.TestCase):\n'
    '    def test_unicode(self):\n        canonical_id("Straße")\n'
    '    def tearDown(self):\n'
    '        self.assertTrue(getattr(__import__("id_utils"), "REGRESSION_READY", False))\n',
])
def test_rejects_red_without_executed_bug_exposing_test_body(test_source):
    answer = extra_test_candidate(test_source, production_suffix="\nREGRESSION_READY = True\n")
    result = checker.check_answer(answer, FIXTURE)
    assert not result["accepted"], result
    assert result["green"]["returncode"] == 0
    assert "original implementation bug" in result["reason"]


def test_rejects_zero_discovered_tests_during_green():
    answer = extra_test_candidate(
        'def load_tests(loader, tests, pattern):\n    return unittest.TestSuite()\n')
    result = checker.check_answer(answer, FIXTURE)
    assert not result["accepted"], result
    assert result["green"]["returncode"] == 5
    assert "Ran 0 tests" in result["green"]["stderr"]
    assert "GREEN" in result["reason"]
    assert "red" not in result


def test_accepts_unicode_regression_in_subtest():
    answer = extra_test_candidate(
        'class UnicodeSubTests(unittest.TestCase):\n'
        '    def test_unicode(self):\n'
        '        with self.subTest(value="Straße"):\n'
        '            self.assertEqual(canonical_id("Straße"), "strasse")\n')
    result = checker.check_answer(answer, FIXTURE)
    assert result["accepted"], result
    assert result["red"]["bug_exposing_tests"] == [
        "test_ids.UnicodeSubTests.test_unicode"]


def test_rejects_interrupted_red_and_retains_raw_phase():
    answer = extra_test_candidate(
        'class InterruptedTests(unittest.TestCase):\n'
        '    def test_unicode(self):\n'
        '        if canonical_id("Straße") != "strasse":\n'
        '            __import__("os")._exit(1)\n')
    result = checker.check_answer(answer, FIXTURE)
    assert not result["accepted"], result
    assert result["red"]["returncode"] == 1
    assert "original implementation bug" in result["reason"]


def test_genuine_red_remains_valid_alongside_unrelated_import_error():
    answer = extra_test_candidate(
        'class MixedTests(unittest.TestCase):\n'
        '    def test_unicode_customer(self):\n'
        '        self.assertEqual(find_customer({"strasse": {"name": "S"}}, "Straße"),\n'
        '                         {"name": "S"})\n'
        '    def test_marker(self):\n'
        '        from id_utils import REGRESSION_READY\n'
        '        self.assertTrue(REGRESSION_READY)\n',
        production_suffix="\nREGRESSION_READY = True\n")
    result = checker.check_answer(answer, FIXTURE)
    assert result["accepted"], result
    assert result["red"]["bug_exposing_tests"] == ["test_ids.MixedTests.test_unicode_customer"]
    assert result["red_control"]["returncode"] != 0
    assert "ImportError" in result["red_control"]["stderr"]
