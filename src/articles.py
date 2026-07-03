"""Article cache: stores fetched articles pending classification (14-day window)."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path


ARTICLES_FILE = "articles.json"


def load_articles(path: str = ARTICLES_FILE) -> list[dict]:
    """Load cached articles from JSON file."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_articles(articles: list[dict], path: str = ARTICLES_FILE) -> None:
    """Save articles to JSON file (atomic write)."""
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(p)


def append_to_articles(new_articles: list[dict], path: str = ARTICLES_FILE) -> int:
    """Append new articles to cache, dedup by link. Returns number added."""
    existing = load_articles(path)
    existing_links = {a["link"] for a in existing if a.get("link")}

    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for a in new_articles:
        link = a.get("link", "")
        if not link or link in existing_links:
            continue
        # Calculate content length for dynamic batching
        content = a.get("content") or ""
        entry = {**a, "cached_at": now, "content_length": len(content)}
        existing.append(entry)
        existing_links.add(link)
        added += 1

    if added:
        save_articles(existing, path)
    return added


def cleanup_articles(keep_days: int = 14, path: str = ARTICLES_FILE) -> int:
    """Remove entries older than keep_days (by published date). Returns count removed."""
    articles = load_articles(path)
    if not articles:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    kept = []
    removed = 0
    for a in articles:
        pub = a.get("published", "")
        if pub:
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if dt < cutoff:
                    removed += 1
                    continue
            except ValueError:
                pass
        kept.append(a)

    if removed:
        save_articles(kept, path)
    return removed


def get_article_links(path: str = ARTICLES_FILE) -> set[str]:
    """Get all links currently in articles cache."""
    return {a["link"] for a in load_articles(path) if a.get("link")}


# Legacy aliases for backward compatibility
load_cache = load_articles
save_cache = save_articles
append_to_cache = append_to_articles
cleanup_cache = cleanup_articles
get_cached_links = get_article_links
