import tempfile
import unittest
from pathlib import Path

from src.storage import load_unclassified_links, update_classifications


class StorageMarkdownParsingTests(unittest.TestCase):
    def test_load_unclassified_links_handles_escaped_pipe_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026"
            day.mkdir(parents=True, exist_ok=True)
            f = day / "0512.md"
            f.write_text(
                "\n".join(
                    [
                        "# 2026-05-12",
                        "",
                        "| Author | Title | Summary | Score |",
                        "|--------|-------|---------|-------|",
                        "| A | [U](https://example.com/u) | hello \\| world | — |",
                        "| B | [C](https://example.com/c) | hello \\| world | 9 |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            links = load_unclassified_links(tmp)

            self.assertIn("https://example.com/u", links)
            self.assertNotIn("https://example.com/c", links)

    def test_update_classifications_updates_row_with_escaped_pipe_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026"
            day.mkdir(parents=True, exist_ok=True)
            f = day / "0512.md"
            f.write_text(
                "\n".join(
                    [
                        "# 2026-05-12",
                        "",
                        "| Author | Title | Summary | Score |",
                        "|--------|-------|---------|-------|",
                        "| A | [T](https://example.com/a) | old \\| summary | — |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            updated = update_classifications(
                tmp,
                {
                    "https://example.com/a": {
                        "category": "Tech",
                        "summary": "new | summary",
                        "score": 8,
                    }
                },
            )

            self.assertEqual(updated, 1)
            text = f.read_text(encoding="utf-8")
            self.assertIn("| A | [T](https://example.com/a) | Tech | new \\| summary | 8 |", text)

    def test_load_unclassified_links_supports_new_category_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026"
            day.mkdir(parents=True, exist_ok=True)
            f = day / "0512.md"
            f.write_text(
                "\n".join(
                    [
                        "# 2026-05-12",
                        "",
                        "| Author | Title | Category | Summary | Score |",
                        "|--------|-------|----------|---------|-------|",
                        "| A | [U](https://example.com/u) | Tech | sum | — |",
                        "| B | [C](https://example.com/c) | Biz | sum | 7 |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            links = load_unclassified_links(tmp)

            self.assertIn("https://example.com/u", links)
            self.assertNotIn("https://example.com/c", links)


if __name__ == "__main__":
    unittest.main()
