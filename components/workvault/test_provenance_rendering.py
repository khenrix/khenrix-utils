"""Operator-only Markdown check using installed RDoc, not an Obsidian observation."""
from html.parser import HTMLParser
import subprocess
import unittest

from curation import parse_publication, render_publication


class ProvenanceRenderingContract(unittest.TestCase):
    def test_entity_like_query_is_preserved_in_the_clickable_backlink(self):
        original_url = "https://example.invalid/?q=&copy;"
        card = {
            "path": "knowledge/alpha.md", "title": "Synthetic Alpha",
            "summary": "Curated synthetic fact.", "details": "Only synthetic content.",
            "date": "2026-10-05", "draft": False, "uncertainty": [],
            "source_links": [original_url], "cross_references": [],
        }
        batch = {
            "schema_version": "workvault-curated-batch-v1",
            "repository": "example/team-wiki",
            "commit": "1" * 40, "inventory_complete": True,
            "discovered_paths": [card["path"]], "indexed_paths": [card["path"]],
            "requested_paths": [card["path"]], "cards": [card],
        }
        publication = render_publication(batch, "2026-10-05T12:00:00Z")
        self.assertEqual(parse_publication(publication)["batch"], batch)
        ruby = (
            'require "rdoc"; require "rdoc/markdown"; require "rdoc/markup/to_html"; '
            'document = RDoc::Markdown.parse(STDIN.read.force_encoding("UTF-8")); '
            'puts document.accept(RDoc::Markup::ToHtml.new(RDoc::Options.new))'
        )
        rendered = subprocess.check_output(
            ["/usr/bin/ruby", "-e", ruby], input=publication, timeout=15).decode("utf-8")
        links = []

        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    href = dict(attrs).get("href")
                    if href is not None:
                        links.append(href)

        Links().feed(rendered)
        self.assertIn(original_url, links)
        self.assertNotIn("https://example.invalid/?q=©", links)


if __name__ == "__main__":
    unittest.main(verbosity=2)
