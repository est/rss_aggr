import unittest

from src.urls import (
    canonical_url,
    is_safe_http_url,
    is_safe_public_http_url,
)


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


class SafePublicHttpUrlTests(unittest.TestCase):
    def test_accepts_public_hosts_and_ipv4_literals(self):
        for url in (
            "https://example.com/feed.xml",
            "http://news.example.org",
            "https://8.8.8.8/feed",
            "https://[2001:4860:4860::8888]/feed",
        ):
            self.assertTrue(is_safe_public_http_url(url), url)

    def test_rejects_private_loopback_link_local_and_reserved_ips(self):
        for url in (
            "http://127.0.0.1/feed",
            "http://127.1.2.3/feed",
            "http://10.0.0.5/feed",
            "http://172.16.0.1/feed",
            "http://192.168.1.10/feed",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/feed",
            "http://[::1]/feed",
            "http://[fe80::1]/feed",
            "http://[::ffff:127.0.0.1]/feed",
            "http://2130706433/feed",
            "http://0x7f000001/feed",
        ):
            self.assertFalse(is_safe_public_http_url(url), url)

    def test_rejects_internal_hostnames(self):
        for url in (
            "http://localhost/feed",
            "http://localhost.localdomain/feed",
            "http://printer.local/feed",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://service.internal/feed",
        ):
            self.assertFalse(is_safe_public_http_url(url), url)

    def test_rejects_non_http_and_missing_host(self):
        for url in (
            "",
            "ftp://example.com/feed",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "/relative",
            "https://",
        ):
            self.assertFalse(is_safe_public_http_url(url), url)


if __name__ == "__main__":
    unittest.main()
