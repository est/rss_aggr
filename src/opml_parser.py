"""Parse feeds config file (TOML) to extract RSS feed list."""
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def parse_feeds(path: str | Path = "feeds.toml") -> list[dict]:
    """Parse a TOML feeds file and return a flat list of RSS feeds.

    Returns list of dicts with keys: title, xml_url, html_url, category
    """
    p = Path(path)
    with open(p, "rb") as f:
        data = tomllib.load(f)

    feeds = []
    for cat in data.get("category", []):
        category = cat.get("name", "Uncategorized")
        for feed in cat.get("feed", []):
            feeds.append({
                "title": feed.get("title", ""),
                "xml_url": feed.get("url", ""),
                "html_url": feed.get("site", ""),
                "category": category,
            })
    return feeds
