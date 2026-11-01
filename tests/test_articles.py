import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.articles import (
    STATUS_CLASSIFIED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SKIPPED,
    STATUS_UNCLASSIFIED,
    append_to_articles,
    archive_fields,
    article_archive_ref,
    article_keys,
    cleanup_articles,
    clear_content,
    get_article_links,
    get_pending_articles,
    load_articles,
    merge_fetched_entries,
    save_articles,
    set_content_hydrated,
    store_fetched_entries,
    summarize_articles,
    validate_articles_file,
)


def _iso(days: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _path(tmp: str) -> str:
    return str(Path(tmp) / "articles.json")


def _archive_record(**overrides):
    record = {
        "file": "archive/2026/05/01.md",
        "offset": 100,
        "length": 220,
        "body_len": 40,
        "sha256": "abc123",
    }
    record.update(overrides)
    return record


class ArchiveFieldTests(unittest.TestCase):
    def test_archive_fields_maps_index_record(self):
        self.assertEqual(
            archive_fields(_archive_record()),
            {
                "archive_file": "archive/2026/05/01.md",
                "archive_offset": 100,
                "archive_length": 220,
                "archive_body_len": 40,
                "archive_sha256": "abc123",
            },
        )

    def test_article_archive_ref_requires_file_and_offset(self):
        article = {
            "archive_file": "archive/2026/05/01.md",
            "archive_offset": 12,
            "archive_length": 99,
        }
        self.assertEqual(
            article_archive_ref(article),
            {"path": "archive/2026/05/01.md", "offset": 12, "length": 99},
        )
        self.assertIsNone(article_archive_ref({"archive_file": "x"}))
        self.assertIsNone(article_archive_ref({"archive_offset": 1}))

    def test_hydrate_and_clear_content(self):
        article = {}
        set_content_hydrated(article, "héllo")
        self.assertEqual(article["content"], "héllo")
        self.assertEqual(article["content_length"], len("héllo".encode("utf-8")))
        clear_content(article)
        self.assertNotIn("content", article)
        self.assertNotIn("content_length", article)


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
            self.assertNotIn("content", entry)

    def test_append_falls_back_to_guid_when_link_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            self.assertEqual(
                append_to_articles([{"guid": "g1", "title": "No link"}], path), 1
            )
            self.assertEqual(
                append_to_articles([{"guid": "g1", "title": "Again"}], path), 0
            )

    def test_store_adds_archive_ref_without_raw_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            entry = {
                "link": "https://example.com/a",
                "guid": "g1",
                "title": "A",
                "content": "<p>raw body</p>",
                "content_length": 17,
            }
            entry.update(archive_fields(_archive_record()))

            self.assertEqual(store_fetched_entries([entry], path), (1, 0))
            cache = load_articles(path)
            self.assertEqual(len(cache), 1)
            row = cache[0]
            self.assertNotIn("content", row)
            self.assertNotIn("content_length", row)
            self.assertEqual(row["archive_file"], "archive/2026/05/01.md")
            self.assertEqual(row["archive_offset"], 100)
            self.assertEqual(row["archive_body_len"], 40)
            self.assertEqual(row["status"], STATUS_PENDING)

    def test_store_reattaches_archive_ref_to_pending_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            save_articles(
                [
                    {
                        "link": "https://example.com/a",
                        "title": "Old title",
                        "status": STATUS_PENDING,
                        "cached_at": _iso(-1),
                    }
                ],
                path,
            )

            incoming = {
                "link": "https://www.example.com/a/?utm_source=x",
                "title": "New title",
                "content": "full body",
                "content_length": 9,
                "feed_title": "Feed",
            }
            incoming.update(archive_fields(_archive_record()))
            added, refreshed = store_fetched_entries([incoming], path)

            self.assertEqual((added, refreshed), (0, 1))
            cache = load_articles(path)
            self.assertEqual(len(cache), 1)
            self.assertEqual(cache[0]["archive_file"], "archive/2026/05/01.md")
            self.assertEqual(cache[0]["title"], "Old title")
            self.assertEqual(cache[0]["feed_title"], "Feed")
            self.assertEqual(cache[0]["status"], STATUS_PENDING)
            self.assertNotIn("content", cache[0])

    def test_store_does_not_touch_finished_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            save_articles(
                [
                    {
                        "link": "https://example.com/a",
                        "status": STATUS_CLASSIFIED,
                        "content": "",
                    }
                ],
                path,
            )

            incoming = {"link": "https://example.com/a", "content": "should not return"}
            incoming.update(archive_fields(_archive_record()))
            added, refreshed = merge_fetched_entries([incoming], path)

            self.assertEqual((added, refreshed), (0, 0))
            row = load_articles(path)[0]
            self.assertEqual(row["status"], STATUS_CLASSIFIED)
            self.assertNotIn("archive_file", row)
            self.assertNotIn("content", row)

    def test_article_keys_match_feed_index_format(self):
        self.assertEqual(
            article_keys(
                {"link": "https://www.Example.com/a/?utm_source=x", "guid": "g1"}
            ),
            ["link:example.com/a", "guid:g1"],
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
                    # Old article classified just now -> kept (classified_at wins).
                    {"link": "https://a.com/c-old-pub", "status": STATUS_CLASSIFIED,
                     "published": _iso(-30), "classified_at": _iso(-0.04)},
                ],
                path,
            )

            removed = cleanup_articles(keep_days=14, path=path)
            self.assertEqual(removed, 4)
            links = {a["link"] for a in load_articles(path)}
            self.assertEqual(
                links,
                {
                    "https://a.com/fallback",
                    "https://a.com/c-new",
                    "https://a.com/c-old-pub",
                },
            )

    def test_unclassified_uses_classified_at_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            save_articles(
                [
                    {"link": "https://a.com/u-old", "status": STATUS_UNCLASSIFIED,
                     "classified_at": _iso(-2)},
                    {"link": "https://a.com/u-new", "status": STATUS_UNCLASSIFIED,
                     "published": _iso(-30), "classified_at": _iso(-0.04)},
                ],
                path,
            )
            removed = cleanup_articles(keep_days=14, path=path)
            self.assertEqual(removed, 1)
            self.assertEqual(
                {a["link"] for a in load_articles(path)},
                {"https://a.com/u-new"},
            )

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

    def test_summarize_articles_uses_archive_body_lengths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            save_articles(
                [
                    {"link": "https://a.com/1", "status": STATUS_PENDING,
                     "published": "2026-01-01T00:00:00Z",
                     "archive_body_len": 10, "archive_file": "archive/x.md"},
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
            self.assertEqual(summary["archived"], 1)
            # 10 archive bytes + 20 + 0 (content was stripped) + 5.
            self.assertEqual(summary["content_bytes"], 35)

    def test_validate_articles_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            self.assertEqual(validate_articles_file(path), "missing")
            Path(path).write_text("{not json", encoding="utf-8")
            self.assertEqual(validate_articles_file(path), "corrupt")
            Path(path).write_text('{"not": "a list"}', encoding="utf-8")
            self.assertEqual(validate_articles_file(path), "corrupt")
            save_articles([], path)
            self.assertEqual(validate_articles_file(path), "ok")

    def test_save_articles_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "nested" / "dir" / "articles.json")
            save_articles([{"link": "https://a.com/1"}], path)
            self.assertTrue(Path(path).exists())

    def test_save_articles_never_writes_raw_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _path(tmp)
            save_articles(
                [{"link": "https://a.com/1", "content": "<p>secret</p>"}], path
            )
            self.assertNotIn("secret", Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
