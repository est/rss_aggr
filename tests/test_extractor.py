from unittest.mock import patch

import requests

from src import extractor


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8", status: int = 200):
        self.content = body
        self.headers = {"Content-Type": content_type}
        self.status_code = status
        self.ok = 200 <= status < 400
        self.encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size=64 * 1024):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index : index + chunk_size]

    def close(self):
        self.closed = True


def test_extract_rejects_non_public_url():
    with patch("src.extractor.requests.get") as get:
        assert extractor.extract_article_text("file:///etc/passwd") == ""
        assert extractor.extract_article_text("http://127.0.0.1/private") == ""
        get.assert_not_called()


def test_extract_returns_empty_for_network_error():
    with patch(
        "src.extractor.requests.get",
        side_effect=requests.ConnectionError("boom"),
    ):
        assert extractor.extract_article_text("https://example.com/post") == ""


def test_extract_returns_empty_for_non_html_response():
    response = _FakeResponse(b"%PDF-1.4", content_type="application/pdf")
    with patch("src.extractor.requests.get", return_value=response):
        assert extractor.extract_article_text("https://example.com/post") == ""


def test_extract_returns_markdown_text():
    html = b"<html><body><article><p>Hello world</p></article></body></html>"
    response = _FakeResponse(html)
    with patch("src.extractor.requests.get", return_value=response):
        with patch(
            "trafilatura.extract",
            return_value="# Hello\n\nworld " + "x" * 60,
        ) as extract:
            text = extractor.extract_article_text("https://example.com/post")
    assert text.startswith("# Hello\n\nworld")
    assert extract.called
    assert response.closed is True


def test_extract_falls_back_when_trafilatura_is_too_short():
    html = b"<html><body><article><p>Hi</p></article></body></html>"
    response = _FakeResponse(html)
    with patch("src.extractor.requests.get", return_value=response):
        with patch("trafilatura.extract", return_value="Hi"):
            assert extractor.extract_article_text("https://example.com/post") == ""


def test_extract_caps_text_length():
    html = b"<html><body><article><p>Long text</p></article></body></html>"
    response = _FakeResponse(html)
    with patch("src.extractor.requests.get", return_value=response):
        with patch("trafilatura.extract", return_value="x" * 1000):
            text = extractor.extract_article_text(
                "https://example.com/post", max_chars=100
            )
    assert len(text) <= 100 + 4
    assert text.endswith("…")
