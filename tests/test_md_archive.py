import json
import re

import pytest

from src.md_archive import (
    MAGIC,
    TRAILER,
    append_articles,
    append_feed_annotation,
    append_feed_record,
    feed_id,
    feed_meta_from_meta,
    iter_records,
    read_feed_index,
    read_record,
    rebuild_indexes,
    reconcile_indexes,
    repair_indexes,
    repair_tail,
    validate_archive,
)


def _meta(**overrides):
    meta = {
        "guid": "guid-1",
        "feed_id": "feed-1",
        "feed_url": "https://example.com/feed.xml",
        "site_url": "https://example.com",
        "link": "https://example.com/a",
        "title": "A -- B",
        "author": "Author",
        "published": "2026-05-01T23:30:00Z",
        "fetched_at": "2026-05-02T00:00:00Z",
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


def test_append_roundtrip_with_utf8_code_blocks_and_comment_markers(tmp_path):
    body = "# 标题 😀\n\n```html\n<!-- not a record -->\n-->\n```\n"
    records = append_articles(str(tmp_path), [(_meta(), body)])

    assert len(records) == 1
    record = records[0]
    path = tmp_path / "archive" / "2026" / "05" / "01.md"
    assert path.is_file()
    assert record["file"] == "archive/2026/05/01.md"

    data = path.read_bytes()
    assert data[record["body_offset"] : record["body_offset"] + record["body_len"]] == (
        body.encode("utf-8")
    )
    assert record["body_len"] == len(body.encode("utf-8"))

    parsed = read_record(path, record["offset"], record["length"])
    assert parsed["body"] == body
    assert parsed["sha256"] == record["sha256"]

    scanned = list(iter_records(path))
    assert len(scanned) == 1
    assert scanned[0]["body"] == body


def test_record_header_is_compact_and_escapes_double_dashes(tmp_path):
    append_articles(str(tmp_path), [(_meta(title="A -- B"), "body\n")])
    data = (tmp_path / "archive" / "2026" / "05" / "01.md").read_bytes()

    assert data.startswith(MAGIC)
    assert data.endswith(TRAILER)
    header_json = data[len(MAGIC) : data.index(b"\n-->\n")]
    assert b"--" not in header_json
    assert b"\\u002d\\u002d" in header_json
    meta = json.loads(header_json.decode("utf-8"))
    assert meta["title"] == "A -- B"
    assert meta["body_len"] == len(b"body\n")


def test_append_articles_groups_by_utc_published_day(tmp_path):
    first = _meta(guid="g1", published="2026-05-01T23:30:00Z")
    second = _meta(
        guid="g2", link="https://example.com/b", published="2026-05-02T01:00:00+00:00"
    )

    records = append_articles(str(tmp_path), [(first, "one\n"), (second, "two\n")])

    assert [record["file"] for record in records] == [
        "archive/2026/05/01.md",
        "archive/2026/05/02.md",
    ]
    assert (tmp_path / "archive/2026/05/01.md").is_file()
    assert (tmp_path / "archive/2026/05/02.md").is_file()
    assert len(list(iter_records(tmp_path / "archive/2026/05/01.md"))) == 1
    assert len(list(iter_records(tmp_path / "archive/2026/05/02.md"))) == 1


def test_append_articles_accepts_an_archive_directory_directly(tmp_path):
    archive = tmp_path / "archive"

    records = append_articles(str(archive), [(_meta(), "body\n")])

    assert records[0]["file"] == "archive/2026/05/01.md"
    assert (archive / "2026/05/01.md").is_file()


def test_feed_id_is_stable_unique_and_filesystem_safe():
    first = feed_id("https://www.example.com/feed.xml")
    same = feed_id("http://example.com/feed.xml")
    other = feed_id("https://other.example.org/feed.xml")

    assert first == same
    assert first != other
    assert re.fullmatch(r"example\.com-[0-9a-f]{12}", first)
    assert "/" not in first and ":" not in first
    assert feed_id("") == feed_id("")


def test_feed_index_header_dedup_and_latest_annotation(tmp_path):
    records = append_articles(
        str(tmp_path),
        [
            (_meta(guid="g1", link="https://example.com/a"), "one\n"),
            (
                _meta(guid="g2", link="https://example.com/b", published="2026-05-02T01:00:00Z"),
                "two\n",
            ),
        ],
    )
    feeds_dir = tmp_path / "feeds"
    feed_meta = _feed_meta()

    append_feed_record(feeds_dir, feed_meta, records[0])
    append_feed_record(feeds_dir, feed_meta, records[0])  # exact duplicate
    append_feed_record(
        feeds_dir, feed_meta, dict(records[0], link="https://example.com/elsewhere")
    )  # same guid
    append_feed_record(
        feeds_dir, feed_meta, dict(records[0], guid="new-guid", link="https://example.com/a?utm_source=x")
    )  # same canonical link
    append_feed_record(feeds_dir, feed_meta, records[1])

    path = feeds_dir / "feed-1.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["kind"] == "feed"

    append_feed_annotation(
        feeds_dir,
        feed_meta,
        {
            "guid": "g1",
            "file": records[0]["file"],
            "offset": records[0]["offset"],
            "category": "Tech",
            "summary": "first",
            "score": 7,
            "classified_at": "2026-05-02T00:00:00Z",
        },
    )
    append_feed_annotation(
        feeds_dir,
        feed_meta,
        {
            "guid": "g1",
            "file": records[0]["file"],
            "offset": records[0]["offset"],
            "category": "News",
            "summary": "latest",
            "score": 9,
            "classified_at": "2026-05-03T00:00:00Z",
        },
    )

    header, all_records = read_feed_index(path)
    articles = [record for record in all_records if record["kind"] == "article"]
    annotations = [record for record in all_records if record["kind"] == "annotation"]

    assert header["feed_id"] == "feed-1"
    assert header["title"] == "Example Feed"
    assert len(articles) == 2
    assert len(annotations) == 2
    assert annotations[-1]["category"] == "News"
    assert annotations[-1]["summary"] == "latest"


def test_repair_tail_truncates_only_the_partial_trailing_record(tmp_path):
    records = append_articles(
        str(tmp_path),
        [(_meta(guid="g1"), "one\n"), (_meta(guid="g2", link="https://example.com/b"), "two\n")],
    )
    path = tmp_path / "archive" / "2026" / "05" / "01.md"
    original = path.read_bytes()
    assert len(records) == 2

    partial = b'<!-- rss-aggr:article v1\n{"v":1,"guid":"half'
    with path.open("ab") as handle:
        handle.write(partial)

    assert len(list(iter_records(path))) == 2
    assert repair_tail(path) == len(partial)
    assert path.read_bytes() == original
    assert validate_archive(tmp_path / "archive") == []

    third = append_articles(str(tmp_path), [(_meta(guid="g3", link="https://example.com/c"), "three\n")])
    assert third[0]["offset"] == len(original)
    assert len(list(iter_records(path))) == 3


def test_repair_tail_refuses_mid_file_corruption(tmp_path):
    records = append_articles(
        str(tmp_path),
        [
            (_meta(guid="g1"), "one\n"),
            (_meta(guid="g2", link="https://example.com/b"), "two\n"),
            (_meta(guid="g3", link="https://example.com/c"), "three\n"),
        ],
    )
    path = tmp_path / "archive" / "2026" / "05" / "01.md"
    original = path.read_bytes()

    with path.open("r+b") as handle:
        handle.seek(records[1]["offset"])
        handle.write(b"X")

    with pytest.raises(ValueError, match="mid-file corruption"):
        repair_tail(path)
    expected = bytearray(original)
    expected[records[1]["offset"]] = ord("X")
    assert path.read_bytes() == bytes(expected)


def test_validate_and_read_record_detect_corruption(tmp_path):
    record = append_articles(str(tmp_path), [(_meta(), "hello world\n")])[0]
    path = tmp_path / "archive" / "2026" / "05" / "01.md"

    with pytest.raises(ValueError):
        read_record(path, record["offset"], record["length"] + 1)

    with path.open("r+b") as handle:
        handle.seek(record["body_offset"])
        handle.write(b"J")

    with pytest.raises(ValueError, match="sha256"):
        read_record(path, record["offset"], record["length"])
    assert any("sha256" in error for error in validate_archive(tmp_path / "archive"))


def test_rebuild_indexes_recovers_feed_files_from_daily_archives(tmp_path):
    records = append_articles(
        str(tmp_path),
        [
            (_meta(guid="g1"), "one\n"),
            (
                _meta(guid="g2", link="https://example.com/b", published="2026-05-02T01:00:00Z"),
                "two\n",
            ),
        ],
    )
    feeds_dir = tmp_path / "feeds"
    feed_meta = _feed_meta()
    for record in records:
        append_feed_record(feeds_dir, feed_meta, record)
    append_feed_annotation(
        feeds_dir,
        feed_meta,
        {
            "guid": "g1",
            "file": records[0]["file"],
            "offset": records[0]["offset"],
            "category": "Tech",
            "summary": "note",
            "score": 8,
            "classified_at": "2026-05-02T00:00:00Z",
        },
    )

    counts = rebuild_indexes(tmp_path / "archive", feeds_dir)

    assert counts == {
        "feeds": 1,
        "articles": 2,
        "files": 2,
        "duplicates": 0,
        "annotations_dropped": 1,
    }
    header, rebuilt = read_feed_index(feeds_dir / "feed-1.jsonl")
    assert header["feed_id"] == "feed-1"
    assert header["title"] == "Example Feed"
    assert [record["kind"] for record in rebuilt] == ["article", "article"]

    for record in rebuilt:
        parsed = read_record(tmp_path / record["file"], record["offset"], record["length"])
        assert parsed["sha256"] == record["sha256"]
        assert parsed["body"].strip() in {"one", "two"}


def test_reconcile_indexes_drops_stale_offsets_and_keeps_valid_ones(tmp_path):
    records = append_articles(
        str(tmp_path),
        [
            (_meta(guid="g1"), "one\n"),
            (_meta(guid="g2", link="https://example.com/b"), "two\n"),
        ],
    )
    feeds_dir = tmp_path / "feeds"
    feed_meta = _feed_meta()
    for record in records:
        append_feed_record(feeds_dir, feed_meta, record)
    append_feed_annotation(
        feeds_dir,
        feed_meta,
        {
            "guid": "g1",
            "file": records[0]["file"],
            "offset": records[0]["offset"],
            "category": "Tech",
            "summary": "note",
            "score": 8,
            "classified_at": "2026-05-02T00:00:00Z",
        },
    )

    path = feeds_dir / "feed-1.jsonl"
    header, entries = read_feed_index(path)
    entries[1]["offset"] = 10_000_000
    path.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in [header, *entries]
        )
        + "\n",
        encoding="utf-8",
    )

    stats = reconcile_indexes(tmp_path / "archive", feeds_dir)

    assert stats["dropped_articles"] == 1
    assert stats["annotations"] == 1
    header, entries = read_feed_index(path)
    assert [entry["kind"] for entry in entries] == ["article", "annotation"]
    assert entries[0]["guid"] == "g1"


