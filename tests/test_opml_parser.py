import tempfile
import unittest
from pathlib import Path

from src.opml_parser import parse_feeds
from src.sync import dedupe_feeds


class ParseFeedsTests(unittest.TestCase):
    def test_dedupes_canonical_urls_and_skips_empty_or_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feeds.toml"
            path.write_text(
                "\n".join(
                    [
                        "[[category]]",
                        'name = "Blogs"',
                        "[[category.feed]]",
                        'title = "A"',
                        'url = "https://example.com/feed.xml"',
                        "[[category.feed]]",
                        'title = "A duplicate"',
                        'url = "http://www.example.com/feed.xml/"',
                        "[[category.feed]]",
                        'title = "No URL"',
                        'url = ""',
                        "[[category.feed]]",
                        'title = "Skipped"',
                        'url = "https://skip.example/feed"',
                        "skip = true",
                    ]
                ),
                encoding="utf-8",
            )
            feeds = parse_feeds(path)
            self.assertEqual([f["title"] for f in feeds], ["A"])
            self.assertEqual(feeds[0]["xml_url"], "https://example.com/feed.xml")


class DedupeFeedsTests(unittest.TestCase):
    def test_dedupe_feeds_removes_canonical_duplicates(self):
        data = {
            "category": [
                {
                    "name": "Blogs",
                    "feed": [
                        {"title": "A", "url": "https://example.com/feed.xml"},
                        {"title": "A2", "url": "http://www.example.com/feed.xml/"},
                        {"title": "B", "url": "https://b.example/feed"},
                    ],
                }
            ]
        }
        self.assertEqual(dedupe_feeds(data), 1)
        self.assertEqual(
            [f["title"] for f in data["category"][0]["feed"]],
            ["A", "B"],
        )

    def test_dedupe_feeds_handles_duplicates_across_categories(self):
        data = {
            "category": [
                {"name": "A", "feed": [{"title": "First", "url": "https://example.com/feed"}]},
                {"name": "B", "feed": [{"title": "Second", "url": "http://example.com/feed/"}]},
            ]
        }
        self.assertEqual(dedupe_feeds(data), 1)
        self.assertEqual(len(data["category"][0]["feed"]), 1)
        self.assertEqual(data["category"][1]["feed"], [])

    def test_dedupe_feeds_prefers_active_entry_over_skip(self):
        data = {
            "category": [
                {
                    "name": "Blogs",
                    "feed": [
                        {"title": "Skipped", "url": "https://example.com/feed", "skip": True},
                        {"title": "Active", "url": "http://www.example.com/feed/"},
                    ],
                }
            ]
        }
        self.assertEqual(dedupe_feeds(data), 1)
        self.assertEqual(
            [f["title"] for f in data["category"][0]["feed"]],
            ["Active"],
        )


if __name__ == "__main__":
    unittest.main()
