"""Article processing state: metadata-only records pending classification.

Raw article bodies live in the durable Markdown archive (see
:mod:`src.md_archive`). ``article_state.json`` only keeps metadata plus the
archive offset reference; ``content`` is hydrated into memory for
classification and removed again before the file is written.

The file is a versioned object::

    {"v": 1, "articles": [ ... ]}

Legacy ``articles.json`` files that contain a bare JSON list are still read
(and are used as a fallback when ``article_state.json`` does not exist), but
they are never written by :func:`save_articles`.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.urls import canonical_url

# New processing-state file. ``ARTICLES_FILE`` stays as a compatibility alias
# for callers that imported the old constant.
ARTICLE_STATE_FILE = "article_state.json"
LEGACY_ARTICLE_STATE_FILE = "articles.json"
ARTICLE_STATE_VERSION = 1

ARTICLES_FILE = ARTICLE_STATE_FILE

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

# Fields copied from an md_archive index record into the state file.
ARCHIVE_FIELDS = (
    "archive_file",
    "archive_offset",
    "archive_length",
    "archive_body_len",
    "archive_sha256",
)

# Per-feed metadata kept with every article for later annotations.
FEED_FIELDS = ("feed_id", "feed_url", "site_url", "feed_title")

# Metadata fields that may be refreshed from a fetch. Raw bodies are never
# included: they belong to the archive only.
_REFRESHABLE_FIELDS = (
    "title",
    "author",
    "published",
    "feed_title",
    "feed_category",
    "feed_id",
    "feed_url",
    "site_url",
    "canonical",
)

# Body fields that must never be persisted in the state file.
_BODY_FIELDS = ("content", "content_length")

_READ_ERRORS = (json.JSONDecodeError, OSError, UnicodeDecodeError)


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


def article_keys(article: dict) -> list[str]:
    """Return archive/index dedup keys (``link:`` canonical and ``guid:``).

    The prefix format matches :mod:`src.md_archive` so a feed index can be
    deduped against fetched entries and state records without any conversion.
    """
    if not isinstance(article, dict):
        return []
    keys: list[str] = []
    link = (article.get("link") or "").strip()
    if link:
        keys.append("link:" + (canonical_url(link) or link))
    guid = str(article.get("guid") or "").strip()
    if guid:
        keys.append("guid:" + guid)
    return keys


def canonical_article_key(article: dict) -> str:
    """Return the scheme-less canonical link key (``""`` when unusable)."""
    link = (article.get("link") or "").strip() if isinstance(article, dict) else ""
    if not link:
        return ""
    return canonical_url(link) or link


def metadata_only(article: dict) -> dict:
    """Return a copy of ``article`` without raw body fields.

    All other fields (including future flags such as ``indexed``) are kept.
    """
    if not isinstance(article, dict):
        return {}
    return {key: value for key, value in article.items() if key not in _BODY_FIELDS}


def archive_fields(record: dict) -> dict:
    """Map an md_archive index record to state-file archive fields.

    Accepts both the index-record names (``file``/``offset``/``length``/
    ``body_len``/``sha256``) and the already-mapped article field names.
    """
    if not isinstance(record, dict):
        return {}

    def _first(*names):
        for name in names:
            if name in record and record[name] not in (None, ""):
                return record[name]
        return None

    fields: dict = {}
    file = _first("file", "archive_file")
    offset = _first("offset", "archive_offset")
    length = _first("length", "archive_length")
    body_len = _first("body_len", "archive_body_len")
    sha256 = _first("sha256", "body_sha256", "archive_sha256")

    if file:
        fields["archive_file"] = str(file)
    if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
        fields["archive_offset"] = offset
    if isinstance(length, int) and not isinstance(length, bool) and length >= 0:
        fields["archive_length"] = length
    if isinstance(body_len, int) and not isinstance(body_len, bool) and body_len >= 0:
        fields["archive_body_len"] = body_len
    if sha256:
        fields["archive_sha256"] = str(sha256)
    return fields


def article_archive_ref(article: dict) -> dict | None:
    """Return ``{"path", "offset", "length"}`` for an archived article, or None."""
    if not isinstance(article, dict):
        return None
    path = str(article.get("archive_file") or "").strip()
    offset = article.get("archive_offset")
    length = article.get("archive_length")
    if not path:
        return None
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return None
    if length is not None and (
        not isinstance(length, int) or isinstance(length, bool) or length < 0
    ):
        return None
    return {"path": path, "offset": offset, "length": length}


def set_content_hydrated(article: dict, body) -> dict:
    """Attach a hydrated archive body to an in-memory article record."""
    text = body or ""
    if not isinstance(text, str):
        text = str(text)
    article["content"] = text
    article["content_length"] = len(text.encode("utf-8"))
    return article


def clear_content(article: dict) -> dict:
    """Remove hydrated body fields so metadata can be persisted safely."""
    article.pop("content", None)
    article.pop("content_length", None)
    article.pop("content_refreshed_at", None)
    return article


def _legacy_fallback_path(p: Path) -> Path | None:
    """Return the legacy sibling path to try when ``p`` is a state file path."""
    if p.name != ARTICLE_STATE_FILE:
        return None
    legacy = p.with_name(LEGACY_ARTICLE_STATE_FILE)
    return None if legacy == p else legacy


def _read_path(p: Path) -> Path | None:
    """Resolve the file to read: ``p`` if present, else its legacy sibling."""
    if p.exists():
        return p
    legacy = _legacy_fallback_path(p)
    if legacy is not None and legacy.exists():
        return legacy
    return None


def _parse_state(data) -> list[dict] | None:
    """Normalize parsed JSON into a list of articles (``None`` when invalid)."""
    if isinstance(data, list):
        # Legacy bare list from articles.json.
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        articles = data.get("articles")
        if isinstance(articles, list):
            return [item for item in articles if isinstance(item, dict)]
    return None


def validate_articles_file(path: str = ARTICLE_STATE_FILE) -> str:
    """Return ``missing``, ``ok`` or ``corrupt`` without mutating the file."""
    target = _read_path(Path(path))
    if target is None:
        return "missing"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except _READ_ERRORS:
        return "corrupt"
    return "ok" if _parse_state(data) is not None else "corrupt"


def load_articles(path: str = ARTICLE_STATE_FILE) -> list[dict]:
    """Load processing state from JSON.

    Accepts the versioned object (``{"v": 1, "articles": [...]}``), a legacy
    bare list, and falls back to ``articles.json`` next to the state file when
    the state file itself is missing. Returns ``[]`` when nothing is readable.
    """
    target = _read_path(Path(path))
    if target is None:
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except _READ_ERRORS:
        return []
    articles = _parse_state(data)
    return articles if articles is not None else []


def save_articles(articles: list[dict], path: str = ARTICLE_STATE_FILE) -> None:
    """Save metadata to the versioned state file (atomic write, dir created).

    Raw ``content`` is always stripped here as a final guard: even a caller
    that forgot to call :func:`clear_content` cannot leak article bodies.
    Unknown fields (for example a future ``indexed`` flag) are preserved.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = [metadata_only(article) for article in articles if isinstance(article, dict)]
    payload = {"v": ARTICLE_STATE_VERSION, "articles": clean}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def append_to_articles(new_articles: list[dict], path: str = ARTICLE_STATE_FILE) -> int:
    """Append new articles to the state, dedup by canonical link.

    Returns the number of records added.
    """
    existing = load_articles(path)
    existing_keys = {_article_key(a) for a in existing if _article_key(a)}

    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for a in new_articles:
        key = _article_key(a)
        if not key or key in existing_keys:
            continue
        content = a.get("content") or ""
        entry = metadata_only(a)
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


