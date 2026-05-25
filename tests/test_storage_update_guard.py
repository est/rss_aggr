import tempfile
import unittest
from pathlib import Path

from src.storage import load_unclassified_links_map, update_classifications


class StorageUpdateGuardTests(unittest.TestCase):
    def test_update_classifications_updates_only_requested_links(self):
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
                        "| A | [A](https://example.com/a) | Tech | old-a | 9 |",
                        "| B | [B](https://example.com/b) | — | — | — |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            unclassified_map = load_unclassified_links_map(tmp)
            only_links = set()
            for links in unclassified_map.values():
                only_links.update(links)

            updated = update_classifications(
                tmp,
                {
                    "https://example.com/a": {"category": "Life", "summary": "new-a", "score": 1},
                    "https://example.com/b": {"category": "Tech", "summary": "new-b", "score": 8},
                },
                only_links=only_links,
            )

            self.assertEqual(1, updated)
            text = f.read_text(encoding="utf-8")
            self.assertIn("| A | [A](https://example.com/a) | Tech | old-a | 9 |", text)
            self.assertIn("| B | [B](https://example.com/b) | Tech | new-b | 8 |", text)

    def test_update_classifications_skips_incomplete_classification(self):
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
                        "| B | [B](https://example.com/b) | — | — | — |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            updated = update_classifications(
                tmp,
                {"https://example.com/b": {"category": "Tech", "summary": "", "score": 8}},
                only_links={"https://example.com/b"},
            )

            self.assertEqual(0, updated)
            text = f.read_text(encoding="utf-8")
            self.assertIn("| B | [B](https://example.com/b) | — | — | — |", text)


if __name__ == "__main__":
    unittest.main()
