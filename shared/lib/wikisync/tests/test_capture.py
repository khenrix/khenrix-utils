import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # shared/lib on path
from wikisync.capture import CaptureStore  # noqa: E402
from wikisync.config import load_config  # noqa: E402


class TestCaptureStore(unittest.TestCase):
    def test_roundtrip_and_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            s = CaptureStore(td)
            c1 = s.put("g1", "caption", b'{"t":1}')
            c2 = s.put("g1", "caption", b'{"t":1}')
            self.assertEqual(c1.capture_hash, c2.capture_hash)   # content-addressed
            self.assertEqual(c1.raw_path, c2.raw_path)           # same path, no dup file
            self.assertEqual(s.get(c1.capture_id), b'{"t":1}')

    def test_distinct_content_distinct_capture(self):
        with tempfile.TemporaryDirectory() as td:
            s = CaptureStore(td)
            a = s.put("g1", "caption", b"one")
            b = s.put("g1", "caption", b"two")
            self.assertNotEqual(a.capture_id, b.capture_id)

    def test_latest_returns_a_capture(self):
        with tempfile.TemporaryDirectory() as td:
            s = CaptureStore(td)
            self.assertIsNone(s.latest("g1", "caption"))
            s.put("g1", "caption", b"one")
            self.assertIsNotNone(s.latest("g1", "caption"))

    def test_url_item_id_roundtrips(self):
        # web bookmarks have no short native id → item_id is the full URL (has / and :)
        with tempfile.TemporaryDirectory() as td:
            s = CaptureStore(td)
            c = s.put("https://ex.com/a/b?x=1", "extraction", b'{"k":1}')
            self.assertEqual(c.capture_id.count("/"), 1)          # exactly one separator
            self.assertEqual(s.get(c.capture_id), b'{"k":1}')
            self.assertIsNotNone(s.latest("https://ex.com/a/b?x=1", "extraction"))

    def test_get_unknown_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            s = CaptureStore(td)
            self.assertIsNone(s.get("nope/caption-000000000000"))


class TestConfig(unittest.TestCase):
    def test_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = load_config(Path(td) / "missing.json")
            self.assertTrue(str(cfg.state_dir).endswith("khenrix-wiki-sync"))
            self.assertIsNotNone(cfg.chrome_profile)
            self.assertIn("chrome-bookmarks", cfg.enabled_sources)

    def test_overrides_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text('{"deep_cap": 3, "per_host_cap": 2}')
            cfg = load_config(p)
            self.assertEqual(cfg.deep_cap, 3)
            self.assertEqual(cfg.per_host_cap, 2)


if __name__ == "__main__":
    unittest.main()
