"""Main entry point with decoupled fetch/classify/save steps."""
import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from src.opml_parser import parse_feeds
from src.fetcher import fetch_all_feeds
from src.classifier import classify_articles
from src.storage import (
    save_daily_results, load_seen_guids, load_unclassified_links,
    update_classifications, cleanup_old_data, _update_index,
)
from src.state import load_state, save_state, mark_fed, mark_failed, get_due_feeds, prioritize_feeds, disable_stats
from src.aggregator import is_aggregator


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def load_config(path: str = "config.toml") -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def step_sync():
    """Sync external feed lists into feeds.toml."""
    config = load_config()
    user_agent = config.get("fetch", {}).get("user_agent", "rss_aggr/1.0")
    print(f"[{ts()}] Syncing external feeds...", flush=True)
    from src.sync import sync
    sync(user_agent=user_agent)
    print(f"[{ts()}] Sync done", flush=True)


def step_fetch():
    """Fetch RSS entries, save unclassified to output markdown."""
    config = load_config()
    fetch_cfg = config.get("fetch", {})
    user_agent = fetch_cfg.get("user_agent", "rss_aggr/1.0")
    keep_days = fetch_cfg.get("keep_days", 7)
    data_dir = config.get("storage", {}).get("data_dir", "output")
    max_feeds = config.get("limits", {}).get("max_feeds", 0)
    fetch_interval = config.get("limits", {}).get("fetch_interval_hours", 24)
    max_failures = config.get("limits", {}).get("disable_after_failures", 3)

    all_feeds = parse_feeds("feeds.toml")
    print(f"[{ts()}] {len(all_feeds)} feeds total", flush=True)

    state = load_state()
    due_feeds = get_due_feeds(all_feeds, state, fetch_interval, max_failures)
    due_feeds = prioritize_feeds(due_feeds)
    if max_feeds > 0:
        due_feeds = due_feeds[:max_feeds]
    print(f"[{ts()}] Due: {len(due_feeds)}", flush=True)

    if not due_feeds:
        print(f"[{ts()}] No feeds to fetch", flush=True)
        return

    results = fetch_all_feeds(
        due_feeds,
        timeout=fetch_cfg.get("timeout_seconds", 15),
        keep_days=keep_days,
        user_agent=user_agent,
    )

    all_entries = []
    aggregator_feeds = 0
    for r in results:
        url = r["feed"]["xml_url"]
        if r["status"] == "ok":
            mark_fed(state, url)
            if is_aggregator(r["entries"]):
                aggregator_feeds += 1
                state["feeds"][url]["is_aggregator"] = True
            all_entries.extend(r["entries"])
        else:
            mark_failed(state, url, r.get("error", "unknown"))

    if aggregator_feeds:
        print(f"[{ts()}] Aggregator feeds: {aggregator_feeds}", flush=True)

    # Dedup against output
    seen_links = load_seen_guids(data_dir)
    new_entries = [e for e in all_entries if e.get("link") not in seen_links]
    print(f"[{ts()}] {len(new_entries)} new articles", flush=True)

    if new_entries:
        written = save_daily_results({"articles": new_entries}, data_dir)
        files_str = ", ".join(str(f) for f in written) if written else "none"
        print(f"[{ts()}] Saved: {files_str}", flush=True)

    save_state(state)


def step_classify():
    """Classify unclassified articles in output markdown."""
    config = load_config()
    ai_cfg = config.get("ai", {})
    categories = [c["name"] for c in config.get("category", [])]
    data_dir = config.get("storage", {}).get("data_dir", "output")

    # Find unclassified links
    unclassified = load_unclassified_links(data_dir)
    print(f"[{ts()}] {len(unclassified)} unclassified articles in output", flush=True)

    if not unclassified:
        print(f"[{ts()}] Nothing to classify", flush=True)
        return

    # Build article dicts from links (need title for AI)
    articles = []
    base = Path(data_dir)
    for f in base.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.startswith("|") or line.startswith("|--") or line.startswith("| Author"):
                    continue
                m = re.search(r"\[([^\]]+)\]\(([^)]+)\)", line)
                if not m:
                    continue
                title, link = m.group(1), m.group(2)
                if link in unclassified:
                    articles.append({"title": title, "link": link})
        except OSError:
            continue

    print(f"[{ts()}] {len(articles)} articles to classify", flush=True)

    if not articles:
        return

    classify_articles(
        articles,
        provider=ai_cfg.get("provider", "openai"),
        model=ai_cfg.get("model"),
        categories=categories,
    )

    # Build updates dict
    updates = {}
    for a in articles:
        if "classification" in a:
            updates[a["link"]] = a["classification"]

    if updates:
        count = update_classifications(data_dir, updates)
        print(f"[{ts()}] Updated {count} articles in output", flush=True)


def main():
    parser = argparse.ArgumentParser(description="RSS Aggregator")
    parser.add_argument("--sync", action="store_true", help="Sync external feeds")
    parser.add_argument("--fetch", action="store_true", help="Fetch RSS entries")
    parser.add_argument("--classify", action="store_true", help="Classify with AI")
    args = parser.parse_args()

    if not any([args.sync, args.fetch, args.classify]):
        step_sync()
        step_fetch()
        step_classify()
    else:
        if args.sync:
            step_sync()
        if args.fetch:
            step_fetch()
        if args.classify:
            step_classify()


if __name__ == "__main__":
    main()
