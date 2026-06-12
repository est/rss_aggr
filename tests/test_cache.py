import json
import tempfile
import unittest
from pathlib import Path

from src.cache import load_cache, save_cache, append_to_cache, cleanup_cache, get_cached_links


class CacheTests(unittest.TestCase):
    def test_load_cache_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cache.json")
            self.assertEqual(load_cache(path), [])

    def test_append_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cache.json")
            articles = [
                {"link": "https://a.com/1", "title": "A1", "published": "2026-06-12T00:00:00Z"},
                {"link": "https://a.com/2", "title": "A2", "published": "2026-06-12T00:00:00Z"},
            ]
            added = append_to_cache(articles, path)
            self.assertEqual(added, 2)

            # Dedup: same links should not be added again
            added = append_to_cache(articles, path)
            self.assertEqual(added, 0)

            cache = load_cache(path)
            self.assertEqual(len(cache), 2)

    def test_cleanup_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cache.json")
            articles = [
                {"link": "https://a.com/old", "title": "Old", "published": "2020-01-01T00:00:00Z"},
                {"link": "https://a.com/new", "title": "New", "published": "2026-06-12T00:00:00Z"},
            ]
            save_cache(articles, path)

            removed = cleanup_cache(keep_days=14, path=path)
            self.assertEqual(removed, 1)

            cache = load_cache(path)
            self.assertEqual(len(cache), 1)
            self.assertEqual(cache[0]["link"], "https://a.com/new")

    def test_get_cached_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cache.json")
            articles = [
                {"link": "https://a.com/1", "title": "A1"},
                {"link": "https://a.com/2", "title": "A2"},
            ]
            save_cache(articles, path)

            links = get_cached_links(path)
            self.assertEqual(links, {"https://a.com/1", "https://a.com/2"})


if __name__ == "__main__":
    unittest.main()
