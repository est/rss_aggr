"""Main entry point with step-based CLI for retryable Actions."""
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
from src.storage import save_daily_results, load_seen_guids, cleanup_old_data, _update_index
from src.state import load_state, save_state, mark_fed, mark_failed, get_due_feeds, prioritize_feeds, disable_stats
from src.aggregator import is_aggregator

ENTRIES_FILE = ".cache/entries.json"


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _load_classified_links(data_dir: str = "output") -> set[str]:
    """Load links that already have a classification score in output markdown."""
    links = set()
    base = Path(data_dir)
    if not base.exists():
        return links

    for f in base.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.startswith("|") or line.startswith("|--") or line.startswith("| Author"):
                    continue
                # Extract link
                m = re.search(r"\]\(([^)]+)\)", line)
                if not m:
                    continue
                link = m.group(1)
                # Check if score column is not "—"
                cols = [c.strip() for c in line.split("|")]
                if len(cols) >= 5 and cols[4] not in ("—", ""):
                    links.add(link)
        except OSError:
            continue
    return links


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
    """Fetch RSS entries, update state, save to cache."""
    config = load_config()
    fetch_cfg = config.get("fetch", {})
    user_agent = fetch_cfg.get("user_agent", "rss_aggr/1.0")
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
        Path(ENTRIES_FILE).write_text("[]", encoding="utf-8")
        return

    results = fetch_all_feeds(
        due_feeds,
        timeout=fetch_cfg.get("timeout_seconds", 15),
        max_articles=fetch_cfg.get("max_articles_per_feed", 20),
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
                for e in r["entries"]:
                    e["_skip_ai"] = True
            all_entries.extend(r["entries"])
        else:
            mark_failed(state, url, r.get("error", "unknown"))

    if aggregator_feeds:
        print(f"[{ts()}] Aggregator feeds: {aggregator_feeds}", flush=True)

    # Dedup
    seen_links = load_seen_guids(data_dir)
    new_entries = [e for e in all_entries if e.get("link") not in seen_links]
    max_articles = config.get("limits", {}).get("max_articles", 0)
    if max_articles > 0:
        new_entries = new_entries[:max_articles]
    print(f"[{ts()}] {len(new_entries)} new articles ({len(all_entries) - len(new_entries)} seen)", flush=True)

    Path(".cache").mkdir(exist_ok=True)
    Path(ENTRIES_FILE).write_text(json.dumps(new_entries, ensure_ascii=False, indent=2), encoding="utf-8")
    save_state(state)
    print(f"[{ts()}] Saved {ENTRIES_FILE}", flush=True)


def step_classify():
    """Classify cached entries, skip already-classified ones."""
    config = load_config()
    ai_cfg = config.get("ai", {})
    categories = [c["name"] for c in config.get("category", [])]
    data_dir = config.get("storage", {}).get("data_dir", "output")

    cache = Path(ENTRIES_FILE)
    if not cache.exists():
        print(f"[{ts()}] No entries to classify", flush=True)
        return

    entries = json.loads(cache.read_text(encoding="utf-8"))
    if not entries:
        print(f"[{ts()}] Empty entries cache", flush=True)
        return

    # Load already-classified links from output
    classified_links = _load_classified_links(data_dir)
    print(f"[{ts()}] {len(entries)} articles, {len(classified_links)} already classified in output", flush=True)

    # Split aggregator vs normal
    ai_entries = []
    skipped_already = 0
    for e in entries:
        if e.get("_skip_ai"):
            continue
        if e.get("link") in classified_links:
            skipped_already += 1
            continue
        ai_entries.append(e)

    if skipped_already:
        print(f"[{ts()}] Skipped {skipped_already} already classified", flush=True)
    print(f"[{ts()}] {len(ai_entries)} to classify", flush=True)

    if ai_entries:
        classify_articles(
            ai_entries,
            provider=ai_cfg.get("provider", "openai"),
            model=ai_cfg.get("model"),
            categories=categories,
        )

    cache.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{ts()}] Classification done", flush=True)


def step_save():
    """Save cached entries to markdown."""
    config = load_config()
    data_dir = config.get("storage", {}).get("data_dir", "output")
    keep_days = config.get("storage", {}).get("keep_days", 90)

    cache = Path(ENTRIES_FILE)
    if not cache.exists():
        print(f"[{ts()}] No entries to save", flush=True)
        return

    entries = json.loads(cache.read_text(encoding="utf-8"))
    if not entries:
        print(f"[{ts()}] Empty entries cache", flush=True)
        return

    removed = cleanup_old_data(data_dir, keep_days)
    if removed:
        print(f"[{ts()}] Cleaned {removed} old files", flush=True)

    written = save_daily_results({"articles": entries}, data_dir)
    files_str = ", ".join(str(f) for f in written) if written else "none"
    print(f"[{ts()}] Saved: {files_str}", flush=True)

    scored = [a for a in entries if "classification" in a]
    if scored:
        top = sorted(scored, key=lambda x: x["classification"]["score"], reverse=True)[:5]
        print(f"\n[{ts()}] Top 5:", flush=True)
        for a in top:
            c = a["classification"]
            print(f"  [{c['score']}/10] {a['title']}")
            print(f"         {c.get('summary', '')}")


def main():
    parser = argparse.ArgumentParser(description="RSS Aggregator")
    parser.add_argument("--sync", action="store_true", help="Sync external feeds")
    parser.add_argument("--fetch", action="store_true", help="Fetch RSS entries")
    parser.add_argument("--classify", action="store_true", help="Classify with AI")
    parser.add_argument("--save", action="store_true", help="Save to markdown")
    args = parser.parse_args()

    # No args = run all steps
    if not any([args.sync, args.fetch, args.classify, args.save]):
        step_sync()
        step_fetch()
        step_classify()
        step_save()
    else:
        if args.sync:
            step_sync()
        if args.fetch:
            step_fetch()
        if args.classify:
            step_classify()
        if args.save:
            step_save()


if __name__ == "__main__":
    main()
