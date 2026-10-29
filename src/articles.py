"""Article cache: stores fetched articles pending classification."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.urls import canonical_url

ARTICLES_FILE = "articles.json"

# Article lifecycle statuses.
STATUS_PENDING = "pending"
STATUS_CLASSIFIED = "classified"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_UNCLASSIFIED = "unclassified"

# Legacy aliases kept for callers that import the constants directly.
PENDING = STATUS_PENDING
CLASSIFIED = STATUS_CLASSIFIED
SKIPPED = STATUS_SKIPPED
FAILED = STATUS_FAILED
UNCLASSIFIED = STATUS_UNCLASSIFIED

_DONE_STATUSES = {STATUS_CLASSIFIED, STATUS_SKIPPED, STATUS_UNCLASSIFIED}


def _parse_dt(value) -> datetime | None:
    """Parse an ISO timestamp (with ``Z`` support) into an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _article_key(article: dict) -> str:
    """Dedup key for an article: canonical link, then link, then guid."""
    link = (article.get("link") or "").strip()
    if link:
        return canonical_url(link) or link
    guid = article.get("guid") or ""
    return str(guid)


def validate_articles_file(path: str = ARTICLES_FILE) -> str:
    """Return ``missing``, ``ok`` or ``corrupt`` without mutating the file."""
    p = Path(path)
    if not p.exists():
        return "missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return "corrupt"
    return "ok" if isinstance(data, list) else "corrupt"


def load_articles(path: str = ARTICLES_FILE) -> list[dict]:
    """Load cached articles from JSON file."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_articles(articles: list[dict], path: str = ARTICLES_FILE) -> None:
    """Save articles to JSON file (atomic write, parent dir created)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def append_to_articles(new_articles: list[dict], path: str = ARTICLES_FILE) -> int:
    """Append new articles to cache, dedup by canonical link. Returns count added."""
    existing = load_articles(path)
    existing_keys = {_article_key(a) for a in existing if _article_key(a)}

    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for a in new_articles:
        key = _article_key(a)
        if not key or key in existing_keys:
            continue
        content = a.get("content") or ""
        entry = {**a}
        entry["canonical"] = canonical_url(a.get("link", "")) or a.get("link", "") or key
        entry["status"] = STATUS_PENDING
        entry["attempts"] = 0
        entry["cached_at"] = a.get("cached_at") or now
        entry["content_length"] = len(content)
        existing.append(entry)
        existing_keys.add(key)
        added += 1

    if added:
        save_articles(existing, path)
    return added


def merge_fetched_entries(entries: list[dict], path: str = ARTICLES_FILE) -> tuple[int, int]:
    """Merge fetched entries into the cache.

    New links are appended. Existing pending/failed records whose body was
    lost (for example after a cache miss) get their ``content`` reattached
    from the feed instead of being dropped. Finished records are never
    modified. Returns ``(added, refreshed)``.
    """
    existing = load_articles(path)
    by_key = {_article_key(a): a for a in existing if _article_key(a)}
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    refreshed = 0

    for a in entries:
        key = _article_key(a)
        if not key:
            continue
        current = by_key.get(key)
        if current is None:
            content = a.get("content") or ""
            entry = {**a}
            entry["canonical"] = canonical_url(a.get("link", "")) or a.get("link", "") or key
            entry["status"] = STATUS_PENDING
            entry["attempts"] = 0
            entry["cached_at"] = a.get("cached_at") or now
            entry["content_length"] = len(content)
            existing.append(entry)
            by_key[key] = entry
            added += 1
            continue

        status = current.get("status") or STATUS_PENDING
        if status in _DONE_STATUSES:
            continue

        incoming = a.get("content") or ""
        if not (current.get("content") or "").strip() and incoming.strip():
            current["content"] = incoming
            current["content_length"] = len(incoming)
            current["content_refreshed_at"] = now
            refreshed += 1
        for field in ("title", "author", "published", "feed_title", "feed_category", "canonical"):
            if not current.get(field) and a.get(field):
                current[field] = a[field]

    if added or refreshed:
        save_articles(existing, path)
    return added, refreshed


def get_pending_articles(limit: int = 0, path: str = ARTICLES_FILE) -> list[dict]:
    """Return not-yet-classified articles ordered by cached_at (oldest first)."""
    pending = [
        a for a in load_articles(path)
        if (a.get("status") or STATUS_PENDING) not in _DONE_STATUSES
    ]
    pending.sort(key=lambda a: _parse_dt(a.get("cached_at")) or datetime.min.replace(tzinfo=timezone.utc))
    if limit and limit > 0:
        pending = pending[:limit]
    return pending


def cleanup_articles(keep_days: int = 14, path: str = ARTICLES_FILE) -> int:
    """Remove expired cache entries.

    Classified/skipped entries are removed after one day; everything else
    (pending/failed) after ``keep_days``. ``published`` is used when valid,
    falling back to ``cached_at``. Entries with no usable date are expired.
    """
    articles = load_articles(path)
    if not articles:
        return 0

    now = datetime.now(timezone.utc)
    done_cutoff = now - timedelta(days=1)
    pending_cutoff = now - timedelta(days=keep_days)

    kept = []
    removed = 0
    for a in articles:
        status = a.get("status") or STATUS_PENDING
        if status in _DONE_STATUSES:
            # A recent classification of an old article must not be deleted
            # just because its publish date is outside the done window.
            cutoff = done_cutoff
            dt = (
                _parse_dt(a.get("classified_at"))
                or _parse_dt(a.get("cached_at"))
                or _parse_dt(a.get("published"))
            )
        else:
            cutoff = pending_cutoff
            dt = _parse_dt(a.get("published")) or _parse_dt(a.get("cached_at"))
        if dt is None or dt < cutoff:
            removed += 1
            continue
        kept.append(a)

    if removed:
        save_articles(kept, path)
    return removed


def get_article_links(path: str = ARTICLES_FILE) -> set[str]:
    """Get canonical links currently in the articles cache."""
    links = set()
    for a in load_articles(path):
        link = a.get("link") or ""
        key = canonical_url(link) or a.get("canonical") or link
        if key:
            links.add(key)
    return links


def summarize_articles(path: str = ARTICLES_FILE) -> dict:
    """Aggregate article-cache counters for the status report."""
    articles = load_articles(path)
    summary = {
        "total": 0,
        "pending": 0,
        "classified": 0,
        "skipped": 0,
        "unclassified": 0,
        "failed": 0,
        "invalid_dates": 0,
        "content_bytes": 0,
    }
    for a in articles:
        summary["total"] += 1
        status = a.get("status") or STATUS_PENDING
        if status in summary:
            summary[status] += 1
        else:
            summary["pending"] += 1

        if _parse_dt(a.get("published")) is None:
            summary["invalid_dates"] += 1

        content_length = a.get("content_length")
        if isinstance(content_length, int):
            summary["content_bytes"] += content_length
        else:
            summary["content_bytes"] += len((a.get("content") or "").encode("utf-8"))
    return summary


# Legacy aliases for backward compatibility
load_cache = load_articles
save_cache = save_articles
append_to_cache = append_to_articles
cleanup_cache = cleanup_articles
get_cached_links = get_article_links
