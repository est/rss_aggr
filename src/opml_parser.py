"""Parse feeds config file (TOML) to extract RSS feed list."""
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from src.urls import canonical_url


def parse_opml_bytes(content: bytes) -> list[dict]:
    """Parse OPML bytes into a flat feed list.

    Every outline with an ``xmlUrl`` is a feed, whether it sits at the top
    level or inside nested category outlines. ``source_category`` comes from
    the nearest ancestor outline that has a name (``""`` for top-level feeds).

    Returns dicts with keys: title, url, site, source_category.
    """
    root = ET.fromstring(content)
    body = root.find("body")
    if body is None:
        body = root

    feeds: list[dict] = []

    def walk(parent: ET.Element, category: str) -> None:
        for outline in parent.findall("outline"):
            xml_url = (outline.get("xmlUrl") or "").strip()
            if xml_url:
                feeds.append(
                    {
                        "title": outline.get("text", outline.get("title", "")),
                        "url": xml_url,
                        "site": outline.get("htmlUrl", ""),
                        "source_category": category,
                    }
                )
                continue
            name = outline.get("text", outline.get("title", ""))
            walk(outline, name or category)

    walk(body, "")
    return feeds


def parse_feeds(path: str | Path = "feeds.toml") -> list[dict]:
    """Parse a TOML feeds file and return a flat, de-duplicated feed list.

    Duplicate URLs are collapsed by canonical URL (http/https, www, trailing
    slash, tracking params), first occurrence wins. Entries with no URL and
    entries with ``skip = true`` are ignored.

    Returns list of dicts with keys: title, xml_url, html_url, category,
    priority, skip_prompt.
    """
    p = Path(path)
    with open(p, "rb") as f:
        data = tomllib.load(f)

    feeds = []
    seen: set[str] = set()
    for cat in data.get("category", []):
        category = cat.get("name", "Uncategorized")
        for feed in cat.get("feed", []):
            if feed.get("skip"):
                continue
            url = (feed.get("url") or "").strip()
            if not url:
                continue
            key = canonical_url(url) or url
            if key in seen:
                continue
            seen.add(key)
            feeds.append({
                "title": feed.get("title", ""),
                "xml_url": url,
                "html_url": feed.get("site", ""),
                "category": category,
                "priority": feed.get("priority", 99),
                "skip_prompt": feed.get("skip_prompt", ""),
            })
    return feeds
