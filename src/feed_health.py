"""Feed health checker: validate RSS URLs, detect dead/broken feeds, 
measure update frequency, and flag content farms.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests


def check_feed_health(feed_info: dict, timeout: int = 15) -> dict:
    """Check health of a single RSS feed.

    Returns dict with:
      - status: ok / error / redirect / empty / timeout
      - entry_count: number of entries in feed
      - avg_entry_age_days: average age of entries
      - has_content: whether entries have substantial content
      - redirect_url: if feed redirected
      - error: error message if any
    """
    url = feed_info.get("xml_url", "")
    if not url:
        return {"status": "error", "error": "no url"}

    headers = {"User-Agent": "rss_aggr/health_check/1.0"}

    try:
        resp = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return {"status": "timeout", "error": "timeout"}
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        return {"status": "error", "error": f"HTTP {status}"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}

    # Check for redirect
    redirect_url = ""
    if resp.history:
        redirect_url = resp.url

    # Parse feed
    resp.encoding = resp.apparent_encoding or "utf-8"
    parsed = feedparser.parse(resp.text)

    entries = parsed.entries if hasattr(parsed, 'entries') else []
    entry_count = len(entries)

    if entry_count == 0:
        return {
            "status": "empty",
            "entry_count": 0,
            "redirect_url": redirect_url,
            "has_content": False,
        }

    # Analyze entry age and content
    now = datetime.now(timezone.utc)
    ages = []
    has_content = False
    for entry in entries[:10]:  # sample first 10
        # Age
        pub = None
        if hasattr(entry, 'published_parsed') and entry.published_parsed:
            pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
            pub = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
        if pub:
            age = (now - pub).total_seconds() / 86400
            ages.append(age)

        # Content
        content = ""
        if hasattr(entry, 'summary'):
            content = entry.summary
        elif hasattr(entry, 'description'):
            content = entry.description
        content_text = re.sub(r'<[^>]+>', '', content)
        if len(content_text) > 200:
            has_content = True

    avg_age = sum(ages) / len(ages) if ages else -1

    return {
        "status": "ok",
        "entry_count": entry_count,
        "avg_entry_age_days": round(avg_age, 1),
        "has_content": has_content,
        "redirect_url": redirect_url,
        "error": "",
    }


def batch_check_health(feeds: list[dict], max_workers: int = 20,
                       timeout: int = 15) -> dict:
    """Check health of multiple feeds in parallel.

    Returns dict mapping feed xml_url -> health result.
    """
    results = {}
    total = len(feeds)

    def _check_one(f):
        url = f.get("xml_url", "")
        return url, check_feed_health(f, timeout)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_check_one, f): f for f in feeds}
        done = 0
        for future in as_completed(futures):
            url, result = future.result()
            results[url] = result
            done += 1
            status = result.get("status", "error")
            if status != "ok":
                print(f"  [{done}/{total}] {status}: {url} - {result.get('error', '')[:80]}")
            elif done % 50 == 0:
                print(f"  [{done}/{total}] checked...")

    return results


def generate_health_report(health_results: dict, output_path: str = "output/health_report.md"):
    """Generate a markdown health report."""
    ok = []
    errors = []
    empty = []
    timeouts = []

    for url, h in health_results.items():
        status = h.get("status", "error")
        entry = {"url": url, **h}
        if status == "ok":
            ok.append(entry)
        elif status == "timeout":
            timeouts.append(entry)
        elif status == "empty":
            empty.append(entry)
        else:
            errors.append(entry)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Feed Health Report - {now}",
        "",
        f"## Summary",
        f"- ✅ OK: {len(ok)}",
        f"- ❌ Error: {len(errors)}",
        f"- 📭 Empty: {len(empty)}",
        f"- ⏰ Timeout: {len(timeouts)}",
        f"- **Total: {len(health_results)}**",
        "",
    ]

    if errors:
        lines.append("## ❌ Error Feeds")
        for e in sorted(errors, key=lambda x: x.get("error", "")):
            lines.append(f"- `{e['url']}` — {e.get('error', '')[:100]}")
        lines.append("")

    if empty:
        lines.append("## 📭 Empty Feeds (no entries)")
        for e in empty:
            lines.append(f"- `{e['url']}`")
        lines.append("")

    if timeouts:
        lines.append("## ⏰ Timeout Feeds")
        for e in timeouts:
            lines.append(f"- `{e['url']}`")
        lines.append("")

    if ok:
        lines.append("## ✅ Healthy Feeds (sample)")
        # Show oldest feeds as they're most valuable
        oldest = sorted([e for e in ok if e.get("avg_entry_age_days", 999) < 365],
                       key=lambda x: x.get("avg_entry_age_days", 999))
        for e in oldest[:20]:
            age = e.get("avg_entry_age_days", -1)
            count = e.get("entry_count", 0)
            lines.append(f"- `{e['url']}` — {count} entries, avg age {age}d")
        lines.append("")

    report = "\n".join(lines)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(report, encoding="utf-8")
    print(f"Health report saved: {output_path}")
    return output_path


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from src.opml_parser import parse_feeds

    feeds = parse_feeds("feeds.toml")
    print(f"Checking {len(feeds)} feeds...")
    results = batch_check_health(feeds)
    generate_health_report(results)

    ok_count = sum(1 for r in results.values() if r["status"] == "ok")
    print(f"\nResult: {ok_count}/{len(feeds)} healthy")
