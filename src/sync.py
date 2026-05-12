"""Sync external feed lists into feeds.toml."""
import sys
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


def parse_remote(source: dict) -> list[dict]:
    """Fetch and parse a remote feed list (OPML or TOML)."""
    url = source["url"]
    print(f"  Fetching {source['name']}... ", end="", flush=True)

    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "RSS-Aggregator/1.0"})
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


def save_feeds_toml(data: dict, path: Path):
    """Save feeds.toml in human-readable format."""
    lines = []
    for cat in data.get("category", []):
        lines.append('[[category]]')
        lines.append(f'name = "{cat["name"]}"')
        lines.append("")
        for feed in cat.get("feed", []):
            lines.append('[[category.feed]]')
            lines.append(f'title = "{feed["title"]}"')
            lines.append(f'url = "{feed["url"]}"')
            if feed.get("site"):
                lines.append(f'site = "{feed["site"]}"')
            if feed.get("priority"):
                lines.append(f'priority = {feed["priority"]}')
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def sync(sources_path: str = "sources.toml", feeds_path: str = "feeds.toml"):
    """Main sync: fetch external sources → merge into feeds.toml."""
    with open(sources_path, "rb") as f:
        config = tomllib.load(f)

    sources = config.get("source", [])
    if not sources:
        print("No sources configured")
        return

    print(f"[sync] {len(sources)} sources to sync")
    existing = load_feeds_toml(Path(feeds_path))

    existing_urls = set()
    for cat in existing.get("category", []):
        for feed in cat.get("feed", []):
            existing_urls.add(feed.get("url", ""))

    new_by_cat: dict[str, list[dict]] = {}
    total_new = 0
    total_skipped = 0

    for source in sources:
        feeds = parse_remote(source)
        target_cat = source.get("category", "Imported")

        for f in feeds:
            if f["url"] in existing_urls:
                total_skipped += 1
                continue
            existing_urls.add(f["url"])
            new_by_cat.setdefault(target_cat, []).append({
                "title": f["title"],
                "url": f["url"],
                "site": f.get("site", ""),
                "priority": source.get("priority", 10),
            })
            total_new += 1

    cat_map = {c["name"]: c for c in existing.get("category", [])}
    for cat_name, new_feeds in new_by_cat.items():
        if cat_name in cat_map:
            cat_map[cat_name]["feed"].extend(new_feeds)
        else:
            existing.setdefault("category", []).append({
                "name": cat_name,
                "feed": new_feeds,
            })

    save_feeds_toml(existing, Path(feeds_path))
    print(f"\n[sync] Done: +{total_new} new, {total_skipped} skipped (dup), {len(existing_urls)} total")


if __name__ == "__main__":
    sync()
