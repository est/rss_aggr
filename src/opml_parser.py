"""Parse OPML file to extract RSS feed list."""
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_opml(opml_path: str | Path) -> list[dict]:
    """Parse an OPML file and return a flat list of RSS feeds.

    Returns list of dicts with keys: title, xml_url, html_url, category
    """
    tree = ET.parse(opml_path)
    root = tree.getroot()

    feeds = []
    for outline in root.findall(".//outline"):
        category = outline.get("text", "Uncategorized")
        for feed_outline in outline.findall("outline[@type='rss']"):
            feeds.append({
                "title": feed_outline.get("text", ""),
                "xml_url": feed_outline.get("xmlUrl", ""),
                "html_url": feed_outline.get("htmlUrl", ""),
                "category": category,
            })

    return feeds
