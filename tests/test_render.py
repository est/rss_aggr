import json

from src.md_archive import append_articles, append_feed_annotation, append_feed_record
from src.render import render_site


def _meta(**overrides):
    meta = {
        "guid": "guid-1",
        "feed_id": "feed-1",
        "feed_url": "https://example.com/feed.xml",
        "site_url": "https://example.com",
        "link": "https://example.com/a",
        "title": "Safe & Sound",
        "author": "Ada",
        "published": "2026-05-01T10:00:00Z",
        "fetched_at": "2026-05-01T11:00:00Z",
    }
    meta.update(overrides)
    return meta


def _feed_meta(**overrides):
    meta = {
        "feed_id": "feed-1",
        "feed_url": "https://example.com/feed.xml",
        "site_url": "https://example.com",
        "title": "Example Feed",
    }
    meta.update(overrides)
    return meta


def _build_archive(tmp_path):
    records = append_articles(
        str(tmp_path),
        [
            (
                _meta(),
                "# Heading\n\nBody **bold** and [link](https://example.com/x).\n",
            ),
            (
                _meta(
                    guid="guid-2",
                    link="https://example.com/b",
                    title='<script>alert("x")</script>Evil & Co',
                    published="2026-05-01T12:00:00Z",
                ),
                "Hello\n\n<script>alert('bad')</script>\n\n"
                '<a href="javascript:alert(1)">click</a>\n',
            ),
        ],
    )
    feeds = tmp_path / "feeds"
    for record in records:
        append_feed_record(str(feeds), _feed_meta(), record)

    append_feed_annotation(
        str(feeds),
        _feed_meta(),
        {
            "guid": "guid-2",
            "file": records[1]["file"],
            "offset": records[1]["offset"],
            "category": "Tech",
            "summary": "Older summary",
            "score": 3,
            "classified_at": "2026-05-02T00:00:00Z",
        },
    )
    append_feed_annotation(
        str(feeds),
        _feed_meta(),
        {
            "guid": "guid-2",
            "file": records[1]["file"],
            "offset": records[1]["offset"],
            "category": "Insight",
            "summary": "Latest <b>summary</b> & more",
            "score": 9,
            "classified_at": "2026-05-03T00:00:00Z",
        },
    )
    return records


def test_render_site_pages_json_feed_and_safety(tmp_path):
    records = _build_archive(tmp_path)
    site = tmp_path / "_site"
    summary = render_site(site, tmp_path / "archive", tmp_path / "feeds")

    assert summary == {"days": 1, "articles": 2, "feeds": 1}

    daily_path = site / "2026" / "05" / "01.html"
    index_path = site / "index.html"
    feed_path = site / "feeds" / "feed-1.html"
    json_path = site / "feeds" / "feed-1.json"
    assert daily_path.is_file()
    assert index_path.is_file()
    assert feed_path.is_file()
    assert json_path.is_file()

    daily = daily_path.read_text(encoding="utf-8")
    assert 'id="guid-1"' in daily
    assert 'id="guid-2"' in daily
    assert "&lt;script&gt;" in daily
    assert "<script" not in daily.lower()
    assert "javascript:alert" not in daily
    assert "<strong>bold</strong>" in daily
    assert "https://example.com/a" in daily
    assert "https://example.com/b" in daily
    assert "Example Feed" in daily

    index = index_path.read_text(encoding="utf-8")
    assert "2026/05/01.html" in index
    assert "feeds/feed-1.html" in index
    assert "feeds/feed-1.json" in index
    assert "Example Feed" in index

    feed_page = feed_path.read_text(encoding="utf-8")
    assert "../2026/05/01.html#guid-1" in feed_page
    assert "../2026/05/01.html#guid-2" in feed_page
    assert "Latest &lt;b&gt;summary&lt;/b&gt; &amp; more" in feed_page
    assert "Older summary" not in feed_page
    assert "Category: Insight" in feed_page
    assert "Score: 9" in feed_page
    assert "https://example.com/a" in feed_page
    assert "https://example.com/b" in feed_page
    assert "<script" not in feed_page.lower()
    # Newest published article is listed first.
    assert feed_page.index("Evil &amp; Co") < feed_page.index("Safe &amp; Sound")

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["version"] == "https://jsonfeed.org/version/1.1"
    assert data["title"] == "Example Feed"
    assert data["home_page_url"] == "https://example.com"
    assert data["feed_url"] == "https://example.com/feeds/feed-1.json"
    assert len(data["items"]) == 2

    items = {item["id"]: item for item in data["items"]}
    assert set(items) == {"guid-1", "guid-2"}
    assert items["guid-1"]["url"] == "https://example.com/a"
    assert items["guid-1"]["date_published"] == "2026-05-01T10:00:00Z"
    assert "<strong>bold</strong>" in items["guid-1"]["content_html"]
    assert items["guid-2"]["url"] == "https://example.com/b"
    assert items["guid-2"]["title"] == "Evil & Co"
    assert items["guid-2"]["summary"] == "Latest summary & more"
    assert "<script" not in items["guid-2"]["content_html"].lower()
    assert "javascript:alert" not in items["guid-2"]["content_html"]

    json_text = json_path.read_text(encoding="utf-8")
    assert "<script" not in json_text.lower()

    # The feed records point back to the exact archived offsets.
    assert records[0]["offset"] != records[1]["offset"]
    assert records[1]["file"] == "archive/2026/05/01.md"


def test_render_is_deterministic(tmp_path):
    _build_archive(tmp_path)
    site = tmp_path / "_site"

    render_site(site, tmp_path / "archive", tmp_path / "feeds")
    first = {
        path.relative_to(site).as_posix(): path.read_bytes()
        for path in sorted(site.rglob("*"))
        if path.is_file()
    }

    render_site(site, tmp_path / "archive", tmp_path / "feeds")
    second = {
        path.relative_to(site).as_posix(): path.read_bytes()
        for path in sorted(site.rglob("*"))
        if path.is_file()
    }

    assert first == second


def test_empty_archive_produces_valid_index(tmp_path):
    site = tmp_path / "_site"
    summary = render_site(site, tmp_path / "missing-archive", tmp_path / "missing-feeds")

    assert summary == {"days": 0, "articles": 0, "feeds": 0}
    index = (site / "index.html").read_text(encoding="utf-8")
    assert index.startswith("<!DOCTYPE html>")
    assert "<title>Markdown Archive</title>" in index
    assert "No archived articles yet." in index
    assert "No feed indexes yet." in index
    assert index.rstrip().endswith("</html>")
    assert "<script" not in index.lower()
