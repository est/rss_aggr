"""RSS feed fetcher using feedparser."""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import feedparser
import requests

DEFAULT_KEEP_DAYS = 7
MAX_RETRIES = 2
RETRY_BACKOFF = [1, 2]  # seconds


def _normalize_timeout(timeout: int | tuple | None) -> tuple:
    """Normalize timeout to (connect, read) tuple."""
    if timeout is None:
        return (5, 15)
    if isinstance(timeout, tuple):
        return timeout
    return (timeout, timeout)


def _detect_encoding(resp: requests.Response) -> str:
    """Detect response encoding with priority: charset header > BOM > apparent > utf-8."""
    # 1. Content-Type charset
    content_type = resp.headers.get("Content-Type", "")
    if "charset=" in content_type.lower():
        import re
        m = re.search(r"charset=([^\s;]+)", content_type, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    # 2. BOM
    raw = resp.content[:4]
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be"
    # 3. FUCK NO apparent_encoding (chardet) !!! It's broken for zh-CN
    # if resp.apparent_encoding and resp.apparent_encoding.lower() not in ("ascii",): return resp.apparent_encoding
    # 4. default
    return "utf-8"


def _request_with_retry(url: str, headers: dict, timeout: tuple, max_retries: int = MAX_RETRIES) -> requests.Response:
    """GET with exponential backoff retry for transient errors."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers=headers)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError:
            raise  # 4xx/5xx from raise_for_status — don't retry 4xx
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(RETRY_BACKOFF[attempt])
    raise last_exc


def fetch_feed(feed_info: dict, timeout: int | tuple | None = None,
               keep_days: int = DEFAULT_KEEP_DAYS,
               user_agent: str = "rss_aggr/1.0", skip_titles: list[str] = None,
               feed_state: dict | None = None) -> dict:
    """Fetch a single RSS feed, return entries within keep_days, filtered by rules.

    Args:
        feed_state: dict from state.json for this feed (stores etag/last_modified).
    """
    timeout = _normalize_timeout(timeout)
    skip_patterns = [s.lower() for s in (skip_titles or [])]
    headers = {"User-Agent": user_agent}

    # Conditional request headers
    if feed_state:
        if feed_state.get("etag"):
            headers["If-None-Match"] = feed_state["etag"]
        if feed_state.get("last_modified"):
            headers["If-Modified-Since"] = feed_state["last_modified"]

    try:
        resp = _request_with_retry(feed_info["xml_url"], headers, timeout)
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else 0
        # 304 Not Modified — feed hasn't changed
        if status_code == 304:
            return {
                "feed": feed_info,
                "status": "not_modified",
                "entries": [],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        return {
            "feed": feed_info,
            "status": "error",
            "error": str(e),
            "entries": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {
            "feed": feed_info,
            "status": "error",
            "error": str(e),
            "entries": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    # Update ETag / Last-Modified from response
    if feed_state is not None:
        etag = resp.headers.get("ETag")
        last_mod = resp.headers.get("Last-Modified")
        if etag:
            feed_state["etag"] = etag
        if last_mod:
            feed_state["last_modified"] = last_mod

    resp.encoding = _detect_encoding(resp)
    parsed = feedparser.parse(resp.text)

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
        if hasattr(entry, "content") and entry.content:
            raw = entry.content
            content = raw if isinstance(raw, str) else raw[0].get("value", "")
        elif hasattr(entry, "summary"):
            content = entry.summary
        elif hasattr(entry, "description"):
            content = entry.description

        entry_id = entry.get("id", entry.get("link", ""))
        guid = hashlib.sha256(entry_id.encode()).hexdigest()[:16]

        author = entry.get("author", "")
        if not author and hasattr(entry, "authors") and entry.authors:
            author = entry.authors[0].get("name", "")

        title = entry.get("title", "")

        # Filter: skip by title keywords
        if skip_patterns and any(p in title.lower() for p in skip_patterns):
            continue

        entries.append({
            "guid": guid,
            "title": title,
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


def fetch_all_feeds(feeds: list[dict], timeout: int = 15, keep_days: int = DEFAULT_KEEP_DAYS,
                    user_agent: str = "rss_aggr/1.0", skip_titles: list[str] = None,
                    max_workers: int = 10, feeds_state: dict | None = None) -> list[dict]:
    """Fetch all feeds in parallel with progress logging.

    Args:
        max_workers: number of parallel threads.
        feeds_state: dict of {url: feed_state} from state.json for conditional requests.
    """
    total = len(feeds)
    done = 0
    ok = 0
    not_mod = 0
    err = 0
    results = []
    print(f"  Starting {total} feeds ({max_workers} workers, keep_days={keep_days})...", flush=True)

    def _fetch_one(f):
        url = f["xml_url"]
        fs = (feeds_state or {}).get(url)
        return fetch_feed(f, timeout, keep_days, user_agent, skip_titles, feed_state=fs)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, f): f for f in feeds}
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            done += 1
            if r["status"] == "ok":
                ok += 1
                n = len(r["entries"])
                print(f"  [{done}/{total}] OK  {r['feed']['title']} ({n} entries)", flush=True)
            elif r["status"] == "not_modified":
                not_mod += 1
                print(f"  [{done}/{total}] 304 {r['feed']['title']}", flush=True)
            else:
                err += 1
                print(f"  [{done}/{total}] ERR {r['feed']['title']}: {r.get('error', '')[:100]}", flush=True)

    print(f"  Fetch done: {ok} ok, {not_mod} not modified, {err} errors", flush=True)
    return results
