"""Main entry point with decoupled fetch/classify/save/report steps."""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from src import md_archive
from src.aggregator import is_aggregator
from src.article_state import (
    archive_fields,
    article_archive_ref,
    article_keys,
    cleanup_articles,
    clear_content,
    load_articles,
    save_articles,
    set_content_hydrated,
    store_fetched_entries,
    validate_articles_file,
)
from src.classifier import classify_articles, is_skip_category
from src.feed_state import (
    collect_feed_urls,
    get_due_feeds,
    load_state,
    mark_failed,
    mark_ok,
    prune_feeds,
    save_state,
)
from src.opml_parser import parse_feeds
from src.paths import ARCHIVE_DIR, FEEDS_DIR, OUTPUT_DIR
from src.sanitize import sanitize_html, sanitize_text
from src.storage import _update_index, save_daily_results
from src.urls import canonical_url, is_safe_http_url

_STATUS_DONE = ("classified", "skipped", "unclassified")


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _archive_root(archive_dir: str) -> Path:
    """Resolve the directory that directly contains ``YYYY/MM/DD.md``."""
    path = Path(archive_dir)
    return path if path.name == "archive" else path / "archive"


def _archive_record_path(archive_root: Path, archive_file: str) -> Path:
    """Resolve an index ``file`` value (e.g. ``archive/2026/05/01.md``)."""
    path = Path(archive_file)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == "archive":
        return Path(archive_root).parent / path
    return Path(archive_root) / path


def _feed_meta_for_result(result: dict) -> dict:
    """Build feed metadata from one fetch result."""
    feed = result.get("feed") or {}
    feed_url = feed.get("xml_url") or ""
    site_url = feed.get("html_url") or feed.get("site_url") or ""
    title = feed.get("title") or ""
    return {
        "feed_id": md_archive.feed_id(feed_url, site_url, title),
        "feed_url": feed_url,
        "site_url": site_url,
        "title": sanitize_text(title),
    }


def _feed_meta_for_article(article: dict) -> dict:
    """Build feed metadata from an article_state.json record."""
    feed_url = article.get("feed_url") or ""
    site_url = article.get("site_url") or ""
    title = article.get("feed_title") or ""
    return {
        "feed_id": article.get("feed_id")
        or md_archive.feed_id(feed_url, site_url, title),
        "feed_url": feed_url,
        "site_url": site_url,
        "title": title,
    }


def _archive_ref_exists(ref: dict | None, archive_root: Path) -> bool:
    """Whether an article-state archive reference still points at a file."""
    if not ref:
        return False
    path = _archive_record_path(archive_root, ref.get("path", ""))
    return path.is_file()


def _repair_pending_indexes(feeds_dir: str) -> int:
    """Append feed-index records for article-state rows marked ``indexed=false``.

    Fetch writes archive -> article_state -> feed index. If the index append
    fails, the row keeps ``indexed=false`` and this pass rebuilds just that
    record from the archive on the next run, preserving annotations and
    avoiding a duplicate archive body.
    """
    articles = load_articles()
    pending = [
        article
        for article in articles
        if article.get("indexed") is False and article_archive_ref(article)
    ]
    if not pending:
        return 0

    repaired = 0
    for article in pending:
        ref = article_archive_ref(article)
        if not ref:
            continue
        path = Path(ref["path"])
        try:
            parsed = md_archive.read_record(path, ref["offset"], ref["length"])
        except (OSError, ValueError) as exc:
            print(
                f"[{ts()}] WARNING: cannot repair feed index for "
                f"{ref['path']}@{ref['offset']}: {exc}",
                flush=True,
            )
            continue
        meta = parsed.get("meta") or {}
        feed_meta = _feed_meta_for_article(article)
        index_record = {
            "file": ref["path"],
            "offset": ref["offset"],
            "length": ref["length"],
            "body_offset": parsed.get("body_offset"),
            "body_len": parsed.get("body_len"),
            "sha256": parsed.get("sha256"),
            "guid": article.get("guid") or meta.get("guid", ""),
            "link": article.get("link") or meta.get("link", ""),
            "title": article.get("title") or meta.get("title", ""),
            "author": article.get("author") or meta.get("author", ""),
            "published": article.get("published") or meta.get("published", ""),
        }
        try:
            md_archive.append_feed_record(feeds_dir, feed_meta, index_record)
        except (OSError, ValueError) as exc:
            print(
                f"[{ts()}] WARNING: could not repair feed index for "
                f"{feed_meta['feed_id']}: {exc}",
                flush=True,
            )
            continue
        article["indexed"] = True
        repaired += 1

    if repaired:
        save_articles(articles)
    return repaired


