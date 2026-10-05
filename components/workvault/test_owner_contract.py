"""Owner acceptance for the admitted pure curation API, not producer internals."""
import unittest

from curation import parse_publication, render_publication, validate_curated_batch


def batch():
    return {
        "schema_version": "workvault-curated-batch-v1",
        "repository": "example/team-wiki",
        "commit": "1" * 40, "inventory_complete": True,
        "discovered_paths": ["knowledge/alpha.md", "knowledge/drafts/beta.md", "knowledge/unindexed.md"],
        "indexed_paths": ["knowledge/alpha.md", "knowledge/drafts/beta.md"],
        "requested_paths": ["knowledge/alpha.md", "knowledge/drafts/beta.md"],
        "cards": [
            {"path": "knowledge/alpha.md", "title": "Synthetic Alpha α", "summary": "Curated synthetic fact.",
             "details": "No workplace source text.", "date": "2026-10-05", "draft": False,
             "uncertainty": ["Synthetic, not a verified live service fact."],
             "source_links": ["https://example.invalid/alpha"], "cross_references": ["knowledge/drafts/beta.md"]},
            {"path": "knowledge/drafts/beta.md", "title": "Synthetic Beta", "summary": "Draft synthetic decision.",
             "details": "Keep draft status explicit.", "date": "2026-10-04", "draft": True,
             "uncertainty": [], "source_links": [], "cross_references": ["knowledge/alpha.md"]},
        ],
    }


class PureCurationContract(unittest.TestCase):
    def test_validated_copy_and_exact_checkpoint_roundtrip(self):
        original = batch()
        validated = validate_curated_batch(original)
        self.assertEqual(validated, original)
        validated["cards"][0]["uncertainty"].append("New local edit")
        self.assertEqual(original["cards"][0]["uncertainty"], ["Synthetic, not a verified live service fact."])
        rendered = render_publication(original, "2026-10-05T10:00:00Z")
        self.assertIsInstance(rendered, bytes)
        self.assertEqual(parse_publication(rendered), {
            "schema_version": "workvault-publication-v1", "batch": original,
            "published_at": "2026-10-05T10:00:00Z"})
        self.assertEqual(render_publication(original, "2026-10-05T10:00:00Z"), rendered)

    def test_each_card_has_exact_pinned_commit_citation(self):
        original = batch()
        rendered = render_publication(original, "2026-10-05T10:00:00Z").decode()
        for card in original["cards"]:
            self.assertIn("https://github.com/example/team-wiki/blob/" + "1" * 40 + "/" + card["path"], rendered)

    def test_quoted_marker_like_text_roundtrips_without_truncation(self):
        original = batch()
        original["cards"][0]["details"] = "Curated punctuation α. -->\n<!-- workvault-publication-v1 -->\nA later curated sentence."
        self.assertEqual(parse_publication(render_publication(original, "2026-10-05T10:00:00Z"))["batch"], original)

    def test_unknown_batch_and_card_fields_cannot_persist(self):
        for location in ("batch", "card"):
            original = batch()
            target = original if location == "batch" else original["cards"][0]
            target["raw_body"] = "Raw-only fixture canary"
            with self.subTest(location=location), self.assertRaises(ValueError):
                validate_curated_batch(original)

    def test_invalid_boolean_commit_inventory_and_dates(self):
        variants = []
        for key, value in [("inventory_complete", False), ("inventory_complete", 1), ("commit", "A" * 40),
                           ("indexed_paths", ["knowledge/not-discovered.md"]),
                           ("requested_paths", ["knowledge/alpha.md", "knowledge/alpha.md"])]:
            original = batch()
            original[key] = value
            variants.append(original)
        for key, value in [("draft", 1), ("date", "2026-02-30"), ("summary", "x" * 5_000_000)]:
            original = batch()
            original["cards"][0][key] = value
            variants.append(original)
        for original in variants:
            with self.subTest(keys=list(original)), self.assertRaises(ValueError):
                validate_curated_batch(original)

    def test_traversal_backslash_and_absolute_paths_refuse(self):
        for path in ("knowledge/../private.md", "/knowledge/alpha.md", "knowledge\\alpha.md", "knowledge/a/../alpha.md"):
            original = batch()
            for field in ("discovered_paths", "indexed_paths", "requested_paths"):
                original[field][0] = path
            original["cards"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_curated_batch(original)

    def test_insecure_or_credential_bearing_provenance_refuses(self):
        for url in ("http://example.invalid/", "https://user:password@example.invalid/", "file:///private/page.md",
                    "https://example.invalid:abc/", "https://example.invalid/line\nbreak"):
            original = batch()
            original["cards"][0]["source_links"] = [url]
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_curated_batch(original)

    def test_wrong_nested_types_refuse_with_value_error(self):
        for field in ("discovered_paths", "indexed_paths", "requested_paths"):
            original = batch()
            original[field] = [{"not": "a path"}]
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_curated_batch(original)
        for field in ("source_links", "cross_references", "uncertainty"):
            original = batch()
            original["cards"][0][field] = [{"not": "a string"}]
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_curated_batch(original)

    def test_error_does_not_echo_rejected_source_text(self):
        original = batch()
        canary = "https://private-fixture.invalid/not-a-permitted-path"
        original["requested_paths"][0] = canary
        try:
            validate_curated_batch(original)
        except ValueError as error:
            self.assertNotIn(canary, str(error))
        else:
            self.fail("Invalid path was accepted")

    def test_oversized_publication_refuses_before_partial_parse(self):
        with self.assertRaises(ValueError):
            parse_publication(b"x" * 5_000_000)

    def test_invalid_utf8_text_refuses_during_validation(self):
        original = batch()
        original["cards"][0]["title"] = "\ud800"
        with self.assertRaises(ValueError):
            validate_curated_batch(original)

    def test_curated_html_cannot_hide_visible_sections(self):
        original = batch()
        original["cards"][0]["details"] = "<!-- synthetic opening marker"
        data = render_publication(original, "2026-10-05T10:00:00Z")
        self.assertEqual(parse_publication(data)["batch"], original)
        self.assertNotIn(b"<!-- synthetic opening marker", data)

    def test_timestamp_requires_valid_utc_format(self):
        for timestamp in ("2026-02-30T10:00:00Z", "2026-10-05", "2026-10-05T10:00:00+02:00"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                render_publication(batch(), timestamp)

    def test_corrupt_or_mismatched_rendering_is_not_success(self):
        rendered = render_publication(batch(), "2026-10-05T10:00:00Z")
        marker = b"Curated synthetic fact."
        index = rendered.rfind(marker)
        self.assertGreaterEqual(index, 0)
        mismatch = rendered[:index] + b"An inconsistent alleged fact." + rendered[index + len(marker):]
        for data in (rendered[:20], mismatch, b"\xff\xfe", rendered + b"Unmatched appended content"):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                parse_publication(data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
