import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from src.sync import SyncSourceError, parse_remote, save_feeds_toml, sync


class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


class ParseRemoteTests(unittest.TestCase):
    def test_detects_opml_from_url_suffix_not_display_name(self):
        content = (
            b'<opml version="1.0"><body>'
            b'<outline text="A" xmlUrl="https://a.example/feed"/>'
            b"</body></opml>"
        )
        source = {"name": "list.toml", "url": "https://example.com/feed.opml"}

        with patch("src.sync.requests.get", return_value=_Response(content)):
            feeds = parse_remote(source)

        self.assertEqual([f["url"] for f in feeds], ["https://a.example/feed"])

    def test_detects_toml_from_content_when_suffix_is_unknown(self):
        content = (
            b'[[category]]\nname = "Tech"\n'
            b'[[category.feed]]\ntitle = "A"\n'
            b'url = "https://a.example/feed"\n'
        )
        source = {"name": "list.opml", "url": "https://example.com/list"}

        with patch("src.sync.requests.get", return_value=_Response(content)):
            feeds = parse_remote(source)

        self.assertEqual(
            feeds,
            [
                {
                    "title": "A",
                    "url": "https://a.example/feed",
                    "site": "",
                    "source_category": "Tech",
                }
            ],
        )

    def test_rejects_non_public_source_url_before_fetching(self):
        source = {"name": "internal", "url": "http://127.0.0.1/list.opml"}

        with patch("src.sync.requests.get") as get:
            with self.assertRaises(SyncSourceError):
                parse_remote(source)

        get.assert_not_called()

    def test_fetch_errors_raise_instead_of_returning_empty(self):
        source = {"name": "broken", "url": "https://example.com/list.opml"}

        with patch("src.sync.requests.get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(SyncSourceError) as raised:
                parse_remote(source)

        self.assertIn("fetch failed", str(raised.exception))

    def test_parse_errors_and_zero_feeds_raise(self):
        source = {"name": "bad", "url": "https://example.com/list.opml"}

        with patch("src.sync.requests.get", return_value=_Response(b"<opml><body>")):
            with self.assertRaises(SyncSourceError) as raised:
                parse_remote(source)
        self.assertIn("cannot parse", str(raised.exception))

        empty = b'<opml version="1.0"><body></body></opml>'
        with patch("src.sync.requests.get", return_value=_Response(empty)):
            with self.assertRaises(SyncSourceError) as raised:
                parse_remote(source)
        self.assertIn("no feeds", str(raised.exception))


class SyncTests(unittest.TestCase):
    def _write_sources(self, path: Path, names: list[str]) -> None:
        lines = []
        for name in names:
            lines.extend(
                [
                    "[[source]]",
                    f'name = "{name}"',
                    f'url = "https://{name}.example/list.opml"',
                    'category = "Tech"',
                    "",
                ]
            )
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_continues_after_source_failure_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / "sources.toml"
            feeds_path = Path(tmp) / "feeds.toml"
            self._write_sources(sources, ["good", "bad"])

            def fake_parse(source, _user_agent):
                if source["name"] == "bad":
                    raise SyncSourceError("bad: source contained no feeds")
                return [
                    {
                        "title": "Good",
                        "url": "https://good.example/feed",
                        "site": "https://good.example",
                        "source_category": "",
                    }
                ]

            with patch("src.sync.parse_remote", side_effect=fake_parse):
                stats = sync(str(sources), str(feeds_path))

            self.assertEqual(
                stats,
                {
                    "new": 1,
                    "skipped": 0,
                    "removed": 0,
                    "total": 1,
                    "errors": ["bad: source contained no feeds"],
                    "warnings": [],
                },
            )
            data = tomllib.loads(feeds_path.read_text(encoding="utf-8"))
            self.assertEqual(data["category"][0]["feed"][0]["url"], "https://good.example/feed")

    def test_invalid_feed_and_site_urls_are_skipped_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / "sources.toml"
            feeds_path = Path(tmp) / "feeds.toml"
            self._write_sources(sources, ["list"])

            imported = [
                {
                    "title": "Valid feed, bad site",
                    "url": "https://ok.example/feed",
                    "site": "http://127.0.0.1/private",
                },
                {
                    "title": "Internal feed",
                    "url": "http://localhost/feed",
                    "site": "https://localhost",
                },
                {"title": "No URL", "url": "", "site": ""},
            ]

            with patch("src.sync.parse_remote", return_value=imported):
                stats = sync(str(sources), str(feeds_path))

            self.assertEqual(stats["new"], 1)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(len(stats["errors"]), 0)
            warnings = stats["warnings"]
            self.assertEqual(len(warnings), 3)
            self.assertTrue(any("non-public feed URL" in e for e in warnings))
            self.assertTrue(any("non-public site URL" in e for e in warnings))
            self.assertTrue(any("no URL" in e for e in warnings))

            data = tomllib.loads(feeds_path.read_text(encoding="utf-8"))
            feeds = data["category"][0]["feed"]
            self.assertEqual(len(feeds), 1)
            self.assertEqual(feeds[0]["url"], "https://ok.example/feed")
            self.assertEqual(feeds[0]["site"], "")

    def test_no_sources_returns_empty_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / "sources.toml"
            sources.write_text("", encoding="utf-8")
            stats = sync(str(sources), str(Path(tmp) / "feeds.toml"))

            self.assertEqual(
                stats,
                {
                    "new": 0,
                    "skipped": 0,
                    "removed": 0,
                    "total": 0,
                    "errors": [],
                    "warnings": [],
                },
            )


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
