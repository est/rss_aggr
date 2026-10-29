"""Main entry point with decoupled fetch/classify/save/report steps."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from src.opml_parser import parse_feeds
from src.classifier import classify_articles, is_skip_category
from src.storage import save_daily_results, load_seen_links, _update_index
from src.articles import (
    append_to_articles,
    cleanup_articles,
    get_article_links,
    load_articles,
    save_articles,
)
from src.urls import canonical_url
from src.state import (
    load_state,
    save_state,
    mark_ok,
    mark_failed,
    get_due_feeds,
    prioritize_feeds,
)
from src.aggregator import is_aggregator

_STATUS_DONE = ("classified", "skipped")


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _cached_sort_key(article: dict) -> datetime:
    dt = _parse_dt(article.get("cached_at"))
    return dt or datetime.min.replace(tzinfo=timezone.utc)


def _last_classified_at(articles: list[dict]) -> str:
    latest = None
    for article in articles:
        dt = _parse_dt(article.get("classified_at"))
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return latest.strftime("%Y-%m-%dT%H:%M:%SZ") if latest else ""


def _extract_site(url: str) -> str:
    """Extract netloc+path prefix from URL for matching.

    Note: scheme is intentionally dropped, so both http://example.com
    and https://example.com normalize to the same prefix.
    www. prefix is also stripped for consistent matching.
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return f"{netloc}{parsed.path.rstrip('/')}"
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


def _combine_skip_prompts(global_prompt: str, site_prompt: str) -> str:
    """Combine global and per-site skip rules (global first)."""
    global_prompt = (global_prompt or "").strip()
    site_prompt = (site_prompt or "").strip()
    if global_prompt and site_prompt and global_prompt != site_prompt:
        return f"{global_prompt} {site_prompt}"
    return site_prompt or global_prompt


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
    """Fetch RSS entries, append new articles to articles.json."""
    config = load_config()
    from src.fetcher import fetch_all_feeds
    fetch_cfg = config.get("fetch", {})
    filter_cfg = config.get("filter", {})
    limits = config.get("limits", {})
    user_agent = fetch_cfg.get("user_agent", "rss_aggr/1.0")
    keep_days = fetch_cfg.get("keep_days", 7)
    data_dir = config.get("storage", {}).get("data_dir", "output")
    max_feeds = limits.get("max_feeds", 0)
    fetch_interval = limits.get("fetch_interval_hours", 24)
    max_failures = limits.get("disable_after_failures", 3)
    max_workers = limits.get("max_workers", fetch_cfg.get("max_workers", 10))
    max_entries = limits.get("max_entries_per_feed", 0)
    skip_titles = filter_cfg.get("skip_titles", [])

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
        skip_titles=skip_titles,
        max_workers=max_workers,
        feeds_state=state.get("feeds", {}),
        max_entries_per_feed=max_entries,
    )

    all_entries = []
    aggregator_feeds = 0
    ok_count = 0
    not_modified_count = 0
    for r in results:
        url = r["feed"]["xml_url"]
        status = r.get("status", "error")
        if status in ("ok", "not_modified"):
            # A 304 is a successful fetch, never a failure.
            mark_ok(state, url, status=status)
            if status == "not_modified":
                not_modified_count += 1
            else:
                ok_count += 1
                if is_aggregator(r["entries"]):
                    aggregator_feeds += 1
                    state["feeds"][url]["is_aggregator"] = True
                all_entries.extend(r["entries"])
        else:
            mark_failed(state, url, r.get("error", "unknown"))

    if aggregator_feeds:
        print(f"[{ts()}] Aggregator feeds: {aggregator_feeds}", flush=True)

    if ok_count == 0 and not_modified_count == 0:
        save_state(state)
        raise RuntimeError(
            f"All {len(due_feeds)} due feeds failed to fetch; runtime is unhealthy"
        )

    # Dedup against rendered output, cached articles, and duplicates within
    # this run, using canonical URL keys.
    seen = set(load_seen_links(data_dir)) | set(get_article_links())
    new_entries = []
    for entry in all_entries:
        link = (entry.get("link") or "").strip()
        key = canonical_url(link) or link or entry.get("guid", "")
        if not key or key in seen:
            continue
        seen.add(key)
        new_entries.append(entry)
    print(f"[{ts()}] {len(new_entries)} new articles", flush=True)

    if new_entries:
        added = append_to_articles(new_entries)
        print(f"[{ts()}] Added {added} articles to cache", flush=True)

    save_state(state)


