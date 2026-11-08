import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.article_state import save_articles
from src.feed_state import save_state
from src.report import build_status, check_status, print_summary, write_status


def _iso(days: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class BuildStatusTests(unittest.TestCase):
    def test_build_status_collects_runtime_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "output"
            (data_dir / "2026").mkdir(parents=True)
            (data_dir / "2026" / "1029.md").write_text("| a |\n", encoding="utf-8")
            (data_dir / "index.md").write_text("# index\n", encoding="utf-8")

            feed_state_path = root / "feed_state.json"
            save_state({
                "feeds": {
                    "https://a.com/feed": {
                        "last_attempt": _iso(0),
                        "last_ok": _iso(0),
                    },
                    "https://b.com/feed": {"disabled": True},
                }
            }, str(feed_state_path))

            article_state_path = root / "article_state.json"
            save_articles([
                {"link": "https://a.com/1", "status": "pending"},
                {"link": "https://a.com/2", "status": "classified",
                 "classified_at": _iso(-1)},
                {"link": "https://a.com/3", "status": "skipped",
                 "classified_at": _iso(0)},
            ], str(article_state_path))

            status = build_status(
                data_dir=str(data_dir),
                feed_state_path=str(feed_state_path),
                article_state_path=str(article_state_path),
            )

            self.assertEqual(status["feeds"]["total"], 2)
            self.assertEqual(status["articles"]["total"], 3)
            self.assertEqual(status["articles"]["pending"], 1)
            self.assertEqual(status["output"]["days"], 1)
            self.assertEqual(status["output"]["last_date"], "2026-10-29")
            self.assertTrue(status["output"]["index_exists"])
            self.assertTrue(status["last_fetch"])
            self.assertTrue(status["last_classify"])
            self.assertEqual(status["warnings"], [])

    def test_disabled_ratio_is_a_warning_not_a_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "output"
            data_dir.mkdir()
            (data_dir / "index.md").write_text("# index\n", encoding="utf-8")
            feed_state_path = root / "feed_state.json"
            feeds = {
                f"https://f{i}.com/feed": {
                    "disabled": i < 9,
                    "last_attempt": _iso(0),
                    "last_ok": _iso(0),
                }
                for i in range(10)
            }
            save_state({"feeds": feeds}, str(feed_state_path))
            article_state_path = root / "article_state.json"
            save_articles([], str(article_state_path))

            status = build_status(
                data_dir=str(data_dir),
                feed_state_path=str(feed_state_path),
                article_state_path=str(article_state_path),
            )
            self.assertTrue(status["warnings"])
            self.assertEqual(check_status(status), [])

    def test_legacy_keyword_aliases_still_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "output"
            data_dir.mkdir()
            (data_dir / "index.md").write_text("# index\n", encoding="utf-8")

            state_path = root / "state.json"
            save_state({"feeds": {"https://a.com/feed": {"last_ok": _iso(0)}}},
                       str(state_path))
            articles_path = root / "articles.json"
            save_articles([{"link": "https://a.com/1", "status": "pending"}],
                          str(articles_path))

            status = build_status(
                data_dir=str(data_dir),
                state_path=str(state_path),
                articles_path=str(articles_path),
            )

            self.assertEqual(status["feeds"]["total"], 1)
            self.assertEqual(status["articles"]["total"], 1)
            self.assertEqual(status["articles"]["pending"], 1)

    def test_legacy_files_are_read_when_new_files_are_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "output"
            data_dir.mkdir()
            (data_dir / "index.md").write_text("# index\n", encoding="utf-8")

            old_cwd = Path.cwd()
            try:
                # The loaders only fall back to legacy names for default
                # relative paths, so run from the temporary directory.
                os.chdir(root)
                save_state({"feeds": {"https://a.com/feed": {"last_ok": _iso(0)}}},
                           "state.json")
                save_articles([{"link": "https://a.com/1", "status": "pending"}],
                              "articles.json")

                status = build_status(data_dir=str(data_dir))
            finally:
                os.chdir(old_cwd)

            self.assertFalse((root / "feed_state.json").exists())
            self.assertFalse((root / "article_state.json").exists())
            self.assertEqual(status["feeds"]["total"], 1)
            self.assertEqual(status["articles"]["total"], 1)
            self.assertEqual(status["articles"]["pending"], 1)

    def test_ai_disabled_does_not_warn_about_unclassified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "output"
            data_dir.mkdir()
            (data_dir / "index.md").write_text("# index\n", encoding="utf-8")
            feed_state_path = root / "feed_state.json"
            save_state({"feeds": {}}, str(feed_state_path))
            article_state_path = root / "article_state.json"
            save_articles([
                {"link": "https://a.com/1", "status": "unclassified",
                 "classified_at": _iso(0)},
            ], str(article_state_path))

            enabled = build_status(
                data_dir=str(data_dir),
                feed_state_path=str(feed_state_path),
                article_state_path=str(article_state_path),
                ai_enabled=True,
            )
            disabled = build_status(
                data_dir=str(data_dir),
                feed_state_path=str(feed_state_path),
                article_state_path=str(article_state_path),
                ai_enabled=False,
            )

            self.assertTrue(
                any("without AI" in warning for warning in enabled["warnings"])
            )
            self.assertEqual(disabled["warnings"], [])

    def test_last_fetch_fallback_ignores_failed_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "output"
            data_dir.mkdir()
            (data_dir / "index.md").write_text("# index\n", encoding="utf-8")
            feed_state_path = root / "feed_state.json"
            article_state_path = root / "article_state.json"
            save_articles([], str(article_state_path))

            save_state(
                {"feeds": {"https://a.com/feed": {"last_attempt": _iso(0)}}},
                str(feed_state_path),
            )
            failed_only = build_status(
                data_dir=str(data_dir),
                feed_state_path=str(feed_state_path),
                article_state_path=str(article_state_path),
            )
            self.assertEqual(failed_only["last_fetch"], "")

            save_state(
                {
                    "feeds": {
                        "https://a.com/feed": {
                            "last_attempt": _iso(0),
                            "last_ok": _iso(-1),
                        }
                    }
                },
                str(feed_state_path),
            )
            with_success = build_status(
                data_dir=str(data_dir),
                feed_state_path=str(feed_state_path),
                article_state_path=str(article_state_path),
            )
            self.assertTrue(with_success["last_fetch"])


class CheckStatusTests(unittest.TestCase):
    def test_all_feeds_disabled_is_a_violation(self):
        status = {
            "feeds": {"total": 3, "disabled": 3},
            "articles": {"pending": 0, "failed": 0, "classified": 0},
            "output": {"index_exists": True},
            "last_fetch": _iso(0),
            "last_classify": _iso(0),
        }
        violations = check_status(status)
        self.assertEqual(len(violations), 1)
        self.assertIn("all feeds are disabled", violations[0])

    def test_reports_backlog_and_freshness_violations(self):
        status = {
            "feeds": {"total": 1},
            "articles": {"pending": 5, "classified": 0},
            "output": {"index_exists": True},
            "last_fetch": _iso(-2),
            "last_classify": "",
        }
        violations = check_status(status, max_backlog=2, freshness_hours=24)
        self.assertEqual(len(violations), 3)
        joined = " ".join(violations)
        self.assertIn("backlog", joined)
        self.assertIn("last fetch", joined)
        self.assertIn("last classify", joined)

    def test_reports_missing_index_when_classified_articles_exist(self):
        status = {
            "feeds": {"total": 0},
            "articles": {"pending": 0, "classified": 3},
            "output": {"index_exists": False},
            "last_fetch": _iso(0),
            "last_classify": _iso(0),
        }
        violations = check_status(status)
        self.assertEqual(len(violations), 1)
        self.assertIn("index.md", violations[0])

    def test_healthy_status_has_no_violations(self):
        status = {
            "feeds": {"total": 2},
            "articles": {"pending": 1, "classified": 2},
            "output": {"index_exists": True},
            "last_fetch": _iso(0),
            "last_classify": _iso(0),
        }
        self.assertEqual(check_status(status), [])

    def test_ai_disabled_ignores_pending_backlog_and_classify_freshness(self):
        status = {
            "feeds": {"total": 1},
            "articles": {"pending": 5000, "failed": 0},
            "output": {"index_exists": True},
            "last_fetch": _iso(0),
            "last_classify": "",
        }
        self.assertEqual(
            check_status(status, max_backlog=10, ai_enabled=False), []
        )

    def test_ai_disabled_still_counts_real_failures(self):
        status = {
            "feeds": {"total": 1},
            "articles": {"pending": 5000, "failed": 20},
            "output": {"index_exists": True},
            "last_fetch": _iso(0),
            "last_classify": "",
        }
        violations = check_status(status, max_backlog=10, ai_enabled=False)
        self.assertEqual(len(violations), 1)
        self.assertIn("backlog", violations[0])
        self.assertIn("20 failed", violations[0])


class WriteStatusTests(unittest.TestCase):
    def test_writes_root_json_and_escaped_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = {
                "generated_at": _iso(0),
                "feeds": {"total": 1, "healthy": 1, "failing": 0, "disabled": 0,
                          "retry_pending": 0},
                "articles": {"total": 0, "pending": 0, "classified": 0,
                             "skipped": 0, "failed": 0},
                "output": {"days": 0, "last_date": "", "index_exists": True},
                "last_fetch": _iso(0),
                "last_classify": _iso(0),
                "warnings": ["<script>alert(1)</script>"],
            }
            write_status(status, root=str(root / "status.json"),
                         data_dir=str(root / "output"))

            self.assertTrue((root / "status.json").exists())
            self.assertFalse((root / "output" / "status.json").exists())
            self.assertEqual(
                json.loads((root / "status.json").read_text(encoding="utf-8")),
                status,
            )
            md = (root / "output" / "status.md").read_text(encoding="utf-8")
            self.assertNotIn("<script>", md)
            self.assertIn("&lt;script&gt;", md)

    def test_print_summary_returns_markdown(self):
        status = {
            "feeds": {"healthy": 1, "failing": 0, "disabled": 0},
            "articles": {"pending": 2, "classified": 3, "skipped": 1, "failed": 0},
            "output": {"days": 4, "index_exists": True},
            "last_fetch": _iso(0),
            "last_classify": _iso(0),
            "warnings": [],
        }
        text = print_summary(status)
        self.assertIn("| Metric | Value |", text)
        self.assertIn("2/3/0/1/0", text)


if __name__ == "__main__":
    unittest.main()
