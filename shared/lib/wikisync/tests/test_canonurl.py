import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # shared/lib on path
from wikisync.canonurl import canonicalize, classify_host, redact_credentials, redact_text_urls  # noqa: E402


class TestCanonicalize(unittest.TestCase):
    def test_ig_shortcode_canonical(self):
        c = canonicalize("https://www.instagram.com/reel/DajH0TsShpP/?igsh=abc")
        self.assertEqual(c.canonical, "https://www.instagram.com/reel/DajH0TsShpP/")
        self.assertEqual(c.native_id, "DajH0TsShpP")
        self.assertEqual(c.kind, "instagram_reel")

    def test_ig_post_kind(self):
        c = canonicalize("https://instagram.com/p/AbC-123_x/")
        self.assertEqual(c.kind, "instagram_post")
        self.assertEqual(c.canonical, "https://www.instagram.com/p/AbC-123_x/")

    def test_tracking_stripped_but_semantic_kept(self):
        self.assertEqual(
            canonicalize("https://x.com/a?utm_source=ig&v=42").canonical,
            "https://x.com/a?v=42",
        )

    def test_youtube_id_and_v_param_kept(self):
        c = canonicalize("https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share")
        self.assertEqual(c.kind, "youtube")
        self.assertEqual(c.native_id, "dQw4w9WgXcQ")
        self.assertIn("v=dQw4w9WgXcQ", c.canonical)
        self.assertNotIn("feature", c.canonical)

    def test_youtu_be_short(self):
        c = canonicalize("https://youtu.be/dQw4w9WgXcQ?si=xyz")
        self.assertEqual(c.native_id, "dQw4w9WgXcQ")
        self.assertEqual(c.kind, "youtube")

    def test_github_native_id(self):
        c = canonicalize("https://github.com/kepano/obsidian-skills?tab=readme")
        self.assertEqual(c.kind, "github")
        self.assertEqual(c.native_id, "kepano/obsidian-skills")

    def test_plain_web_defaults(self):
        c = canonicalize("https://example.com/recipe")
        self.assertEqual(c.kind, "web")
        self.assertIsNone(c.native_id)
        self.assertEqual(c.original, "https://example.com/recipe")


class TestClassifyHost(unittest.TestCase):
    def test_localhost_is_local(self):
        self.assertEqual(classify_host("http://localhost:8080/x"), "local")

    def test_loopback_ip_is_local(self):
        self.assertEqual(classify_host("http://127.0.0.1/x"), "local")

    def test_rfc1918_is_internal(self):
        self.assertEqual(classify_host("http://10.1.2.3/x"), "internal")
        self.assertEqual(classify_host("http://192.168.0.5/x"), "internal")

    def test_single_label_host_is_internal(self):
        self.assertEqual(classify_host("http://intranet/x"), "internal")

    def test_nonhttp_scheme(self):
        self.assertEqual(classify_host("file:///etc/passwd"), "nonhttp")

    def test_public(self):
        self.assertEqual(classify_host("https://www.instagram.com/p/x/"), "public")


class TestRedactCredentials(unittest.TestCase):
    def test_preserves_raw_order_flags_and_noncredential_signed_values(self):
        self.assertEqual(
            redact_credentials("https://s/x?flag&variant=blue&token=SECRET&signed=one%2Ftwo&v=42"),
            "https://s/x?flag&variant=blue&token=REDACTED&signed=one%2Ftwo&v=42",
        )

    def test_redacts_token_keeps_others(self):
        self.assertEqual(
            redact_credentials("https://s/x?token=SECRET&q=1"),
            "https://s/x?token=REDACTED&q=1",
        )

    def test_no_credentials_unchanged(self):
        self.assertEqual(redact_credentials("https://s/x?q=1"), "https://s/x?q=1")

    def test_multiple_credential_params(self):
        out = redact_credentials("https://s/x?api_key=A&password=B&keep=C")
        self.assertIn("api_key=REDACTED", out)
        self.assertIn("password=REDACTED", out)
        self.assertIn("keep=C", out)


class TestTextQueryBoundary(unittest.TestCase):
    def test_parenthesized_query_before_credential(self):
        self.assertEqual(
            redact_text_urls("https://example.invalid/x?q=(article)&token=BOUNDARY_SECRET"),
            "https://example.invalid/x?q=(article)&token=REDACTED",
        )

    def test_markdown_parenthesized_query_before_credential(self):
        self.assertEqual(
            redact_text_urls("[Link](https://example.invalid/x?q=(article)&token=BOUNDARY_SECRET)"),
            "[Link](https://example.invalid/x?q=(article)&token=REDACTED)",
        )

    def test_bracketed_query_before_credential(self):
        self.assertEqual(
            redact_text_urls("https://example.invalid/x?q=[article]&api_key=BOUNDARY_SECRET"),
            "https://example.invalid/x?q=[article]&api_key=REDACTED",
        )

    def test_credentials_on_both_sides_preserve_raw_query_order_and_flags(self):
        for value in ("(article)", "[article]", "([nested(article)])"):
            with self.subTest(value=value):
                self.assertEqual(
                    redact_text_urls(f"https://example.invalid/x?flag&token=FIRST&q={value}&signed=a%2Fb&api_key=LAST"),
                    f"https://example.invalid/x?flag&token=REDACTED&q={value}&signed=a%2Fb&api_key=REDACTED",
                )

    def test_noncredential_urls_and_nonurl_identifiers_are_byte_identical(self):
        for text in (
            "https://example.invalid/x?flag&token&q=([article])&signed=a%2Fb&variant=blue",
            "[Link](https://example.invalid/x?q=(article)&signed=a%2Fb).",
            "opaque:(article)[token=KEEP] and prose api_key=KEEP",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_text_urls(text), text)

    def test_adjacent_markdown_and_prose_punctuation_is_preserved(self):
        url = "https://example.invalid/x?q=([article])&token=BOUNDARY_SECRET"
        safe = "https://example.invalid/x?q=([article])&token=REDACTED"
        for before, after in (("[Link](", ")."), ("(See ", "),"), ("Read ", ";"), ("[", "]")):
            with self.subTest(before=before, after=after):
                self.assertEqual(redact_text_urls(before + url + after), before + safe + after)

    def test_closing_delimiter_preserves_adjacent_nonurl_prose(self):
        self.assertEqual(
            redact_text_urls("Read [Link](https://example.invalid/x?q=(article)&token=BOUNDARY_SECRET)next."),
            "Read [Link](https://example.invalid/x?q=(article)&token=REDACTED)next.",
        )
        self.assertEqual(
            redact_text_urls("Read [https://example.invalid/x?q=[article]&api_key=BOUNDARY_SECRET]next."),
            "Read [https://example.invalid/x?q=[article]&api_key=REDACTED]next.",
        )

    def test_adjacent_prose_does_not_hide_the_next_url(self):
        self.assertEqual(
            redact_text_urls("[A](https://example.invalid/x?q=(article)&token=FIRST)next."
                             "[B](https://example.invalid/y?q=[article]&api_key=LAST)done."),
            "[A](https://example.invalid/x?q=(article)&token=REDACTED)next."
            "[B](https://example.invalid/y?q=[article]&api_key=REDACTED)done.",
        )


if __name__ == "__main__":
    unittest.main()