def step_classify():
    """Classify cached articles, write classified results to output (immutable)."""
    config = load_config()
    ai_cfg = config.get("ai", {})
    filter_cfg = config.get("filter", {})
    limits = config.get("limits", {})
    categories = [c["name"] for c in config.get("category", [])]
    data_dir = config.get("storage", {}).get("data_dir", "output")
    max_articles = limits.get("max_articles", 0)
    global_skip_prompt = (filter_cfg.get("skip_prompt") or "").strip()

    feeds = parse_feeds("feeds.toml")
    site_skip_prompt_rules = _build_site_skip_prompt_rules(feeds)

    # Load from articles cache, exclude already-rendered output. Classified
    # entries missing from output are retried so a failed write can self-heal;
    # skipped entries never need output.
    cached = load_articles()
    output_links = load_seen_links(data_dir)
    articles = []
    for a in cached:
        link = (a.get("link") or "").strip()
        if not link:
            continue
        status = a.get("status") or "pending"
        key = canonical_url(link) or link
        if status == "skipped":
            continue
        if key in output_links:
            continue
        articles.append(a)

    # Drop stale classifications so a failed retry cannot be mistaken for a
    # fresh result when statuses are persisted.
    for a in articles:
        a.pop("classification", None)
        a.pop("classification_meta", None)

    articles.sort(key=_cached_sort_key)
    if max_articles and max_articles > 0:
        articles = articles[:max_articles]
    print(f"[{ts()}] {len(articles)} articles to classify (from cache)", flush=True)

    if not articles:
        print(f"[{ts()}] Nothing to classify", flush=True)
        _update_index(
            data_dir,
            last_fetched=_now_iso(),
            last_classified=_last_classified_at(cached),
        )
        return

    by_site_rule: dict[str, list[dict]] = {}
    rule_prompts: dict[str, tuple[str, str]] = {}
    for a in articles:
        site_norm, site_prompt = _resolve_site_skip_prompt(a.get("link", ""), site_skip_prompt_rules)
        effective = _combine_skip_prompts(global_skip_prompt, site_prompt)
        by_site_rule.setdefault(effective, []).append(a)
        rule_prompts[effective] = (site_norm, site_prompt)

    total_classified = 0
    total_skipped = 0
    total_failed = 0

    for skip_prompt, arts in by_site_rule.items():
        site_norm, site_prompt = rule_prompts[skip_prompt]
        if site_prompt:
            print(f"  [{site_norm}] skip_prompt: {site_prompt}", flush=True)

        stats = classify_articles(
            arts,
            provider=ai_cfg.get("provider", "openai"),
            model=ai_cfg.get("model"),
            categories=categories,
            skip_prompt=skip_prompt,
            base_url=ai_cfg.get("base_url", ""),
            api_key_env=ai_cfg.get("api_key_env", ""),
        )
        total_classified += stats.classified
        total_skipped += stats.skipped
        total_failed += stats.failed

        now = _now_iso()
        classified = []
        for a in arts:
            cls = a.get("classification")
            if cls:
                a["classified_at"] = now
                if is_skip_category(cls.get("category", "")):
                    a["status"] = "skipped"
                else:
                    a["status"] = "classified"
                    classified.append(a)
                # Drop the full text; only the AI summary is retained.
                a["content"] = ""
            else:
                a["status"] = "failed"
                a["attempts"] = int(a.get("attempts") or 0) + 1

        # Persist statuses after every group so progress survives later failures.
        save_articles(cached)

        # Write only classified articles, immediately per group.
        if classified:
            save_daily_results(
                {"articles": classified},
                data_dir,
                last_fetched=now,
                last_classified=now,
                keep_days=0,
            )

    # Always regenerate the index, even when nothing was written.
    _update_index(
        data_dir,
        last_fetched=_now_iso(),
        last_classified=_last_classified_at(cached),
    )

    if total_classified or total_skipped:
        print(
            f"[{ts()}] Classified {total_classified} articles "
            f"({total_skipped} skipped) total",
            flush=True,
        )

    if articles and total_classified == 0 and total_skipped == 0 and total_failed > 0:
        raise RuntimeError(
            f"Classification failed for all {len(articles)} pending articles "
            f"({total_failed} failures); refusing to report a silent no-op"
        )


