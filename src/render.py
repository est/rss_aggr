"""Render the Markdown archive as a static site and JSON Feed indexes.

The archive is the source of truth: daily files are rendered to
``<site>/YYYY/MM/DD.html`` and the per-feed JSONL indexes become
``<site>/feeds/<feed_id>.html`` plus ``<site>/feeds/<feed_id>.json``.

All article text is untrusted. Titles, authors, summaries and URLs are
HTML-escaped; article bodies are converted from Markdown and then passed
through bleach with the same allowlist used by the ingest sanitizer.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import bleach
import markdown

from src import md_archive
from src.sanitize import ALLOWED_ATTRIBUTES, ALLOWED_PROTOCOLS, ALLOWED_TAGS

MARKDOWN_EXTENSIONS = ("fenced_code", "tables", "sane_lists")
JSON_FEED_VERSION = "https://jsonfeed.org/version/1.1"

_DAY_FILE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})\.md")
_DAY_IN_PATH_RE = re.compile(r"(?:^|/)(\d{4})/(\d{2})/(\d{2})\.md$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ABSOLUTE_URL_SCHEMES = ("http", "https")
_SCRIPT_BLOCK_RE = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]*>")

CSS = """\
:root {
  color-scheme: light dark;
  --fg: #1b1b1b;
  --muted: #5f6368;
  --bg: #ffffff;
  --panel: #f6f7f9;
  --border: #d9dde3;
  --link: #0b5cad;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fg: #e8e8e8;
    --muted: #a5a5a5;
    --bg: #151515;
    --panel: #1e1e1e;
    --border: #333333;
    --link: #7cb7ff;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0 auto;
  max-width: 54rem;
  padding: 2rem 1.25rem 4rem;
  color: var(--fg);
  background: var(--bg);
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
}
a { color: var(--link); }
h1, h2, h3 { line-height: 1.25; }
h1 { margin-bottom: 0.25rem; }
h2 { margin: 1.75rem 0 0.5rem; }
nav { margin-bottom: 1.5rem; }
.meta, .count { color: var(--muted); font-size: 0.9rem; }
article, .feed-item {
  margin: 0 0 2rem;
  padding: 1rem 1.1rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
}
article h2, .feed-item h2 { margin-top: 0; }
.body { overflow-wrap: anywhere; }
.body pre { overflow-x: auto; padding: 0.75rem; background: var(--bg); }
.body img { max-width: 100%; height: auto; }
.body table { border-collapse: collapse; }
.body th, .body td { border: 1px solid var(--border); padding: 0.3rem 0.5rem; }
ul.plain { padding-left: 1.2rem; }
.summary { overflow-wrap: anywhere; }
.links { font-size: 0.9rem; }
.empty { color: var(--muted); font-style: italic; }
"""


@dataclass
class FeedIndex:
    """One per-feed JSONL index, already split into header and records."""

    feed_id: str
    output_id: str
    header: dict
    records: list[dict]

    @property
    def title(self) -> str:
        return _text(self.header.get("title")).strip() or self.feed_id


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _escape(value) -> str:
    """Escape text content, leaving quotes readable."""
    return html.escape(_text(value), quote=False)


def _attr(value) -> str:
    """Escape a value for a double-quoted HTML attribute."""
    return html.escape(_text(value), quote=True)


def _plain_text(value) -> str:
    """Return plain text for JSON Feed fields (never raw HTML markup)."""
    text = _SCRIPT_BLOCK_RE.sub("", _text(value))
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def _parse_dt(value) -> datetime | None:
    """Parse an ISO timestamp (or epoch number) into an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_url(value) -> str:
    """Return an http(s)/mailto/relative URL, or an empty string if unsafe."""
    url = _text(value).strip()
    if not url:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme and scheme not in ALLOWED_PROTOCOLS:
        return ""
    if not scheme and url.lower().startswith(
        ("javascript:", "data:", "vbscript:", "file:")
    ):
        return ""
    return url


def _is_absolute_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(parts.scheme in _ABSOLUTE_URL_SCHEMES and parts.netloc)


def _day_from_file(value) -> str:
    text = _text(value).replace("\\", "/")
    match = _DAY_IN_PATH_RE.search(text)
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _day_from_record(record: dict) -> str:
    day = _day_from_file(record.get("file"))
    if day:
        return day
    published = _parse_dt(record.get("published"))
    return published.strftime("%Y-%m-%d") if published else ""


