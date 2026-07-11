import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # shared/lib on path
from wikisync.ledger import Ledger  # noqa: E402
from wikisync.sources.bookmarks import read_bookmarks  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "Bookmarks"
CARBONARA = "https://ex.com/carbonara"


class TestReadBookmarks(unittest.TestCase):
    def setUp(self):
        self.snap = read_bookmarks(FIXTURE)

    def test_complete_snapshot(self):
        self.assertEqual(self.snap.status, "complete")
        self.assertEqual(self.snap.channel, "chrome-bookmarks")

    def test_one_item_per_node(self):
        self.assertEqual(len(self.snap.items), 4)               # 4 url nodes
        self.assertEqual(len({it.native_id for it in self.snap.items}), 4)  # distinct GUIDs

    def test_dup_url_two_nodes_two_folders(self):
        dup = [it for it in self.snap.items if it.canonical_url == CARBONARA]
        self.assertEqual(len(dup), 2)                           # two distinct GUID nodes
        self.assertEqual({it.collection for it in dup},
                         {"Bokmärkesfältet/Food/Main/Italian", "Bokmärkesfältet/Favorites"})

    def test_tracking_stripped_semantic_kept(self):
        tracked = next(it for it in self.snap.items if "deal" in it.canonical_url)
        self.assertNotIn("utm_", tracked.canonical_url)
        self.assertIn("v=42", tracked.canonical_url)

    def test_webkit_date_converted(self):
        dated = next(it for it in self.snap.items if it.native_id == "g-carbonara")
        self.assertTrue(dated.added_at.startswith("20"))        # ISO year
        zero = next(it for it in self.snap.items if it.native_id == "g-tracked")
        self.assertEqual(zero.added_at, "")                     # date_added "0" → empty

    def test_ledger_dedups_page_not_occurrence(self):
        # Two GUID nodes, same URL → two occurrences, but they own ONE page.
        L = Ledger(":memory:")
        for it in self.snap.items:
            L.observe(chan=self.snap.channel, native_id=it.native_id,
                      url=it.canonical_url, collection=it.collection)
        self.assertEqual(L.item_count(), 4)
        pid = L.record_page(path="wiki/recipes/carbonara.md", source_url=CARBONARA)
        self.assertFalse(L.page_removable(pid))                 # both occurrences active


if __name__ == "__main__":
    unittest.main()
