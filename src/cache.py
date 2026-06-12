"""Single-file JSON cache for fetched articles (14-day window)."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path


def load_cache(path: str = "cache.json") -> list[dict]:
    """Load cached articles from JSON file."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_cache(articles: list[dict], path: str = "cache.json") -> None:
    """Save articles to cache JSON file (overwrite)."""
    Path(path).write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")


def append_to_cache(new_articles: list[dict], path: str = "cache.json") -> int:
    """Append new articles to cache, dedup by link. Returns number added."""
    existing = load_cache(path)
    existing_links = {a["link"] for a in existing if a.get("link")}

    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for a in new_articles:
        link = a.get("link", "")
        if not link or link in existing_links:
            continue
        entry = {**a, "cached_at": now}
        existing.append(entry)
        existing_links.add(link)
        added += 1

    if added:
        save_cache(existing, path)
    return added


def cleanup_cache(keep_days: int = 14, path: str = "cache.json") -> int:
    """Remove entries older than keep_days (by published date). Returns count removed."""
    articles = load_cache(path)
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
        save_cache(kept, path)
    return removed


def get_cached_links(path: str = "cache.json") -> set[str]:
    """Get all links currently in cache."""
    return {a["link"] for a in load_cache(path) if a.get("link")}
