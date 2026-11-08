import tempfile
import unittest
from pathlib import Path

from src.opml_parser import parse_feeds, parse_opml_bytes
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


class ParseOpmlBytesTests(unittest.TestCase):
    def test_includes_top_level_and_nested_feeds_with_nearest_category(self):
        content = b"""<?xml version="1.0"?>
<opml version="2.0">
  <head><title>Mixed</title></head>
  <body>
    <outline text="Top Feed" type="rss"
             xmlUrl="https://top.example/feed" htmlUrl="https://top.example"/>
    <outline text="Tech">
      <outline text="Nested One" type="rss" xmlUrl="https://one.example/feed"/>
      <outline text="Deeper">
        <outline text="Nested Two" xmlUrl="https://two.example/feed"/>
      </outline>
    </outline>
  </body>
</opml>"""

        feeds = parse_opml_bytes(content)

        self.assertEqual(
            [(f["title"], f["url"], f["source_category"]) for f in feeds],
            [
                ("Top Feed", "https://top.example/feed", ""),
                ("Nested One", "https://one.example/feed", "Tech"),
                ("Nested Two", "https://two.example/feed", "Deeper"),
            ],
        )
        self.assertEqual(feeds[0]["site"], "https://top.example")

    def test_flat_opml_still_parses(self):
        content = b"""<opml version="1.0"><body>
          <outline text="A" type="rss" xmlUrl="https://a.example/feed"/>
          <outline text="B" type="rss" xmlUrl="https://b.example/feed"/>
        </body></opml>"""

        feeds = parse_opml_bytes(content)

        self.assertEqual([f["title"] for f in feeds], ["A", "B"])
        self.assertTrue(all(f["source_category"] == "" for f in feeds))


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
