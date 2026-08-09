import json
import sys
import tempfile
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


class TestEmptyReadCannotAuthoriseRemoval(unittest.TestCase):
    """Only a `complete` snapshot may mark ledger items removable (ledger.plan_diff), and
    `read_bookmarks` used to return `complete` for ANY file that parsed — including one that
    yielded zero items. A wrong profile, a reset, or a half-written file that still parses
    would then present as "the user deleted all 376 bookmarks", and the whole channel landed
    in removable[]."""

    def _snap(self, doc):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "Bookmarks"
            f.write_text(json.dumps(doc), encoding="utf-8")
            return read_bookmarks(f)

    def test_a_clean_parse_with_no_urls_is_partial(self):
        snap = self._snap({"roots": {"bookmark_bar": {"type": "folder", "children": []}}})
        self.assertEqual(snap.status, "partial")
        self.assertEqual(snap.items, [])
        self.assertTrue(any("partial" in e for e in snap.errors), snap.errors)

    def test_a_populated_ledger_survives_an_empty_read(self):
        """THE DISCRIMINATION CHECK, driven through the real `plan_diff`: the status is only
        worth changing if it changes the OUTCOME. Populate the ledger from the real fixture,
        then hand it an empty read — every known item is now absent from the snapshot, which
        is precisely the shape that fills `removable`."""
        led = Ledger(":memory:")
        for it in read_bookmarks(FIXTURE).items:
            led.upsert_item(chan="chrome-bookmarks", native_id=it.native_id,
                            url=it.canonical_url, title=it.title, now="2026-01-01")
        empty = self._snap({"roots": {}})
        diff = led.plan_diff(empty)
        self.assertEqual(empty.status, "partial")
        self.assertEqual(list(diff.removable), [],
                         "an empty read emptied the whole channel — the guard did not hold")

    def test_a_complete_read_that_lost_items_STILL_removes(self):
        """The other half: the guard must not disarm real removals. A COMPLETE snapshot that
        genuinely dropped one item still reports it removable, or the guard would have traded
        a false delete for a permanent leak."""
        led = Ledger(":memory:")
        items = read_bookmarks(FIXTURE).items
        for it in items:
            led.upsert_item(chan="chrome-bookmarks", native_id=it.native_id,
                            url=it.canonical_url, title=it.title, now="2026-01-01")
        snap = read_bookmarks(FIXTURE)
        snap.items.pop()                      # one bookmark genuinely deleted by the user
        self.assertEqual(snap.status, "complete")
        self.assertEqual(len(list(led.plan_diff(snap).removable)), 1)

    def test_a_populated_parse_is_still_complete(self):
        self.assertEqual(read_bookmarks(FIXTURE).status, "complete")


class TestChromeProfileIsDiscovered(unittest.TestCase):
    """The default was one machine's literal path, so `probe` reported bookmarks:false
    everywhere else — including this machine, whose Windows user and profile both differ."""

    def test_default_is_not_a_hardcoded_username(self):
        from wikisync.config import _default_chrome_profile
        got = _default_chrome_profile()
        self.assertNotIn("/chris/", got, "the one-machine literal is back")
        self.assertTrue(got.endswith("Bookmarks"), got)

    def test_it_finds_a_real_profile_when_one_exists(self):
        from wikisync.config import _default_chrome_profile
        roots = list(Path("/mnt/c/Users").glob(
            "*/AppData/Local/Google/Chrome/User Data/*/Bookmarks")) \
            if Path("/mnt/c/Users").is_dir() else []
        if not roots:
            self.skipTest("no Windows Chrome profile on this machine")
        self.assertTrue(Path(_default_chrome_profile()).is_file())