def _load_feed_index_keys(feeds_dir: str, feed_meta: dict) -> tuple[dict, set[str]]:
    """Load one feed index once and return ``(feed_meta, article keys)``."""
    path = Path(feeds_dir) / f"{feed_meta['feed_id']}.jsonl"
    keys: set[str] = set()
    try:
        _header, records = md_archive.read_feed_index(path)
    except (OSError, ValueError) as exc:
        print(f"[{ts()}] WARNING: cannot read feed index {path}: {exc}", flush=True)
        records = []
    for record in records:
        if record.get("kind", "article") == "article":
            keys.update(article_keys(record))
    return feed_meta, keys


def _archive_article_meta(entry: dict, feed_meta: dict, fetched_at: str) -> dict:
    """Article header metadata for :func:`src.md_archive.append_articles`."""
    return {
        "guid": entry.get("guid") or "",
        "feed_id": feed_meta["feed_id"],
        "feed_url": feed_meta["feed_url"],
        "site_url": feed_meta["site_url"],
        "feed_title": sanitize_text(feed_meta.get("title") or entry.get("feed_title") or ""),
        "feed_category": sanitize_text(entry.get("feed_category") or ""),
        "link": (entry.get("link") or "").strip(),
        "title": sanitize_text(entry.get("title") or ""),
        "author": sanitize_text(entry.get("author") or ""),
        "published": entry.get("published") or "",
        "fetched_at": fetched_at,
    }


def _article_metadata_record(entry: dict, feed_meta: dict, record: dict) -> dict:
    """Build the article_state.json row for an appended archive record."""
    metadata = {
        "guid": entry.get("guid") or record.get("guid") or "",
        "title": sanitize_text(entry.get("title") or ""),
        "link": (entry.get("link") or "").strip(),
        "author": sanitize_text(entry.get("author") or ""),
        "published": entry.get("published") or "",
        "feed_title": sanitize_text(feed_meta.get("title") or ""),
        "feed_category": sanitize_text(entry.get("feed_category") or ""),
        "feed_id": feed_meta["feed_id"],
        "feed_url": feed_meta.get("feed_url") or "",
        "site_url": feed_meta.get("site_url") or "",
    }
    metadata["canonical"] = (
        canonical_url(metadata["link"]) or metadata["link"] or metadata["guid"]
    )
    metadata.update(archive_fields(record))
    metadata["indexed"] = False
    return metadata


def _annotation_for_article(article: dict) -> dict:
    """Build an append-only feed annotation for a classified/title-only row."""
    classification = article.get("classification")
    if not isinstance(classification, dict):
        classification = {}
    return {
        "guid": article.get("guid") or "",
        "file": article.get("archive_file") or "",
        "offset": article.get("archive_offset"),
        "category": classification.get("category") or "",
        "summary": classification.get("summary") or "",
        "score": classification.get("score"),
        "classified_at": article.get("classified_at") or "",
    }


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


def _storage_paths(config: dict) -> tuple[str, str, str]:
    """Return the fixed layout and reject unsupported path overrides.

    The GitHub Actions workflow, the archive tooling and the renderer all
    assume these three top-level names. Allowing config.toml to move only the
    Python half of the pipeline would silently write data outside the cache
    and snapshot trees, so overrides are a hard error instead.
    """
    storage = config.get("storage", {})
    fixed = {
        "data_dir": OUTPUT_DIR,
        "archive_dir": ARCHIVE_DIR,
        "feeds_dir": FEEDS_DIR,
    }
    for key, expected in fixed.items():
        value = storage.get(key, expected)
        if value != expected:
            raise RuntimeError(
                f"storage.{key}={value!r} is unsupported; the runtime layout is "
                f"fixed to {expected!r}. Remove the override from config.toml."
            )
    return OUTPUT_DIR, ARCHIVE_DIR, FEEDS_DIR


def step_sync():
    """Sync external feed lists into feeds.toml."""
    config = load_config()
    user_agent = config.get("fetch", {}).get("user_agent", "rss_aggr/1.0")
    print(f"[{ts()}] Syncing external feeds...", flush=True)
    from src.sync import sync
    sync(user_agent=user_agent)
    print(f"[{ts()}] Sync done", flush=True)