def _daily_path(day: str) -> str:
    return f"{day[0:4]}/{day[5:7]}/{day[8:10]}.html"


def _anchor_id(record: dict, fallback: str = "") -> str:
    guid = _text(record.get("guid")).strip()
    if not guid:
        meta = record.get("meta")
        if isinstance(meta, dict):
            guid = _text(meta.get("guid")).strip()
    if guid:
        return guid
    if fallback:
        return fallback
    offset = record.get("offset")
    if isinstance(offset, int) and not isinstance(offset, bool):
        return f"article-{offset}"
    return ""


def _daily_href(record: dict, prefix: str = "") -> str:
    day = _day_from_record(record)
    if not day:
        return ""
    path = _daily_path(day)
    href = f"{prefix}/{path}" if prefix else path
    anchor = _anchor_id(record)
    if anchor:
        href += "#" + quote(anchor, safe="")
    return href


def _sort_key(record: dict) -> tuple:
    published = _parse_dt(record.get("published"))
    offset = record.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool):
        offset = -1
    return (
        1 if published else 0,
        published.timestamp() if published else 0.0,
        _text(record.get("file")),
        offset,
    )


def _article_records(feed: FeedIndex) -> list[dict]:
    records = [
        record
        for record in feed.records
        if (_text(record.get("kind")).strip() or "article") == "article"
    ]
    return sorted(records, key=_sort_key, reverse=True)


def _latest_annotations(records: list[dict]) -> dict[str, dict]:
    """Return the newest annotation per guid (later record wins on a tie)."""
    latest: dict[str, tuple[tuple, dict]] = {}
    for index, record in enumerate(records):
        if _text(record.get("kind")).strip() != "annotation":
            continue
        guid = _text(record.get("guid")).strip()
        if not guid:
            continue
        classified = _parse_dt(record.get("classified_at"))
        key = (
            1 if classified else 0,
            classified.timestamp() if classified else 0.0,
            index,
        )
        previous = latest.get(guid)
        if previous is None or key > previous[0]:
            latest[guid] = (key, record)
    return {guid: record for guid, (_key, record) in latest.items()}


def _slug(value, fallback: str = "feed") -> str:
    text = _SLUG_RE.sub("-", _text(value).strip()).strip("-._")
    return text[:120] or fallback


def _plural(count: int, singular: str) -> str:
    return f"{count} {singular}" + ("" if count == 1 else "s")


def _render_markdown(body: str) -> str:
    """Convert archived Markdown to sanitized HTML."""
    if not body or not body.strip():
        return ""
    converter = markdown.Markdown(
        extensions=list(MARKDOWN_EXTENSIONS), output_format="html"
    )
    rendered = converter.convert(body)
    return bleach.clean(
        rendered,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    ).strip()


def _document(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_escape(title)}</title>\n"
        f"<style>\n{CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body.rstrip()}\n"
        "</body>\n"
        "</html>\n"
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------
def _resolve_archive(path: str | Path) -> Path:
    """Accept an archive directory or a project root that contains one."""
    directory = Path(path)
    if directory.name != "archive":
        nested = directory / "archive"
        if nested.is_dir():
            return nested
    return directory


def _daily_files(archive_dir: Path) -> list[tuple[str, Path]]:
    if not archive_dir.is_dir():
        return []
    files: list[tuple[str, Path]] = []
    for path in sorted(archive_dir.rglob("*.md")):
        try:
            relative = path.relative_to(archive_dir).as_posix()
        except ValueError:
            continue
        match = _DAY_FILE_RE.fullmatch(relative)
        if not match:
            continue
        day = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        files.append((day, path))
    files.sort(key=lambda item: (item[0], item[1].as_posix()))
    return files


def _load_feeds(feeds_dir: Path) -> list[FeedIndex]:
    if not feeds_dir.is_dir():
        return []
    feeds: list[FeedIndex] = []
    used: set[str] = set()
    for path in sorted(feeds_dir.glob("*.jsonl")):
        try:
            header, records = md_archive.read_feed_index(path)
        except (OSError, ValueError):
            continue
        if not header:
            continue
        feed_id = _text(header.get("feed_id")).strip() or path.stem
        output_id = _slug(feed_id, _slug(path.stem, "feed"))
        if output_id in used:
            number = 2
            while f"{output_id}-{number}" in used:
                number += 1
            output_id = f"{output_id}-{number}"
        used.add(output_id)
        feeds.append(FeedIndex(feed_id, output_id, header, records))
    return feeds


