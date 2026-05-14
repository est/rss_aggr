"""RSS feed fetcher using feedparser."""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import feedparser
import requests

DEFAULT_KEEP_DAYS = 7


def fetch_feed(feed_info: dict, timeout: int = None, keep_days: int = DEFAULT_KEEP_DAYS, user_agent: str = "rss_aggr/1.0") -> dict:
    """Fetch a single RSS feed, return entries within keep_days."""
    if not timeout:
        timeout = (2, 5)
    try:
        resp = requests.get(
            feed_info["xml_url"],
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
        parsed = feedparser.parse(resp.text)
    except Exception as e:
        return {
            "feed": feed_info,
            "status": "error",
            "error": str(e),
            "entries": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    entries = []
    for entry in parsed.entries:
        pub_date = ""
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            pub_date = time.strftime("%Y-%m-%dT%H:%M:%SZ", entry.published_parsed)
        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
            pub_date = time.strftime("%Y-%m-%dT%H:%M:%SZ", entry.updated_parsed)

        # Skip articles older than keep_days
        if pub_date:
            try:
                pub_dt = datetime.strptime(pub_date, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
                if pub_dt > datetime.now(timezone.utc) + timedelta(hours=1):
                    continue
            except ValueError:
                pass

        content = ""
        if hasattr(entry, "summary"):
            content = entry.summary
        elif hasattr(entry, "description"):
            content = entry.description

        entry_id = entry.get("id", entry.get("link", ""))
        guid = hashlib.sha256(entry_id.encode()).hexdigest()[:16]

        author = entry.get("author", "")
        if not author and hasattr(entry, "authors") and entry.authors:
            author = entry.authors[0].get("name", "")

        entries.append({
            "guid": guid,
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "author": author,
            "content": content,
            "published": pub_date,
            "feed_title": feed_info.get("title", ""),
            "feed_category": feed_info.get("category", ""),
        })

    return {
        "feed": feed_info,
        "status": "ok",
        "entries": entries,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_all_feeds(feeds: list[dict], timeout: int = 15, keep_days: int = DEFAULT_KEEP_DAYS, user_agent: str = "rss_aggr/1.0") -> list[dict]:
    """Fetch all feeds in parallel with progress logging."""
    total = len(feeds)
    done = 0
    ok = 0
    err = 0
    results = []
    print(f"  Starting {total} feeds (10 workers, keep_days={keep_days})...", flush=True)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_feed, f, timeout, keep_days, user_agent): f
            for f in feeds
        }
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            done += 1
            if r["status"] == "ok":
                ok += 1
                n = len(r["entries"])
                print(f"  [{done}/{total}] OK  {r['feed']['title']} ({n} entries)", flush=True)
            else:
                err += 1
                print(f"  [{done}/{total}] ERR {r['feed']['title']}: {r.get('error', '')[:100]}", flush=True)

    print(f"  Fetch done: {ok} ok, {err} errors", flush=True)
    return results
