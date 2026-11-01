"""Render the Markdown archive as a static site and JSON Feed indexes.

The archive is the source of truth: daily files are rendered to
``<site>/YYYY/MM/DD.html`` and copied to ``<site>/archive/YYYY/MM/DD.md``.
Each per-feed JSONL index becomes a metadata-only
``<site>/feeds/<feed_id>.json`` plus a small
``<site>/feeds/<feed_id>.html`` shell. The shell loads the JSON with
JavaScript and fetches individual bodies from the daily Markdown files with
HTTP Range requests, so article text is not duplicated into the JSON Feed or
the per-feed HTML.

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
from pathlib import Path
from urllib.parse import quote, urlsplit

import bleach
import markdown

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

from src import md_archive
from src.sanitize import ALLOWED_ATTRIBUTES, ALLOWED_PROTOCOLS, ALLOWED_TAGS
from src.urls import canonical_url

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
article {
  margin: 0 0 2rem;
  padding: 1rem 1.1rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
}
article h2 { margin-top: 0; }
.feed-item {
  margin: 0 0 1.5rem;
  padding: 1rem 1.1rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
}
.feed-item h2 { margin: 0 0 0.35rem; font-size: 1.15rem; }
.feed-item .summary { overflow-wrap: anywhere; }
.feed-item .links { font-size: 0.9rem; }
.feed-item .body-text {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  max-height: 42rem;
  overflow: auto;
  padding: 0.75rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  font: 0.9rem/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
}
.feed-item button {
  margin-top: 0.25rem;
  padding: 0.3rem 0.7rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--fg);
  cursor: pointer;
}
.feed-item button:disabled { cursor: wait; opacity: 0.6; }
.body { overflow-wrap: anywhere; }
.body pre { overflow-x: auto; padding: 0.75rem; background: var(--bg); }
.body img { max-width: 100%; height: auto; }
.body table { border-collapse: collapse; }
.body th, .body td { border: 1px solid var(--border); padding: 0.3rem 0.5rem; }
ul.plain { padding-left: 1.2rem; }
.empty { color: var(--muted); font-style: italic; }
"""

