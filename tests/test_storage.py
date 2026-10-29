import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.storage import (
    _md_escape,
    load_seen_guids,
    load_seen_links,
    save_daily_results,
)


def _today_key() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y/%m%d")


class MarkdownEscapeTests(unittest.TestCase):
    def test_escapes_html_pipes_and_newlines(self):
        escaped = _md_escape('<script>alert("x")</script> | a\nb')
        self.assertNotIn("<script>", escaped)
        self.assertNotIn("\n", escaped)
        self.assertIn("&lt;script&gt;", escaped)
        self.assertIn("&quot;", escaped)
        self.assertIn("\\|", escaped)


class SaveDailyResultsTests(unittest.TestCase):
    def _article(self, **overrides) -> dict:
        article = {
            "link": "https://example.com/post",
            "title": "Hello",
            "author": "Author",
            "published": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "classification": {"category": "Tech", "summary": "Summary", "score": 8},
        }
        article.update(overrides)
        return article

    def test_rejects_unsafe_links_and_escapes_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            article = self._article(
                link="javascript:alert(1)",
                title="<b>Bold</b>",
                author='<img src=x onerror="alert(1)">',
                classification={"category": "<i>Tech</i>", "summary": "<script>x</script>", "score": 8},
            )
            written = save_daily_results({"articles": [article]}, tmp)
            self.assertEqual(len(written), 1)
            content = written[0].read_text(encoding="utf-8")
            self.assertNotIn("javascript:", content)
            self.assertNotIn("<b>", content)
            self.assertNotIn("<script>", content)
            self.assertNotIn("<img", content)
            self.assertIn("&lt;b&gt;Bold&lt;/b&gt;", content)

    def test_invalid_published_falls_back_to_cached_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            article = self._article(published="not-a-date")
            written = save_daily_results({"articles": [article]}, tmp)
            self.assertEqual(len(written), 1)
            expected = Path(tmp) / (datetime.now(timezone.utc).strftime("%Y/%m%d") + ".md")
            self.assertEqual(written[0], expected)

    def test_dedups_canonical_url_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._article(title="First", link="https://example.com/post")
            second = self._article(
                title="Second",
                link="https://www.example.com/post/?utm_source=x",
            )
            save_daily_results({"articles": [first]}, tmp)
            written = save_daily_results({"articles": [second]}, tmp)
            daily = Path(tmp) / (datetime.now(timezone.utc).strftime("%Y/%m%d") + ".md")
            content = daily.read_text(encoding="utf-8")
            self.assertEqual(content.count("](<https"), 1)
            self.assertIn("First", content)
            self.assertNotIn("Second", content)
            self.assertEqual(len(written), 1)

    def test_parentheses_in_url_round_trip_through_seen_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = "https://example.com/a(b)"
            save_daily_results({"articles": [self._article(link=link)]}, tmp)
            daily = Path(tmp) / (datetime.now(timezone.utc).strftime("%Y/%m%d") + ".md")
            content = daily.read_text(encoding="utf-8")
            self.assertIn(f"](<{link}>)", content)
            self.assertEqual(load_seen_links(tmp), {"example.com/a(b)"})

    def test_summary_links_do_not_pollute_seen_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "2026" / "1029.md"
            daily.parent.mkdir(parents=True)
            daily.write_text(
                "| A | [T](<https://example.com/article>) | Tech | "
                "[fake](https://evil.com/trap) | 8 |\n",
                encoding="utf-8",
            )
            self.assertEqual(load_seen_links(tmp), {"example.com/article"})

    def test_pipe_in_url_is_not_rendered_as_a_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            article = self._article(link="https://example.com/a|b")
            written = save_daily_results({"articles": [article]}, tmp)
            content = written[0].read_text(encoding="utf-8")
            self.assertNotIn("](<https://example.com/a", content)
            self.assertIn("Hello", content)
            self.assertEqual(load_seen_links(tmp), set())

    def test_clamps_score_and_truncates_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            article = self._article(
                classification={
                    "category": "Tech",
                    "summary": "x" * 400,
                    "score": 99,
                }
            )
            written = save_daily_results({"articles": [article]}, tmp)
            content = written[0].read_text(encoding="utf-8")
            self.assertIn("| 10 |", content)
            self.assertEqual(content.count("x" * 280), 1)
            self.assertNotIn("x" * 281, content)

    def test_index_exists_when_no_articles(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = save_daily_results({"articles": []}, tmp)
            self.assertEqual(written, [])
            self.assertTrue((Path(tmp) / "index.md").exists())

    def test_preserves_and_upgrades_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / (datetime.now(timezone.utc).strftime("%Y/%m%d") + ".md")
            daily.parent.mkdir(parents=True)
            daily.write_text(
                "# Old\n\n"
                "| Author | Title | Summary | Score |\n"
                "|--------|-------|---------|-------|\n"
                "| Old | [Old](https://old.com/a) | sum | 7 |\n",
                encoding="utf-8",
            )
            save_daily_results({"articles": [self._article(title="New")]}, tmp)
            content = daily.read_text(encoding="utf-8")
            self.assertIn("| Old | [Old](https://old.com/a) | — | sum | 7 |", content)
            self.assertIn("New", content)


class SeenLinksTests(unittest.TestCase):
    def test_load_seen_links_is_canonical_and_has_legacy_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "2026" / "1029.md"
            daily.parent.mkdir(parents=True)
            daily.write_text(
                "| A | [T](https://www.Example.com/a/?utm_source=x) | Tech | S | 8 |\n",
                encoding="utf-8",
            )
            self.assertEqual(load_seen_links(tmp), {"example.com/a"})
            self.assertEqual(load_seen_guids(tmp), {"example.com/a"})


if __name__ == "__main__":
    unittest.main()
