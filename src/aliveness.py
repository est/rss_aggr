"""Aliveness monitoring for RSS feeds."""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests


def check_feed_health(feed_info: dict, timeout: int = 15) -> dict:
    """Check if a feed is reachable and healthy."""
    url = feed_info["xml_url"]
    start = time.time()
    headers = {"User-Agent": "RSS-Aggregator/1.0"}
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True, headers=headers)
        if resp.status_code == 405:
            resp = requests.get(url, timeout=timeout, allow_redirects=True, headers=headers)
        latency_ms = round((time.time() - start) * 1000)
        return {
            "feed_url": url,
            "feed_title": feed_info.get("title", ""),
            "status": "alive" if resp.status_code < 400 else "error",
            "http_status": resp.status_code,
            "latency_ms": latency_ms,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    except requests.Timeout:
        return {
            "feed_url": url,
            "feed_title": feed_info.get("title", ""),
            "status": "timeout",
            "http_status": 0,
            "latency_ms": -1,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {
            "feed_url": url,
            "feed_title": feed_info.get("title", ""),
            "status": "error",
            "http_status": 0,
            "latency_ms": -1,
            "error": str(e),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


def check_all_feeds(feeds: list[dict], timeout: int = 15) -> list[dict]:
    """Check health of all feeds in parallel."""
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(check_feed_health, f, timeout): f for f in feeds}
        for future in as_completed(futures):
            results.append(future.result())
    return results
