import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from src import md_archive
from src.article_state import load_articles
from src.backfill import backfill, main


def _legacy_articles():
    return [
        {
            "guid": "g1",
            "link": "https://www.example.com/a/?utm_source=x",
            "title": "A",
            "author": "Author",
            "published": "2026-05-01T12:00:00Z",
            "cached_at": "2026-05-02T00:00:00Z",
            "feed_title": "Example Feed",
            "status": "pending",
            "content": '<p>Hello <script>alert("x")</script></p>',
            "content_length": 42,
        },
        {
            "guid": "g2",
            "link": "https://example.com/b",
            "title": "B",
            "author": "",
            "published": "2026-05-02T03:00:00Z",
            "cached_at": "2026-05-02T04:00:00Z",
            "feed_title": "Example Feed",
            "status": "classified",
            "classification": {"category": "Tech", "score": 8, "summary": "ok"},
            "content": "<p>Second body</p>",
            "content_length": 18,
        },
    ]


class BackfillTests(unittest.TestCase):
    def test_backfill_migrates_content_into_archive_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            Path("old.json").write_text(
                json.dumps(_legacy_articles()), encoding="utf-8"
            )

            stats = backfill(
                "old.json", root=".", feeds_dir="feeds", out="articles.json"
            )

            self.assertEqual(stats["entries"], 2)
            self.assertEqual(stats["archived"], 2)
            self.assertEqual(stats["skipped_existing"], 0)

            first = Path("archive/2026/05/01.md")
            second = Path("archive/2026/05/02.md")
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            self.assertNotIn(
                "<script", first.read_text(encoding="utf-8")
            )
            self.assertNotIn("alert", first.read_text(encoding="utf-8"))

            rows = load_articles("articles.json")
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertNotIn("content", row)
                self.assertNotIn("content_length", row)
                self.assertTrue(row["archive_file"].startswith("archive/"))
                self.assertIn("archive_offset", row)
                self.assertIn("archive_length", row)
                self.assertIn("archive_body_len", row)
                self.assertIn("archive_sha256", row)

            self.assertEqual(rows[0]["status"], "pending")
            self.assertEqual(rows[0]["canonical"], "example.com/a")
            self.assertEqual(rows[1]["status"], "classified")
            self.assertEqual(rows[1]["classification"]["category"], "Tech")

            feed_id = rows[0]["feed_id"]
            index_path = Path("feeds") / f"{feed_id}.jsonl"
            self.assertTrue(index_path.is_file())
            header, records = md_archive.read_feed_index(index_path)
            self.assertEqual(header["title"], "Example Feed")
            articles = [r for r in records if r.get("kind") == "article"]
            self.assertEqual(len(articles), 2)

            parsed = md_archive.read_record(
                first, articles[0]["offset"], articles[0]["length"]
            )
            self.assertIn("Hello", parsed["body"])
            self.assertNotIn("<script", parsed["body"])
            self.assertEqual(articles[0]["sha256"], rows[0]["archive_sha256"])

    def test_backfill_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            Path("old.json").write_text(
                json.dumps(_legacy_articles()), encoding="utf-8"
            )
            backfill("old.json", root=".", feeds_dir="feeds", out="articles.json")

            archive_files = sorted(Path("archive").rglob("*.md"))
            sizes = {path: path.stat().st_size for path in archive_files}
            index_path = next(Path("feeds").glob("*.jsonl"))
            index_size = index_path.stat().st_size

            stats = backfill(
                "old.json", root=".", feeds_dir="feeds", out="articles.json"
            )

            self.assertEqual(stats["archived"], 0)
            self.assertEqual(stats["skipped_existing"], 2)
            for path in archive_files:
                self.assertEqual(path.stat().st_size, sizes[path])
            self.assertEqual(index_path.stat().st_size, index_size)

            rows = load_articles("articles.json")
            self.assertEqual(len(rows), 2)
            self.assertTrue(all("content" not in row for row in rows))
            self.assertTrue(all("archive_file" in row for row in rows))

    def test_backfill_dedups_canonical_variants_and_skips_unidentifiable(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            articles = [
                {
                    "guid": "g1",
                    "link": "https://example.com/a/?utm_source=x",
                    "title": "First",
                    "published": "2026-05-01T12:00:00Z",
                    "content": "<p>one</p>",
                },
                {
                    "guid": "g2",
                    "link": "https://www.example.com/a",
                    "title": "Duplicate",
                    "published": "2026-05-01T12:00:00Z",
                    "content": "<p>two</p>",
                },
                {"title": "No identity", "content": "<p>lost</p>"},
            ]
            Path("old.json").write_text(json.dumps(articles), encoding="utf-8")

            stats = backfill(
                "old.json", root=".", feeds_dir="feeds", out="articles.json"
            )

            self.assertEqual(stats["entries"], 1)
            self.assertEqual(stats["archived"], 1)
            records = list(md_archive.iter_records("archive/2026/05/01.md"))
            self.assertEqual(len(records), 1)
            self.assertIn("one", records[0]["body"])
            self.assertEqual(len(load_articles("articles.json")), 1)

    def test_cli_writes_out_file(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            Path("old.json").write_text(
                json.dumps(_legacy_articles()), encoding="utf-8"
            )
            code = main(
                [
                    "--articles",
                    "old.json",
                    "--root",
                    ".",
                    "--feeds",
                    "feeds",
                    "--out",
                    "articles.json",
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(Path("articles.json").is_file())
            self.assertTrue(Path("feeds").is_dir())
            self.assertTrue(Path("archive").is_dir())


if __name__ == "__main__":
    unittest.main()
