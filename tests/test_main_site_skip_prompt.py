import unittest

from src.main import _build_site_skip_prompt_rules, _resolve_site_skip_prompt


class MainSiteSkipPromptTests(unittest.TestCase):
    def test_resolve_site_skip_prompt_prefers_most_specific_prefix(self):
        feeds = [
            {"html_url": "https://example.com", "skip_prompt": "root-rule"},
            {"html_url": "https://example.com/blog", "skip_prompt": "blog-rule"},
            {"html_url": "https://other.com", "skip_prompt": "other-rule"},
        ]
        rules = _build_site_skip_prompt_rules(feeds)

        site, prompt = _resolve_site_skip_prompt("https://example.com/blog/post-1", rules)
        self.assertEqual("example.com/blog", site)
        self.assertEqual("blog-rule", prompt)

        site, prompt = _resolve_site_skip_prompt("https://example.com/news/post-2", rules)
        self.assertEqual("example.com", site)
        self.assertEqual("root-rule", prompt)

    def test_resolve_site_skip_prompt_no_match(self):
        feeds = [{"html_url": "https://example.com", "skip_prompt": "rule"}]
        rules = _build_site_skip_prompt_rules(feeds)

        site, prompt = _resolve_site_skip_prompt("https://unknown.com/x", rules)
        self.assertEqual("", site)
        self.assertEqual("", prompt)

    def test_resolve_site_skip_prompt_ignores_http_https(self):
        feeds = [{"html_url": "http://example.com/blog", "skip_prompt": "rule"}]
        rules = _build_site_skip_prompt_rules(feeds)

        site, prompt = _resolve_site_skip_prompt("https://example.com/blog/post-1", rules)
        self.assertEqual("example.com/blog", site)
        self.assertEqual("rule", prompt)


if __name__ == "__main__":
    unittest.main()