def step_fetch():
    """Fetch RSS entries, archive Markdown bodies, and update metadata cache."""
    config = load_config()
    from src.fetcher import fetch_all_feeds
    fetch_cfg = config.get("fetch", {})
    filter_cfg = config.get("filter", {})
    limits = config.get("limits", {})
    data_dir, archive_dir, feeds_dir = _storage_paths(config)
    user_agent = fetch_cfg.get("user_agent", "rss_aggr/1.0")
    keep_days = fetch_cfg.get("keep_days", 7)
    max_feeds = limits.get("max_feeds", 0)
    fetch_interval = limits.get("fetch_interval_hours", 24)
    if os.environ.get("RSS_FORCE_FETCH") == "1":
        # Cache miss: data-branch article_state.json has no raw bodies, so ignore
        # the normal interval and rebuild the 14-day window immediately.
        fetch_interval = 0
    max_failures = limits.get("disable_after_failures", 3)
    max_workers = limits.get("max_workers", fetch_cfg.get("max_workers", 10))
    max_entries = limits.get("max_entries_per_feed", 0)
    skip_titles = filter_cfg.get("skip_titles", [])

    cache_status = validate_articles_file()
    if cache_status == "corrupt":
        raise RuntimeError(
            "article_state.json is corrupt; restore it from the data branch before running"
        )
    # With no cache, persisted ETags would produce 304s and the cache would
    # never be rebuilt. Force a full fetch in that case. The workflow also
    # sets RSS_FORCE_FETCH=1 on a cache miss even when a metadata-only
    # article_state.json survived.
    force_refresh = (
        cache_status == "missing"
        or os.environ.get("RSS_FORCE_FETCH") == "1"
    )
    cache_missing = cache_status == "missing"

    all_feeds = parse_feeds("feeds.toml")
    print(f"[{ts()}] {len(all_feeds)} feeds total", flush=True)

    state = load_state()
    if Path("feeds.toml").is_file():
        removed = prune_feeds(state, collect_feed_urls("feeds.toml"))
        if removed:
            print(
                f"[{ts()}] Pruned {removed} feed state entry(ies) not in feeds.toml",
                flush=True,
            )
    repaired = _repair_pending_indexes(feeds_dir)
    if repaired:
        print(f"[{ts()}] Repaired {repaired} missing feed index record(s)", flush=True)
    due_feeds = get_due_feeds(all_feeds, state, fetch_interval, max_failures)
    if max_feeds > 0:
        due_feeds = due_feeds[:max_feeds]
    print(f"[{ts()}] Due: {len(due_feeds)}", flush=True)

    if not due_feeds:
        # get_due_feeds() may have auto-disabled or re-enabled feeds.
        state["last_fetch"] = _now_iso()
        save_state(state)
        if cache_missing:
            save_articles([])
        print(f"[{ts()}] No feeds to fetch", flush=True)
        return

    results = fetch_all_feeds(
        due_feeds,
        timeout=fetch_cfg.get("timeout_seconds", 15),
        keep_days=keep_days,
        user_agent=user_agent,
        skip_titles=skip_titles,
        max_workers=max_workers,
        feeds_state={} if force_refresh else state.get("feeds", {}),
        max_entries_per_feed=max_entries,
    )

    aggregator_feeds = 0
    ok_count = 0
    not_modified_count = 0
    for r in results:
        url = (r.get("feed") or {}).get("xml_url", "")
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
        else:
            mark_failed(state, url, r.get("error", "unknown"))

    if aggregator_feeds:
        print(f"[{ts()}] Aggregator feeds: {aggregator_feeds}", flush=True)

    if ok_count == 0 and not_modified_count == 0:
        save_state(state)
        raise RuntimeError(
            f"All {len(due_feeds)} due feeds failed to fetch; runtime is unhealthy"
        )

    fetched_at = _now_iso()
    archive_root = _archive_root(archive_dir)

    # Existing metadata rows are used for dedup so a cache miss can reattach an
    # archive reference to a pending record without creating a duplicate.
    existing_by_key: dict[str, dict] = {}
    for article in load_articles():
        for key in article_keys(article):
            existing_by_key.setdefault(key, article)

    run_links: set[str] = set()
    run_keys: set[str] = set()
    index_cache: dict[str, tuple[dict, set[str]]] = {}
    metadata_entries: list[dict] = []
    feed_records: list[tuple[dict, dict]] = []
    archived_count = 0

    for r in results:
        if r.get("status") != "ok":
            continue
        feed_meta = _feed_meta_for_result(r)
        feed_key = feed_meta["feed_id"]
        if feed_key not in index_cache:
            index_cache[feed_key] = _load_feed_index_keys(feeds_dir, feed_meta)
        feed_meta, index_keys = index_cache[feed_key]

        items: list[tuple[dict, str]] = []
        queued: list[dict] = []
        for entry in r.get("entries") or []:
            link = (entry.get("link") or "").strip()
            if link:
                entry = dict(entry, link=link)
            keys = article_keys(entry)
            if not keys:
                continue
            canonical = canonical_url(link) or link
            if any(key in run_keys for key in keys):
                continue
            if any(key in index_keys for key in keys):
                continue
            if canonical and canonical in run_links:
                continue

            existing = next(
                (existing_by_key[key] for key in keys if key in existing_by_key),
                None,
            )
            existing_ref = article_archive_ref(existing) if existing else None
            if existing is not None and (
                _archive_ref_exists(existing_ref, archive_root)
                or existing.get("status") in _STATUS_DONE
            ):
                # Already archived/finished; mark it seen so another feed in
                # this run cannot archive the same canonical link again.
                if canonical:
                    run_links.add(canonical)
                run_keys.update(article_keys(existing))
                continue

            body = sanitize_html(
                entry.get("content") or "",
                base_url=entry.get("link") or feed_meta.get("site_url") or "",
            )
            meta = _archive_article_meta(entry, feed_meta, fetched_at)
            items.append((meta, body))
            queued.append(entry)
            # Reserve the keys before writing so a later feed in the same run
            # cannot queue a duplicate.
            run_keys.update(keys)
            if canonical:
                run_links.add(canonical)

        if not items:
            continue

        try:
            records = md_archive.append_articles(str(archive_root), items)
        except Exception as exc:  # noqa: BLE001 - fail the step, lose nothing
            raise RuntimeError(
                f"failed to append {len(items)} article(s) to {archive_root}: {exc}"
            ) from exc
        if len(records) != len(items):
            raise RuntimeError(
                f"archive append returned {len(records)} record(s) "
                f"for {len(items)} article(s)"
            )

        for entry, record in zip(queued, records):
            metadata_entries.append(
                _article_metadata_record(entry, feed_meta, record)
            )
            feed_records.append((feed_meta, record))
            archived_count += 1

    if metadata_entries:
        # Persist metadata before the derived feed indexes. If index appends
        # fail afterwards, the article is still pending/classifiable and the
        # index can be rebuilt from the archive; the reverse order could leave
        # indexed records that no future fetch can attach to metadata.
        added, refreshed = store_fetched_entries(metadata_entries)
        for feed_meta, record in feed_records:
            try:
                md_archive.append_feed_record(feeds_dir, feed_meta, record)
            except Exception as exc:  # noqa: BLE001 - fail the step
                raise RuntimeError(
                    f"failed to append feed index record for {feed_meta['feed_id']}: {exc}"
                ) from exc

        # Mark rows indexed only after every feed-index append succeeded. If an
        # append failed above, the persisted ``indexed=false`` rows are repaired
        # from the archive on the next fetch.
        articles = load_articles()
        by_key: dict[str, dict] = {}
        for article in articles:
            for key in article_keys(article):
                by_key.setdefault(key, article)
        for entry in metadata_entries:
            row = next(
                (by_key[key] for key in article_keys(entry) if key in by_key),
                None,
            )
            if row is not None:
                row["indexed"] = True
        save_articles(articles)
        print(
            f"[{ts()}] Archived {archived_count} article(s); "
            f"merged fetch: {added} new, {refreshed} archive refs refreshed",
            flush=True,
        )
    elif cache_missing:
        # Create the file even if the full refresh found nothing new.
        save_articles([])

    # Only a run with at least one successful/304 response counts as a fresh
    # fetch for the runtime contract.
    state["last_fetch"] = _now_iso()
    save_state(state)


