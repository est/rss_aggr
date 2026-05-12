"""Detect aggregator feeds by link domain diversity."""
from urllib.parse import urlparse


def _extract_domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        # strip www.
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def is_aggregator(entries: list[dict], threshold: float = 0.7, min_articles: int = 5) -> bool:
    """Check if a feed is an aggregator (articles link to many different domains).

    Args:
        entries: list of article dicts with 'link' key
        threshold: unique_domain_ratio above which we consider it an aggregator
        min_articles: need at least this many articles to judge

    Returns:
        True if the feed looks like an aggregator (Hacker News, Reddit, etc.)
    """
    if len(entries) < min_articles:
        return False

    feed_domain = _extract_domain(entries[0].get("link", ""))  # not used for comparison
    domains = set()
    same_domain = 0

    for e in entries:
        link = e.get("link", "")
        domain = _extract_domain(link)
        if domain:
            domains.add(domain)

    unique_ratio = len(domains) / len(entries) if entries else 0
    return unique_ratio >= threshold
