import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # shared/lib on path
from wikisync.sources import SourceItem  # noqa: E402
from wikisync.taxonomy import route  # noqa: E402
from wikisync.render import filename, render_page  # noqa: E402


def _item(collection="Bokmärkesfältet/Food/Main/Italian",
          url="https://ex.com/carbonara", nid="i1"):
    return SourceItem(native_id=nid, canonical_url=url, collection=collection)


class TestFilename(unittest.TestCase):
    def test_slug_collision_distinct_authors(self):
        a = filename("Carbonara", "@x", "i1")
        b = filename("Carbonara", "@y", "i2")
        self.assertNotEqual(a, b)
        self.assertTrue(a.endswith(".md"))

    def test_no_author_uses_hash(self):
        a = filename("Carbonara", "", "i1")
        self.assertTrue(a.startswith("carbonara-") and a.endswith(".md"))

    def test_same_title_author_different_item_distinct(self):
        a = filename("Carbonara", "@x", "i1")
        b = filename("Carbonara", "@x", "i2")
        self.assertNotEqual(a, b)          # item_id folded in → collision-safe

    def test_unicode_ascii_fold(self):
        a = filename("Bò lá lốt", "cuppabeans", "i1")
        self.assertTrue(a.startswith("bo-la-lot"))


class TestRoute(unittest.TestCase):
    def test_recipe_tags_namespaced(self):
        r = route(_item(), {"diet": ["vegetarian"]})
        self.assertLessEqual({"course/main", "cuisine/italian", "diet/vegetarian"},
                             set(r.tags))
        self.assertEqual(r.kind, "recipe")
        self.assertTrue(r.target_dir.endswith("recipes"))

    def test_course_cuisine_from_compound_segments(self):
        r = route(_item(collection="Food/Dessert & Baking/Chinese & Sichuan"), {})
        self.assertIn("course/dessert", r.tags)
        self.assertIn("cuisine/chinese", r.tags)

    def test_product_routing(self):
        r = route(_item(collection="Bokmärkesfältet/Köpa?/Home & Garden"), {})
        self.assertEqual(r.kind, "product")
        self.assertTrue(r.target_dir.endswith("products"))

    def test_shopping_intent_beats_recipe_keyword(self):
        # "Kitchen & Cooking" is kitchen GEAR under the Köpa? (buy) tree, not recipes.
        r = route(_item(collection="Bokmärkesfältet/Köpa?/Kitchen & Cooking"), {})
        self.assertEqual(r.kind, "product")

    def test_inspiration_routing(self):
        r = route(_item(collection="Bokmärkesfältet/Github Inspo"), {})
        self.assertEqual(r.kind, "inspiration")

    def test_default_source(self):
        r = route(_item(collection="Bokmärkesfältet/Other/Memes"), {})
        self.assertEqual(r.kind, "source")

    def test_explicit_extraction_type_wins(self):
        r = route(_item(collection="Other/Memes"), {"type": "recipe"})
        self.assertEqual(r.kind, "recipe")

    def test_method_steps_do_not_become_tags(self):
        # 'method' is the recipe STEPS field; only 'technique' feeds method/* tags.
        r = route(_item(), {"method": ["Soak capers in wine 10 min", "Char the chilli"],
                            "technique": ["grill"]})
        self.assertIn("method/grill", r.tags)
        self.assertFalse(any("soak" in t or "char" in t or "capers" in t for t in r.tags))


class TestRenderMerge(unittest.TestCase):
    def test_manual_section_survives_refetch(self):
        item = _item()
        r = route(item, {})
        front = "---\ntype: recipe\n---\n"
        existing = (front + "<!-- khenrix:managed:start -->\nOLD BODY\n"
                    "<!-- khenrix:managed:end -->\n## My notes\nkeep me\n")
        doc = render_page(item, {"summary": "fresh summary"}, r, existing_text=existing)
        self.assertIn("keep me", doc.text)
        self.assertNotIn("OLD BODY", doc.text)
        self.assertIn("fresh summary", doc.text)

    def test_frontmatter_provenance_and_redaction(self):
        item = _item(url="https://ex.com/x?token=SEKRET&v=9")
        r = route(item, {})
        doc = render_page(item, {"source_channel": "chrome-bookmarks",
                                 "capture_id": "i1/caption-abc"}, r)
        for field in ("schema_version:", "source_url:", "native_id: i1",
                      "source_channel: chrome-bookmarks", "capture_id:",
                      "taxonomy_version:"):
            self.assertIn(field, doc.text)
        self.assertNotIn("SEKRET", doc.text)     # credentials redacted in visible URL
        self.assertIn("v=9", doc.text)            # semantic param kept

    def test_generated_hash_deterministic(self):
        item = _item()
        r = route(item, {})
        kw = dict(now="2026-07-11T00:00:00+00:00")
        d1 = render_page(item, {"summary": "s"}, r, **kw)
        d2 = render_page(item, {"summary": "s"}, r, **kw)
        self.assertEqual(d1.generated_hash, d2.generated_hash)

    def test_path_under_target_dir(self):
        item = _item()
        r = route(item, {})
        doc = render_page(item, {"title": "Best Carbonara", "author": "@chef"}, r)
        self.assertTrue(doc.path.startswith("wiki/recipes/"))
        self.assertTrue(doc.path.endswith(".md"))


if __name__ == "__main__":
    unittest.main()
