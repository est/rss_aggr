"""One-time migration of a legacy ``articles.json`` into the Markdown archive.

Old caches stored raw feed HTML in ``content``/``content_length``. This tool
sanitizes each body into Markdown, appends it to the durable daily archive,
creates/extends the per-feed index and rewrites ``articles.json`` as a
metadata-only file carrying archive offsets.

Usage::

    python -m src.backfill --articles articles.json --root . \
        --feeds feeds --out articles.json

The migration is idempotent: entries whose canonical link/guid already exists
in the feed index are not appended again.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from src import md_archive
from src.articles import (
    STATUS_PENDING,
    archive_fields,
    article_keys,
    save_articles,
)
from src.sanitize import sanitize_html


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_legacy(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON array of articles")
    return [item for item in data if isinstance(item, dict)]


def _dedup_entries(entries: list[dict]) -> list[dict]:
    """Keep the first entry for each canonical link/guid pair."""
    seen: set[str] = set()
    unique: list[dict] = []
    for entry in entries:
        keys = article_keys(entry)
        if not keys:
            continue
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        unique.append(entry)
    return unique


def _feed_meta(article: dict) -> dict:
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


def _archive_meta(article: dict, feed_meta: dict, fetched_at: str) -> dict:
    return {
        "guid": article.get("guid") or "",
        "feed_id": feed_meta["feed_id"],
        "feed_url": feed_meta["feed_url"],
        "site_url": feed_meta["site_url"],
        "link": (article.get("link") or "").strip(),
        "title": article.get("title") or "",
        "author": article.get("author") or "",
        "published": article.get("published") or "",
        "fetched_at": fetched_at,
    }


def _index_map(feeds_dir: str | Path, feed_meta: dict) -> dict[str, dict]:
    """Map canonical/guid keys to existing article records for one feed."""
    path = Path(feeds_dir) / f"{feed_meta['feed_id']}.jsonl"
    try:
        _header, records = md_archive.read_feed_index(path)
    except (OSError, ValueError):
        return {}
    index: dict[str, dict] = {}
    for record in records:
        if record.get("kind", "article") != "article":
            continue
        for key in article_keys(record):
            index.setdefault(key, record)
    return index


def _existing_record(article: dict, index: dict[str, dict]) -> dict | None:
    for key in article_keys(article):
        if key in index:
            return index[key]
    return None


def _metadata_record(
    article: dict, record: dict | None, feed_meta: dict, fetched_at: str
) -> dict:
    """Build the metadata-only articles.json row for one migrated article."""
    metadata = {
        key: value
        for key, value in article.items()
        if key not in ("content", "content_length")
    }
    link = (article.get("link") or "").strip()
    metadata["guid"] = article.get("guid") or (record or {}).get("guid") or ""
    metadata["link"] = link
    metadata.setdefault("title", "")
    metadata.setdefault("author", "")
    metadata.setdefault("published", "")
    metadata["feed_id"] = metadata.get("feed_id") or feed_meta["feed_id"]
    metadata["feed_url"] = metadata.get("feed_url") or feed_meta["feed_url"]
    metadata["site_url"] = metadata.get("site_url") or feed_meta["site_url"]
    metadata["feed_title"] = metadata.get("feed_title") or feed_meta["title"]
    metadata["canonical"] = link or metadata["guid"]
    if link:
        from src.urls import canonical_url

        metadata["canonical"] = canonical_url(link) or link
    metadata["status"] = metadata.get("status") or STATUS_PENDING
    metadata["attempts"] = int(metadata.get("attempts") or 0)
    metadata["cached_at"] = metadata.get("cached_at") or fetched_at
    if record:
        metadata.update(archive_fields(record))
    return metadata


def backfill(
    articles_path: str | Path,
    root: str | Path = ".",
    feeds_dir: str | Path = "feeds",
    out: str | Path | None = None,
) -> dict:
    """Migrate a legacy articles.json into the Markdown archive.

    Returns a small counters dict for logging/tests.
    """
    legacy = _load_legacy(articles_path)
    entries = _dedup_entries(legacy)
    fetched_at = _now_iso()

    # Group by feed so one feed header/index is used for all of its articles.
    groups: dict[str, tuple[dict, list[dict]]] = {}
    for article in entries:
        feed_meta = _feed_meta(article)
        group = groups.setdefault(feed_meta["feed_id"], (feed_meta, []))
        group[1].append(article)

    output: list[dict] = []
    stats = {
        "input": len(legacy),
        "entries": len(entries),
        "archived": 0,
        "skipped_existing": 0,
        "feeds": len(groups),
    }

    for feed_id, (feed_meta, group_entries) in groups.items():
        index = _index_map(feeds_dir, feed_meta)
        by_identity: dict[int, dict] = {}
        pending_items: list[tuple[dict, str]] = []
        pending_entries: list[dict] = []

        for article in group_entries:
            existing = _existing_record(article, index)
            if existing is not None:
                by_identity[id(article)] = existing
                stats["skipped_existing"] += 1
                continue
            link = (article.get("link") or "").strip()
            body = sanitize_html(
                article.get("content") or "",
                base_url=link or feed_meta.get("site_url") or "",
            )
            pending_items.append(
                (_archive_meta(article, feed_meta, fetched_at), body)
            )
            pending_entries.append(article)

        if pending_items:
            records = md_archive.append_articles(root, pending_items)
            if len(records) != len(pending_items):
                raise RuntimeError(
                    f"{feed_id}: archive returned {len(records)} record(s) "
                    f"for {len(pending_items)} article(s)"
                )
            for article, record in zip(pending_entries, records):
                md_archive.append_feed_record(feeds_dir, feed_meta, record)
                for key in article_keys(record):
                    index.setdefault(key, record)
                by_identity[id(article)] = record
                stats["archived"] += 1

        for article in group_entries:
            record = by_identity.get(id(article))
            output.append(
                _metadata_record(article, record, feed_meta, fetched_at)
            )

    destination = out if out is not None else articles_path
    save_articles(output, str(destination))
    stats["out"] = str(destination)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate a legacy articles.json into the Markdown archive"
    )
    parser.add_argument(
        "--articles", required=True, help="legacy articles.json to read"
    )
    parser.add_argument(
        "--root", default=".", help="project root (archive files go to ROOT/archive)"
    )
    parser.add_argument(
        "--feeds", default="feeds", help="per-feed index directory"
    )
    parser.add_argument(
        "--out",
        default=None,
        help="write metadata-only cache here (default: overwrite --articles)",
    )
    args = parser.parse_args(argv)

    stats = backfill(
        articles_path=args.articles,
        root=args.root,
        feeds_dir=args.feeds,
        out=args.out,
    )
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
