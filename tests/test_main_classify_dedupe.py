import tempfile
import unittest
from pathlib import Path

from src.storage import collect_articles_for_links


class MainClassifyDedupeTests(unittest.TestCase):
    def test_collect_articles_deduplicates_by_link(self):
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
                        "| A | [T1](https://example.com/a) | — | — |",
                        "| B | [T2](https://example.com/a) | — | — |",
                        "| C | [T3](https://example.com/c) | — | — |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            articles = collect_articles_for_links(tmp, {"https://example.com/a", "https://example.com/c"})

            self.assertEqual(2, len(articles))
            self.assertEqual("https://example.com/a", articles[0]["link"])
            self.assertEqual("https://example.com/c", articles[1]["link"])


if __name__ == "__main__":
    unittest.main()
