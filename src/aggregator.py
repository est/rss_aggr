"""Detect aggregator feeds by link domain and author diversity."""
from urllib.parse import urlparse


def _extract_domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def is_aggregator(entries: list[dict], threshold: float = 0.7, min_articles: int = 5) -> bool:
    """Check if a feed is an aggregator.

    Returns True only if BOTH domain diversity AND author diversity are high.
    - Many different domains linking = likely aggregator
    - Many different authors = confirms it's not a single blogger on multiple platforms
    """
    if len(entries) < min_articles:
        return False

    domains = set()
    authors = set()

    for e in entries:
        domain = _extract_domain(e.get("link", ""))
        if domain:
            domains.add(domain)

        author = (e.get("author") or "").strip()
        if author:
            authors.add(author.lower())

    domain_ratio = len(domains) / len(entries) if entries else 0
    author_ratio = len(authors) / len(entries) if authors else 1  # no authors = not aggregator
    # 域名分散 或 作者分散，判定为聚合RSS。多作者博客属于非个人类，也不要
    return domain_ratio >= threshold or author_ratio >= threshold
