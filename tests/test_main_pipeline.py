import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import md_archive
from src.article_state import (
    archive_fields,
    article_archive_ref,
    load_articles,
    save_articles,
)
from src.classifier import ClassifyStats
from src.main import _storage_paths, step_classify, step_fetch


def _fetch_config():
    return {
        "fetch": {"timeout_seconds": 5, "keep_days": 7, "user_agent": "test"},
        "filter": {"skip_titles": []},
        "limits": {
            "max_feeds": 10,
            "max_workers": 3,
            "max_entries_per_feed": 7,
            "fetch_interval_hours": 1,
            "disable_after_failures": 3,
        },
        "storage": {
            "data_dir": "output",
            "archive_dir": "archive",
            "feeds_dir": "feeds",
        },
    }


def _classify_config(archive_dir="archive", feeds_dir="feeds"):
    return {
        "ai": {"provider": "custom", "model": "m", "base_url": "", "api_key_env": ""},
        "filter": {"skip_prompt": "global rule"},
        "limits": {"max_articles": 0},
        "fetch": {"keep_days": 3650},
        "storage": {
            "data_dir": "output",
            "archive_dir": archive_dir,
            "feeds_dir": feeds_dir,
        },
        "category": [{"name": "Tech"}],
    }


def _feed(host="a.com", title="A Feed"):
    return {
        "xml_url": f"https://{host}/feed.xml",
        "html_url": f"https://{host}",
        "title": title,
        "priority": 1,
    }


def _entry(link="https://a.com/post/1", guid="g1", content="<p>Body</p>"):
    return {
        "guid": guid,
        "title": "Post",
        "link": link,
        "author": "Author",
        "content": content,
        "published": "2026-11-01T00:00:00Z",
        "feed_title": "A Feed",
        "feed_category": "Tech",
    }


def _seed_archived(
    body="Hydrated body.\n",
    link="https://a.com/ok",
    published="2026-11-01T00:00:00Z",
    feed_title="A Feed",
):
    """Create a real archive record + feed index + metadata row in cwd."""
    feed_meta = {
        "feed_id": md_archive.feed_id("https://a.com/feed.xml", "https://a.com", feed_title),
        "feed_url": "https://a.com/feed.xml",
        "site_url": "https://a.com",
        "title": feed_title,
    }
    meta = {
        "guid": "g-ok",
        "feed_id": feed_meta["feed_id"],
        "feed_url": feed_meta["feed_url"],
        "site_url": feed_meta["site_url"],
        "link": link,
        "title": "Post",
        "author": "Author",
        "published": published,
        "fetched_at": published,
    }
    record = md_archive.append_articles(".", [(meta, body)])[0]
    md_archive.append_feed_record("feeds", feed_meta, record)

    article = {
        "guid": "g-ok",
        "link": link,
        "title": "Post",
        "author": "Author",
        "published": published,
        "cached_at": published,
        "feed_id": feed_meta["feed_id"],
        "feed_url": feed_meta["feed_url"],
        "site_url": feed_meta["site_url"],
        "feed_title": feed_title,
        "status": "pending",
        "attempts": 0,
    }
    article.update(archive_fields(record))
    save_articles([article])
    return article, record, feed_meta


class StorageLayoutTests(unittest.TestCase):
    def test_fixed_layout_is_default(self):
        self.assertEqual(_storage_paths({}), ("output", "archive", "feeds"))

    def test_storage_override_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            _storage_paths({"storage": {"archive_dir": "elsewhere"}})


