from src.aggregator import is_aggregator


def _entries(domains: list[str], authors: list[str] | None = None) -> list[dict]:
    authors = authors or [""] * len(domains)
    return [
        {"link": f"https://{domain}/post", "author": author}
        for domain, author in zip(domains, authors)
    ]


def test_many_domains_without_authors_is_aggregator():
    entries = _entries([f"site{i}.example" for i in range(6)])
    assert is_aggregator(entries)


def test_many_authors_on_one_domain_is_not_aggregator():
    entries = _entries(
        ["blog.example"] * 6,
        [f"author{i}" for i in range(6)],
    )
    assert not is_aggregator(entries)


def test_many_domains_and_many_authors_is_aggregator():
    entries = _entries(
        [f"site{i}.example" for i in range(6)],
        [f"author{i}" for i in range(6)],
    )
    assert is_aggregator(entries)


def test_small_feed_is_not_aggregator():
    assert not is_aggregator(_entries([f"site{i}.example" for i in range(3)]))
