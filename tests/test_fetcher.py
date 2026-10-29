import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

from src import fetcher
from src.fetcher import _parse_entry_published, _request_with_retry, fetch_feed


def _response(status_code, headers=None, body=b"", url="https://example.com/feed"):
    resp = requests.Response()
    resp.status_code = status_code
    resp.headers.update(headers or {})
    resp._content = body if isinstance(body, bytes) else body.encode("utf-8")
    resp.url = url
    resp.encoding = "utf-8"
    return resp


def _rss(items):
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<rss version=\"2.0\"><channel><title>Test</title>",
    ]
    for item in items:
        parts.append("<item>")
        parts.append(f"<title>{item['title']}</title>")
        parts.append(f"<link>{item['link']}</link>")
        parts.append(f"<guid>{item['link']}</guid>")
        if item.get("published"):
            parts.append(f"<pubDate>{item['published']}</pubDate>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts)


FEED = {"xml_url": "https://example.com/feed", "title": "Test", "category": "tech"}


class RequestRetryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("src.fetcher.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_304_returned_without_raising(self):
        resp = _response(304, {"ETag": '"v1"'})
        with mock.patch("src.fetcher.requests.get", return_value=resp) as get:
            result = _request_with_retry("https://example.com/feed", {}, (5, 15))
        self.assertEqual(result.status_code, 304)
        self.assertEqual(get.call_count, 1)

    def test_5xx_is_retried_then_succeeds(self):
        responses = [_response(500), _response(503), _response(200)]
        with mock.patch("src.fetcher.requests.get", side_effect=responses) as get:
            result = _request_with_retry("https://example.com/feed", {}, (5, 15))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_429_is_retried_with_retry_after(self):
        responses = [_response(429, {"Retry-After": "7"}), _response(200)]
        with mock.patch("src.fetcher.requests.get", side_effect=responses):
            result = _request_with_retry("https://example.com/feed", {}, (5, 15))
        self.assertEqual(result.status_code, 200)
        self.sleep.assert_called_once_with(7.0)

    def test_404_is_not_retried(self):
        with mock.patch("src.fetcher.requests.get", return_value=_response(404)) as get:
            with self.assertRaises(requests.exceptions.HTTPError):
                _request_with_retry("https://example.com/feed", {}, (5, 15))
        self.assertEqual(get.call_count, 1)
        self.sleep.assert_not_called()

    def test_5xx_exhausts_retries_and_raises(self):
        with mock.patch("src.fetcher.requests.get", return_value=_response(500)) as get:
            with self.assertRaises(requests.exceptions.HTTPError):
                _request_with_retry("https://example.com/feed", {}, (5, 15))
        self.assertEqual(get.call_count, fetcher.MAX_RETRIES + 1)

    def test_connection_error_is_retried(self):
        responses = [requests.exceptions.ConnectionError("reset"), _response(200)]
        with mock.patch("src.fetcher.requests.get", side_effect=responses):
            result = _request_with_retry("https://example.com/feed", {}, (5, 15))
        self.assertEqual(result.status_code, 200)


class FetchFeedTests(unittest.TestCase):
    def test_304_returns_not_modified_and_updates_validators(self):
        resp = _response(304, {"ETag": '"new"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"})
        feed_state = {"etag": '"old"'}
        with mock.patch("src.fetcher._request_with_retry", return_value=resp), \
                mock.patch("src.fetcher.feedparser.parse") as parse:
            result = fetch_feed(FEED, feed_state=feed_state)

        self.assertEqual(result["status"], "not_modified")
        self.assertEqual(result["entries"], [])
        self.assertEqual(feed_state["etag"], '"new"')
        self.assertIn("last_modified", feed_state)
        parse.assert_not_called()

    def test_http_error_returns_error_status(self):
        resp = _response(404)
        exc = requests.exceptions.HTTPError("404 Client Error", response=resp)
        with mock.patch("src.fetcher._request_with_retry", side_effect=exc):
            result = fetch_feed(FEED)
        self.assertEqual(result["status"], "error")
        self.assertIn("404", result["error"])
        self.assertEqual(result["entries"], [])

    def test_invalid_published_dates_become_empty(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(
            _parse_entry_published({"published_parsed": (1, 1, 1, 0, 0, 0, 0, 0, 0)}, now), "")
        self.assertEqual(
            _parse_entry_published({"published_parsed": (1970, 1, 1, 0, 0, 0, 0, 0, 0)}, now), "")
        future = (now + timedelta(days=30)).timetuple()
        self.assertEqual(_parse_entry_published({"published_parsed": future}, now), "")
        self.assertEqual(_parse_entry_published({}, now), "")

    def test_valid_published_date_normalized(self):
        now = datetime.now(timezone.utc)
        parsed = (2025, 6, 15, 12, 30, 0, 0, 0, 0)
        self.assertEqual(
            _parse_entry_published({"published_parsed": parsed}, now),
            "2025-06-15T12:30:00Z",
        )
        # updated_parsed is the fallback.
        self.assertEqual(
            _parse_entry_published({"updated_parsed": parsed}, now),
            "2025-06-15T12:30:00Z",
        )

    def test_fetch_with_invalid_dates_returns_empty_published(self):
        body = _rss([
            {"title": "bad", "link": "https://example.com/1",
             "published": "Mon, 01 Jan 0001 00:00:00 GMT"},
        ])
        with mock.patch("src.fetcher._request_with_retry", return_value=_response(200, body=body)):
            result = fetch_feed(FEED)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["published"], "")

    def test_max_entries_per_feed_caps_and_sorts_newest_first(self):
        items = [
            {"title": f"item{i}", "link": f"https://example.com/{i}",
             "published": pub}
            for i, pub in enumerate([
                "Wed, 01 Jan 2025 00:00:00 GMT",
                "Sun, 05 Jan 2025 00:00:00 GMT",
                "Fri, 03 Jan 2025 00:00:00 GMT",
                "Tue, 07 Jan 2025 00:00:00 GMT",
                "Thu, 02 Jan 2025 00:00:00 GMT",
            ])
        ]
        body = _rss(items)
        with mock.patch("src.fetcher._request_with_retry", return_value=_response(200, body=body)):
            result = fetch_feed(FEED, keep_days=3650, max_entries_per_feed=2)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["entries"]), 2)
        self.assertEqual(
            [e["published"] for e in result["entries"]],
            ["2025-01-07T00:00:00Z", "2025-01-05T00:00:00Z"],
        )
        self.assertEqual(result["entries"][0]["title"], "item3")

    def test_max_entries_zero_is_unlimited(self):
        items = [
            {"title": f"item{i}", "link": f"https://example.com/{i}",
             "published": "Wed, 01 Jan 2025 00:00:00 GMT"}
            for i in range(5)
        ]
        body = _rss(items)
        with mock.patch("src.fetcher._request_with_retry", return_value=_response(200, body=body)):
            result = fetch_feed(FEED, keep_days=3650, max_entries_per_feed=0)
        self.assertEqual(len(result["entries"]), 5)

    def test_fetch_all_feeds_passes_max_entries(self):
        body = _rss([
            {"title": f"item{i}", "link": f"https://example.com/{i}",
             "published": "Wed, 01 Jan 2025 00:00:00 GMT"}
            for i in range(4)
        ])
        with mock.patch("src.fetcher._request_with_retry", return_value=_response(200, body=body)):
            results = fetcher.fetch_all_feeds([FEED], keep_days=3650, max_entries_per_feed=2)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(len(results[0]["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