def _record_path(record: dict, archive_dir: Path) -> Path | None:
    file_value = _text(record.get("file")).replace("\\", "/").lstrip("/")
    if file_value:
        parts = PurePosixPath(file_value).parts
        candidates = [archive_dir / file_value]
        if parts and parts[0] == archive_dir.name:
            candidates.append(archive_dir.joinpath(*parts[1:]))
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    day = _day_from_record(record)
    if day:
        candidate = archive_dir / day[0:4] / day[5:7] / f"{day[8:10]}.md"
        if candidate.is_file():
            return candidate
    return None


def _rendered_body(
    record: dict, rendered: dict[tuple[str, int], str], archive_dir: Path
) -> str:
    day = _day_from_record(record)
    offset = record.get("offset")
    if day and isinstance(offset, int) and not isinstance(offset, bool):
        cached = rendered.get((day, offset))
        if cached is not None:
            return cached
    path = _record_path(record, archive_dir)
    if path is None or not isinstance(offset, int) or isinstance(offset, bool):
        return ""
    length = record.get("length")
    if not isinstance(length, int) or isinstance(length, bool):
        length = None
    try:
        parsed = md_archive.read_record(path, offset, length)
    except (OSError, ValueError):
        return ""
    return _render_markdown(parsed.get("body", ""))


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------
def _render_day(
    day: str,
    records: list[dict],
    feed_lookup: dict[str, tuple[str, str]],
    rendered: dict[tuple[str, int], str],
) -> str:
    articles: list[str] = []
    for index, record in enumerate(records):
        meta = record.get("meta") or {}
        anchor = _anchor_id(record, f"article-{index}")
        title = _text(meta.get("title")).strip() or "(untitled)"
        link = _safe_url(meta.get("link"))
        if link:
            title_html = f'<a href="{_attr(link)}">{_escape(title)}</a>'
        else:
            title_html = _escape(title)

        author = _text(meta.get("author")).strip()
        published = _text(meta.get("published")).strip()
        feed_id = _text(meta.get("feed_id")).strip()
        feed_title = ""
        feed_page = ""
        if feed_id:
            info = feed_lookup.get(feed_id)
            if info:
                feed_title, feed_page = info
            else:
                feed_title = (
                    _text(meta.get("feed_title")).strip()
                    or feed_id
                )
        if not feed_title:
            feed_title = _text(meta.get("feed_title")).strip()

        meta_parts: list[str] = []
        if author:
            meta_parts.append(f"By {_escape(author)}")
        if published:
            meta_parts.append(_escape(published))
        if feed_title:
            if feed_page:
                meta_parts.append(
                    f'<a href="../../feeds/{_attr(feed_page)}.html">'
                    f"{_escape(feed_title)}</a>"
                )
            else:
                meta_parts.append(_escape(feed_title))
        meta_html = (
            f'<p class="meta">{" · ".join(meta_parts)}</p>' if meta_parts else ""
        )

        body = record.get("body", "")
        body_html = _render_markdown(body)
        offset = record.get("offset")
        if isinstance(offset, int) and not isinstance(offset, bool):
            rendered[(day, offset)] = body_html
        body_block = f'<div class="body">{body_html}</div>' if body_html else ""

        articles.append(
            f'<article id="{_attr(anchor)}">\n'
            f"<h2>{title_html}</h2>\n"
            f"{meta_html}\n"
            f"{body_block}\n"
            f"</article>"
        )

    if articles:
        content = "\n".join(articles)
    else:
        content = '<p class="empty">No articles.</p>'
    body = (
        "<main>\n"
        '<nav><a href="../../index.html">← Archive</a></nav>\n'
        "<header>\n"
        f"<h1>{_escape(day)}</h1>\n"
        f'<p class="meta">{_plural(len(records), "article")}</p>\n'
        "</header>\n"
        f"{content}\n"
        "</main>"
    )
    return _document(day, body)


