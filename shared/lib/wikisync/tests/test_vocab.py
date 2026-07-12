import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wikisync.vocab import Vocabulary  # noqa: E402


class TestVocabulary(unittest.TestCase):
    def test_record_is_idempotent_per_tag(self):
        with tempfile.TemporaryDirectory() as td:
            v = Vocabulary(td)
            v.record(["cuisine/japanese", "recipe"])
            v.record(["cuisine/japanese", "recipe"])   # re-commit same page
            data = v.data()
            self.assertEqual(data["cuisine"]["japanese"], 1)   # not inflated to 2
            self.assertEqual(data["_"]["recipe"], 1)           # bare tag -> '_' namespace

    def test_record_adds_new_tags(self):
        with tempfile.TemporaryDirectory() as td:
            v = Vocabulary(td)
            v.record(["cuisine/japanese"])
            v.record(["cuisine/chinese", "diet/vegan"])
            self.assertEqual(set(v.data()["cuisine"]), {"japanese", "chinese"})
            self.assertIn("diet/vegan", v.known())

    def test_rebuild_counts_page_frequency(self):
        with tempfile.TemporaryDirectory() as td:
            v = Vocabulary(td)
            v.record(["cuisine/japanese"])          # seed with count 1
            v.rebuild([["cuisine/japanese"], ["cuisine/japanese"], ["cuisine/chinese"]])
            data = v.data()
            self.assertEqual(data["cuisine"]["japanese"], 2)   # authoritative recount
            self.assertEqual(data["cuisine"]["chinese"], 1)

    def test_dashboard_lists_tags(self):
        with tempfile.TemporaryDirectory() as td:
            v = Vocabulary(td)
            v.rebuild([["cuisine/japanese", "recipe"], ["cuisine/japanese"]])
            md = v.render_dashboard()
            self.assertIn("## cuisine", md)
            self.assertIn("`cuisine/japanese` — 2", md)


if __name__ == "__main__":
    unittest.main()