def _apply_archive_ref(current: dict, incoming: dict) -> bool:
    """Copy archive fields from ``incoming``; return True when they changed."""
    changed = False
    for field in ARCHIVE_FIELDS:
        value = incoming.get(field)
        if value is None:
            continue
        if current.get(field) != value:
            current[field] = value
            changed = True
    return changed


def store_fetched_entries(
    entries: list[dict], path: str = ARTICLE_STATE_FILE
) -> tuple[int, int]:
    """Merge archive-backed fetched entries into the state file.

    ``entries`` are built by ``step_fetch`` after the body has been appended to
    the Markdown archive, so they carry archive fields but no raw content.

    - New canonical links/guids are added as ``pending``.
    - Existing pending/failed records are updated in place and gain archive
      refs (used after a cache miss) without being duplicated.
    - Finished records are never reopened or modified.
    - Raw ``content`` is never persisted.

    Returns ``(added, refreshed)`` where ``refreshed`` counts existing records
    that gained an archive reference.
    """
    existing = load_articles(path)
    by_key: dict[str, dict] = {}
    for article in existing:
        for key in article_keys(article):
            by_key.setdefault(key, article)

    now = datetime.now(timezone.utc).isoformat()
    added = 0
    refreshed = 0
    dirty = False

    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        incoming = metadata_only(raw)
        keys = article_keys(incoming)
        if not keys:
            continue

        current = next((by_key[key] for key in keys if key in by_key), None)
        if current is None:
            key = _article_key(incoming)
            entry = dict(incoming)
            entry["canonical"] = (
                canonical_url(incoming.get("link", ""))
                or incoming.get("link", "")
                or key
            )
            entry["status"] = STATUS_PENDING
            entry["attempts"] = 0
            entry["indexed"] = False
            entry["cached_at"] = incoming.get("cached_at") or now
            existing.append(entry)
            for item_key in article_keys(entry):
                by_key[item_key] = entry
            added += 1
            dirty = True
            continue

        status = current.get("status") or STATUS_PENDING
        if status in _DONE_STATUSES:
            # Finished rows are immutable; they were already rendered.
            continue
        changed = False

        # Legacy rows may still carry a body in memory; never keep it.
        if "content" in current or "content_length" in current:
            current.pop("content", None)
            current.pop("content_length", None)
            changed = True

        if _apply_archive_ref(current, incoming):
            changed = True
            current["status"] = STATUS_PENDING
            current["attempts"] = 0
            current["indexed"] = False
            refreshed += 1

        for field in _REFRESHABLE_FIELDS:
            value = incoming.get(field)
            if value in (None, ""):
                continue
            if not current.get(field) or field in ("feed_id", "feed_url", "site_url"):
                if current.get(field) != value:
                    current[field] = value
                    changed = True

        if changed:
            dirty = True
            for item_key in article_keys(current):
                by_key.setdefault(item_key, current)

    if dirty:
        save_articles(existing, path)
    return added, refreshed


