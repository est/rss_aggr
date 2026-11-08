"""Detect aggregator feeds by link domain and author diversity."""
from urllib.parse import urlparse


def _extract_domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        return host
    except ValueError:
        return ""


def is_aggregator(entries: list[dict], threshold: float = 0.7, min_articles: int = 5) -> bool:
    """Check if a feed is an aggregator.

    Domain diversity is the primary signal. Author diversity only confirms it
    when the feed actually exposes authors; a multi-author blog on one domain
    must not be classified as an aggregator just because it has many authors.
    """
    if len(entries) < min_articles:
        return False

    domains = set()
    authors = set()

    for entry in entries:
        domain = _extract_domain(entry.get("link", ""))
        if domain:
            domains.add(domain)

        author = (entry.get("author") or "").strip()
        if author:
            authors.add(author.lower())

    domain_ratio = len(domains) / len(entries)
    if domain_ratio < threshold:
        return False
    if not authors:
        return True
    author_ratio = len(authors) / len(entries)
    return author_ratio >= threshold
