import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import feed_state as state_mod
from src.feed_state import (
    ERROR_BLOCKED,
    ERROR_PARSE,
    ERROR_PERMANENT,
    ERROR_RATE_LIMIT,
    ERROR_TRANSIENT,
    FEED_STATE_FILE,
    LEGACY_FEED_STATE_FILE,
    classify_error,
    collect_feed_urls,
    disable_feed,
    forget_feed,
    get_due_feeds,
    load_state,
    mark_failed,
    mark_ok,
    prune_feeds,
    record_content_lengths,
    save_state,
    state_summary,
)
from src.migrate_state import migrate


def _now():
    return datetime.now(timezone.utc)


def _feed(url, priority=10):
    return {"xml_url": url, "title": url, "priority": priority}


def _empty_state():
    return {"feeds": {}}


class ClassifyErrorTests(unittest.TestCase):
    def test_status_mapping(self):
        self.assertEqual(classify_error("404 Client Error"), ERROR_PERMANENT)
        self.assertEqual(classify_error("410 Gone"), ERROR_PERMANENT)
        self.assertEqual(classify_error("401 Unauthorized"), ERROR_BLOCKED)
        self.assertEqual(classify_error("403 Forbidden"), ERROR_BLOCKED)
        self.assertEqual(classify_error("429 Too Many Requests"), ERROR_RATE_LIMIT)
        self.assertEqual(classify_error("500 Server Error"), ERROR_TRANSIENT)
        self.assertEqual(classify_error("503 Service Unavailable"), ERROR_TRANSIENT)
        self.assertEqual(classify_error("Read timed out"), ERROR_TRANSIENT)
        self.assertEqual(classify_error("Connection reset by peer"), ERROR_TRANSIENT)
        self.assertEqual(classify_error("DNS resolution failed"), ERROR_TRANSIENT)

    def test_parse_and_default(self):
        self.assertEqual(classify_error("XML parse error"), ERROR_PARSE)
        self.assertEqual(classify_error("invalid feed document"), ERROR_PARSE)
        self.assertEqual(classify_error("something odd happened"), ERROR_TRANSIENT)


class MarkOkTests(unittest.TestCase):
    def test_mark_ok_clears_failure_bookkeeping(self):
        state = _empty_state()
        url = "https://example.com/feed"
        mark_failed(state, url, "500 Server Error")
        state["feeds"][url]["etag"] = '"abc"'
        disable_feed(state, url, "manual")

        mark_ok(state, url)

        info = state["feeds"][url]
        self.assertEqual(info["consecutive_failures"], 0)
        self.assertEqual(info["last_status"], "ok")
        self.assertIn("last_ok", info)
        self.assertIn("last_attempt", info)
        for key in ("last_error", "error_kind", "retry_at", "disabled",
                    "disabled_at", "disabled_reason", "disabled_until"):
            self.assertNotIn(key, info)
        # Conditional-request metadata is preserved.
        self.assertEqual(info["etag"], '"abc"')

    def test_mark_fed_alias(self):
        state = _empty_state()
        url = "https://example.com/feed"
        state_mod.mark_fed(state, url)
        self.assertEqual(state["feeds"][url]["consecutive_failures"], 0)
        self.assertIn("last_ok", state["feeds"][url])


