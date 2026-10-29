import unittest
from unittest.mock import patch

from src.classifier import ClassifyStats
from src.main import step_classify, step_fetch


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
        "storage": {"data_dir": "out"},
    }


def _classify_config():
    return {
        "ai": {"provider": "custom", "model": "m", "base_url": "", "api_key_env": ""},
        "filter": {"skip_prompt": "global rule"},
        "limits": {"max_articles": 0},
        "fetch": {"keep_days": 3650},
        "storage": {"data_dir": "out"},
        "category": [{"name": "Tech"}],
    }


class FetchStepTests(unittest.TestCase):
    def test_not_modified_is_treated_as_success(self):
        feed = {"xml_url": "https://a.com/feed", "title": "A", "priority": 1}
        state = {"feeds": {}}
        results = [{"feed": feed, "status": "not_modified", "entries": []}]

        with patch("src.main.load_config", return_value=_fetch_config()), \
                patch("src.main.parse_feeds", return_value=[feed]), \
                patch("src.main.load_state", return_value=state), \
                patch("src.main.save_state") as save_state, \
                patch("src.main.validate_articles_file", return_value="ok"), \
                patch("src.fetcher.fetch_all_feeds", return_value=results) as fetch_all, \
                patch("src.main.load_seen_links", return_value=set()), \
                patch("src.main.get_article_links", return_value=set()), \
                patch("src.main.append_to_articles") as append:
            step_fetch()

        save_state.assert_called_once()
        append.assert_not_called()
        self.assertEqual(state["feeds"][feed["xml_url"]]["last_status"], "not_modified")
        self.assertEqual(state["feeds"][feed["xml_url"]]["consecutive_failures"], 0)
        _, kwargs = fetch_all.call_args
        self.assertEqual(kwargs["max_workers"], 3)
        self.assertEqual(kwargs["max_entries_per_feed"], 7)

    def test_missing_articles_cache_forces_full_fetch(self):
        feed = {"xml_url": "https://a.com/feed", "title": "A", "priority": 1}
        state = {"feeds": {feed["xml_url"]: {"etag": '"old"'}}}
        results = [{"feed": feed, "status": "ok", "entries": []}]

        with patch("src.main.load_config", return_value=_fetch_config()), \
                patch("src.main.parse_feeds", return_value=[feed]), \
                patch("src.main.load_state", return_value=state), \
                patch("src.main.save_state"), \
                patch("src.main.validate_articles_file", return_value="missing"), \
                patch("src.main.save_articles") as save_articles, \
                patch("src.fetcher.fetch_all_feeds", return_value=results) as fetch_all, \
                patch("src.main.load_seen_links", return_value=set()), \
                patch("src.main.get_article_links", return_value=set()):
            step_fetch()

        _, kwargs = fetch_all.call_args
        self.assertEqual(kwargs["feeds_state"], {})
        save_articles.assert_called_once_with([])

    def test_all_due_feeds_failing_raises(self):
        feed = {"xml_url": "https://a.com/feed", "title": "A", "priority": 1}
        state = {"feeds": {}}
        results = [{"feed": feed, "status": "error", "error": "boom", "entries": []}]

        with patch("src.main.load_config", return_value=_fetch_config()), \
                patch("src.main.parse_feeds", return_value=[feed]), \
                patch("src.main.load_state", return_value=state), \
                patch("src.main.save_state") as save_state, \
                patch("src.main.validate_articles_file", return_value="ok"), \
                patch("src.fetcher.fetch_all_feeds", return_value=results), \
                patch("src.main.load_seen_links", return_value=set()), \
                patch("src.main.get_article_links", return_value=set()):
            with self.assertRaises(RuntimeError):
                step_fetch()

        save_state.assert_called_once()
        self.assertEqual(state["feeds"][feed["xml_url"]]["last_error"], "boom")