def _render_index(days: list[tuple[str, int]], feeds: list[FeedIndex]) -> str:
    if days:
        day_items = "\n".join(
            f'<li><a href="{_attr(_daily_path(day))}">{_escape(day)}</a> '
            f'<span class="count">· {_plural(count, "article")}</span></li>'
            for day, count in sorted(days, key=lambda item: item[0], reverse=True)
        )
        day_section = f'<ul class="plain">\n{day_items}\n</ul>'
    else:
        day_section = '<p class="empty">No archived articles yet.</p>'

    if feeds:
        feed_items = "\n".join(
            f'<li><a href="feeds/{_attr(feed.output_id)}.html">'
            f"{_escape(feed.title)}</a> "
            f'<span class="count">· <a href="feeds/'
            f'{_attr(feed.output_id)}.json">JSON Feed</a></span></li>'
            for feed in sorted(
                feeds, key=lambda item: (item.title.casefold(), item.output_id)
            )
        )
        feed_section = f'<ul class="plain">\n{feed_items}\n</ul>'
    else:
        feed_section = '<p class="empty">No feed indexes yet.</p>'

    body = (
        "<main>\n"
        "<header>\n"
        "<h1>Markdown Archive</h1>\n"
        '<p class="meta">A static view of the RSS archive and per-feed indexes.</p>\n'
        "</header>\n"
        "<section>\n"
        "<h2>Days</h2>\n"
        f"{day_section}\n"
        "</section>\n"
        "<section>\n"
        "<h2>Feeds</h2>\n"
        f"{feed_section}\n"
        "</section>\n"
        "</main>"
    )
    return _document("Markdown Archive", body)


def _render_feed(feed: FeedIndex, annotations: dict[str, dict]) -> str:
    items: list[str] = []
    for record in _article_records(feed):
        title = _text(record.get("title")).strip() or "(untitled)"
        link = _safe_url(record.get("link"))
        if link:
            title_html = f'<a href="{_attr(link)}">{_escape(title)}</a>'
        else:
            title_html = _escape(title)

        published = _text(record.get("published")).strip()
        annotation = annotations.get(_text(record.get("guid")).strip(), {})
        category = _text(annotation.get("category")).strip()
        score = _text(annotation.get("score")).strip()
        summary = _text(annotation.get("summary")).strip()

        meta_parts: list[str] = []
        if published:
            meta_parts.append(_escape(published))
        if category:
            meta_parts.append(f"Category: {_escape(category)}")
        if score:
            meta_parts.append(f"Score: {_escape(score)}")
        meta_html = (
            f'<p class="meta">{" · ".join(meta_parts)}</p>' if meta_parts else ""
        )
        summary_html = (
            f'<p class="summary">{_escape(summary)}</p>' if summary else ""
        )

        daily = _daily_href(record, "..")
        link_parts: list[str] = []
        if daily:
            link_parts.append(f'<a href="{_attr(daily)}">Archive entry</a>')
        if link:
            link_parts.append(f'<a href="{_attr(link)}">Original</a>')
        links_html = (
            f'<p class="links">{" · ".join(link_parts)}</p>' if link_parts else ""
        )

        items.append(
            '<li class="feed-item">\n'
            f"<h2>{title_html}</h2>\n"
            f"{meta_html}\n"
            f"{summary_html}\n"
            f"{links_html}\n"
            "</li>"
        )

    content = (
        f'<ul class="plain">\n{chr(10).join(items)}\n</ul>'
        if items
        else '<p class="empty">No articles in this feed index.</p>'
    )

    header_parts: list[str] = []
    feed_url = _safe_url(feed.header.get("feed_url"))
    site_url = _safe_url(feed.header.get("site_url"))
    if feed_url:
        header_parts.append(f'<a href="{_attr(feed_url)}">Source feed</a>')
    if site_url:
        header_parts.append(f'<a href="{_attr(site_url)}">Website</a>')
    header_parts.append(
        f'<a href="{_attr(feed.output_id)}.json">JSON Feed</a>'
    )
    header_meta = f'<p class="meta">{" · ".join(header_parts)}</p>'

    body = (
        "<main>\n"
        '<nav><a href="../index.html">← Archive</a></nav>\n'
        "<header>\n"
        f"<h1>{_escape(feed.title)}</h1>\n"
        f"{header_meta}\n"
        "</header>\n"
        "<section>\n"
        f"{content}\n"
        "</section>\n"
        "</main>"
    )
    return _document(feed.title, body)


