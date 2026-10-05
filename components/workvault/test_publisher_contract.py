"""Owner acceptance checks derived from the admitted WorkVault contract."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from curation import parse_publication
from workvault import publish_curated_batch, status


def fixture(commit="1" * 40):
    return {
        "schema_version": "workvault-curated-batch-v1",
        "repository": "example/team-wiki",
        "commit": commit,
        "inventory_complete": True,
        "discovered_paths": ["knowledge/alpha.md", "knowledge/drafts/beta.md", "knowledge/unindexed.md"],
        "indexed_paths": ["knowledge/alpha.md", "knowledge/drafts/beta.md"],
        "requested_paths": ["knowledge/alpha.md", "knowledge/drafts/beta.md"],
        "cards": [
            {"path": "knowledge/alpha.md", "title": "Synthetic Alpha", "summary": "Curated synthetic fact.",
             "details": "No workplace source text.", "date": "2026-10-05", "draft": False,
             "uncertainty": ["Synthetic; not a verified live service fact."],
             "source_links": ["https://example.invalid/source/alpha"],
             "cross_references": ["knowledge/drafts/beta.md"]},
            {"path": "knowledge/drafts/beta.md", "title": "Synthetic Beta", "summary": "Draft synthetic decision.",
             "details": "Keep draft status explicit.", "date": "2026-10-04", "draft": True,
             "uncertainty": [], "source_links": [], "cross_references": ["knowledge/alpha.md"]},
        ],
    }


class WorkVaultContract(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.snapshots = self.root / "snapshots"

    def publish(self, batch=None):
        return publish_curated_batch(self.vault, self.snapshots, batch or fixture())

    def old_bytes(self):
        return (self.vault / "Knowledge.md").read_bytes()

    def tree_state(self):
        observed = {}
        for path in [self.root, *self.root.rglob("*")]:
            info = path.lstat()
            observed[str(path.relative_to(self.root))] = (
                info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
                info.st_ino, info.st_mtime_ns, info.st_ctime_ns,
                path.read_bytes() if stat.S_ISREG(info.st_mode) else None)
        return observed

    def test_empty_status_does_not_create_any_path(self):
        self.assertEqual(status(self.vault), {"schema_version": "workvault-status-v1", "state": "empty"})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_roundtrip_exact_coverage_citations_and_curated_metadata(self):
        self.publish()
        observed = status(self.vault)
        self.assertEqual(observed["state"], "published")
        self.assertEqual(observed["repository"], fixture()["repository"])
        self.assertEqual(observed["published_commit"], "1" * 40)
        self.assertEqual(observed["covered_paths"], ["knowledge/alpha.md", "knowledge/drafts/beta.md"])
        self.assertEqual(observed["uncovered_paths"], ["knowledge/unindexed.md"])
        self.assertEqual(observed["index_gaps"], ["knowledge/unindexed.md"])
        self.assertIs(observed["full_source_coverage"], False)
        self.assertEqual(observed["card_count"], 2)
        self.assertEqual(observed["published_at"], parse_publication(self.old_bytes())["published_at"])
        text = self.old_bytes().decode()
        for card in fixture()["cards"]:
            self.assertIn("https://github.com/example/team-wiki/blob/" + "1" * 40 + "/" + card["path"], text)
            for value in (card["title"], card["summary"], card["details"], card["date"]):
                self.assertIn(value, text)
            for value in card["uncertainty"] + card["source_links"] + card["cross_references"]:
                self.assertIn(value, text)
        self.assertIn("draft", text.lower())

    def test_invalid_batches_leave_previous_publication_unchanged(self):
        self.publish()
        before = self.old_bytes()
        invalid = []
        for key, value in [("raw_source", "must never persist"), ("inventory_complete", False),
                           ("commit", "ABC"), ("repository", "different/repo"),
                           ("requested_paths", ["knowledge/alpha.md"]),
                           ("indexed_paths", ["knowledge/not-discovered.md"])]:
            batch = fixture("2" * 40)
            batch[key] = value
            invalid.append(batch)
        for key, value in [("raw_body", "must never persist"), ("date", "2026-02-30"),
                           ("draft", 1), ("summary", "x" * 5_000_000),
                           ("source_links", ["https://user:password@example.invalid/"]),
                           ("source_links", ["http://example.invalid/"])]:
            batch = fixture("2" * 40)
            batch["cards"][0][key] = value
            invalid.append(batch)
        for batch in invalid:
            with self.subTest(changed=batch.keys()):
                with self.assertRaises(Exception):
                    self.publish(batch)
                self.assertEqual(self.old_bytes(), before)

    def test_changed_selected_set_cannot_drop_old_cards(self):
        self.publish()
        before = self.old_bytes()
        batch = fixture("2" * 40)
        batch["requested_paths"] = ["knowledge/alpha.md"]
        batch["cards"] = batch["cards"][:1]
        with self.assertRaises(Exception):
            self.publish(batch)
        self.assertEqual(self.old_bytes(), before)

    def test_first_publish_has_no_snapshot_then_retains_seven_actual_generations(self):
        self.publish()
        self.assertEqual([path for path in self.snapshots.rglob("*") if not path.is_dir()], [])
        previous = []
        for index in range(2, 11):
            previous.append(self.old_bytes())
            self.publish(fixture(format(index, "040x")))
        files = [path for path in self.snapshots.rglob("*") if not path.is_dir()]
        self.assertEqual(len(files), 7)
        for path in files:
            self.assertTrue(stat.S_ISREG(path.lstat().st_mode))
        self.assertCountEqual([path.read_bytes() for path in files], previous[-7:])
        for path in files:
            checkpoint = parse_publication(path.read_bytes())
            self.assertEqual(checkpoint["schema_version"], "workvault-publication-v1")
            self.assertEqual(checkpoint["batch"]["requested_paths"], fixture()["requested_paths"])

    def test_atomic_publication_failure_preserves_last_good_file(self):
        self.publish()
        before = self.old_bytes()
        replace = os.replace

        def fail_publication(source, destination, *args, **kwargs):
            if Path(destination).name == "Knowledge.md":
                raise OSError("Synthetic publication fault")
            return replace(source, destination, *args, **kwargs)

        with patch("os.replace", side_effect=fail_publication):
            with self.assertRaises(Exception):
                self.publish(fixture("2" * 40))
        self.assertEqual(self.old_bytes(), before)
        self.assertEqual(status(self.vault)["published_commit"], "1" * 40)

    def test_failed_replacements_do_not_rotate_away_real_snapshot_history(self):
        for index in range(1, 9):
            self.publish(fixture(format(index, "040x")))
        before = {p.read_bytes() for p in self.snapshots.iterdir()}
        current = self.old_bytes()
        with patch('os.replace', side_effect=OSError('synthetic replacement fault')):
            for _ in range(7):
                with self.assertRaises(Exception):
                    self.publish(fixture(format(9, '040x')))
        after = [p.read_bytes() for p in self.snapshots.iterdir()]
        self.assertTrue(before <= set(after), 'failed replacements erased prior generations')
        self.assertEqual(len(after), len(set(after)), 'failed retries duplicated current generation')
        self.assertEqual(self.old_bytes(), current)
        self.publish(fixture(format(9, '040x')))
        commits = [parse_publication(p.read_bytes())['batch']['commit'] for p in self.snapshots.iterdir()]
        self.assertCountEqual(commits, [format(i, '040x') for i in range(2, 9)])

    def test_status_never_rewrites_published_files(self):
        self.publish()
        before = self.tree_state()
        status(self.vault)
        self.assertEqual(self.tree_state(), before)

    def test_corrupted_or_changed_rendering_is_not_reported_as_good(self):
        self.publish()
        path = self.vault / "Knowledge.md"
        original = path.read_bytes()
        marker = b"Curated synthetic fact."
        index = original.rfind(marker)
        self.assertGreaterEqual(index, 0)
        mismatched = original[:index] + b"Different alleged fact." + original[index + len(marker):]
        for content in (original[:20], mismatched):
            path.write_bytes(content)
            with self.assertRaises(Exception):
                status(self.vault)

    def test_private_modes_and_unrelated_note_preservation(self):
        self.vault.mkdir(mode=0o700)
        note = self.vault / "Human-note.md"
        note.write_text("Keep this human note unchanged.\n")
        self.publish()
        self.publish(fixture("2" * 40))
        self.assertEqual(note.read_text(), "Keep this human note unchanged.\n")
        self.assertEqual(stat.S_IMODE(self.vault.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.snapshots.stat().st_mode), 0o700)
        for path in [self.vault / "Knowledge.md", *[p for p in self.snapshots.rglob("*") if p.is_file()]]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_broad_preexisting_root_refuses_without_publication(self):
        self.vault.mkdir(mode=0o755)
        self.vault.chmod(0o755)
        with self.assertRaises(Exception):
            self.publish()
        self.assertFalse((self.vault / "Knowledge.md").exists())

    def test_symlink_root_and_managed_file_refuse_without_target_write(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        self.vault.symlink_to(target, target_is_directory=True)
        with self.assertRaises(Exception):
            self.publish()
        self.assertEqual(list(target.iterdir()), [])
        self.vault.unlink()
        self.vault.mkdir(mode=0o700)
        external = self.root / "human.md"
        external.write_text("Human content")
        (self.vault / "Knowledge.md").symlink_to(external)
        with self.assertRaises(Exception):
            self.publish()
        self.assertEqual(external.read_text(), "Human content")

    def test_overlapping_snapshot_root_refuses(self):
        with self.assertRaises(Exception):
            publish_curated_batch(self.vault, self.vault / "snapshots", fixture())
        self.assertFalse((self.vault / "Knowledge.md").exists())

    def test_traversal_card_paths_refuse_without_publication(self):
        for bad in ("knowledge/../private.md", "/knowledge/alpha.md", "knowledge\\alpha.md", "knowledge/a/../alpha.md"):
            batch = fixture()
            for key in ("discovered_paths", "indexed_paths", "requested_paths"):
                batch[key][0] = bad
            batch["cards"][0]["path"] = bad
            with self.subTest(path=bad), self.assertRaises(Exception):
                self.publish(batch)
            self.assertFalse((self.vault / "Knowledge.md").exists())


    def assert_fsync_failure_keeps_last_good(self, target_root, excluded_names=()):
        self.publish()
        before = self.old_bytes()
        original_fsync = os.fsync
        faulted = False

        def fail_target_file_fsync(descriptor):
            nonlocal faulted
            info = os.fstat(descriptor)
            if stat.S_ISREG(info.st_mode):
                for path in target_root.rglob("*"):
                    if path.name in excluded_names or not path.is_file():
                        continue
                    candidate = path.stat()
                    if (candidate.st_dev, candidate.st_ino) == (info.st_dev, info.st_ino):
                        faulted = True
                        raise OSError("Synthetic prepublication fsync fault")
            return original_fsync(descriptor)

        with patch("os.fsync", side_effect=fail_target_file_fsync):
            with self.assertRaises(Exception):
                self.publish(fixture("2" * 40))
        self.assertTrue(faulted, "Required file fsync was not reached")
        self.assertEqual(self.old_bytes(), before)
        self.assertEqual(status(self.vault)["published_commit"], "1" * 40)

    def test_snapshot_file_fsync_failure_keeps_last_good(self):
        self.assert_fsync_failure_keeps_last_good(self.snapshots)

    def test_staged_publication_fsync_failure_keeps_last_good(self):
        self.assert_fsync_failure_keeps_last_good(
            self.vault, excluded_names=("Knowledge.md", ".workvault.lock"))

    def test_broad_snapshot_root_refuses_without_modifying_notes(self):
        self.snapshots.mkdir(mode=0o755)
        self.snapshots.chmod(0o755)
        note = self.snapshots / "operator-note.txt"
        note.write_text("Keep operator note")
        with self.assertRaises(Exception):
            self.publish()
        self.assertFalse((self.vault / "Knowledge.md").exists())
        self.assertEqual(note.read_text(), "Keep operator note")

    def test_hardlinked_managed_file_refuses_without_external_change(self):
        self.publish()
        external = self.root / "external.md"
        os.link(self.vault / "Knowledge.md", external)
        before = external.read_bytes()
        with self.assertRaises(Exception):
            self.publish(fixture("2" * 40))
        self.assertEqual(external.read_bytes(), before)
        self.assertEqual(self.old_bytes(), before)

    def test_status_refuses_symbolic_link_without_creating_any_path(self):
        self.publish()
        managed = self.vault / "Knowledge.md"
        external = self.root / "external.md"
        managed.rename(external)
        managed.symlink_to(external)
        before = external.read_bytes()
        with self.assertRaises(Exception):
            status(self.vault)
        self.assertTrue(managed.is_symlink())
        self.assertEqual(external.read_bytes(), before)

    def test_broad_managed_file_refuses_without_repairing_permissions(self):
        self.publish()
        managed = self.vault / "Knowledge.md"
        before = managed.read_bytes()
        managed.chmod(0o644)
        with self.assertRaises(Exception):
            self.publish(fixture("2" * 40))
        self.assertEqual(managed.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(managed.stat().st_mode), 0o644)

    def test_corrupt_last_good_is_not_silently_replaced(self):
        self.publish()
        managed = self.vault / "Knowledge.md"
        managed.write_bytes(b"Truncated curated checkpoint")
        before = managed.read_bytes()
        with self.assertRaises(Exception):
            self.publish(fixture("2" * 40))
        self.assertEqual(managed.read_bytes(), before)

    def test_corrections_and_archived_source_backlinks_survive_publication(self):
        batch = fixture()
        correction = "Correction: the earlier fixture assumption was uncertain, not confirmed."
        archived = "https://github.com/example/team-wiki/blob/" + "1" * 40 + "/sources/archived-fixture.md"
        batch["cards"][0]["details"] = correction
        batch["cards"][0]["source_links"].append(archived)
        self.publish(batch)
        observed = parse_publication(self.old_bytes())["batch"]
        self.assertEqual(observed, batch)
        self.assertIn(correction.encode(), self.old_bytes())
        self.assertIn(archived.encode(), self.old_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
