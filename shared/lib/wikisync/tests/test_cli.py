import sys
import json
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


def _persistent_ctx(td):
    ctx = _ctx(td)
    ctx.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    ctx.ledger.close()
    ctx.ledger = Ledger(ctx.cfg.state_dir / "ledger.sqlite")
    return ctx


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
    def test_snapshot_identity_is_sanitized_before_diff_and_observe(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _persistent_ctx(td)
            snapshot = Path(td) / "snapshot.json"
            results = []
            for secret in ("PLAN_FIRST_SECRET", "PLAN_SECOND_SECRET"):
                url = f"https://ex.com/x?token={secret}&variant=blue&signed=one%2Ftwo"
                snapshot.write_text(json.dumps({"status": "complete", "items": [{
                    "native_id": url, "canonical_url": url, "original_url": url,
                }]}))
                results.append(cli.cmd_plan(ctx, "chrome-bookmarks", str(snapshot), NOW))
            safe = "https://ex.com/x?token=REDACTED&variant=blue&signed=one%2Ftwo"
            self.assertEqual(results[1]["jobs"][0]["native_id"], safe)
            self.assertEqual(results[1]["jobs"][0]["reason"], "updated")
            self.assertEqual(results[0]["jobs"][0]["item_id"], results[1]["jobs"][0]["item_id"])
            self.assertEqual(results[1]["removable"], [])
            self.assertEqual(ctx.ledger.item_count(), 1)
            persisted = "\n".join(ctx.ledger.conn.iterdump())
            self.assertNotIn("PLAN_FIRST_SECRET", persisted)
            self.assertNotIn("PLAN_SECOND_SECRET", persisted)
            ctx.ledger.close()

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
    def test_commit_sanitizes_actual_ledger_captures_and_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _persistent_ctx(td)
            url = "https://ex.com/x?token=COMMIT_SECRET&variant=blue&signed=one%2Ftwo"
            job = {"source_url": url, "native_id": url, "title": "A",
                   "summary": "Read https://ex.com/article?api_key=TEXT_SECRET&q=42.",
                   "ingredients": ["https://ex.com/ingredient?password=LIST_SECRET&size=L"],
                   "captures": [{"kind": "original-source",
                                 "text": "https://ex.com/source?secret=CAPTURE_SECRET&v=1"}],
                   "sources": [{"url": "https://ex.com/archive?auth=SOURCE_SECRET&v=2"}],
                   "now": NOW}
            first = cli.cmd_commit(ctx, job)
            page = ctx.cfg.vault / first["path"]
            page.write_text(page.read_text() + "\n## My notes\nmanual token=not-a-url\n")
            second = cli.cmd_commit(ctx, job)
            self.assertEqual(first["path"], second["path"])
            self.assertEqual(ctx.ledger.item_count(), 1)
            self.assertIn("manual token=not-a-url", page.read_text())
            safe = "https://ex.com/x?token=REDACTED&variant=blue&signed=one%2Ftwo"
            self.assertIsNotNone(ctx.ledger.find_page_by_url(safe))
            capture = ctx.store.latest(safe, "original-source")
            self.assertIsNotNone(capture)
            self.assertEqual(Path(capture.raw_path).read_bytes(),
                             b"https://ex.com/source?secret=REDACTED&v=1")
            extraction = ctx.store.latest(safe, "extraction")
            self.assertIsNotNone(extraction)
            ext = json.loads(ctx.store.get(extraction.capture_id))
            self.assertEqual(ext["summary"], "Read https://ex.com/article?api_key=REDACTED&q=42.")
            self.assertEqual(ext["ingredients"], ["https://ex.com/ingredient?password=REDACTED&size=L"])
            persisted = "\n".join(ctx.ledger.conn.iterdump()).encode() + page.read_bytes()
            persisted += b"".join(path.read_bytes() for path in ctx.store.root.rglob("*") if path.is_file())
            for secret in ("COMMIT_SECRET", "TEXT_SECRET", "LIST_SECRET", "CAPTURE_SECRET", "SOURCE_SECRET"):
                self.assertNotIn(secret.encode(), persisted)
            self.assertEqual(job["source_url"], url)
            ctx.ledger.close()

    def test_rejects_missing_source_url(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                cli.cmd_commit(_ctx(td), {"native_id": "i1", "summary": "x"})

    def test_job_now_used_when_flag_empty(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            res = cli.cmd_commit(ctx, {"native_id": "i9", "source_url": "https://ex.com/z",
                                       "title": "Z", "now": NOW})
            self.assertIn(NOW, (ctx.cfg.vault / res["path"]).read_text())

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

    def test_retitle_removes_stale_file(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            base = {"native_id": "i1", "source_url": "https://ex.com/x",
                    "collection": "Food/Main/Italian", "now": NOW}
            first = cli.cmd_commit(ctx, {**base, "title": "Old Title"})
            second = cli.cmd_commit(ctx, {**base, "title": "New Title"})
            self.assertNotEqual(first["path"], second["path"])           # filename changed
            self.assertFalse((ctx.cfg.vault / first["path"]).exists())   # old file removed
            self.assertTrue((ctx.cfg.vault / second["path"]).exists())   # new file present


class TestAdopt(unittest.TestCase):
    def test_adoption_sanitizes_url_identity_without_rewriting_manual_page(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _persistent_ctx(td)
            md = ctx.cfg.vault / "wiki" / "adopt.md"
            url = "https://ex.com/x?token=ADOPT_SECRET&variant=blue"
            original = f'---\nsource_url: "{url}"\nnative_id: "{url}"\n---\nmanual body\n'
            md.write_text(original)
            one = cli.cmd_adopt(ctx, str(md), NOW)
            two = cli.cmd_adopt(ctx, str(md), NOW)
            self.assertEqual(one["page_id"], two["page_id"])
            self.assertEqual(one["source_url"], "https://ex.com/x?token=REDACTED&variant=blue")
            self.assertNotIn("ADOPT_SECRET", "\n".join(ctx.ledger.conn.iterdump()))
            self.assertEqual(md.read_text(), original)
            ctx.ledger.close()

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
    def test_reprocess_sanitizes_new_render_but_does_not_clean_historical_cache(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _persistent_ctx(td)
            url = "https://ex.com/x?token=HISTORICAL_SECRET&variant=blue"
            ctx.ledger.observe(chan="manual", native_id=url, url=url, title="A", now=NOW)
            path = "wiki/sources/old.md"
            ctx.ledger.record_page(path=path, source_url=url, now=NOW)
            page = ctx.cfg.vault / path
            page.parent.mkdir(parents=True)
            page.write_text("<!-- khenrix:managed:start -->\nold\n<!-- khenrix:managed:end -->\nmanual survives\n")
            payload = json.dumps({"title": "A", "summary": "https://ex.com/y?key=REPROCESS_SECRET&q=2"}).encode()
            historical = ctx.store.put(url, "extraction", payload)
            rendered = cli.cmd_reprocess(ctx, now=NOW)
            text = (ctx.cfg.vault / rendered[0]).read_text()
            self.assertNotIn("HISTORICAL_SECRET", text)
            self.assertNotIn("REPROCESS_SECRET", text)
            self.assertIn("key=REDACTED&q=2", text)
            self.assertIn("manual survives", text)
            self.assertEqual(ctx.store.get(historical.capture_id), payload)
            self.assertEqual(ctx.ledger.active_items_for_url(url)[0]["native_id"], url)
            ctx.ledger.close()

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