def test_feed_meta_from_meta_keeps_feed_fields():
    meta = _meta()

    assert feed_meta_from_meta(meta) == {
        "feed_id": "feed-1",
        "feed_url": "https://example.com/feed.xml",
        "site_url": "https://example.com",
        "title": "",
    }


def _feed_two_meta(**overrides):
    return _feed_meta(
        **{
            "feed_id": "feed-2",
            "feed_url": "https://other.example.org/feed.xml",
            "site_url": "https://other.example.org",
            "title": "Other Feed",
            **overrides,
        }
    )


def _build_two_feed_archive(tmp_path):
    records = append_articles(
        str(tmp_path),
        [
            (
                _meta(
                    guid="g1",
                    link="https://example.com/a",
                    feed_title="Example Feed",
                ),
                "one\n",
            ),
            (
                _meta(guid="g2", link="https://example.com/b", feed_title="Example Feed"),
                "two\n",
            ),
            (
                _meta(
                    guid="g3",
                    link="https://other.example.org/c",
                    feed_id="feed-2",
                    feed_url="https://other.example.org/feed.xml",
                    site_url="https://other.example.org",
                    feed_title="Other Feed",
                    published="2026-05-02T01:00:00Z",
                ),
                "three\n",
            ),
        ],
    )
    feeds_dir = tmp_path / "feeds"
    append_feed_record(feeds_dir, _feed_meta(), records[0])
    append_feed_record(feeds_dir, _feed_meta(), records[1])
    append_feed_record(feeds_dir, _feed_two_meta(), records[2])
    return records, feeds_dir


