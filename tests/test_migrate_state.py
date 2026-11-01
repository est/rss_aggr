import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.article_state import ARTICLE_STATE_FILE, LEGACY_ARTICLE_STATE_FILE
from src.feed_state import FEED_STATE_FILE, LEGACY_FEED_STATE_FILE
from src.migrate_state import main


class MigrateStateTests(unittest.TestCase):
    def test_migrates_legacy_files_and_can_remove_them(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            Path(LEGACY_FEED_STATE_FILE).write_text(
                json.dumps({"feeds": {"https://a.com/feed": {"last_ok": "x"}}}),
                encoding="utf-8",
            )
            Path(LEGACY_ARTICLE_STATE_FILE).write_text(
                json.dumps([{"guid": "g", "link": "https://a.com/p", "status": "pending"}]),
                encoding="utf-8",
            )

            main([])

            feed = json.loads(Path(FEED_STATE_FILE).read_text(encoding="utf-8"))
            article = json.loads(Path(ARTICLE_STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(feed["v"], 1)
            self.assertEqual(article["v"], 1)
            self.assertEqual(len(article["articles"]), 1)
            self.assertTrue(Path(LEGACY_FEED_STATE_FILE).exists())
            self.assertTrue(Path(LEGACY_ARTICLE_STATE_FILE).exists())

            main(["--remove-legacy"])

            self.assertFalse(Path(LEGACY_FEED_STATE_FILE).exists())
            self.assertFalse(Path(LEGACY_ARTICLE_STATE_FILE).exists())
            self.assertTrue(Path(FEED_STATE_FILE).exists())
            self.assertTrue(Path(ARTICLE_STATE_FILE).exists())

    def test_empty_runtime_writes_valid_empty_files(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with patch("sys.stdout", new=io.StringIO()):
                main([])
            feed = json.loads(Path(FEED_STATE_FILE).read_text(encoding="utf-8"))
            article = json.loads(Path(ARTICLE_STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(feed["feeds"], {})
            self.assertEqual(article["articles"], [])


if __name__ == "__main__":
    unittest.main()
