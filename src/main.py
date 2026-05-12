"""Main entry point: orchestrate fetch → classify → store."""
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from src.opml_parser import parse_feeds
from src.fetcher import fetch_all_feeds
from src.aliveness import check_all_feeds
from src.classifier import classify_articles
from src.storage import save_daily_results, load_seen_guids, cleanup_old_data
from src.state import load_state, save_state, mark_fed, mark_failed, get_due_feeds, prioritize_feeds


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def load_config(path: str = "config.toml") -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def main():
    t0 = time.time()
    config = load_config()
    fetch_cfg = config.get("fetch", {})
    ai_cfg = config.get("ai", {})
    categories = [c["name"] for c in config.get("category", [])]
    data_dir = config.get("storage", {}).get("data_dir", "output")
    keep_days = config.get("storage", {}).get("keep_days", 90)
    max_feeds = config.get("limits", {}).get("max_feeds", 0)
    max_articles = config.get("limits", {}).get("max_articles", 0)
    fetch_interval = config.get("limits", {}).get("fetch_interval_hours", 24)

    print(f"[{ts()}] [1/5] Parsing feeds config...", flush=True)
    all_feeds = parse_feeds("feeds.toml")
    print(f"[{ts()}]   Total: {len(all_feeds)} feeds", flush=True)

    state = load_state()
    due_feeds = get_due_feeds(all_feeds, state, fetch_interval)
    due_feeds = prioritize_feeds(due_feeds)
    if max_feeds > 0:
        due_feeds = due_feeds[:max_feeds]
    print(f"[{ts()}]   Due: {len(due_feeds)} (interval={fetch_interval}h, limit={max_feeds or 'none'})", flush=True)

    if not due_feeds:
        print(f"[{ts()}] No feeds to process, exiting", flush=True)
        return

    print(f"\n[{ts()}] [2/5] Fetching RSS...", flush=True)
    t1 = time.time()
    results = fetch_all_feeds(
        due_feeds,
        timeout=fetch_cfg.get("timeout_seconds", 15),
        max_articles=fetch_cfg.get("max_articles_per_feed", 20),
    )
    elapsed = time.time() - t1

    all_entries = []
    for r in results:
        url = r["feed"]["xml_url"]
        if r["status"] == "ok":
            mark_fed(state, url)
            all_entries.extend(r["entries"])
        else:
            mark_failed(state, url, r.get("error", "unknown"))
    print(f"[{ts()}]   Done ({elapsed:.0f}s): {len(all_entries)} entries from {len(results)} feeds\n", flush=True)

    print(f"[{ts()}] [3/5] Filtering new articles...", flush=True)
    seen_links = load_seen_guids(data_dir)
    new_entries = [e for e in all_entries if e.get("link") not in seen_links]
    if max_articles > 0:
        new_entries = new_entries[:max_articles]
    print(f"[{ts()}]   {len(new_entries)} new (skipped {len(all_entries) - len(new_entries)} seen)\n", flush=True)

    print(f"[{ts()}] [4/5] Classifying with AI...", flush=True)
    classified = []
    skipped = 0
    if not new_entries:
        print(f"[{ts()}]   No new articles to classify", flush=True)
    else:
        t2 = time.time()
        classify_articles(
            new_entries,
            provider=ai_cfg.get("provider", "openai"),
            model=ai_cfg.get("model"),
            categories=categories,
        )
        elapsed = time.time() - t2
        for a in new_entries:
            if "classification" in a:
                classified.append(a)
            else:
                skipped += 1
        print(f"[{ts()}]   Done ({elapsed:.0f}s): {len(classified)} classified, {skipped} skipped\n", flush=True)

    removed = cleanup_old_data(data_dir, keep_days)
    if removed:
        print(f"[{ts()}]   Cleaned up {removed} old files", flush=True)

    output = {"articles": classified}
    file_path = save_daily_results(output, data_dir)
    save_state(state)
    total = time.time() - t0
    print(f"\n[{ts()}] [5/5] Saved to {file_path}", flush=True)
    print(f"[{ts()}] Total time: {total:.0f}s", flush=True)

    if classified:
        top = sorted(classified, key=lambda x: x["classification"]["score"], reverse=True)[:5]
        print(f"\n[{ts()}] Top 5:", flush=True)
        for a in top:
            c = a["classification"]
            print(f"  [{c['score']}/10] {a['title']}", flush=True)
            print(f"         {c.get('summary', '')}", flush=True)
            print()


if __name__ == "__main__":
    main()