def _drop_article_line(path, guid):
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [
        line
        for line in lines
        if not (
            json.loads(line).get("kind") == "article"
            and json.loads(line).get("guid") == guid
        )
    ]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def test_repair_indexes_restores_missing_article_and_preserves_annotations(tmp_path):
    records, feeds_dir = _build_two_feed_archive(tmp_path)
    feed_meta = _feed_meta()
    append_feed_annotation(
        feeds_dir,
        feed_meta,
        {
            "guid": "g1",
            "file": records[0]["file"],
            "offset": records[0]["offset"],
            "category": "Tech",
            "summary": "keep me",
            "score": 8,
            "classified_at": "2026-05-02T00:00:00Z",
        },
    )

    path = feeds_dir / "feed-1.jsonl"
    _drop_article_line(path, "g1")
    after_loss = path.read_bytes()

    stats = repair_indexes(tmp_path / "archive", feeds_dir)

    assert stats == {"articles": 3, "feeds": 2, "appended": 1, "skipped": 2}
    repaired = path.read_bytes()
    # Existing bytes stay untouched; the missing record is only appended.
    assert repaired.startswith(after_loss)
    header, entries = read_feed_index(path)
    assert header["title"] == "Example Feed"
    assert [entry["kind"] for entry in entries] == [
        "article",
        "annotation",
        "article",
    ]
    assert [entry["guid"] for entry in entries] == ["g2", "g1", "g1"]
    annotation = entries[1]
    assert annotation["summary"] == "keep me"
    assert annotation["category"] == "Tech"
    restored = entries[2]
    assert restored["offset"] == records[0]["offset"]
    assert restored["length"] == records[0]["length"]

    again = repair_indexes(tmp_path / "archive", feeds_dir)
    assert again == {"articles": 3, "feeds": 2, "appended": 0, "skipped": 3}
    assert path.read_bytes() == repaired


def test_repair_indexes_recreates_a_missing_index_file(tmp_path):
    _records, feeds_dir = _build_two_feed_archive(tmp_path)
    path = feeds_dir / "feed-2.jsonl"
    path.unlink()

    stats = repair_indexes(tmp_path, feeds_dir)

    assert stats == {"articles": 3, "feeds": 2, "appended": 1, "skipped": 2}
    header, entries = read_feed_index(path)
    assert header["kind"] == "feed"
    assert header["feed_id"] == "feed-2"
    assert header["title"] == "Other Feed"
    assert [entry["kind"] for entry in entries] == ["article"]
    assert entries[0]["guid"] == "g3"

    assert repair_indexes(tmp_path, feeds_dir)["appended"] == 0
