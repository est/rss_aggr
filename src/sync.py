"""Sync external feed lists into feeds.toml."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def parse_opml_bytes(content: bytes) -> list[dict]:
    """Parse OPML content and return flat feed list."""
    root = ET.fromstring(content)
    feeds = []

    # Case 1: feeds nested inside category outlines
    for outline in root.findall(".//outline"):
        category = outline.get("text", "Uncategorized")
        for feed in outline.findall("outline[@type='rss']"):
            xml_url = feed.get("xmlUrl", "")
            if xml_url:
                feeds.append({
                    "title": feed.get("text", feed.get("title", "")),
                    "url": xml_url,
                    "site": feed.get("htmlUrl", ""),
                    "source_category": category,
                })

    # Case 2: flat structure, feeds directly under body
    if not feeds:
        for feed in root.findall(".//outline[@type='rss']"):
            xml_url = feed.get("xmlUrl", "")
            if xml_url:
                feeds.append({
                    "title": feed.get("text", feed.get("title", "")),
                    "url": xml_url,
                    "site": feed.get("htmlUrl", ""),
                    "source_category": "",
                })

    return feeds


def parse_remote(source: dict, user_agent: str = "rss_aggr/1.0") -> list[dict]:
    """Fetch and parse a remote feed list (OPML or TOML)."""
    url = source["url"]
    print(f"  Fetching {source['name']}... ", end="", flush=True)

    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": user_agent})
        resp.raise_for_status()
    except Exception as e:
        print(f"FAILED: {e}")
        return []

    content = resp.content
    name = source["name"].lower()

    if name.endswith(".opml") or name.endswith(".xml") or b"<opml" in content[:500]:
        feeds = parse_opml_bytes(content)
    elif name.endswith(".toml"):
        data = tomllib.loads(content.decode())
        feeds = []
        for cat in data.get("category", []):
            for f in cat.get("feed", []):
                feeds.append({
                    "title": f.get("title", ""),
                    "url": f.get("url", ""),
                    "site": f.get("site", ""),
                    "source_category": cat.get("name", ""),
                })
    else:
        # Try OPML as default
        try:
            feeds = parse_opml_bytes(content)
        except ET.ParseError:
            print("FAILED: unknown format")
            return []

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
    """
    return json.dumps(str(value), ensure_ascii=False)


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
    return None


def _write_mapping(lines: list[str], mapping: dict, skip: set[str] | None = None):
    for key, value in mapping.items():
        if skip and key in skip:
            continue
        encoded = _toml_value(value)
        if encoded is not None:
            lines.append(f"{key} = {encoded}")


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
    path.write_text("\n".join(lines), encoding="utf-8")


def sync(sources_path: str = "sources.toml", feeds_path: str = "feeds.toml", user_agent: str = "rss_aggr/1.0"):
    """Main sync: fetch external sources → merge into feeds.toml."""
    with open(sources_path, "rb") as f:
        config = tomllib.load(f)

    sources = config.get("source", [])
    if not sources:
        print("No sources configured")
        return

    print(f"[sync] {len(sources)} sources to sync")
    existing = load_feeds_toml(Path(feeds_path))

    existing_feeds_by_url: dict[str, dict] = {}
    for cat in existing.get("category", []):
        for feed in cat.get("feed", []):
            url = feed.get("url", "")
            if url:
                existing_feeds_by_url[url] = feed

    new_by_cat: dict[str, list[dict]] = {}
    total_new = 0
    total_skipped = 0

    for source in sources:
        feeds = parse_remote(source, user_agent)
        target_cat = source.get("category", "Imported")

        for f in feeds:
            if f["url"] in existing_feeds_by_url:
                total_skipped += 1
                continue
            existing_feeds_by_url[f["url"]] = {
                "title": f["title"],
                "url": f["url"],
                "site": f.get("site", ""),
                "priority": source.get("priority", 10),
            }
            new_by_cat.setdefault(target_cat, []).append(f["url"])
            total_new += 1

    for cat_name, new_urls in new_by_cat.items():
        for url in new_urls:
            feed = existing_feeds_by_url[url]
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

    save_feeds_toml(existing, Path(feeds_path))
    print(f"\n[sync] Done: +{total_new} new, {total_skipped} skipped (dup), {len(existing_feeds_by_url)} total")


if __name__ == "__main__":
    sync()