def merge_fetched_entries(
    entries: list[dict], path: str = ARTICLE_STATE_FILE
) -> tuple[int, int]:
    """Backward-compatible alias for :func:`store_fetched_entries`.

    Older callers passed raw ``content``; it is discarded here because bodies
    now belong to the durable archive.
    """
    return store_fetched_entries(entries, path)


def get_pending_articles(limit: int = 0, path: str = ARTICLE_STATE_FILE) -> list[dict]:
    """Return not-yet-classified articles ordered by cached_at (oldest first)."""
    pending = [
        a for a in load_articles(path)
        if (a.get("status") or STATUS_PENDING) not in _DONE_STATUSES
    ]
    pending.sort(key=lambda a: _parse_dt(a.get("cached_at")) or datetime.min.replace(tzinfo=timezone.utc))
    if limit and limit > 0:
        pending = pending[:limit]
    return pending


def cleanup_articles(keep_days: int = 14, path: str = ARTICLE_STATE_FILE) -> int:
    """Remove expired state entries.

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


def get_article_links(path: str = ARTICLE_STATE_FILE) -> set[str]:
    """Get canonical links currently in the article state."""
    links = set()
    for a in load_articles(path):
        link = a.get("link") or ""
        key = canonical_url(link) or a.get("canonical") or link
        if key:
            links.add(key)
    return links


def summarize_articles(path: str = ARTICLE_STATE_FILE) -> dict:
    """Aggregate article-state counters for the status report."""
    articles = load_articles(path)
    summary = {
        "total": 0,
        "pending": 0,
        "classified": 0,
        "skipped": 0,
        "unclassified": 0,
        "failed": 0,
        "invalid_dates": 0,
        "archived": 0,
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

        if a.get("archive_file"):
            summary["archived"] += 1

        archive_body_len = a.get("archive_body_len")
        content_length = a.get("content_length")
        if isinstance(archive_body_len, int):
            summary["content_bytes"] += archive_body_len
        elif isinstance(content_length, int):
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