def step_cleanup():
    """Remove expired articles cache entries."""
    config = load_config()
    keep_days = config.get("fetch", {}).get("keep_days", 14)
    removed = cleanup_articles(keep_days=keep_days)
    print(f"[{ts()}] Cleaned up {removed} old cache entries", flush=True)


def step_report():
    """Build runtime status, write status.json + output/status.md, print summary."""
    from src.report import build_status, print_summary, write_status

    config = load_config()
    data_dir = config.get("storage", {}).get("data_dir", "output")
    status = build_status(data_dir=data_dir)
    if not status["output"]["index_exists"]:
        # The site index must exist even before the first classified article.
        _update_index(
            data_dir,
            last_fetched=status["last_fetch"],
            last_classified=status["last_classify"],
        )
        status = build_status(data_dir=data_dir)
    write_status(status, root="status.json", data_dir=data_dir)
    summary = print_summary(status)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(summary.rstrip() + "\n")
        except OSError:
            pass


def step_check():
    """Validate the runtime contract, exiting non-zero on hard violations."""
    from src.report import build_status, check_status, write_status

    config = load_config()
    limits = config.get("limits", {})
    data_dir = config.get("storage", {}).get("data_dir", "output")

    status_path = Path("status.json")
    status = None
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            status = None
    if not isinstance(status, dict):
        status = build_status(data_dir=data_dir)
        write_status(status, root="status.json", data_dir=data_dir)

    violations = check_status(
        status,
        max_backlog=limits.get("max_backlog", 2000),
        freshness_hours=limits.get("freshness_hours", 24),
    )
    for warning in status.get("warnings") or []:
        print(f"WARNING: {warning}", flush=True)

    if violations:
        print(f"[{ts()}] Runtime contract violations:", flush=True)
        for violation in violations:
            print(f"  - {violation}", flush=True)
        sys.exit(1)

    print(f"[{ts()}] Runtime contract OK", flush=True)


def main():
    parser = argparse.ArgumentParser(description="RSS Aggregator")
    parser.add_argument("--sync", action="store_true", help="Sync external feeds")
    parser.add_argument("--fetch", action="store_true", help="Fetch RSS entries")
    parser.add_argument("--classify", action="store_true", help="Classify with AI")
    parser.add_argument("--cleanup", action="store_true", help="Clean up old cache entries")
    parser.add_argument("--report", action="store_true", help="Write status report")
    parser.add_argument("--check", action="store_true", help="Verify runtime contract")
    args = parser.parse_args()

    actions = [args.sync, args.fetch, args.classify, args.cleanup, args.report, args.check]
    if not any(actions):
        step_sync()
        step_fetch()
        step_classify()
        step_cleanup()
    else:
        if args.sync:
            step_sync()
        if args.fetch:
            step_fetch()
        if args.classify:
            step_classify()
        if args.cleanup:
            step_cleanup()
        if args.report:
            step_report()
        if args.check:
            step_check()


if __name__ == "__main__":
    main()