def step_classify():
    """Classify cached articles, write classified results to output (immutable)."""
    config = load_config()
    ai_cfg = config.get("ai", {})
    filter_cfg = config.get("filter", {})
    limits = config.get("limits", {})
    categories = [c["name"] for c in config.get("category", [])]
    data_dir, archive_dir, feeds_dir = _storage_paths(config)
    max_articles = limits.get("max_articles", 0)
    keep_days = config.get("fetch", {}).get("keep_days", 14)
    global_skip_prompt = (filter_cfg.get("skip_prompt") or "").strip()
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)

    if validate_articles_file() == "corrupt":
        raise RuntimeError(
            "article_state.json is corrupt; restore it from the data branch before classifying"
        )

    archive_root = _archive_root(archive_dir)
    repaired = _repair_pending_indexes(feeds_dir)
    if repaired:
        print(f"[{ts()}] Repaired {repaired} missing feed index record(s)", flush=True)

    feeds = parse_feeds("feeds.toml")
    site_skip_prompt_rules = _build_site_skip_prompt_rules(feeds)

    # Load from the article state. Finished rows never need output/ to exist;
    # output/ is derived and is deliberately not used as a dedup authority.
    cached = load_articles()
    articles = []
    for a in cached:
        link = (a.get("link") or "").strip()
        if not link:
            # No URL means it can never be rendered or deduped; retire it
            # instead of keeping it pending forever.
            a["status"] = "skipped"
            a["skip_reason"] = "missing_link"
            clear_content(a)
            continue
        status = a.get("status") or "pending"
        # Finished rows are already archived/rendered; output/ may be a cache-
        # only human summary and can be absent after a cold start.
        if status in _STATUS_DONE:
            continue
        # Do not spend AI tokens on stale backlog. cleanup_articles() removes
        # these after classify; filtering here keeps the first runs after a
        # long outage focused on fresh content.
        effective = _parse_dt(a.get("published")) or _parse_dt(a.get("cached_at"))
        if effective is None or effective < cutoff:
            continue
        articles.append(a)

    articles.sort(key=_cached_sort_key)
    if max_articles and max_articles > 0:
        articles = articles[:max_articles]

    # Drop stale classifications only for articles actually selected this run,
    # so the max_articles cap cannot strip results from the rest of the cache.
    for a in articles:
        a.pop("classification", None)
        a.pop("classification_meta", None)

    # Hydrate bodies from the durable archive. A missing/corrupt record is a
    # hard archive problem: keep the metadata row pending (never delete it) and
    # retry on the next run instead of sending an empty body to the model.
    hydrated: list[dict] = []
    for a in articles:
        ref = article_archive_ref(a)
        if ref is None:
            # Legacy metadata-only row: classify the title now rather than
            # keeping it pending forever. Cleanup still owns its expiry.
            clear_content(a)
            set_content_hydrated(a, "")
            hydrated.append(a)
            continue
        path = _archive_record_path(archive_root, ref["path"])
        try:
            record = md_archive.read_record(path, ref["offset"], ref["length"])
        except (OSError, ValueError) as exc:
            clear_content(a)
            a["status"] = "pending"
            a["archive_error"] = str(exc)
            a["archive_checked_at"] = _now_iso()
            print(
                f"[{ts()}] WARNING: keeping article pending; archive record "
                f"unreadable ({path}@{ref['offset']}): {exc}",
                flush=True,
            )
            continue
        a.pop("archive_error", None)
        a.pop("archive_checked_at", None)
        set_content_hydrated(a, record.get("body") or "")
        hydrated.append(a)
    articles = hydrated
    print(
        f"[{ts()}] {len(articles)} articles to classify (hydrated from archive)",
        flush=True,
    )

    if not articles:
        print(f"[{ts()}] Nothing to classify", flush=True)
        save_articles(cached)
        # If pending rows remain (e.g. stale backlog waiting for cleanup),
        # do not refresh last_classify: that would hide a stalled pipeline.
        pending_left = any(
            (a.get("status") or "pending") not in _STATUS_DONE for a in cached
        )
        if not pending_left:
            state = load_state()
            state["last_classify"] = _now_iso()
            save_state(state)
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
    total_unclassified = 0

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
        now = _now_iso()
        to_write = []
        for a in arts:
            cls = a.get("classification")
            if cls:
                a["classified_at"] = now
                if is_skip_category(cls.get("category", "")):
                    a["status"] = "skipped"
                    clear_content(a)
                    continue
                if not is_safe_http_url(a.get("link", "")):
                    a["status"] = "skipped"
                    a["skip_reason"] = "unsafe_link"
                    clear_content(a)
                    continue
                a["status"] = "classified"
                clear_content(a)
                to_write.append(a)
            else:
                # AI unavailable or the model did not return a usable result.
                # Never drop the article: publish the title now with empty
                # category/summary/score instead of retrying forever.
                a["attempts"] = int(a.get("attempts") or 0) + 1
                a["status"] = "unclassified"
                a["classified_at"] = now
                a["unclassified_at"] = now
                a["unclassified_reason"] = (
                    "provider_unavailable"
                    if stats.provider_failed
                    else "classification_failed"
                )
                clear_content(a)
                to_write.append(a)

        # Append annotations BEFORE committing "classified" status. If an
        # annotation cannot be written, the row goes back to pending and is
        # retried next run instead of becoming permanently classified without
        # a summary in the renderer.
        writable = []
        annotation_errors = 0
        for a in to_write:
            if a.get("status") != "classified":
                writable.append(a)
                continue
            try:
                md_archive.append_feed_annotation(
                    feeds_dir,
                    _feed_meta_for_article(a),
                    _annotation_for_article(a),
                )
            except (OSError, ValueError) as exc:
                annotation_errors += 1
                a["status"] = "pending"
                a["attempts"] = int(a.get("attempts") or 0) + 1
                a["annotation_error"] = str(exc)
                if annotation_errors == 1:
                    print(
                        f"[{ts()}] WARNING: could not append feed "
                        f"annotation: {exc}",
                        flush=True,
                    )
                continue
            a.pop("annotation_error", None)
            writable.append(a)

        # Write output before persisting statuses. If this fails, the rows stay
        # pending and the whole group is retried; annotations are append-only
        # and "latest wins", so a retry cannot lose the classification.
        if writable:
            save_daily_results(
                {"articles": writable},
                data_dir,
                last_fetched=now,
                last_classified=now,
                keep_days=0,
            )
        save_articles(cached)

        total_classified += sum(
            1 for a in writable if a.get("status") == "classified"
        )
        total_unclassified += sum(
            1 for a in writable if a.get("status") == "unclassified"
        )
        total_skipped += sum(1 for a in arts if a.get("status") == "skipped")
        if annotation_errors:
            print(
                f"[{ts()}] WARNING: {annotation_errors} feed annotation(s) "
                f"were not written; those articles stay pending",
                flush=True,
            )

    # Always regenerate the index, even when nothing was written.
    _update_index(
        data_dir,
        last_fetched=_now_iso(),
        last_classified=_last_classified_at(cached),
    )

    if total_classified or total_skipped or total_unclassified:
        print(
            f"[{ts()}] Wrote {total_classified} classified, "
            f"{total_unclassified} title-only, {total_skipped} skipped",
            flush=True,
        )
        state = load_state()
        state["last_classify"] = _now_iso()
        state["last_unclassified"] = total_unclassified
        save_state(state)

    if total_unclassified:
        print(
            f"::warning::{total_unclassified} articles were written without AI "
            f"classification (title only)",
            flush=True,
        )


