import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # shared/lib on path
from wikisync.ledger import Ledger  # noqa: E402
from wikisync.sources import Snapshot, SourceItem  # noqa: E402


def _snap(status, items=()):
    return Snapshot(channel="bm", scope="all", status=status, items=list(items))


class TestSnapshotCompleteness(unittest.TestCase):
    def test_partial_snapshot_no_removals(self):
        L = Ledger(":memory:")
        L.upsert_item(chan="bm", native_id="g1", url="u1")
        self.assertEqual(L.plan_diff(_snap("partial", [])).removable, [])

    def test_failed_and_unavailable_never_remove(self):
        L = Ledger(":memory:")
        L.upsert_item(chan="bm", native_id="g1", url="u1")
        self.assertEqual(L.plan_diff(_snap("failed", [])).removable, [])
        self.assertEqual(L.plan_diff(_snap("unavailable", [])).removable, [])

    def test_complete_snapshot_removes_absent(self):
        L = Ledger(":memory:")
        L.upsert_item(chan="bm", native_id="g1", url="u1")
        diff = L.plan_diff(_snap("complete", []))
        self.assertEqual([r.native_id for r in diff.removable], ["g1"])


class TestDiffClassification(unittest.TestCase):
    def test_new_and_updated(self):
        L = Ledger(":memory:")
        L.upsert_item(chan="bm", native_id="known", url="u_known")
        snap = _snap("complete", [
            SourceItem(native_id="known", canonical_url="u_known"),
            SourceItem(native_id="fresh", canonical_url="u_fresh"),
        ])
        diff = L.plan_diff(snap)
        self.assertEqual([i.native_id for i in diff.new], ["fresh"])
        self.assertEqual([i.native_id for i in diff.updated], ["known"])
        self.assertEqual(diff.removable, [])

    def test_reappeared(self):
        L = Ledger(":memory:")
        L.upsert_item(chan="bm", native_id="g1", url="u1")
        L.deactivate_item(chan="bm", native_id="g1")
        snap = _snap("partial", [SourceItem(native_id="g1", canonical_url="u1")])
        diff = L.plan_diff(snap)
        self.assertEqual([i.native_id for i in diff.reappeared], ["g1"])


class TestMembership(unittest.TestCase):
    def test_one_url_two_collections_one_item(self):
        L = Ledger(":memory:")
        L.observe(chan="bm", native_id="g1", url="u1", collection="Food/Main/Italian")
        L.observe(chan="bm", native_id="g1", url="u1", collection="Favorites")
        self.assertEqual(L.item_count(), 1)
        self.assertEqual(set(L.memberships("g1")), {"Food/Main/Italian", "Favorites"})


class TestPageRemoval(unittest.TestCase):
    def test_page_removed_only_when_all_occurrences_inactive(self):
        L = Ledger(":memory:")
        L.upsert_item(chan="bm", native_id="i1", url="u1")
        L.upsert_item(chan="ig", native_id="i2", url="u1")   # same URL, other channel
        pid = L.record_page(path="wiki/recipes/x.md", source_url="u1", generated_hash="h1")
        L.deactivate_item(chan="bm", native_id="i1")
        self.assertFalse(L.page_removable(pid))              # i2 still active
        L.deactivate_item(chan="ig", native_id="i2")
        self.assertTrue(L.page_removable(pid))               # all occurrences inactive


class TestJobStates(unittest.TestCase):
    def test_job_transition_roundtrip(self):
        L = Ledger(":memory:")
        iid = L.upsert_item(chan="bm", native_id="g1", url="u1")
        L.job_transition(iid, "prepared")
        L.job_transition(iid, "fetched")
        L.job_transition(iid, "committed")
        self.assertEqual(L.job_state(iid), "committed")

    def test_bad_state_rejected(self):
        L = Ledger(":memory:")
        iid = L.upsert_item(chan="bm", native_id="g1", url="u1")
        with self.assertRaises(ValueError):
            L.job_transition(iid, "not-a-state")


if __name__ == "__main__":
    unittest.main()