class MarkFailedTests(unittest.TestCase):
    def test_transient_failure_sets_retry_at_without_disabling(self):
        state = _empty_state()
        url = "https://example.com/feed"
        for _ in range(10):
            mark_failed(state, url, "Read timed out")

        info = state["feeds"][url]
        self.assertEqual(info["error_kind"], ERROR_TRANSIENT)
        self.assertEqual(info["consecutive_failures"], 10)
        self.assertFalse(info.get("disabled", False))
        self.assertIn("retry_at", info)
        retry_at = datetime.fromisoformat(info["retry_at"])
        self.assertGreater(retry_at, _now())

        # Still never disabled even past max_failures.
        due = get_due_feeds([_feed(url)], state, interval_hours=0, max_failures=3)
        self.assertEqual(due, [])

    def test_transient_backoff_grows_and_caps(self):
        state = _empty_state()
        url = "https://example.com/feed"
        mark_failed(state, url, "timeout")
        first = datetime.fromisoformat(state["feeds"][url]["retry_at"]) - _now()
        mark_failed(state, url, "timeout")
        second = datetime.fromisoformat(state["feeds"][url]["retry_at"]) - _now()
        self.assertGreater(second, first + timedelta(hours=1))

        for _ in range(20):
            mark_failed(state, url, "timeout")
        delay = datetime.fromisoformat(state["feeds"][url]["retry_at"]) - _now()
        self.assertLessEqual(delay, timedelta(days=7, minutes=1))

    def test_rate_limit_and_blocked_retry_windows(self):
        state = _empty_state()
        mark_failed(state, "https://a.com/rl", "429 Too Many Requests")
        mark_failed(state, "https://a.com/blocked", "403 Forbidden")

        rl = datetime.fromisoformat(state["feeds"]["https://a.com/rl"]["retry_at"]) - _now()
        blocked = datetime.fromisoformat(state["feeds"]["https://a.com/blocked"]["retry_at"]) - _now()
        self.assertAlmostEqual(rl.total_seconds() / 3600, 2, delta=0.1)
        self.assertAlmostEqual(blocked.total_seconds() / 86400, 7, delta=0.1)
        self.assertFalse(state["feeds"]["https://a.com/rl"].get("disabled"))

    def test_5xx_on_xml_url_is_transient_not_parse(self):
        kinds = [
            "500 Server Error for url: https://example.com/feed.xml",
            "502 Bad Gateway for url: https://example.com/rss.xml",
            "ConnectionResetError for url: https://example.com/feed.xml",
            "500 Server Error for url: https://example.com/404-feed.xml",
            "503 Server Error for url: https://example.com/410/rss",
            "Read timed out for url https://example.com/429-feed",
        ]
        for error in kinds:
            self.assertEqual(classify_error(error), ERROR_TRANSIENT, error)

    def test_permanent_failure_has_no_retry_and_disables_after_max(self):
        state = _empty_state()
        url = "https://example.com/feed"
        for _ in range(3):
            mark_failed(state, url, "404 Not Found")

        info = state["feeds"][url]
        self.assertEqual(info["error_kind"], ERROR_PERMANENT)
        self.assertNotIn("retry_at", info)
        self.assertFalse(info.get("disabled", False))

        due = get_due_feeds([_feed(url)], state, interval_hours=0, max_failures=3)
        self.assertEqual(due, [])
        info = state["feeds"][url]
        self.assertTrue(info["disabled"])
        self.assertIn("disabled_until", info)

    def test_parse_failure_disables_after_max(self):
        state = _empty_state()
        url = "https://example.com/feed"
        for _ in range(3):
            mark_failed(state, url, "XML parse error")

        due = get_due_feeds([_feed(url)], state, interval_hours=0, max_failures=3)
        self.assertEqual(due, [])
        self.assertTrue(state["feeds"][url]["disabled"])
        self.assertEqual(state["feeds"][url]["error_kind"], ERROR_PARSE)


class DueFeedTests(unittest.TestCase):
    def test_disabled_feed_auto_re_enables_after_disabled_until(self):
        state = _empty_state()
        url = "https://example.com/feed"
        mark_failed(state, url, "500 Server Error")
        disable_feed(state, url, "test", disabled_until=_now() - timedelta(minutes=1))

        due = get_due_feeds([_feed(url)], state, interval_hours=24, max_failures=3)

        self.assertEqual([url], [f["xml_url"] for f in due])
        info = state["feeds"][url]
        self.assertFalse(info.get("disabled", False))
        self.assertEqual(info["consecutive_failures"], 0)
        self.assertNotIn("disabled_until", info)

    def test_legacy_disabled_feed_without_until_re_enables_after_quarantine(self):
        state = _empty_state()
        url = "https://example.com/feed"
        state["feeds"][url] = {
            "disabled": True,
            "disabled_at": (_now() - timedelta(days=8)).isoformat(),
        }

        due = get_due_feeds([_feed(url)], state, interval_hours=24, max_failures=3)

        self.assertEqual([url], [f["xml_url"] for f in due])
        self.assertFalse(state["feeds"][url].get("disabled", False))

    def test_legacy_disabled_feed_without_timestamps_is_re_probed(self):
        state = _empty_state()
        url = "https://example.com/feed"
        state["feeds"][url] = {"disabled": True}

        due = get_due_feeds([_feed(url)], state, interval_hours=24, max_failures=3)

        self.assertEqual([url], [f["xml_url"] for f in due])

    def test_disabled_feed_with_future_until_is_skipped(self):
        state = _empty_state()
        url = "https://example.com/feed"
        disable_feed(state, url, "test", disabled_until=_now() + timedelta(days=1))
        self.assertEqual(get_due_feeds([_feed(url)], state), [])

    def test_retry_at_in_future_skips_feed(self):
        state = _empty_state()
        url = "https://example.com/feed"
        mark_failed(state, url, "timeout")
        self.assertEqual(get_due_feeds([_feed(url)], state, interval_hours=0), [])

    def test_due_order_puts_oldest_last_attempt_first(self):
        state = _empty_state()
        now = _now()
        timestamps = {
            "https://a.com/recent": now - timedelta(hours=5),
            "https://a.com/never": None,
            "https://a.com/oldest": now - timedelta(days=3),
            "https://a.com/middle": now - timedelta(days=1),
        }
        feeds = [_feed(url) for url in timestamps]
        for url, ts in timestamps.items():
            info = state["feeds"].setdefault(url, {})
            if ts is not None:
                info["last_attempt"] = ts.isoformat()

        due = get_due_feeds(feeds, state, interval_hours=1)
        self.assertEqual(
            [f["xml_url"] for f in due],
            [
                "https://a.com/never",
                "https://a.com/oldest",
                "https://a.com/middle",
                "https://a.com/recent",
            ],
        )

    def test_oldest_attempt_wins_and_priority_breaks_ties(self):
        state = _empty_state()
        now = _now()
        state["feeds"]["https://a.com/low"] = {"last_attempt": (now - timedelta(days=5)).isoformat()}
        state["feeds"]["https://a.com/high"] = {"last_attempt": (now - timedelta(days=1)).isoformat()}
        feeds = [_feed("https://a.com/low", priority=50), _feed("https://a.com/high", priority=1)]
        due = get_due_feeds(feeds, state, interval_hours=1)
        self.assertEqual([f["xml_url"] for f in due], ["https://a.com/low", "https://a.com/high"])

    def test_priority_breaks_ties_for_never_attempted(self):
        state = _empty_state()
        feeds = [_feed("https://a.com/low", priority=50), _feed("https://a.com/high", priority=1)]
        due = get_due_feeds(feeds, state, interval_hours=1)
        self.assertEqual([f["xml_url"] for f in due], ["https://a.com/high", "https://a.com/low"])

    def test_interval_respected_with_last_attempt(self):
        state = _empty_state()
        now = _now()
        state["feeds"]["https://a.com/feed"] = {"last_attempt": (now - timedelta(hours=2)).isoformat()}
        self.assertEqual(get_due_feeds([_feed("https://a.com/feed")], state, interval_hours=24), [])
        self.assertEqual(len(get_due_feeds([_feed("https://a.com/feed")], state, interval_hours=1)), 1)


