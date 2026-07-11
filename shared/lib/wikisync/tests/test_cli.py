import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # shared/lib on path
from wikisync import cli  # noqa: E402
from wikisync.config import Config  # noqa: E402
from wikisync.ledger import Ledger  # noqa: E402
from wikisync.capture import CaptureStore  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
NOW = "2026-07-12T00:00:00+00:00"


def _ctx(td):
    cfg = Config(state_dir=Path(td) / "state", vault=Path(td) / "vault",
                 chrome_profile=str(FIXTURES / "Bookmarks"))
    (cfg.vault / "wiki").mkdir(parents=True, exist_ok=True)
    return cli.Context(cfg=cfg, ledger=Ledger(":memory:"),
                       store=CaptureStore(cfg.state_dir))


class TestProbe(unittest.TestCase):
    def test_all_capability_keys_present(self):
        with tempfile.TemporaryDirectory() as td:
            caps = cli.cmd_probe(_ctx(td))
            for k in ("bookmarks", "instagram_export", "instagram_live",
                      "watch", "wiki_plugin"):
                self.assertIn(k, caps)
            self.assertTrue(caps["bookmarks"])          # fixture Bookmarks exists
            self.assertTrue(caps["wiki_plugin"])        # temp vault has wiki/


class TestPlan(unittest.TestCase):
    def test_bookmarks_emits_prepared_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            res = cli.cmd_plan(_ctx(td), channel="chrome-bookmarks")
            self.assertFalse(res["deferred"])
            self.assertGreaterEqual(len(res["jobs"]), 1)
            self.assertEqual(res["jobs"][0]["state"], "prepared")
            self.assertIn("target_capabilities", res["jobs"][0])

    def test_unavailable_channel_is_deferred_not_empty(self):
        with tempfile.TemporaryDirectory() as td:
            res = cli.cmd_plan(_ctx(td), channel="instagram-live")   # no snapshot given
            self.assertTrue(res["deferred"])
            self.assertEqual(res["reason"], "capability_unavailable")


class TestCommit(unittest.TestCase):
    def test_rejects_missing_source_url(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                cli.cmd_commit(_ctx(td), {"native_id": "i1", "summary": "x"})

    def test_writes_page_and_records(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            job = {"native_id": "i1", "source_url": "https://ex.com/carbonara",
                   "collection": "Food/Main/Italian", "title": "Carbonara",
                   "source_channel": "chrome-bookmarks", "summary": "yum", "now": NOW}
            res = cli.cmd_commit(ctx, job)
            page = ctx.cfg.vault / res["path"]
            self.assertTrue(page.exists())
            self.assertIn("yum", page.read_text())
            self.assertTrue(res["path"].startswith("wiki/recipes/"))
            self.assertIsNotNone(ctx.ledger.find_page_by_url("https://ex.com/carbonara"))


class TestAdopt(unittest.TestCase):
    def test_matches_by_source_url_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            md = ctx.cfg.vault / "wiki" / "sources" / "x.md"
            md.parent.mkdir(parents=True, exist_ok=True)
            md.write_text('---\ntype: source\nsource_url: "https://ex.com/x"\n'
                          'source_channel: instagram-saved\nnative_id: ABC\n---\nbody kept\n')
            r1 = cli.cmd_adopt(ctx, str(md))
            r2 = cli.cmd_adopt(ctx, str(md))
            self.assertEqual(r1["page_id"], r2["page_id"])   # one page, not duplicated
            self.assertEqual(ctx.ledger.item_count(), 1)
            self.assertIn("body kept", md.read_text())       # body preserved

    def test_rejects_md_without_source_url(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            md = ctx.cfg.vault / "wiki" / "n.md"
            md.write_text("---\ntype: source\n---\nno url\n")
            with self.assertRaises(ValueError):
                cli.cmd_adopt(ctx, str(md))


class TestReprocess(unittest.TestCase):
    def test_reprocess_preserves_manual_and_rerenders(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            job = {"native_id": "i1", "source_url": "https://ex.com/carbonara",
                   "collection": "Food/Main/Italian", "title": "Carbonara",
                   "summary": "v1", "now": NOW}
            res = cli.cmd_commit(ctx, job)
            page = ctx.cfg.vault / res["path"]
            page.write_text(page.read_text() + "## My notes\nkeep this\n")  # user edits
            out = cli.cmd_reprocess(ctx, now=NOW)
            self.assertIn(res["path"], out)
            txt = page.read_text()
            self.assertIn("keep this", txt)   # manual survives
            self.assertIn("v1", txt)          # re-rendered from cached extraction


class TestReport(unittest.TestCase):
    def test_report_counts_committed(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            cli.cmd_commit(ctx, {"native_id": "i1", "source_url": "https://ex.com/a",
                                 "title": "A", "now": NOW})
            rep = cli.cmd_report(ctx)
            self.assertGreaterEqual(rep.get("committed", 0), 1)


class TestMainDispatch(unittest.TestCase):
    def test_probe_via_main_prints_json(self):
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text('{"vault": "%s", "state_dir": "%s", "chrome_profile": "%s"}'
                                % (Path(td) / "vault", Path(td) / "state", FIXTURES / "Bookmarks"))
            (Path(td) / "vault" / "wiki").mkdir(parents=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["--config", str(cfg_path), "probe"])
            self.assertEqual(rc, 0)
            self.assertIn("bookmarks", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