def step_cleanup():
    """Remove expired articles cache entries."""
    config = load_config()
    keep_days = config.get("fetch", {}).get("keep_days", 14)
    if validate_articles_file() == "corrupt":
        raise RuntimeError(
            "article_state.json is corrupt; restore it from the data branch before cleanup"
        )
    removed = cleanup_articles(keep_days=keep_days)
    print(f"[{ts()}] Cleaned up {removed} old cache entries", flush=True)


def step_report():
    """Build runtime status, write status.json + output/status.md, print summary."""
    from src.report import build_status, print_summary, write_status

    config = load_config()
    data_dir, archive_dir, feeds_dir = _storage_paths(config)
    status = build_status(data_dir=data_dir)
    if not status["output"]["index_exists"]:
        # The site index must exist even before the first classified article.
        _update_index(
            data_dir,
            last_fetched=status["last_fetch"],
            last_classified=status["last_classify"],
        )
        status = build_status(data_dir=data_dir)
        # Record the repair so --check does not silently hide the condition.
        status.setdefault("warnings", []).insert(
            0, "output/index.md was missing and has been regenerated"
        )
    write_status(status, root="status.json", data_dir=data_dir)
    summary = print_summary(status)

    archive_root = _archive_root(archive_dir)
    daily_files = (
        len(list(archive_root.rglob("*.md"))) if archive_root.exists() else 0
    )
    feed_indexes = (
        len(list(Path(feeds_dir).glob("*.jsonl"))) if Path(feeds_dir).exists() else 0
    )
    print(
        f"[{ts()}] Archive: {daily_files} daily file(s), "
        f"{feed_indexes} feed index(es)",
        flush=True,
    )

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
    data_dir, _archive_dir, _feeds_dir = _storage_paths(config)

    # Always rebuild from the current runtime files. Reading a stale
    # status.json could make --check pass after the data changed.
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
