"""Main entry point with decoupled fetch/classify/save steps."""
import argparse
from datetime import datetime, timezone, timedelta

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from src.opml_parser import parse_feeds
from src.classifier import classify_articles, is_skip_category
from src.storage import (
    save_daily_results, load_seen_guids, load_unclassified_links, load_unclassified_links_map,
    update_classifications, remove_articles,
    collect_articles_for_links,
    save_pending_content, load_pending_content, clear_pending_content,
)
from src.state import load_state, save_state, mark_fed, mark_failed, get_due_feeds, prioritize_feeds
from src.aggregator import is_aggregator


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _extract_site(url: str) -> str:
    """Extract netloc+path prefix from URL for matching.

    Note: scheme is intentionally dropped, so both http://example.com
    and https://example.com normalize to the same prefix.
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path.rstrip('/')}"
    except Exception:
        return ""


def _build_site_skip_prompt_rules(feeds: list[dict]) -> list[tuple[str, str]]:
    """Build site->skip_prompt rules sorted by most-specific site prefix first."""
    rules = []
    for f in feeds:
        sp = (f.get("skip_prompt") or "").strip()
        site = (f.get("html_url") or "").strip()
        site_norm = _extract_site(site)
        if site_norm and sp:
            rules.append((site_norm, sp))
    rules.sort(key=lambda x: len(x[0]), reverse=True)
    return rules


def _resolve_site_skip_prompt(article_link: str, rules: list[tuple[str, str]]) -> tuple[str, str]:
    """Resolve per-site skip_prompt for an article link."""
    article_site = _extract_site(article_link)
    if not article_site:
        return "", ""
    for site_norm, skip_prompt in rules:
        if article_site == site_norm or article_site.startswith(site_norm + "/"):
            return site_norm, skip_prompt
    return "", ""


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
    from src.fetcher import fetch_all_feeds
    fetch_cfg = config.get("fetch", {})
    filter_cfg = config.get("filter", {})
    user_agent = fetch_cfg.get("user_agent", "rss_aggr/1.0")
    keep_days = fetch_cfg.get("keep_days", 7)
    data_dir = config.get("storage", {}).get("data_dir", "output")
    max_feeds = config.get("limits", {}).get("max_feeds", 0)
    fetch_interval = config.get("limits", {}).get("fetch_interval_hours", 24)
    max_failures = config.get("limits", {}).get("disable_after_failures", 3)
    skip_titles = filter_cfg.get("skip_titles", [])

    all_feeds = parse_feeds("feeds.toml")
    print(f"[{ts()}] {len(all_feeds)} feeds total", flush=True)

    state = load_state()
    skipped_links = set(state.get("skipped_links", {}))
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
        skip_titles=skip_titles,
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
    new_entries = [
        e for e in all_entries
        if e.get("link") not in seen_links and e.get("link") not in skipped_links
    ]
    print(
        f"[{ts()}] {len(new_entries)} new articles "
        f"(excluded {len(skipped_links)} skipped links)",
        flush=True,
    )

    if new_entries:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        written = save_daily_results({"articles": new_entries}, data_dir, last_fetched=now, keep_days=keep_days)
        files_str = ", ".join(str(f) for f in written) if written else "none"
        print(f"[{ts()}] Saved: {files_str}", flush=True)
        save_pending_content(new_entries, data_dir)
        print(f"[{ts()}] Cached content for {len(new_entries)} articles", flush=True)

    save_state(state)


def step_classify():
    """Classify unclassified articles in output markdown."""
    config = load_config()
    ai_cfg = config.get("ai", {})
    categories = [c["name"] for c in config.get("category", [])]
    data_dir = config.get("storage", {}).get("data_dir", "output")
    keep_days = config.get("fetch", {}).get("keep_days", 14)
    max_articles = config.get("limits", {}).get("max_articles", 0)

    feeds = parse_feeds("feeds.toml")
    site_skip_prompt_rules = _build_site_skip_prompt_rules(feeds)

    state = load_state()
    skipped_links = set(state.get("skipped_links", {}))

    unclassified = load_unclassified_links(data_dir)
    unclassified_map = load_unclassified_links_map(data_dir)
    only_unclassified_links = set()
    for links in unclassified_map.values():
        only_unclassified_links.update(links)
    unclassified -= skipped_links
    only_unclassified_links -= skipped_links
    print(f"[{ts()}] {len(unclassified)} unclassified (excluded {len(skipped_links)} previously skipped)", flush=True)

    if not unclassified:
        print(f"[{ts()}] Nothing to classify", flush=True)
        return

    articles = collect_articles_for_links(data_dir, unclassified)

    pending_content = load_pending_content(data_dir)
    if pending_content:
        for a in articles:
            if a.get("link") in pending_content and not a.get("content"):
                a["content"] = pending_content[a["link"]]
        print(f"  Merged content for {len(pending_content)} pending articles", flush=True)

    print(f"[{ts()}] {len(articles)} articles to classify", flush=True)

    if not articles:
        return

    if max_articles > 0 and len(articles) > max_articles:
        articles = articles[:max_articles]
        print(f"  Limited to {max_articles} articles", flush=True)

    by_site_rule: dict[tuple[str, str], list[dict]] = {}
    for a in articles:
        rule = _resolve_site_skip_prompt(a.get("link", ""), site_skip_prompt_rules)
        by_site_rule.setdefault(rule, []).append(a)

    all_updates = {}
    all_newly_skipped = {}

    for (site_norm, skip_prompt), arts in by_site_rule.items():
        if skip_prompt:
            print(f"  [{site_norm}] skip_prompt: {skip_prompt}", flush=True)

        classify_articles(
            arts,
            provider=ai_cfg.get("provider", "openai"),
            model=ai_cfg.get("model"),
            categories=categories,
            skip_prompt=skip_prompt,
        )

        for a in arts:
            cls = a.get("classification", {})
            if is_skip_category(cls.get("category", "")):
                all_newly_skipped[a["link"]] = datetime.now(timezone.utc).isoformat()
            elif "classification" in a:
                all_updates[a["link"]] = a["classification"]

    if all_newly_skipped:
        all_skipped = {**state.get("skipped_links", {}), **all_newly_skipped}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        state["skipped_links"] = {k: v for k, v in all_skipped.items() if v > cutoff}
        save_state(state)
        removed = remove_articles(data_dir, set(all_newly_skipped.keys()))
        print(f"[{ts()}] Removed {removed} skipped articles from output", flush=True)

    if all_updates:
        count = update_classifications(data_dir, all_updates, only_links=only_unclassified_links)
        print(f"[{ts()}] Updated {count} articles in output", flush=True)

    clear_pending_content(data_dir)


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