class ClassifyStepTests(unittest.TestCase):
    def _articles(self):
        return [
            {"link": "https://a.com/ok", "status": "pending", "content": "ok text",
             "cached_at": "2026-01-01T00:00:00Z"},
            {"link": "https://a.com/skip", "status": "pending", "content": "skip text",
             "cached_at": "2026-01-02T00:00:00Z"},
            {"link": "https://a.com/fail", "status": "pending", "content": "fail text",
             "cached_at": "2026-01-03T00:00:00Z"},
        ]

    def test_persists_classified_skipped_and_failed_statuses(self):
        cached = self._articles()
        calls = {}

        def fake_classify(articles, **kwargs):
            calls["skip_prompt"] = kwargs.get("skip_prompt")
            for article in articles:
                if article["link"].endswith("/ok"):
                    article["classification"] = {
                        "category": "Tech", "score": 8, "summary": "good",
                    }
                elif article["link"].endswith("/skip"):
                    article["classification"] = {
                        "category": "skip", "score": 5, "summary": "",
                    }
            return ClassifyStats(
                articles=len(articles), classified=1, skipped=1, failed=1,
                batches=1, provider="custom", model="m",
            )

        with patch("src.main.load_config", return_value=_classify_config()), \
                patch("src.main.parse_feeds", return_value=[]), \
                patch("src.main.load_articles", return_value=cached), \
                patch("src.main.load_seen_links", return_value=set()), \
                patch("src.main.load_state", return_value={"feeds": {}}), \
                patch("src.main.save_state"), \
                patch("src.main.classify_articles", side_effect=fake_classify), \
                patch("src.main.save_articles") as save_articles, \
                patch("src.main.save_daily_results") as save_daily, \
                patch("src.main._update_index") as update_index:
            step_classify()

        self.assertEqual(calls["skip_prompt"], "global rule")
        self.assertEqual(cached[0]["status"], "classified")
        self.assertEqual(cached[0]["content"], "")
        self.assertTrue(cached[0]["classified_at"])
        self.assertEqual(cached[1]["status"], "skipped")
        self.assertEqual(cached[1]["content"], "")
        self.assertEqual(cached[2]["status"], "failed")
        self.assertEqual(cached[2]["attempts"], 1)
        self.assertEqual(cached[2]["content"], "fail text")

        save_articles.assert_called()
        save_daily.assert_called_once()
        written = save_daily.call_args.args[0]["articles"]
        self.assertEqual([a["link"] for a in written], ["https://a.com/ok"])
        update_index.assert_called()

    def test_fail_fast_when_every_article_fails(self):
        cached = [self._articles()[2]]

        def fake_classify(articles, **kwargs):
            return ClassifyStats(articles=len(articles), failed=len(articles))

        with patch("src.main.load_config", return_value=_classify_config()), \
                patch("src.main.parse_feeds", return_value=[]), \
                patch("src.main.load_articles", return_value=cached), \
                patch("src.main.load_seen_links", return_value=set()), \
                patch("src.main.load_state", return_value={"feeds": {}}), \
                patch("src.main.save_state"), \
                patch("src.main.classify_articles", side_effect=fake_classify), \
                patch("src.main.save_articles") as save_articles, \
                patch("src.main.save_daily_results") as save_daily, \
                patch("src.main._update_index") as update_index:
            with self.assertRaises(RuntimeError):
                step_classify()

        self.assertEqual(cached[0]["status"], "failed")
        self.assertEqual(cached[0]["attempts"], 1)
        self.assertEqual(cached[0]["content"], "fail text")
        save_articles.assert_called()
        save_daily.assert_not_called()
        update_index.assert_called()

    def test_stale_backlog_is_not_sent_to_ai(self):
        cached = self._articles()
        config = _classify_config()
        config["fetch"] = {"keep_days": 1}

        with patch("src.main.load_config", return_value=config), \
                patch("src.main.parse_feeds", return_value=[]), \
                patch("src.main.load_articles", return_value=cached), \
                patch("src.main.load_seen_links", return_value=set()), \
                patch("src.main.load_state", return_value={"feeds": {}}), \
                patch("src.main.save_state"), \
                patch("src.main.classify_articles") as classify, \
                patch("src.main.save_articles"), \
                patch("src.main.save_daily_results"), \
                patch("src.main._update_index") as update_index:
            step_classify()

        classify.assert_not_called()
        update_index.assert_called_once()

    def test_all_skipped_does_not_fail(self):
        cached = [self._articles()[1]]

        def fake_classify(articles, **kwargs):
            for article in articles:
                article["classification"] = {
                    "category": "skip", "score": 5, "summary": "",
                }
            return ClassifyStats(articles=len(articles), skipped=len(articles))

        with patch("src.main.load_config", return_value=_classify_config()), \
                patch("src.main.parse_feeds", return_value=[]), \
                patch("src.main.load_articles", return_value=cached), \
                patch("src.main.load_seen_links", return_value=set()), \
                patch("src.main.load_state", return_value={"feeds": {}}), \
                patch("src.main.save_state"), \
                patch("src.main.classify_articles", side_effect=fake_classify), \
                patch("src.main.save_articles"), \
                patch("src.main.save_daily_results"), \
                patch("src.main._update_index"):
            step_classify()

        self.assertEqual(cached[0]["status"], "skipped")


class SkipPromptCombinationTests(unittest.TestCase):
    def test_global_and_site_prompts_are_combined(self):
        from src.main import _combine_skip_prompts

        self.assertEqual(_combine_skip_prompts("global", "site"), "global site")
        self.assertEqual(_combine_skip_prompts("global", ""), "global")
        self.assertEqual(_combine_skip_prompts("", "site"), "site")
        self.assertEqual(_combine_skip_prompts("same", "same"), "same")


if __name__ == "__main__":
    unittest.main()
