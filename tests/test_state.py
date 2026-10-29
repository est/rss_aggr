import unittest
from datetime import datetime, timedelta, timezone

from src import state as state_mod
from src.state import (
    ERROR_BLOCKED,
    ERROR_PARSE,
    ERROR_PERMANENT,
    ERROR_RATE_LIMIT,
    ERROR_TRANSIENT,
    classify_error,
    disable_feed,
    get_due_feeds,
    mark_failed,
    mark_ok,
    state_summary,
)


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

    def test_priority_is_primary_sort_key(self):
        state = _empty_state()
        now = _now()
        state["feeds"]["https://a.com/low"] = {"last_attempt": (now - timedelta(days=5)).isoformat()}
        state["feeds"]["https://a.com/high"] = {"last_attempt": (now - timedelta(days=1)).isoformat()}
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


if __name__ == "__main__":
    unittest.main()