class FetchStepTests(unittest.TestCase):
    def test_not_modified_is_treated_as_success(self):
        feed = _feed()
        state = {"feeds": {}}
        results = [{"feed": feed, "status": "not_modified", "entries": []}]

        with patch("src.main.load_config", return_value=_fetch_config()), \
                patch("src.main.parse_feeds", return_value=[feed]), \
                patch("src.main.load_state", return_value=state), \
                patch("src.main.save_state") as save_state, \
                patch("src.main.validate_articles_file", return_value="ok"), \
                patch("src.fetcher.fetch_all_feeds", return_value=results) as fetch_all:
            step_fetch()

        save_state.assert_called_once()
        self.assertEqual(state["feeds"][feed["xml_url"]]["last_status"], "not_modified")
        self.assertEqual(state["feeds"][feed["xml_url"]]["consecutive_failures"], 0)
        _, kwargs = fetch_all.call_args
        self.assertEqual(kwargs["max_workers"], 3)
        self.assertEqual(kwargs["max_entries_per_feed"], 7)

    def test_fetch_archives_sanitized_markdown_and_metadata_only(self):
        feed = _feed()
        raw = '<p>Hello <script>alert("x")</script><a href="/rel">link</a></p>'
        results = [{"feed": feed, "status": "ok", "entries": [_entry(content=raw)]}]
        state = {"feeds": {}}

        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with patch("src.main.load_config", return_value=_fetch_config()), \
                    patch("src.main.parse_feeds", return_value=[feed]), \
                    patch("src.main.load_state", return_value=state), \
                    patch("src.main.save_state"), \
                    patch("src.main.validate_articles_file", return_value="ok"), \
                    patch("src.fetcher.fetch_all_feeds", return_value=results):
                step_fetch()

            archive_path = Path("archive/2026/11/01.md")
            self.assertTrue(archive_path.is_file())
            archived = archive_path.read_text(encoding="utf-8")
            self.assertIn("Hello", archived)
            self.assertNotIn("<script", archived)
            self.assertNotIn("alert", archived)
            self.assertIn("https://a.com/rel", archived)

            articles = load_articles()
            self.assertEqual(len(articles), 1)
            article = articles[0]
            self.assertNotIn("content", article)
            self.assertNotIn("content_length", article)
            self.assertEqual(article["status"], "pending")
            self.assertEqual(article["feed_id"], md_archive.feed_id(
                feed["xml_url"], feed["html_url"], feed["title"]
            ))
            self.assertEqual(article["feed_title"], feed["title"])
            self.assertTrue(article["archive_file"].startswith("archive/"))

            ref = article_archive_ref(article)
            parsed = md_archive.read_record(
                ref["path"], ref["offset"], ref["length"]
            )
            self.assertIn("Hello", parsed["body"])
            self.assertEqual(parsed["sha256"], article["archive_sha256"])

            index_path = Path("feeds") / f"{article['feed_id']}.jsonl"
            self.assertTrue(index_path.is_file())
            header, records = md_archive.read_feed_index(index_path)
            self.assertEqual(header["title"], feed["title"])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["kind"], "article")
            self.assertEqual(records[0]["offset"], article["archive_offset"])

            # A rerun with the same feed payload must not duplicate anything.
            archive_size = archive_path.stat().st_size
            index_size = index_path.stat().st_size
            with patch("src.main.load_config", return_value=_fetch_config()), \
                    patch("src.main.parse_feeds", return_value=[feed]), \
                    patch("src.main.load_state", return_value=state), \
                    patch("src.main.save_state"), \
                    patch("src.main.validate_articles_file", return_value="ok"), \
                    patch("src.fetcher.fetch_all_feeds", return_value=results):
                step_fetch()

            self.assertEqual(archive_path.stat().st_size, archive_size)
            self.assertEqual(index_path.stat().st_size, index_size)
            self.assertEqual(len(load_articles()), 1)

    def test_cross_feed_duplicates_are_not_archived_twice(self):
        feed_a = _feed("a.com", "A")
        feed_b = _feed("b.com", "B")
        entry = _entry(link="https://shared.example/post", guid="same-guid")
        results = [
            {"feed": feed_a, "status": "ok", "entries": [entry]},
            {
                "feed": feed_b,
                "status": "ok",
                "entries": [dict(entry, feed_title="B")],
            },
        ]
        state = {"feeds": {}}

        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with patch("src.main.load_config", return_value=_fetch_config()), \
                    patch("src.main.parse_feeds", return_value=[feed_a, feed_b]), \
                    patch("src.main.load_state", return_value=state), \
                    patch("src.main.save_state"), \
                    patch("src.main.validate_articles_file", return_value="ok"), \
                    patch("src.fetcher.fetch_all_feeds", return_value=results):
                step_fetch()

            records = list(md_archive.iter_records("archive/2026/11/01.md"))
            self.assertEqual(len(records), 1)
            self.assertEqual(len(load_articles()), 1)
            b_id = md_archive.feed_id(feed_b["xml_url"], feed_b["html_url"], feed_b["title"])
            self.assertFalse((Path("feeds") / f"{b_id}.jsonl").exists())

    def test_archive_append_failure_fails_the_step(self):
        feed = _feed()
        results = [{"feed": feed, "status": "ok", "entries": [_entry()]}]
        state = {"feeds": {}}

        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with patch("src.main.load_config", return_value=_fetch_config()), \
                    patch("src.main.parse_feeds", return_value=[feed]), \
                    patch("src.main.load_state", return_value=state), \
                    patch("src.main.save_state"), \
                    patch("src.main.validate_articles_file", return_value="ok"), \
                    patch("src.fetcher.fetch_all_feeds", return_value=results), \
                    patch(
                        "src.main.md_archive.append_articles",
                        side_effect=OSError("disk full"),
                    ):
                with self.assertRaisesRegex(RuntimeError, "disk full"):
                    step_fetch()

    def test_feed_index_append_failure_fails_the_step(self):
        feed = _feed()
        results = [{"feed": feed, "status": "ok", "entries": [_entry()]}]
        state = {"feeds": {}}

        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with patch("src.main.load_config", return_value=_fetch_config()), \
                    patch("src.main.parse_feeds", return_value=[feed]), \
                    patch("src.main.load_state", return_value=state), \
                    patch("src.main.save_state"), \
                    patch("src.main.validate_articles_file", return_value="ok"), \
                    patch("src.fetcher.fetch_all_feeds", return_value=results), \
                    patch(
                        "src.main.md_archive.append_feed_record",
                        side_effect=OSError("index full"),
                    ):
                with self.assertRaisesRegex(RuntimeError, "index full"):
                    step_fetch()

    def test_missing_articles_cache_forces_full_fetch(self):
        feed = _feed()
        state = {"feeds": {feed["xml_url"]: {"etag": '"old"'}}}
        results = [{"feed": feed, "status": "ok", "entries": []}]

        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with patch("src.main.load_config", return_value=_fetch_config()), \
                    patch("src.main.parse_feeds", return_value=[feed]), \
                    patch("src.main.load_state", return_value=state), \
                    patch("src.main.save_state"), \
                    patch("src.main.validate_articles_file", return_value="missing"), \
                    patch("src.main.save_articles") as save_articles, \
                    patch("src.fetcher.fetch_all_feeds", return_value=results) as fetch_all:
                step_fetch()

            _, kwargs = fetch_all.call_args
            self.assertEqual(kwargs["feeds_state"], {})
            save_articles.assert_called_once_with([])

    def test_cache_miss_env_forces_immediate_refetch(self):
        feed = _feed()
        state = {"feeds": {}}
        results = [{"feed": feed, "status": "not_modified", "entries": []}]

        with patch.dict(os.environ, {"RSS_FORCE_FETCH": "1"}), \
                patch("src.main.load_config", return_value=_fetch_config()), \
                patch("src.main.parse_feeds", return_value=[feed]), \
                patch("src.main.load_state", return_value=state), \
                patch("src.main.save_state"), \
                patch("src.main.validate_articles_file", return_value="ok"), \
                patch("src.main.get_due_feeds", return_value=[feed]) as due, \
                patch("src.fetcher.fetch_all_feeds", return_value=results) as fetch_all:
            step_fetch()

        self.assertEqual(due.call_args.args[2], 0)
        self.assertEqual(fetch_all.call_args.kwargs["feeds_state"], {})

    def test_all_due_feeds_failing_raises(self):
        feed = _feed()
        state = {"feeds": {}}
        results = [{"feed": feed, "status": "error", "error": "boom", "entries": []}]

        with patch("src.main.load_config", return_value=_fetch_config()), \
                patch("src.main.parse_feeds", return_value=[feed]), \
                patch("src.main.load_state", return_value=state), \
                patch("src.main.save_state") as save_state, \
                patch("src.main.validate_articles_file", return_value="ok"), \
                patch("src.fetcher.fetch_all_feeds", return_value=results):
            with self.assertRaises(RuntimeError):
                step_fetch()

        save_state.assert_called_once()
        self.assertEqual(state["feeds"][feed["xml_url"]]["last_error"], "boom")

    def test_output_is_not_a_dedup_authority(self):
        feed = _feed()
        results = [
            {
                "feed": feed,
                "status": "ok",
                "entries": [_entry(link="https://a.com/post/1")],
            }
        ]
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            Path("output/2026").mkdir(parents=True)
            Path("output/2026/0101.md").write_text(
                "# 2026-01-01\n\n"
                "| Author | Title | Category | Summary | Score |\n"
                "|--------|-------|----------|---------|-------|\n"
                "| A | [Post](https://a.com/post/1) | Tech | old | 5 |\n",
                encoding="utf-8",
            )
            with patch("src.main.load_config", return_value=_fetch_config()), \
                    patch("src.main.parse_feeds", return_value=[feed]), \
                    patch("src.main.load_state", return_value={"feeds": {}}), \
                    patch("src.main.save_state"), \
                    patch("src.main.validate_articles_file", return_value="ok"), \
                    patch("src.fetcher.fetch_all_feeds", return_value=results):
                step_fetch()

            rows = load_articles()
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["archive_file"])
            self.assertTrue(rows[0]["indexed"])

    def test_pending_index_flag_is_repaired_from_archive(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            article, _record, feed_meta = _seed_archived()
            article["indexed"] = False
            save_articles([article])
            index_path = Path("feeds") / f"{feed_meta['feed_id']}.jsonl"
            index_path.unlink()

            with patch("src.main.load_config", return_value=_fetch_config()), \
                    patch("src.main.parse_feeds", return_value=[]), \
                    patch("src.main.load_state", return_value={"feeds": {}}), \
                    patch("src.main.save_state"), \
                    patch("src.main.validate_articles_file", return_value="ok"):
                step_fetch()

            rows = load_articles()
            self.assertTrue(rows[0]["indexed"])
            header, records = md_archive.read_feed_index(index_path)
            self.assertEqual(header.get("feed_id"), feed_meta["feed_id"])
            self.assertTrue(
                any(r.get("guid") == "g-ok" for r in records),
                records,
            )


class ClassifyStepTests(unittest.TestCase):
    def test_classify_hydrates_body_and_appends_annotation(self):
        seen = {}

        def fake_classify(articles, **kwargs):
            seen["content"] = articles[0]["content"]
            seen["skip_prompt"] = kwargs.get("skip_prompt")
            for article in articles:
                article["classification"] = {
                    "category": "Tech", "score": 8, "summary": "good",
                }
            return ClassifyStats(articles=len(articles), classified=len(articles))

        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            article, record, feed_meta = _seed_archived()
            with patch("src.main.load_config", return_value=_classify_config()), \
                    patch("src.main.parse_feeds", return_value=[]), \
                    patch("src.main.load_state", return_value={"feeds": {}}), \
                    patch("src.main.save_state") as save_state, \
                    patch("src.main.classify_articles", side_effect=fake_classify):
                step_classify()

            self.assertEqual(seen["content"], "Hydrated body.\n")
            self.assertEqual(seen["skip_prompt"], "global rule")

            rows = load_articles()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["status"], "classified")
            self.assertTrue(row["classified_at"])
            self.assertNotIn("content", row)
            self.assertNotIn("content_length", row)
            raw = Path("article_state.json").read_text(encoding="utf-8")
            self.assertNotIn("Hydrated body", raw)

            index_path = Path("feeds") / f"{feed_meta['feed_id']}.jsonl"
            _, records = md_archive.read_feed_index(index_path)
            annotations = [r for r in records if r.get("kind") == "annotation"]
            self.assertEqual(len(annotations), 1)
            annotation = annotations[0]
            self.assertEqual(annotation["guid"], "g-ok")
            self.assertEqual(annotation["file"], record["file"])
            self.assertEqual(annotation["offset"], record["offset"])
            self.assertEqual(annotation["category"], "Tech")
            self.assertEqual(annotation["summary"], "good")
            self.assertEqual(annotation["score"], 8)
            self.assertTrue(annotation["classified_at"])

            output = Path("output/2026/1101.md").read_text(encoding="utf-8")
            self.assertIn("Post", output)
            self.assertIn("Tech", output)
            save_state.assert_called()

    def test_classify_title_only_when_ai_unavailable(self):
        def fake_classify(articles, **kwargs):
            return ClassifyStats(
                articles=len(articles), failed=len(articles), provider_failed=True
            )

        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            article, _record, feed_meta = _seed_archived(body="Some body.\n")
            with patch("src.main.load_config", return_value=_classify_config()), \
                    patch("src.main.parse_feeds", return_value=[]), \
                    patch("src.main.load_state", return_value={"feeds": {}}), \
                    patch("src.main.save_state"), \
                    patch("src.main.classify_articles", side_effect=fake_classify):
                step_classify()

            rows = load_articles()
            self.assertEqual(rows[0]["status"], "unclassified")
            self.assertEqual(rows[0]["attempts"], 1)
            self.assertEqual(rows[0]["unclassified_reason"], "provider_unavailable")
            self.assertNotIn("content", rows[0])

            index_path = Path("feeds") / f"{feed_meta['feed_id']}.jsonl"
            _, records = md_archive.read_feed_index(index_path)
            annotations = [r for r in records if r.get("kind") == "annotation"]
            # Title-only rows have no classification to annotate.
            self.assertEqual(len(annotations), 0)

    def test_missing_archive_record_keeps_article_pending(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            article, record, _feed_meta = _seed_archived()
            Path(record["file"]).unlink()

            with patch("src.main.load_config", return_value=_classify_config()), \
                    patch("src.main.parse_feeds", return_value=[]), \
                    patch("src.main.load_state", return_value={"feeds": {}}), \
                    patch("src.main.save_state"), \
                    patch("src.main.classify_articles") as classify:
                step_classify()

            classify.assert_not_called()
            rows = load_articles()
            self.assertEqual(rows[0]["status"], "pending")
            self.assertIn("archive_error", rows[0])
            self.assertNotIn("content", rows[0])

    def test_stale_backlog_is_not_sent_to_ai(self):
        cached = [
            {
                "link": "https://a.com/stale",
                "status": "pending",
                "published": "2020-01-01T00:00:00Z",
            }
        ]
        config = _classify_config()
        config["fetch"] = {"keep_days": 1}

        with patch("src.main.load_config", return_value=config), \
                patch("src.main.parse_feeds", return_value=[]), \
                patch("src.main.load_articles", return_value=cached), \
                patch("src.main.load_state", return_value={"feeds": {}}), \
                patch("src.main.save_state") as save_state, \
                patch("src.main.classify_articles") as classify, \
                patch("src.main.save_articles"), \
                patch("src.main.save_daily_results"), \
                patch("src.main._update_index") as update_index:
            step_classify()

        classify.assert_not_called()
        save_state.assert_not_called()
        update_index.assert_called_once()

    def test_all_skipped_does_not_fail(self):
        def fake_classify(articles, **kwargs):
            for article in articles:
                article["classification"] = {
                    "category": "skip", "score": 5, "summary": "",
                }
            return ClassifyStats(articles=len(articles), skipped=len(articles))

        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            _seed_archived()
            with patch("src.main.load_config", return_value=_classify_config()), \
                    patch("src.main.parse_feeds", return_value=[]), \
                    patch("src.main.load_state", return_value={"feeds": {}}), \
                    patch("src.main.save_state"), \
                    patch("src.main.classify_articles", side_effect=fake_classify):
                step_classify()

            self.assertEqual(load_articles()[0]["status"], "skipped")

    def test_annotation_failure_keeps_article_pending(self):
        out = io.StringIO()

        def fake_classify(articles, **kwargs):
            for article in articles:
                article["classification"] = {
                    "category": "Tech", "score": 8, "summary": "s",
                }
            return ClassifyStats(articles=len(articles), classified=len(articles))

        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            _seed_archived()
            with contextlib.redirect_stdout(out), \
                    patch("src.main.load_config", return_value=_classify_config()), \
                    patch("src.main.parse_feeds", return_value=[]), \
                    patch("src.main.load_state", return_value={"feeds": {}}), \
                    patch("src.main.save_state"), \
                    patch("src.main.classify_articles", side_effect=fake_classify), \
                    patch(
                        "src.main.md_archive.append_feed_annotation",
                        side_effect=OSError("disk full"),
                    ):
                step_classify()

            row = load_articles()[0]
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 1)
            self.assertIn("annotation_error", row)
            self.assertIn("WARNING", out.getvalue())
            self.assertIn("disk full", out.getvalue())
            # No output row is committed without its annotation.
            self.assertFalse(Path("output/2026/1101.md").is_file())

    def test_archive_error_is_reported_in_articles_json(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            _article, record, _feed_meta = _seed_archived()
            Path(record["file"]).unlink()
            with patch("src.main.load_config", return_value=_classify_config()), \
                    patch("src.main.parse_feeds", return_value=[]), \
                    patch("src.main.load_state", return_value={"feeds": {}}), \
                    patch("src.main.save_state"), \
                    patch("src.main.classify_articles"):
                step_classify()

            data = json.loads(Path("article_state.json").read_text(encoding="utf-8"))["articles"]
            self.assertEqual(data[0]["status"], "pending")
            self.assertIn("archive_error", data[0])
            self.assertNotIn("content", data[0])


class SkipPromptCombinationTests(unittest.TestCase):
    def test_global_and_site_prompts_are_combined(self):
        from src.main import _combine_skip_prompts

        self.assertEqual(_combine_skip_prompts("global", "site"), "global site")
        self.assertEqual(_combine_skip_prompts("global", ""), "global")
        self.assertEqual(_combine_skip_prompts("", "site"), "site")
        self.assertEqual(_combine_skip_prompts("same", "same"), "same")


if __name__ == "__main__":
    unittest.main()