FEED_JS = """\
(() => {
  "use strict";
  const root = document.querySelector("[data-feed-json]");
  if (!root) return;
  const list = document.getElementById("items");
  const status = document.getElementById("status");
  const title = document.getElementById("feed-title");
  const jsonUrl = root.dataset.feedJson;

  const text = (value) => (value == null ? "" : String(value));
  const node = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value != null) element.textContent = value;
    return element;
  };
  const safeUrl = (value) => {
    const raw = text(value).trim();
    if (!raw) return "";
    try {
      const url = new URL(raw, location.href);
      if (["http:", "https:", "mailto:"].includes(url.protocol)) return url.href;
    } catch (_) {
      return "";
    }
    return "";
  };
  const link = (href, label) => {
    const safe = safeUrl(href);
    if (!safe) return node("span", "", label);
    const anchor = node("a", "", label);
    anchor.href = safe;
    anchor.rel = "noopener noreferrer";
    return anchor;
  };
  const dailyHref = (item) => {
    const archive = item._archive || {};
    const match = /^archive\\/(\\d{4})\\/(\\d{2})\\/(\\d{2})\\.md$/.exec(text(archive.file));
    if (!match) return "";
    const anchor = encodeURIComponent(text(item.id));
    return `../${match[1]}/${match[2]}/${match[3]}.html#${anchor}`;
  };
  const toHex = (buffer) =>
    Array.from(new Uint8Array(buffer))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  const digest = async (bytes) => {
    if (!window.crypto || !crypto.subtle) return "";
    return toHex(await crypto.subtle.digest("SHA-256", bytes));
  };
  const fetchBytes = async (url, offset, length) => {
    let response = await fetch(url, {
      headers: { Range: `bytes=${offset}-${offset + length - 1}` },
    });
    if (response.status === 416) response = await fetch(url);
    if (!response.ok && response.status !== 206) {
      throw new Error(`HTTP ${response.status}`);
    }
    if (response.status === 206) {
      const contentRange = response.headers.get("Content-Range") || "";
      if (contentRange && !contentRange.startsWith(`bytes ${offset}-`)) {
        throw new Error("unexpected Content-Range");
      }
      const bytes = new Uint8Array(await response.arrayBuffer());
      if (bytes.byteLength === length) return bytes;
    }
    const full = new Uint8Array(await (await fetch(url)).arrayBuffer());
    return full.slice(offset, offset + length);
  };
  const fetchFullSlice = async (url, offset, length) => {
    const full = new Uint8Array(await (await fetch(url)).arrayBuffer());
    return full.slice(offset, offset + length);
  };
  const loadBody = async (entry) => {
    if (entry.loaded) {
      entry.body.hidden = !entry.body.hidden;
      entry.button.textContent = entry.body.hidden ? "Show full text" : "Hide full text";
      return;
    }
    if (entry.loading) return;
    entry.loading = true;
    entry.button.disabled = true;
    entry.button.textContent = "Loading...";
    const archive = entry.item._archive || {};
    const file = text(archive.file);
    const offset = Number(archive.body_offset);
    const length = Number(archive.body_len);
    const expected = text(archive.sha256);
    try {
      if (!file || !Number.isInteger(offset) || !Number.isInteger(length) || length <= 0) {
        entry.body.hidden = false;
        entry.body.textContent = "No archived body for this item.";
        entry.loaded = true;
        entry.button.textContent = "No body";
        entry.button.disabled = true;
        return;
      }
      const url = new URL(`../${file}`, location.href).href;
      let bytes = await fetchBytes(url, offset, length);
      if (bytes.byteLength !== length) {
        throw new Error(`expected ${length} bytes, got ${bytes.byteLength}`);
      }
      let actual = await digest(bytes);
      if (expected && actual && actual !== expected) {
        // A proxy may apply Range to a compressed representation. Retry with
        // the whole file (decoded by the browser) and slice the same bytes.
        bytes = await fetchFullSlice(url, offset, length);
        actual = await digest(bytes);
        if (actual && actual !== expected) {
          throw new Error("archive checksum mismatch");
        }
      }
      entry.body.textContent = new TextDecoder("utf-8").decode(bytes);
      entry.body.hidden = false;
      entry.loaded = true;
      entry.button.disabled = false;
      entry.button.textContent = "Hide full text";
    } catch (error) {
      entry.body.hidden = false;
      entry.body.textContent =
        `Could not load full text: ${error.message}\\n` +
        "Open the daily page for the rendered view.";
      entry.button.disabled = false;
      entry.button.textContent = "Retry";
      entry.loading = false;
    }
  };
  const renderItem = (item) => {
    const entry = { item, loaded: false, loading: false };
    const li = node("li", "feed-item");
    const heading = node("h2");
    heading.append(link(item.url, text(item.title) || "(untitled)"));
    li.append(heading);

    const metaParts = [];
    if (item.date_published) metaParts.push(text(item.date_published));
    if (Array.isArray(item.tags) && item.tags.length) {
      metaParts.push(item.tags.map(text).join(", "));
    }
    if (item._score != null) metaParts.push(`Score: ${text(item._score)}`);
    if (metaParts.length) li.append(node("p", "meta", metaParts.join(" \\u00b7 ")));

    if (item.summary) li.append(node("p", "summary", text(item.summary)));

    const links = node("p", "links");
    const daily = dailyHref(item);
    if (daily) links.append(link(daily, "Daily page"));
    if (item.url) {
      if (links.childNodes.length) links.append(document.createTextNode(" \\u00b7 "));
      links.append(link(item.url, "Original"));
    }
    if (links.childNodes.length) li.append(links);

    entry.body = node("pre", "body-text");
    entry.body.hidden = true;
    li.append(entry.body);

    entry.button = node("button", "", "Load full text");
    entry.button.type = "button";
    entry.button.addEventListener("click", () => loadBody(entry));
    li.append(entry.button);
    entry.li = li;
    return entry;
  };

  const observer =
    "IntersectionObserver" in window
      ? new IntersectionObserver(
          (entries) => {
            for (const observation of entries) {
              if (!observation.isIntersecting) continue;
              observer.unobserve(observation.target);
              const entry = observation.target._feedEntry;
              if (entry) loadBody(entry);
            }
          },
          { rootMargin: "300px" }
        )
      : null;

  const main = async () => {
    const response = await fetch(jsonUrl, { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const feed = await response.json();
    if (feed.title) {
      document.title = `${feed.title} - Markdown Archive`;
      if (title) title.textContent = feed.title;
    }
    const items = Array.isArray(feed.items) ? feed.items : [];
    if (!items.length) {
      status.textContent = "No articles in this feed.";
      return;
    }
    status.textContent = `${items.length} articles`;
    for (const item of items) {
      const entry = renderItem(item);
      entry.li._feedEntry = entry;
      list.append(entry.li);
      if (observer) observer.observe(entry.li);
    }
  };

  main().catch((error) => {
    status.textContent = `Could not load feed: ${error.message}`;
  });
})();
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


# ---------------------------------------------------------------------------
# Current feed metadata (feeds.toml)
# ---------------------------------------------------------------------------
def _load_feed_config(
    path: str | Path | None,
) -> tuple[dict[str, tuple[str, str, str]], dict[str, tuple[str, str, str]]]:
    """Read current feed titles/sites from ``feeds.toml``.

    Returns ``(by_url, by_id)`` where each value is ``(title, site_url,
    feed_url)``. The JSONL index stays append-only; this config is only
    overlaid onto a copy of each header while rendering.
    """
    if path is None:
        return {}, {}
    target = Path(path)
    if not target.is_file():
        return {}, {}
    try:
        with target.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return {}, {}

    by_url: dict[str, tuple[str, str, str]] = {}
    by_id: dict[str, tuple[str, str, str]] = {}
    for category in data.get("category") or []:
        if not isinstance(category, dict):
            continue
        for feed in category.get("feed") or []:
            if not isinstance(feed, dict):
                continue
            title = _text(feed.get("title")).strip()
            site_url = _text(feed.get("site")).strip()
            feed_url = _text(feed.get("url")).strip()
            if not (title or site_url or feed_url):
                continue
            override = (title, site_url, feed_url)
            canonical = canonical_url(feed_url)
            if canonical:
                by_url.setdefault(canonical, override)
            identifier = md_archive.feed_id(feed_url, site_url, title)
            if identifier:
                by_id.setdefault(identifier, override)
    return by_url, by_id


def _overlay_feed_header(
    header: dict,
    by_url: dict[str, tuple[str, str, str]],
    by_id: dict[str, tuple[str, str, str]],
) -> dict:
    """Return a copy of ``header`` with feeds.toml metadata overlaid."""
    if not by_url and not by_id:
        return header
    feed_url = _text(header.get("feed_url")).strip()
    site_url = _text(header.get("site_url")).strip()
    title = _text(header.get("title")).strip()
    feed_id = _text(header.get("feed_id")).strip()

    override = by_url.get(canonical_url(feed_url)) if feed_url else None
    if override is None and feed_id:
        override = by_id.get(feed_id)
    if override is None:
        override = by_id.get(md_archive.feed_id(feed_url, site_url, title))
    if override is None:
        return header

    merged = dict(header)
    for key, value in zip(("title", "site_url", "feed_url"), override):
        if value:
            merged[key] = value
    return merged


def _apply_feed_config(
    feed_indexes: list[FeedIndex], feed_config: str | Path | None
) -> list[FeedIndex]:
    """Overlay current feeds.toml titles/sites onto copies of the headers."""
    by_url, by_id = _load_feed_config(feed_config)
    if not by_url and not by_id:
        return feed_indexes
    return [
        FeedIndex(
            feed.feed_id,
            feed.output_id,
            _overlay_feed_header(feed.header, by_url, by_id),
            feed.records,
        )
        for feed in feed_indexes
    ]


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------
def _render_day(
    day: str,
    records: list[dict],
    feed_lookup: dict[str, tuple[str, str]],
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
                feed_title = _text(meta.get("feed_title")).strip() or feed_id
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


def _render_feed(feed: FeedIndex) -> str:
    """Return a small HTML shell; feed data is loaded from JSON by feed.js."""
    header_parts: list[str] = []
    feed_url = _safe_url(feed.header.get("feed_url"))
    site_url = _safe_url(feed.header.get("site_url"))
    if feed_url:
        header_parts.append(f'<a href="{_attr(feed_url)}">Source feed</a>')
    if site_url:
        header_parts.append(f'<a href="{_attr(site_url)}">Website</a>')
    header_parts.append(f'<a href="{_attr(feed.output_id)}.json">JSON Feed</a>')
    header_meta = f'<p class="meta">{" · ".join(header_parts)}</p>'

    body = (
        f'<main class="feed" data-feed-json="{_attr(feed.output_id)}.json">\n'
        '<nav><a href="../index.html">← Archive</a></nav>\n'
        "<header>\n"
        f'<h1 id="feed-title">{_escape(feed.title)}</h1>\n'
        f"{header_meta}\n"
        "</header>\n"
        '<p id="status" class="meta">Loading feed...</p>\n'
        '<ul id="items" class="plain"></ul>\n'
        "</main>\n"
        '<script src="../assets/feed.js" defer></script>'
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
        category = _plain_text(annotation.get("category"))
        score = annotation.get("score")

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
        if category and category != "—":
            item["tags"] = [category]
        if score not in (None, ""):
            item["_score"] = score
        # Full article text lives in archive/YYYY/MM/DD.md (and the daily HTML
        # page). JSON Feed requires a content field, so use the short summary
        # (or title) instead of embedding the archived body.
        item["content_text"] = summary or title

        # Extension fields used by the per-feed page to fetch just this body
        # with an HTTP Range request. Offsets are UTF-8 byte offsets.
        archive = {
            "file": _text(record.get("file")),
            "offset": record.get("offset"),
            "length": record.get("length"),
            "body_offset": record.get("body_offset"),
            "body_len": record.get("body_len"),
            "sha256": _text(record.get("sha256")),
        }
        if archive["file"]:
            item["_archive"] = archive
        if daily:
            item["_daily_url"] = daily
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
    feed_config: str | Path | None = None,
) -> dict:
    """Render all pages and JSON Feeds; return a small deterministic summary."""
    site_dir = Path(site)
    archive_dir = _resolve_archive(archive)
    feed_indexes = _apply_feed_config(_load_feeds(Path(feeds)), feed_config)

    feed_lookup: dict[str, tuple[str, str]] = {}
    for feed in feed_indexes:
        info = (feed.title, feed.output_id)
        feed_lookup.setdefault(feed.feed_id, info)
        feed_lookup.setdefault(feed.output_id, info)

    days: list[tuple[str, int]] = []
    for day, path in _daily_files(archive_dir):
        records = list(md_archive.iter_records(path))
        _write(site_dir / _daily_path(day), _render_day(day, records, feed_lookup))
        # Publish the raw daily Markdown too: the per-feed page range-fetches
        # only the bytes needed for one article instead of embedding bodies in
        # the JSON Feed or the HTML shell.
        md_rel = Path("archive") / Path(_daily_path(day)).with_suffix(".md")
        md_target = site_dir / md_rel
        md_target.parent.mkdir(parents=True, exist_ok=True)
        md_target.write_bytes(path.read_bytes())
        days.append((day, len(records)))

    _write(site_dir / "index.html", _render_index(days, feed_indexes))

    if feed_indexes:
        asset = site_dir / "assets" / "feed.js"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(FEED_JS, encoding="utf-8")

    for feed in feed_indexes:
        annotations = _latest_annotations(feed.records)
        _write(site_dir / "feeds" / f"{feed.output_id}.html", _render_feed(feed))
        feed_json = _build_json_feed(feed, annotations, base_url=base_url)
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
    parser.add_argument(
        "--feed-config",
        default="feeds.toml",
        help="feeds.toml with current feed titles/sites ('' disables)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = render_site(
        args.site,
        args.archive,
        args.feeds,
        base_url=args.base_url,
        feed_config=args.feed_config or None,
    )
    print(
        f"Rendered {_plural(summary['days'], 'day')}, "
        f"{_plural(summary['articles'], 'article')} and "
        f"{_plural(summary['feeds'], 'feed')} into {args.site}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
