import json
import tempfile
import unittest
from pathlib import Path

from src.articles import load_articles, save_articles, append_to_articles, cleanup_articles, get_article_links


class ArticlesTests(unittest.TestCase):
    def test_load_articles_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "articles.json")
            self.assertEqual(load_articles(path), [])

    def test_append_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "articles.json")
            articles = [
                {"link": "https://a.com/1", "title": "A1", "published": "2026-06-12T00:00:00Z"},
                {"link": "https://a.com/2", "title": "A2", "published": "2026-06-12T00:00:00Z"},
            ]
            added = append_to_articles(articles, path)
            self.assertEqual(added, 2)

            # Dedup: same links should not be added again
            added = append_to_articles(articles, path)
            self.assertEqual(added, 0)

            cache = load_articles(path)
            self.assertEqual(len(cache), 2)

    def test_cleanup_articles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "articles.json")
            articles = [
                {"link": "https://a.com/old", "title": "Old", "published": "2020-01-01T00:00:00Z"},
                {"link": "https://a.com/new", "title": "New", "published": "2099-01-01T00:00:00Z"},
            ]
            save_articles(articles, path)

            removed = cleanup_articles(keep_days=14, path=path)
            self.assertEqual(removed, 1)

            cache = load_articles(path)
            self.assertEqual(len(cache), 1)
            self.assertEqual(cache[0]["link"], "https://a.com/new")

    def test_get_article_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "articles.json")
            articles = [
                {"link": "https://a.com/1", "title": "A1"},
                {"link": "https://a.com/2", "title": "A2"},
            ]
            save_articles(articles, path)

            links = get_article_links(path)
            self.assertEqual(links, {"https://a.com/1", "https://a.com/2"})


if __name__ == "__main__":
    unittest.main()