class SummaryTests(unittest.TestCase):
    def test_state_summary_counts(self):
        state = _empty_state()
        mark_ok(state, "https://a.com/ok")
        mark_failed(state, "https://a.com/fail", "timeout")
        disable_feed(state, "https://a.com/disabled", "test")

        summary = state_summary(state)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["healthy"], 1)
        self.assertEqual(summary["failing"], 1)
        self.assertEqual(summary["disabled"], 1)
        self.assertEqual(summary["error_kinds"][ERROR_TRANSIENT], 1)

    def test_disable_stats_still_works(self):
        state = _empty_state()
        mark_failed(state, "https://a.com/fail", "timeout")
        disable_feed(state, "https://a.com/disabled", "test")
        stats = state_mod.disable_stats(state)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["disabled"], 1)
        self.assertEqual(stats["failing"], 1)


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, self._cwd)

    def _write_json(self, name, data):
        Path(name).write_text(json.dumps(data), encoding="utf-8")

    def test_file_constants(self):
        self.assertEqual(FEED_STATE_FILE, "feed_state.json")
        self.assertEqual(LEGACY_FEED_STATE_FILE, "state.json")
        self.assertEqual(state_mod.STATE_FILE, FEED_STATE_FILE)

    def test_load_falls_back_to_legacy_state_json(self):
        legacy = {"feeds": {"https://a.com/feed": {"last_status": "ok"}}}
        self._write_json("state.json", legacy)

        self.assertEqual(load_state(), legacy)

    def test_load_prefers_new_file_over_legacy(self):
        self._write_json("state.json", {"feeds": {"legacy": {}}})
        new = {"feeds": {"new": {}}}
        self._write_json("feed_state.json", new)

        self.assertEqual(load_state(), new)

    def test_load_returns_empty_when_no_file_exists(self):
        self.assertEqual(load_state(), {"feeds": {}})

    def test_save_writes_versioned_new_file_and_not_legacy(self):
        state = {"feeds": {"https://a.com/feed": {}}, "last_fetch": "2026-01-01T00:00:00+00:00"}

        save_state(state)

        self.assertTrue(Path("feed_state.json").exists())
        self.assertFalse(Path("state.json").exists())
        saved = json.loads(Path("feed_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["v"], 1)
        self.assertEqual(saved["feeds"], state["feeds"])
        self.assertEqual(saved["last_fetch"], state["last_fetch"])

    def test_save_preserves_extra_top_level_keys(self):
        state = {"feeds": {}, "last_classify": "x", "custom": {"a": 1}}
        save_state(state, "custom_state.json")

        saved = json.loads(Path("custom_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["v"], 1)
        self.assertEqual(saved["last_classify"], "x")
        self.assertEqual(saved["custom"], {"a": 1})

    def test_load_state_rejects_non_object_root(self):
        self._write_json("feed_state.json", ["not", "a", "dict"])
        with self.assertRaisesRegex(ValueError, "JSON object"):
            load_state()

    def test_load_state_rejects_non_dict_feeds(self):
        self._write_json("feed_state.json", {"feeds": []})
        with self.assertRaisesRegex(ValueError, "'feeds'"):
            load_state()

    def test_load_state_rejects_non_dict_feed_entry(self):
        self._write_json(
            "feed_state.json", {"feeds": {"https://a.com/feed": []}}
        )
        with self.assertRaisesRegex(ValueError, "feed entry"):
            load_state()

    def test_migrate_refuses_corrupt_article_state_without_overwriting(self):
        Path("article_state.json").write_text(
            '{"v": 1, "articles": "oops"}', encoding="utf-8"
        )
        before = Path("article_state.json").read_text(encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "corrupt"):
            migrate()

        self.assertEqual(
            Path("article_state.json").read_text(encoding="utf-8"), before
        )
        self.assertFalse(Path("feed_state.json").exists())

    def test_migrate_refuses_invalid_feed_state(self):
        self._write_json("feed_state.json", {"feeds": []})
        with self.assertRaisesRegex(RuntimeError, "structurally invalid"):
            migrate()


class PruneForgetTests(unittest.TestCase):
    def test_prune_removes_unknown_and_keeps_known_including_skipped(self):
        state = {
            "feeds": {
                "https://a.com/known": {"last_status": "ok"},
                "https://a.com/skipped": {"last_status": "ok"},
                "https://a.com/gone": {"last_status": "error"},
            },
            "last_fetch": "2026-01-01T00:00:00+00:00",
        }
        known = {"https://a.com/known", "https://a.com/skipped"}

        removed = prune_feeds(state, known)

        self.assertEqual(removed, 1)
        self.assertEqual(set(state["feeds"]), known)
        self.assertEqual(state["last_fetch"], "2026-01-01T00:00:00+00:00")

    def test_prune_with_no_unknown_urls_returns_zero(self):
        state = {"feeds": {"https://a.com/known": {}}}
        self.assertEqual(prune_feeds(state, {"https://a.com/known"}), 0)

    def test_forget_feed(self):
        state = _empty_state()
        url = "https://a.com/feed"
        mark_ok(state, url)

        self.assertTrue(forget_feed(state, url))
        self.assertNotIn(url, state["feeds"])
        self.assertFalse(forget_feed(state, url))

    def test_collect_feed_urls_includes_skipped_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "feeds.toml"
            config.write_text(
                "\n".join(
                    [
                        "[[category]]",
                        'name = "Blogs"',
                        "",
                        "[[category.feed]]",
                        'title = "Active"',
                        'url = "https://a.com/active"',
                        "",
                        "[[category.feed]]",
                        'title = "Skipped"',
                        'url = "https://a.com/skipped"',
                        "skip = true",
                        "",
                        "[[category]]",
                        'name = "News"',
                        "",
                        "[[category.feed]]",
                        'title = "Other"',
                        'url = "https://b.com/other"',
                        "skip = true",
                    ]
                ),
                encoding="utf-8",
            )

            urls = collect_feed_urls(config)

            self.assertEqual(
                urls,
                {
                    "https://a.com/active",
                    "https://a.com/skipped",
                    "https://b.com/other",
                },
            )


class ContentLengthTests(unittest.TestCase):
    def test_short_bodies_disable_feed_after_threshold(self):
        state = _empty_state()
        url = "https://short.example/feed"

        self.assertFalse(record_content_lengths(state, url, [100, 120], max_short=5))
        self.assertFalse(record_content_lengths(state, url, [90, 130], max_short=5))
        self.assertTrue(record_content_lengths(state, url, [80, 110], max_short=5))

        feed = state["feeds"][url]
        self.assertTrue(feed["disabled"])
        self.assertEqual(feed["disabled_reason"], "rss does not provide full text")
        self.assertEqual(feed["error_kind"], "no_full_text")
        self.assertNotIn(url, [f["xml_url"] for f in get_due_feeds([_feed(url)], state)])

    def test_one_full_text_article_resets_streak(self):
        state = _empty_state()
        url = "https://mixed.example/feed"
        record_content_lengths(state, url, [100, 100], max_short=3)
        self.assertEqual(state["feeds"][url]["short_content_streak"], 2)

        self.assertFalse(record_content_lengths(state, url, [5000], max_short=3))
        self.assertEqual(state["feeds"][url]["short_content_streak"], 0)
        self.assertFalse(state["feeds"][url].get("disabled", False))

    def test_empty_batch_does_not_change_streak(self):
        state = _empty_state()
        url = "https://empty.example/feed"
        self.assertFalse(record_content_lengths(state, url, [], max_short=2))
        self.assertEqual(state, {"feeds": {}})


if __name__ == "__main__":
    unittest.main()
