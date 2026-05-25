import unittest


class MainFetchSkipFilterTests(unittest.TestCase):
    def test_filter_new_entries_excludes_skipped_links(self):
        seen_links = {"https://example.com/seen"}
        skipped_links = {"https://example.com/skip"}
        all_entries = [
            {"link": "https://example.com/new"},
            {"link": "https://example.com/seen"},
            {"link": "https://example.com/skip"},
        ]

        new_entries = [
            e for e in all_entries
            if e.get("link") not in seen_links and e.get("link") not in skipped_links
        ]

        self.assertEqual([{"link": "https://example.com/new"}], new_entries)


if __name__ == "__main__":
    unittest.main()
