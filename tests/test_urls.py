import unittest

from src.urls import canonical_url, is_safe_http_url


class CanonicalUrlTests(unittest.TestCase):
    def test_strips_scheme_www_fragment_tracking_and_sorts_query(self):
        url = "https://www.Example.com:443/Path/?utm_source=x&b=2&a=1#frag"
        self.assertEqual(canonical_url(url), "example.com/Path?a=1&b=2")

    def test_drops_default_ports_but_keeps_custom(self):
        self.assertEqual(canonical_url("http://example.com:80/a"), "example.com/a")
        self.assertEqual(canonical_url("https://example.com:8443/a"), "example.com:8443/a")

    def test_root_and_trailing_slash(self):
        self.assertEqual(canonical_url("https://example.com"), "example.com/")
        self.assertEqual(canonical_url("https://example.com/"), "example.com/")
        self.assertEqual(canonical_url("https://example.com/a/"), "example.com/a")

    def test_removes_all_tracking_params(self):
        url = "https://example.com/a?ref=1&source=x&spm=2&fbclid=z&gclid=q&z=9&a=2"
        self.assertEqual(canonical_url(url), "example.com/a?a=2&z=9")

    def test_invalid_urls(self):
        for url in ("", "javascript:alert(1)", "data:text/plain,x", "file:///etc/passwd",
                    "/relative", "ftp://example.com/x", "https://", "not a url"):
            self.assertEqual(canonical_url(url), "", url)


class SafeHttpUrlTests(unittest.TestCase):
    def test_accepts_http_and_https(self):
        self.assertTrue(is_safe_http_url("https://example.com/a"))
        self.assertTrue(is_safe_http_url("http://example.com"))

    def test_rejects_unsafe_or_relative(self):
        for url in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd",
                    "/relative", "//example.com/x", "ftp://example.com/x", "",
                    "https://exa mple.com", "java\nscript:alert(1)"):
            self.assertFalse(is_safe_http_url(url), url)


if __name__ == "__main__":
    unittest.main()
