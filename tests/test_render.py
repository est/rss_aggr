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
    script_path = site / "assets" / "feed.js"
    md_path = site / "archive" / "2026" / "05" / "01.md"
    assert daily_path.is_file()
    assert index_path.is_file()
    assert feed_path.is_file()
    assert json_path.is_file()
    assert script_path.is_file()
    assert "Range" in script_path.read_text(encoding="utf-8")
    assert md_path.read_bytes() == (
        tmp_path / "archive" / "2026" / "05" / "01.md"
    ).read_bytes()

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
    assert 'data-feed-json="feed-1.json"' in feed_page
    assert "../assets/feed.js" in feed_page
    assert "Example Feed" in feed_page
    assert "<strong>bold</strong>" not in feed_page
    assert "Hello" not in feed_page

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["version"] == "https://jsonfeed.org/version/1.1"
    assert data["title"] == "Example Feed"
    assert data["home_page_url"] == "https://example.com"
    assert data["feed_url"] == "../feeds/feed-1.json"
    assert len(data["items"]) == 2

    items = {item["id"]: item for item in data["items"]}
    assert set(items) == {"guid-1", "guid-2"}
    assert items["guid-1"]["url"] == "https://example.com/a"
    assert items["guid-1"]["date_published"] == "2026-05-01T10:00:00Z"
    assert "content_html" not in items["guid-1"]
    assert items["guid-1"]["content_text"] == "Safe & Sound"
    assert items["guid-2"]["url"] == "https://example.com/b"
    assert items["guid-2"]["title"] == "Evil & Co"
    assert items["guid-2"]["summary"] == "Latest summary & more"
    assert "content_html" not in items["guid-2"]
    assert items["guid-2"]["content_text"] == "Latest summary & more"
    assert items["guid-2"]["tags"] == ["Insight"]
    assert items["guid-2"]["_score"] == 9
    assert items["guid-2"]["_daily_url"].startswith("../2026/05/01.html#")

    archive_ref = items["guid-1"]["_archive"]
    assert archive_ref["file"] == "archive/2026/05/01.md"
    assert archive_ref["offset"] == records[0]["offset"]
    assert archive_ref["length"] == records[0]["length"]
    assert archive_ref["body_offset"] == records[0]["body_offset"]
    assert archive_ref["body_len"] == records[0]["body_len"]
    assert archive_ref["sha256"] == records[0]["sha256"]

    json_text = json_path.read_text(encoding="utf-8")
    assert "<strong>bold</strong>" not in json_text
    assert "Hello" not in json_text
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


def test_feed_config_overlays_current_title_and_site(tmp_path):
    _build_archive(tmp_path)
    config = tmp_path / "feeds.toml"
    config.write_text(
        '[[category]]\n'
        'name = "Test"\n'
        "\n"
        "[[category.feed]]\n"
        'title = "Renamed Feed"\n'
        'url = "https://example.com/feed.xml"\n'
        'site = "https://new.example.org"\n',
        encoding="utf-8",
    )
    jsonl_path = tmp_path / "feeds" / "feed-1.jsonl"
    original_jsonl = jsonl_path.read_bytes()

    site = tmp_path / "_site_with_config"
    render_site(
        site,
        tmp_path / "archive",
        tmp_path / "feeds",
        feed_config=config,
    )

    # Rendering never rewrites the append-only JSONL header.
    assert jsonl_path.read_bytes() == original_jsonl

    index = (site / "index.html").read_text(encoding="utf-8")
    assert "Renamed Feed" in index
    assert "Example Feed" not in index

    feed_page = (site / "feeds" / "feed-1.html").read_text(encoding="utf-8")
    assert "<title>Renamed Feed</title>" in feed_page
    assert 'href="https://new.example.org"' in feed_page
    assert "Example Feed" not in feed_page

    daily = (site / "2026" / "05" / "01.html").read_text(encoding="utf-8")
    assert "Renamed Feed" in daily
    assert "Example Feed" not in daily
    assert 'href="../../feeds/feed-1.html"' in daily

    data = json.loads((site / "feeds" / "feed-1.json").read_text(encoding="utf-8"))
    assert data["title"] == "Renamed Feed"
    assert data["home_page_url"] == "https://new.example.org"
    assert data["feed_url"] == "../feeds/feed-1.json"

    # Without a feed config the archived header remains authoritative.
    plain_site = tmp_path / "_site_without_config"
    render_site(plain_site, tmp_path / "archive", tmp_path / "feeds")

    plain_index = (plain_site / "index.html").read_text(encoding="utf-8")
    assert "Example Feed" in plain_index
    assert "Renamed Feed" not in plain_index

    plain_data = json.loads(
        (plain_site / "feeds" / "feed-1.json").read_text(encoding="utf-8")
    )
    assert plain_data["title"] == "Example Feed"
    assert plain_data["home_page_url"] == "https://example.com"
    assert jsonl_path.read_bytes() == original_jsonl


