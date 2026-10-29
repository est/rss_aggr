import tempfile
import tomllib
import unittest
from pathlib import Path

from src.sync import save_feeds_toml


class SaveFeedsTomlTests(unittest.TestCase):
    def test_escapes_quotes_and_newlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feeds.toml"
            data = {
                "category": [
                    {
                        "name": 'Blogs "quoted"',
                        "feed": [
                            {
                                "title": 'A "quoted" title\nwith newline',
                                "url": "https://example.com/feed.xml",
                                "site": "https://example.com",
                                "priority": 1,
                            }
                        ],
                    }
                ]
            }

            save_feeds_toml(data, path)
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(parsed["category"][0]["name"], 'Blogs "quoted"')
            self.assertEqual(
                parsed["category"][0]["feed"][0]["title"],
                'A "quoted" title\nwith newline',
            )

    def test_escapes_del_and_preserves_arrays(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feeds.toml"
            data = {
                "category": [
                    {
                        "name": "Blogs",
                        "feed": [
                            {
                                "title": "bad\x7ftitle",
                                "url": "https://example.com/feed.xml",
                                "tags": ["a", "b"],
                            }
                        ],
                    }
                ]
            }

            save_feeds_toml(data, path)
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            feed = parsed["category"][0]["feed"][0]
            self.assertEqual(feed["title"], "bad\x7ftitle")
            self.assertEqual(feed["tags"], ["a", "b"])

    def test_unsupported_value_raises_instead_of_silently_dropping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feeds.toml"
            data = {
                "category": [
                    {
                        "name": "Blogs",
                        "feed": [{"url": "https://example.com", "nested": {"a": 1}}],
                    }
                ]
            }
            with self.assertRaises(ValueError):
                save_feeds_toml(data, path)


if __name__ == "__main__":
    unittest.main()
