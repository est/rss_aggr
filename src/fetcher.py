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
RETRY_AFTER_MAX = 60  # cap server-provided Retry-After so a run cannot stall
MIN_PUBLISHED_YEAR = 1990


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


def _retry_after_seconds(resp: requests.Response) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP date)."""
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        retry_dt = parsedate_to_datetime(value)
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        delta = (retry_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _backoff_delay(attempt: int, resp: requests.Response | None = None) -> float:
    """Exponential backoff, preferring a server-provided Retry-After."""
    if resp is not None:
        retry_after = _retry_after_seconds(resp)
        if retry_after is not None:
            return min(retry_after, RETRY_AFTER_MAX)
    if attempt < len(RETRY_BACKOFF):
        return RETRY_BACKOFF[attempt]
    return min(2 ** attempt, RETRY_AFTER_MAX)


def _request_with_retry(url: str, headers: dict, timeout: tuple,
                        max_retries: int = MAX_RETRIES) -> requests.Response:
    """GET with exponential backoff retry for transient errors.

    304 is returned as-is (never raised). 429 and 5xx responses are retried;
    other 4xx responses fail immediately.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        resp = None
        try:
            resp = requests.get(url, timeout=timeout, headers=headers)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last_exc = e
        else:
            if resp.status_code == 304:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = requests.exceptions.HTTPError(
                    f"{resp.status_code} Server Error for url: {url}", response=resp)
            else:
                resp.raise_for_status()
                return resp

        if attempt < max_retries:
            time.sleep(_backoff_delay(attempt, resp))
        else:
            break

    if last_exc is None:
        raise requests.exceptions.RequestException(f"request failed for {url}")
    raise last_exc


def _parse_entry_published(entry, now: datetime | None = None) -> str:
    """Return a normalized UTC ISO timestamp, or "" when unusable.

    Rejects pre-1990 dates (feedparser often yields year 1 / epoch for broken
    feeds) and dates more than an hour in the future.
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return ""
    try:
        dt = datetime(*tuple(parsed)[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    if dt.year < MIN_PUBLISHED_YEAR:
        return ""
    now = now or datetime.now(timezone.utc)
    if dt > now + timedelta(hours=1):
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _update_conditional_state(feed_state: dict | None, resp: requests.Response):
    if feed_state is None:
        return
    etag = resp.headers.get("ETag")
    last_mod = resp.headers.get("Last-Modified")
    if etag:
        feed_state["etag"] = etag
    if last_mod:
        feed_state["last_modified"] = last_mod


def fetch_feed(feed_info: dict, timeout: int | tuple | None = None,
               keep_days: int = DEFAULT_KEEP_DAYS,
               user_agent: str = "rss_aggr/1.0", skip_titles: list[str] = None,
               feed_state: dict | None = None,
               max_entries_per_feed: int = 0) -> dict:
    """Fetch a single RSS feed, return entries within keep_days, filtered by rules.

    Args:
        feed_state: dict from state.json for this feed (stores etag/last_modified).
        max_entries_per_feed: cap on returned entries (0 = unlimited). Entries
            are sorted newest-first before the cap is applied.
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

    # Update ETag / Last-Modified from response (including 304).
    _update_conditional_state(feed_state, resp)

    if resp.status_code == 304:
        return {
            "feed": feed_info,
            "status": "not_modified",
            "entries": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    resp.encoding = _detect_encoding(resp)
    parsed = feedparser.parse(resp.text)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=keep_days)
    entries = []
    for entry in parsed.entries:
        pub_date = _parse_entry_published(entry, now)

        # Skip articles older than keep_days
        if pub_date:
            try:
                pub_dt = datetime.strptime(pub_date, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            except ValueError:
                pub_date = ""

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

    # Newest first; undated entries sort last. Keeps ties stable.
    entries.sort(key=lambda e: (e["published"] != "", e["published"]), reverse=True)
    if max_entries_per_feed and max_entries_per_feed > 0:
        entries = entries[:max_entries_per_feed]

    return {
        "feed": feed_info,
        "status": "ok",
        "entries": entries,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_all_feeds(feeds: list[dict], timeout: int = 15, keep_days: int = DEFAULT_KEEP_DAYS,
                    user_agent: str = "rss_aggr/1.0", skip_titles: list[str] = None,
                    max_workers: int = 10, feeds_state: dict | None = None,
                    max_entries_per_feed: int = 0) -> list[dict]:
    """Fetch all feeds in parallel with progress logging.

    Args:
        max_workers: number of parallel threads.
        feeds_state: dict of {url: feed_state} from state.json for conditional requests.
        max_entries_per_feed: per-feed entry cap (0 = unlimited).
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
        return fetch_feed(f, timeout, keep_days, user_agent, skip_titles,
                          feed_state=fs, max_entries_per_feed=max_entries_per_feed)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, f): f for f in feeds}
        for future in as_completed(futures):
            try:
                r = future.result()
            except Exception as e:
                f = futures[future]
                r = {
                    "feed": f,
                    "status": "error",
                    "error": str(e),
                    "entries": [],
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
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
