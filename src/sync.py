"""Sync external feed lists into feeds.toml."""
import json
from pathlib import Path
from urllib.parse import urlsplit

import requests

from src.opml_parser import parse_opml_bytes
from src.urls import canonical_url, is_safe_public_http_url

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


class SyncSourceError(RuntimeError):
    """A remote feed-list source could not be fetched or parsed."""


def _source_label(source: dict) -> str:
    name = str(source.get("name") or "").strip()
    url = str(source.get("url") or "").strip()
    return name or url or "source"


def _looks_like_opml(content: bytes, url_path: str) -> bool:
    """Decide OPML vs TOML from the URL suffix, then from the content."""
    path = (url_path or "").lower()
    if path.endswith((".opml", ".xml")):
        return True
    if path.endswith(".toml"):
        return False
    head = content[:2000]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    head = head.lstrip()
    return head.startswith(b"<") or b"<opml" in head.lower()


def _parse_toml_feeds(content: bytes) -> list[dict]:
    try:
        data = tomllib.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SyncSourceError(f"invalid TOML feed list: {exc}") from exc
    if not isinstance(data, dict):
        raise SyncSourceError("invalid TOML feed list: top level is not a table")

    feeds: list[dict] = []
    for cat in data.get("category", []) or []:
        if not isinstance(cat, dict):
            continue
        for feed in cat.get("feed", []) or []:
            if not isinstance(feed, dict):
                continue
            feeds.append(
                {
                    "title": feed.get("title", ""),
                    "url": feed.get("url", ""),
                    "site": feed.get("site", ""),
                    "source_category": cat.get("name", ""),
                }
            )
    return feeds


def parse_remote(source: dict, user_agent: str = "rss_aggr/1.0") -> list[dict]:
    """Fetch and parse a remote feed list (OPML or TOML).

    Raises :class:`SyncSourceError` when the source URL is not a public
    http(s) URL, the request fails, the payload cannot be parsed, or the
    payload contains no feeds. It never silently returns an empty list.
    """
    url = str(source.get("url") or "").strip()
    label = _source_label(source)
    if not is_safe_public_http_url(url):
        raise SyncSourceError(
            f"{label}: invalid or non-public source URL: {url!r}"
        )

    print(f"  Fetching {label}... ", end="", flush=True)
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": user_agent})
        resp.raise_for_status()
    except requests.RequestException as exc:
        print("FAILED")
        raise SyncSourceError(f"{label}: fetch failed: {exc}") from exc

    content = resp.content
    try:
        if _looks_like_opml(content, urlsplit(url).path):
            feeds = parse_opml_bytes(content)
        else:
            feeds = _parse_toml_feeds(content)
    except SyncSourceError:
        print("FAILED")
        raise
    except Exception as exc:  # XML/TOML parser errors are source-specific
        print("FAILED")
        raise SyncSourceError(f"{label}: cannot parse feed list: {exc}") from exc

    if not feeds:
        print("FAILED")
        raise SyncSourceError(f"{label}: source contained no feeds")

    print(f"{len(feeds)} feeds")
    return feeds


def load_feeds_toml(path: Path) -> dict:
    """Load existing feeds.toml."""
    if not path.exists():
        return {"category": []}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _toml_string(value: str) -> str:
    """Encode a Python string as a TOML basic string.

    JSON string escaping is compatible with TOML basic strings for the
    characters we care about (quotes, backslashes, newlines, control chars).
    TOML also forbids DEL (U+007F), which JSON leaves unescaped.
    """
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_value(value) -> str | None:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        encoded = [_toml_value(item) for item in value]
        if any(item is None for item in encoded):
            return None
        return "[" + ", ".join(item for item in encoded if item is not None) + "]"
    return None


def _write_mapping(lines: list[str], mapping: dict, skip: set[str] | None = None):
    for key, value in mapping.items():
        if skip and key in skip:
            continue
        encoded = _toml_value(value)
        if encoded is None:
            raise ValueError(
                f"unsupported TOML value for {key!r}: {type(value).__name__}"
            )
        lines.append(f"{key} = {encoded}")


def dedupe_feeds(data: dict) -> int:
    """Remove canonical duplicate feed entries in place.

    An active entry wins over a ``skip = true`` duplicate; otherwise the first
    occurrence wins. Returns the number of removed entries.
    """
    seen: dict[str, tuple[list[dict], int]] = {}
    removed = 0
    for cat in data.get("category", []):
        kept: list[dict] = []
        for feed in cat.get("feed", []):
            url = (feed.get("url") or "").strip()
            key = canonical_url(url) or url
            if key and key in seen:
                previous, index = seen[key]
                if previous[index].get("skip") and not feed.get("skip"):
                    previous[index] = feed
                removed += 1
                continue
            if key:
                seen[key] = (kept, len(kept))
            kept.append(feed)
        if "feed" in cat:
            cat["feed"] = kept
    return removed