def test_skip_annotation_hides_article_from_daily_and_feed(tmp_path):
    records = _build_archive(tmp_path)
    feeds = tmp_path / "feeds"
    append_feed_annotation(
        str(feeds),
        _feed_meta(),
        {
            "guid": "guid-1",
            "file": records[0]["file"],
            "offset": records[0]["offset"],
            "category": " Skip ",
            "summary": "Hidden summary",
            "score": 1,
            "classified_at": "2026-05-04T00:00:00Z",
        },
    )

    site = tmp_path / "_site_skip"
    summary = render_site(site, tmp_path / "archive", tmp_path / "feeds")

    assert summary == {"days": 1, "articles": 1, "feeds": 1}
    index = (site / "index.html").read_text(encoding="utf-8")
    assert "1 article" in index

    daily = (site / "2026" / "05" / "01.html").read_text(encoding="utf-8")
    assert 'id="guid-1"' not in daily
    assert "Safe &amp; Sound" not in daily
    assert 'id="guid-2"' in daily
    assert "Evil &amp; Co" in daily

    data = json.loads((site / "feeds" / "feed-1.json").read_text(encoding="utf-8"))
    assert {item["id"] for item in data["items"]} == {"guid-2"}

    # The published Markdown copy must not leak the skipped record either.
    md_text = (site / "archive" / "2026" / "05" / "01.md").read_text(
        encoding="utf-8"
    )
    assert "guid-1" not in md_text
    assert "Safe & Sound" not in md_text
    assert "guid-2" in md_text

    # Offsets in the JSON Feed are translated to the filtered Markdown file.
    item = data["items"][0]
    md_bytes = (site / "archive" / "2026" / "05" / "01.md").read_bytes()
    ref = item["_archive"]
    body = md_bytes[ref["body_offset"] : ref["body_offset"] + ref["body_len"]]
    assert b"Hello" in body

    # A newer annotation can make the article visible again.
    append_feed_annotation(
        str(feeds),
        _feed_meta(),
        {
            "guid": "guid-1",
            "file": records[0]["file"],
            "offset": records[0]["offset"],
            "category": "Tech",
            "summary": "Visible again",
            "score": 8,
            "classified_at": "2026-05-05T00:00:00Z",
        },
    )

    visible_site = tmp_path / "_site_visible"
    visible_summary = render_site(
        visible_site, tmp_path / "archive", tmp_path / "feeds"
    )
    assert visible_summary == {"days": 1, "articles": 2, "feeds": 1}

    visible_daily = (visible_site / "2026" / "05" / "01.html").read_text(
        encoding="utf-8"
    )
    assert 'id="guid-1"' in visible_daily
    assert "Safe &amp; Sound" in visible_daily

    visible_data = json.loads(
        (visible_site / "feeds" / "feed-1.json").read_text(encoding="utf-8")
    )
    assert {item["id"] for item in visible_data["items"]} == {"guid-1", "guid-2"}


def test_feed_urls_use_base_url_for_feed_document(tmp_path):
    _build_archive(tmp_path)
    site = tmp_path / "_site"
    render_site(
        site,
        tmp_path / "archive",
        tmp_path / "feeds",
        base_url="https://aggregator.example/",
    )

    data = json.loads((site / "feeds" / "feed-1.json").read_text(encoding="utf-8"))
    # home_page_url stays the source site; feed_url points at the aggregator.
    assert data["home_page_url"] == "https://example.com"
    assert data["feed_url"] == "https://aggregator.example/feeds/feed-1.json"


def test_guidless_record_anchor_matches_json_item_id(tmp_path):
    records = append_articles(
        str(tmp_path),
        [
            (
                _meta(guid="", link="https://example.com/no-guid", title="No GUID"),
                "# No guid\n\nBody text.\n",
            )
        ],
    )
    feeds = tmp_path / "feeds"
    append_feed_record(str(feeds), _feed_meta(), records[0])

    site = tmp_path / "_site"
    render_site(site, tmp_path / "archive", tmp_path / "feeds")

    anchor = f"article-{records[0]['offset']}"
    daily = (site / "2026" / "05" / "01.html").read_text(encoding="utf-8")
    assert f'id="{anchor}"' in daily

    data = json.loads((site / "feeds" / "feed-1.json").read_text(encoding="utf-8"))
    assert data["items"][0]["id"] == anchor
    assert data["items"][0]["_daily_url"].endswith(f"#{anchor}")
