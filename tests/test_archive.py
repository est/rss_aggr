import json
import tempfile
import unittest
from pathlib import Path

from src.archive import (
    METADATA_FIELDS,
    append_metadata,
    metadata_record,
    repair_archive_dir,
    validate_archive_dir,
    write_snapshot,
)

RAW_BODY = "TOP SECRET RAW BODY that must never reach git"


def _article(**overrides) -> dict:
    article = {
        "guid": "g-1",
        "title": "Hello",
        "link": "https://example.com/a?utm_source=feed",
        "author": "Author",
        "published": "2026-05-01T09:00:00Z",
        "feed_title": "Example Feed",
        "feed_category": "Tech",
        "classification": {"category": "Tech", "score": 8, "summary": "sum"},
        "classification_meta": {"model": "m"},
        "status": "classified",
        "classified_at": "2026-05-01T10:00:00Z",
        "unclassified_reason": "",
        "unclassified_at": "",
        "cached_at": "2026-05-01T08:00:00Z",
        "canonical": "example.com/a",
        "attempts": 0,
        "skip_reason": "",
        "content": RAW_BODY,
        "content_length": len(RAW_BODY),
    }
    article.update(overrides)
    return article


class MetadataRecordTests(unittest.TestCase):
    def test_strips_raw_content(self):
        article = _article(extra="drop me")
        record = metadata_record(article)

        self.assertNotIn("content", record)
        self.assertNotIn("content_length", record)
        self.assertNotIn("extra", record)
        for field in METADATA_FIELDS:
            self.assertIn(field, record)

        self.assertNotIn(RAW_BODY, json.dumps(record))
        # The source article must not be mutated.
        self.assertEqual(article["content"], RAW_BODY)
        self.assertIn("content", article)


class AppendMetadataTests(unittest.TestCase):
    def test_writes_per_day_compact_jsonl_and_dedups(self):
        first = _article()
        second = _article(
            guid="g-2",
            link="https://example.com/b",
            cached_at="2026-05-02T01:00:00Z",
            canonical="example.com/b",
        )

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive"
            self.assertEqual(append_metadata([first, second], str(archive)), 2)

            day_one = archive / "2026-05-01.jsonl"
            day_two = archive / "2026-05-02.jsonl"
            self.assertTrue(day_one.exists())
            self.assertTrue(day_two.exists())

            line = day_one.read_text(encoding="utf-8").splitlines()[0]
            expected = json.dumps(
                metadata_record(first), ensure_ascii=False, separators=(",", ":")
            )
            self.assertEqual(line, expected)
            self.assertNotIn(": ", line)
            self.assertNotIn(", ", line)
            self.assertNotIn(RAW_BODY, line)

            # Same article with tracking noise + www is a duplicate.
            duplicate = _article(link="https://www.example.com/a")
            self.assertEqual(append_metadata([duplicate], str(archive)), 0)
            self.assertEqual(len(day_one.read_text(encoding="utf-8").splitlines()), 1)

    def test_missing_cached_at_falls_back_to_today(self):
        article = _article(cached_at="")

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(append_metadata([article], tmp), 1)
            files = sorted(Path(tmp).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            self.assertRegex(files[0].name, r"^\d{4}-\d{2}-\d{2}\.jsonl$")

    def test_corrupt_jsonl_line_does_not_break_dedup(self):
        article = _article()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-05-01.jsonl"
            valid = json.dumps(
                metadata_record(article), ensure_ascii=False, separators=(",", ":")
            )
            # Second line is partial/truncated and has no trailing newline.
            path.write_text(valid + "\n" + '{"link":"https://example.com/part', encoding="utf-8")

            new_article = _article(
                guid="g-2",
                link="https://example.com/b",
                canonical="example.com/b",
            )
            self.assertEqual(append_metadata([article, new_article], tmp), 1)

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0], valid)
            self.assertEqual(
                lines[2],
                json.dumps(
                    metadata_record(new_article),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )

            # Appending again must dedup against both the valid and partial rows.
            self.assertEqual(append_metadata([new_article], tmp), 0)


class ValidateArchiveTests(unittest.TestCase):
    def test_flags_partial_lines_and_raw_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)
            (archive / "2026-05-01.jsonl").write_text(
                json.dumps(metadata_record(_article()), ensure_ascii=False) + "\n"
                + '{"link":"https://example.com/part\n'
                + json.dumps({"link": "https://example.com/raw", "content": RAW_BODY})
                + "\n",
                encoding="utf-8",
            )
            errors = validate_archive_dir(str(archive))
            self.assertTrue(any("invalid JSON" in e for e in errors))
            self.assertTrue(any("raw content" in e for e in errors))

    def test_repair_drops_partial_and_raw_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)
            valid = json.dumps(metadata_record(_article()), ensure_ascii=False)
            (archive / "2026-05-01.jsonl").write_text(
                valid + "\n"
                + '{"link":"https://example.com/part\n'
                + json.dumps({"link": "https://example.com/raw", "content": RAW_BODY})
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(repair_archive_dir(str(archive)), 2)
            self.assertEqual(validate_archive_dir(str(archive)), [])
            self.assertEqual(
                (archive / "2026-05-01.jsonl").read_text(encoding="utf-8"),
                valid + "\n",
            )

    def test_clean_archive_has_no_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)
            append_metadata([_article()], str(archive))
            self.assertEqual(validate_archive_dir(str(archive)), [])


class WriteSnapshotTests(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp) / "root"
        (root / "output" / "2026").mkdir(parents=True)
        (root / "output" / "2026" / "0501.md").write_text(
            "# 2026-05-01\n\n| a | b |\n", encoding="utf-8"
        )
        (root / "archive").mkdir()
        archive_record = metadata_record(_article())
        (root / "archive" / "2026-05-01.jsonl").write_text(
            json.dumps(archive_record, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (root / "state.json").write_text('{"feeds":{}}', encoding="utf-8")
        (root / "status.json").write_text('{"ok":true}', encoding="utf-8")
        (root / "articles.json").write_text(
            json.dumps([_article(), _article(guid="g-2", link="https://example.com/b")]),
            encoding="utf-8",
        )
        return root

    def test_snapshot_is_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            dest = Path(tmp) / "snapshot"

            manifest = write_snapshot(str(dest), root=str(root))

            self.assertEqual(
                manifest,
                {"articles": 2, "archive_files": 1, "output_files": 1},
            )
            self.assertTrue((dest / "output" / "2026" / "0501.md").is_file())
            self.assertTrue((dest / "archive" / "2026-05-01.jsonl").is_file())
            self.assertTrue((dest / "state.json").is_file())
            self.assertTrue((dest / "status.json").is_file())

            articles = json.loads((dest / "articles.json").read_text(encoding="utf-8"))
            self.assertEqual(len(articles), 2)
            for record in articles:
                self.assertNotIn("content", record)
                self.assertNotIn("content_length", record)

            expected = json.dumps(articles, ensure_ascii=False, separators=(",", ":"))
            self.assertEqual(
                (dest / "articles.json").read_text(encoding="utf-8"), expected
            )

            for path in dest.rglob("*"):
                if path.is_file():
                    self.assertNotIn(
                        RAW_BODY, path.read_text(encoding="utf-8", errors="ignore")
                    )

    def test_missing_optional_paths_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            dest = Path(tmp) / "snapshot"

            manifest = write_snapshot(str(dest), root=str(root))

            self.assertEqual(
                manifest, {"articles": 0, "archive_files": 0, "output_files": 0}
            )
            self.assertFalse((dest / "articles.json").exists())


if __name__ == "__main__":
    unittest.main()
