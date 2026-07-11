import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # shared/lib on path
from wikisync.sources.instagram_export import read_export  # noqa: E402
from wikisync.sources.instagram_live import normalize_live  # noqa: E402

FX = Path(__file__).parent / "fixtures"


class TestExport(unittest.TestCase):
    def setUp(self):
        self.snap = read_export(FX / "saved_posts.json")

    def test_complete_channel(self):
        self.assertEqual(self.snap.channel, "instagram-saved")
        self.assertEqual(self.snap.status, "complete")

    def test_items_have_shortcodes(self):
        self.assertEqual(len(self.snap.items), 2)
        self.assertEqual({it.native_id for it in self.snap.items},
                         {"DajH0TsShpP", "AbC123defG"})
        for it in self.snap.items:
            self.assertTrue(it.canonical_url.startswith("https://www.instagram.com/"))

    def test_timestamp_to_iso(self):
        it = next(i for i in self.snap.items if i.native_id == "DajH0TsShpP")
        self.assertTrue(it.added_at.startswith("20"))

    def test_missing_file_unavailable(self):
        snap = read_export(FX / "does-not-exist.json")
        self.assertEqual(snap.status, "unavailable")


class TestLive(unittest.TestCase):
    def setUp(self):
        self.arr = json.loads((FX / "ig_live.json").read_text())

    def test_partial_stays_partial(self):
        snap = normalize_live(self.arr, run_status="partial")
        self.assertEqual(snap.status, "partial")           # so ledger blocks removals
        self.assertEqual(len(snap.items), 2)
        self.assertEqual(snap.items[0].native_id, "DajH0TsShpP")   # igsh stripped

    def test_complete_when_observed(self):
        snap = normalize_live(self.arr, run_status="complete")
        self.assertEqual(snap.status, "complete")

    def test_malformed_is_failed(self):
        snap = normalize_live("not-a-list", run_status="complete")
        self.assertEqual(snap.status, "failed")
        self.assertEqual(snap.items, [])

    def test_empty_is_failed_not_authoritative(self):
        # An empty live scrape must NEVER read as an authoritative "you saved nothing".
        snap = normalize_live([], run_status="complete")
        self.assertEqual(snap.status, "failed")

    def test_bad_run_status_defaults_partial(self):
        snap = normalize_live(self.arr, run_status="garbage")
        self.assertEqual(snap.status, "partial")


if __name__ == "__main__":
    unittest.main()