def _feed_urls(feed: FeedIndex, base_url: str = "") -> tuple[str, str]:
    """Return ``(home_page_url, feed_url)`` for the JSON Feed."""
    settings_base = _safe_url(base_url)
    quoted_id = quote(feed.output_id, safe="")
    if _is_absolute_url(settings_base):
        home = settings_base.rstrip("/") + "/"
        feed_url = f"{home}feeds/{quoted_id}.json"
        return home, feed_url
    site_url = _safe_url(feed.header.get("site_url"))
    if _is_absolute_url(site_url):
        home = site_url
        feed_url = f"{site_url.rstrip('/')}/feeds/{quoted_id}.json"
    else:
        # Relative to the JSON Feed document at <site>/feeds/<id>.json.
        home = "../index.html"
        feed_url = f"../feeds/{quoted_id}.json"
    return home, feed_url


def _build_json_feed(
    feed: FeedIndex,
    annotations: dict[str, dict],
    rendered: dict[tuple[str, int], str],
    archive_dir: Path,
    base_url: str = "",
) -> dict:
    home_page_url, feed_url = _feed_urls(feed, base_url)
    items: list[dict] = []
    for record in _article_records(feed):
        title = _plain_text(record.get("title")) or "(untitled)"
        link = _safe_url(record.get("link"))
        daily = _daily_href(record, "..")
        guid = _anchor_id(record)
        item_id = (
            _plain_text(guid)
            or link
            or f"{_text(record.get('file'))}:{_text(record.get('offset'))}"
        )
        annotation = annotations.get(_text(record.get("guid")).strip(), {})
        summary = _plain_text(annotation.get("summary"))

        item: dict = {"id": item_id, "title": title}
        if link:
            item["url"] = link
        elif daily:
            item["url"] = daily
        published = _parse_dt(record.get("published"))
        if published:
            item["date_published"] = _iso(published)
        if summary:
            item["summary"] = summary

        body_html = _rendered_body(record, rendered, archive_dir)
        if body_html:
            item["content_html"] = body_html
        elif daily:
            item["content_html"] = (
                f'<p><a href="{_attr(daily)}">Read the archived entry</a></p>'
            )
        elif summary:
            item["content_text"] = summary
        else:
            item["content_text"] = title
        items.append(item)

    return {
        "version": JSON_FEED_VERSION,
        "title": feed.title,
        "home_page_url": home_page_url,
        "feed_url": feed_url,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def render_site(
    site: str | Path,
    archive: str | Path,
    feeds: str | Path,
    base_url: str = "",
) -> dict:
    """Render all pages and JSON Feeds; return a small deterministic summary."""
    site_dir = Path(site)
    archive_dir = _resolve_archive(archive)
    feed_indexes = _load_feeds(Path(feeds))

    feed_lookup: dict[str, tuple[str, str]] = {}
    for feed in feed_indexes:
        info = (feed.title, feed.output_id)
        feed_lookup.setdefault(feed.feed_id, info)
        feed_lookup.setdefault(feed.output_id, info)

    rendered: dict[tuple[str, int], str] = {}
    days: list[tuple[str, int]] = []
    for day, path in _daily_files(archive_dir):
        records = list(md_archive.iter_records(path))
        _write(site_dir / _daily_path(day), _render_day(day, records, feed_lookup, rendered))
        days.append((day, len(records)))

    _write(site_dir / "index.html", _render_index(days, feed_indexes))

    for feed in feed_indexes:
        annotations = _latest_annotations(feed.records)
        _write(
            site_dir / "feeds" / f"{feed.output_id}.html",
            _render_feed(feed, annotations),
        )
        feed_json = _build_json_feed(
            feed, annotations, rendered, archive_dir, base_url=base_url
        )
        _write(
            site_dir / "feeds" / f"{feed.output_id}.json",
            json.dumps(feed_json, ensure_ascii=False, indent=2) + "\n",
        )

    return {
        "days": len(days),
        "articles": sum(count for _day, count in days),
        "feeds": len(feed_indexes),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the Markdown archive as static HTML and JSON Feeds."
    )
    parser.add_argument("--site", default="_site", help="output site directory")
    parser.add_argument("--archive", default="archive", help="archive directory or root")
    parser.add_argument("--feeds", default="feeds", help="per-feed JSONL directory")
    parser.add_argument(
        "--base-url",
        default="",
        help="absolute public site URL (used for JSON Feed URLs)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = render_site(args.site, args.archive, args.feeds, base_url=args.base_url)
    print(
        f"Rendered {_plural(summary['days'], 'day')}, "
        f"{_plural(summary['articles'], 'article')} and "
        f"{_plural(summary['feeds'], 'feed')} into {args.site}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
