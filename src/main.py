"""Main entry point: orchestrate fetch → classify → store."""
import sys
from pathlib import Path

import yaml

from src.opml_parser import parse_opml
from src.fetcher import fetch_all_feeds
from src.aliveness import check_all_feeds
from src.classifier import classify_articles
from src.storage import save_daily_results, load_seen_guids, cleanup_old_data


def load_config(path: str = "config.yml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    config = load_config()
    fetch_cfg = config.get("fetch", {})
    ai_cfg = config.get("ai", {})
    categories = [c["name"] for c in config.get("categories", [])]

    print("[1/5] Parsing OPML feeds...")
    feeds = parse_opml("feeds.opml")
    print(f"  Found {len(feeds)} feeds")

    print("[2/5] Checking feed aliveness...")
    health = check_all_feeds(feeds, timeout=fetch_cfg.get("timeout_seconds", 15))
    alive_count = sum(1 for h in health if h["status"] == "alive")
    print(f"  {alive_count}/{len(feeds)} feeds alive")

    print("[3/5] Fetching RSS entries...")
    results = fetch_all_feeds(
        feeds,
        timeout=fetch_cfg.get("timeout_seconds", 15),
        max_articles=fetch_cfg.get("max_articles_per_feed", 20),
    )
    all_entries = []
    for r in results:
        if r["status"] == "ok":
            all_entries.extend(r["entries"])
    print(f"  Fetched {len(all_entries)} entries total")

    print("[4/5] Filtering new articles...")
    seen_guids = load_seen_guids(config.get("storage", {}).get("data_dir", "output/data"))
    new_entries = [e for e in all_entries if e["guid"] not in seen_guids]
    print(f"  {len(new_entries)} new articles (skipped {len(all_entries) - len(new_entries)} seen)")

    print("[5/5] Classifying articles with AI...")
    if not new_entries:
        print("  No new articles to classify")
    else:
        new_entries = classify_articles(
            new_entries,
            provider=ai_cfg.get("provider", "openai"),
            model=ai_cfg.get("model"),
            categories=categories,
        )
        avg_score = sum(
            e["classification"]["score"] for e in new_entries
        ) / len(new_entries)
        print(f"  Classified {len(new_entries)} articles, avg score: {avg_score:.1f}")

    # Save results
    data_dir = config.get("storage", {}).get("data_dir", "output/data")
    keep_days = config.get("storage", {}).get("keep_days", 90)

    removed = cleanup_old_data(data_dir, keep_days)
    if removed:
        print(f"  Cleaned up {removed} old data files (keep_days={keep_days})")

    output = {
        "feeds_count": len(feeds),
        "health": health,
        "articles": new_entries,
        "stats": {
            "total_fetched": len(all_entries),
            "new_articles": len(new_entries),
            "feeds_alive": alive_count,
        },
    }
    file_path = save_daily_results(output, data_dir)
    print(f"\nResults saved to {file_path}")

    # Print top articles
    if new_entries:
        top = sorted(new_entries, key=lambda x: x["classification"]["score"], reverse=True)[:5]
        print("\nTop 5 articles:")
        for a in top:
            c = a["classification"]
            print(f"  [{c['score']}/10] {a['title']}")
            print(f"         {c['category']} | {', '.join(c['tags'])}")
            print(f"         {c['summary']}")
            print()


if __name__ == "__main__":
    main()