def save_feeds_toml(data: dict, path: Path):
    """Save feeds.toml in human-readable format with escaped values."""
    lines = []
    for cat in data.get("category", []):
        lines.append("[[category]]")
        lines.append(f"name = {_toml_string(cat.get('name', ''))}")
        _write_mapping(lines, cat, skip={"name", "feed"})
        lines.append("")
        for feed in cat.get("feed", []):
            lines.append("[[category.feed]]")
            _write_mapping(lines, feed)
            lines.append("")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


def sync(
    sources_path: str = "sources.toml",
    feeds_path: str = "feeds.toml",
    user_agent: str = "rss_aggr/1.0",
) -> dict:
    """Fetch external sources, merge into feeds.toml and return stats.

    Source failures are isolated: the remaining sources are still processed
    and feeds.toml is still written for the sources that succeeded.

    Returns a dict with ``new``, ``skipped``, ``removed``, ``total``,
    ``errors`` (source-level failures; fatal for the sync step) and
    ``warnings`` (individual invalid/empty entries that were skipped).
    """
    with open(sources_path, "rb") as f:
        config = tomllib.load(f)

    sources = config.get("source", [])
    existing = load_feeds_toml(Path(feeds_path))
    errors: list[str] = []
    warnings: list[str] = []
    stats = {
        "new": 0,
        "skipped": 0,
        "removed": 0,
        "total": 0,
        "errors": errors,
        "warnings": warnings,
    }

    existing_feeds_by_url: dict[str, dict] = {}
    for cat in existing.get("category", []):
        for feed in cat.get("feed", []):
            url = (feed.get("url") or "").strip()
            if url:
                key = canonical_url(url) or url
                existing_feeds_by_url.setdefault(key, feed)
    stats["total"] = len(existing_feeds_by_url)

    if not sources:
        print("No sources configured")
        return stats

    print(f"[sync] {len(sources)} sources to sync")

    new_by_cat: dict[str, list[str]] = {}

    for source in sources:
        label = _source_label(source)
        try:
            feeds = parse_remote(source, user_agent)
        except Exception as exc:  # keep syncing the remaining sources
            message = str(exc) or f"{label}: unknown error"
            if not message.startswith(label):
                message = f"{label}: {message}"
            errors.append(message)
            print(f"  {message}")
            continue

        target_cat = source.get("category", "Imported")
        for f in feeds:
            url = (f.get("url") or "").strip()
            if not url:
                warnings.append(f"{label}: skipped feed with no URL")
                continue
            if not is_safe_public_http_url(url):
                warnings.append(
                    f"{label}: skipped invalid or non-public feed URL: {url!r}"
                )
                continue

            site = (f.get("site") or "").strip()
            if site and not is_safe_public_http_url(site):
                warnings.append(
                    f"{label}: dropped invalid or non-public site URL "
                    f"for {url!r}: {site!r}"
                )
                site = ""

            key = canonical_url(url) or url
            if key in existing_feeds_by_url:
                stats["skipped"] += 1
                continue
            existing_feeds_by_url[key] = {
                "title": f.get("title", ""),
                "url": url,
                "site": site,
                "priority": source.get("priority", 10),
            }
            new_by_cat.setdefault(target_cat, []).append(key)
            stats["new"] += 1

    for cat_name, new_keys in new_by_cat.items():
        for key in new_keys:
            feed = existing_feeds_by_url[key]
            found = False
            for cat in existing.get("category", []):
                if cat["name"] == cat_name:
                    cat.setdefault("feed", []).append(feed)
                    found = True
                    break
            if not found:
                existing.setdefault("category", []).append({
                    "name": cat_name,
                    "feed": [feed],
                })

    stats["removed"] = dedupe_feeds(existing)
    stats["total"] = sum(
        len(cat.get("feed", [])) for cat in existing.get("category", [])
    )
    save_feeds_toml(existing, Path(feeds_path))
    print(
        f"\n[sync] Done: +{stats['new']} new, {stats['skipped']} skipped (dup), "
        f"{stats['removed']} removed (canonical dup), "
        f"{stats['total']} total, {len(errors)} errors, "
        f"{len(warnings)} warnings"
    )
    return stats


if __name__ == "__main__":
    sync()
