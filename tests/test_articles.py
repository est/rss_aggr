import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.articles import (
    STATUS_CLASSIFIED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SKIPPED,
    append_to_articles,
    cleanup_articles,
    get_article_links,
    get_pending_articles,
    load_articles,
    save_articles,
    summarize_articles,
)


def _iso(days: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _path(tmp: str) -> str:
    return str(Path(tmp) / "articles.json")


class ArticleCacheTests(unittest.TestCase):
    def test_append_adds_metadata_and_dedups_canonical_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            articles = [
                {
                    "link": "https://www.Example.com/post/?utm_source=x",
                    "title": "A",
                    "content": "hello",
                },
                {
                    "link": "https://example.com/post",
                    "title": "Duplicate",
                    "content": "ignored",
                },
            ]
            self.assertEqual(append_to_articles(articles, path), 1)

            cache = load_articles(path)
            self.assertEqual(len(cache), 1)
            entry = cache[0]
            self.assertEqual(entry["canonical"], "example.com/post")
            self.assertEqual(entry["status"], STATUS_PENDING)
            self.assertEqual(entry["attempts"], 0)
            self.assertTrue(entry["cached_at"])
            self.assertEqual(entry["content_length"], len("hello"))

    def test_append_falls_back_to_guid_when_link_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            self.assertEqual(
                append_to_articles([{"guid": "g1", "title": "No link"}], path), 1
            )
            self.assertEqual(
                append_to_articles([{"guid": "g1", "title": "Again"}], path), 0
            )

    def test_get_pending_sorts_and_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            save_articles(
                [
                    {"link": "https://a.com/2", "cached_at": "2026-01-02T00:00:00Z"},
                    {"link": "https://a.com/1", "cached_at": "2026-01-01T00:00:00Z"},
                    {"link": "https://a.com/3", "cached_at": "2026-01-03T00:00:00Z",
                     "status": STATUS_CLASSIFIED},
                    {"link": "https://a.com/4", "cached_at": "2026-01-04T00:00:00Z",
                     "status": STATUS_FAILED},
                ],
                path,
            )
            pending = get_pending_articles(path=path)
            self.assertEqual([a["link"] for a in pending], [
                "https://a.com/1", "https://a.com/2", "https://a.com/4",
            ])
            limited = get_pending_articles(limit=1, path=path)
            self.assertEqual([a["link"] for a in limited], ["https://a.com/1"])

    def test_cleanup_uses_status_specific_windows_and_date_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            save_articles(
                [
                    # Classified more than one day old -> removed.
                    {"link": "https://a.com/c-old", "status": STATUS_CLASSIFIED,
                     "published": _iso(-2)},
                    # Skipped more than one day old -> removed.
                    {"link": "https://a.com/s-old", "status": STATUS_SKIPPED,
                     "cached_at": _iso(-2)},
                    # Pending older than keep_days -> removed.
                    {"link": "https://a.com/p-old", "status": STATUS_PENDING,
                     "published": _iso(-30)},
                    # Invalid published falls back to a recent cached_at -> kept.
                    {"link": "https://a.com/fallback", "status": STATUS_PENDING,
                     "published": "not-a-date", "cached_at": _iso(-1)},
                    # Both dates malformed -> expired.
                    {"link": "https://a.com/broken", "status": STATUS_PENDING,
                     "published": "nope", "cached_at": "also-nope"},
                    # Recent classified -> kept.
                    {"link": "https://a.com/c-new", "status": STATUS_CLASSIFIED,
                     "published": _iso(0)},
                ],
                path,
            )

            removed = cleanup_articles(keep_days=14, path=path)
            self.assertEqual(removed, 4)
            links = {a["link"] for a in load_articles(path)}
            self.assertEqual(links, {"https://a.com/fallback", "https://a.com/c-new"})

    def test_get_article_links_returns_canonical_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            save_articles(
                [
                    {"link": "https://www.Example.com/a/?utm_source=x"},
                    {"link": "http://example.com/b"},
                ],
                path,
            )
            self.assertEqual(get_article_links(path), {"example.com/a", "example.com/b"})

    def test_summarize_articles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            save_articles(
                [
                    {"link": "https://a.com/1", "status": STATUS_PENDING,
                     "published": "2026-01-01T00:00:00Z", "content_length": 10},
                    {"link": "https://a.com/2", "status": STATUS_CLASSIFIED,
                     "published": "not-a-date", "content_length": 20},
                    {"link": "https://a.com/3", "status": STATUS_SKIPPED,
                     "published": "2026-01-01T00:00:00Z", "content": "abc"},
                    {"link": "https://a.com/4", "status": STATUS_FAILED,
                     "published": "", "content_length": 5},
                ],
                path,
            )
            summary = summarize_articles(path)
            self.assertEqual(summary["total"], 4)
            self.assertEqual(summary["pending"], 1)
            self.assertEqual(summary["classified"], 1)
            self.assertEqual(summary["skipped"], 1)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["invalid_dates"], 2)
            self.assertEqual(summary["content_bytes"], 38)

    def test_save_articles_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "nested" / "dir" / "articles.json")
            save_articles([{"link": "https://a.com/1"}], path)
            self.assertTrue(Path(path).exists())


if __name__ == "__main__":
    unittest.main()
