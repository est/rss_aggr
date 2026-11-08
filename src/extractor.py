"""Best-effort full-text extraction for feeds that only publish excerpts.

The durable archive normally stores the content supplied by the feed. Many
feeds only include a one-line summary or an image, so this module optionally
fetches the linked page and extracts its main text. It is intentionally
best-effort: any network/parse failure returns an empty string and the caller
keeps the original feed body.
"""
from __future__ import annotations

import re

import requests

try:  # pragma: no cover - fallback for older checkouts during refactors
    from src.urls import is_safe_public_http_url
except ImportError:  # pragma: no cover
    from src.urls import is_safe_http_url as is_safe_public_http_url

DEFAULT_USER_AGENT = "rss_aggr/1.0 (+https://github.com/est/rss_aggr/)"
DEFAULT_TIMEOUT = 10
MAX_BYTES = 2_000_000
MAX_TEXT_CHARS = 100_000
MIN_EXTRACTED_CHARS = 40

_TEXT_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_CHARSET_RE = re.compile(r"charset=([^\s;]+)", re.IGNORECASE)


def _looks_like_html(html: str) -> bool:
    head = html[:2048].lower()
    return "<html" in head or "<body" in head or "<article" in head


def extract_article_text(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
    min_chars: int = MIN_EXTRACTED_CHARS,
    max_chars: int = MAX_TEXT_CHARS,
) -> str:
    """Fetch ``url`` and return its main text as Markdown, or ``""``.

    The URL must be a public http(s) URL. Response size is capped so a broken
    or hostile page cannot exhaust the runner. Any failure is swallowed: this
    is an enhancement, never a reason to fail the fetch step.
    """
    if not is_safe_public_http_url(url):
        return ""

    try:
        resp = requests.get(
            url,
            timeout=(5, timeout),
            headers={"User-Agent": user_agent},
            stream=True,
        )
    except requests.RequestException:
        return ""

    try:
        if not resp.ok:
            return ""
        # Re-check after redirects: a public URL could redirect to an internal
        # address. The request has already been made, but refuse to read/parse
        # such a response.
        if not is_safe_public_http_url(getattr(resp, "url", "") or url):
            return ""
        content_type = resp.headers.get("Content-Type", "").lower()
        if content_type and not any(kind in content_type for kind in _TEXT_CONTENT_TYPES):
            return ""

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            remaining = MAX_BYTES - total
            if remaining <= 0:
                break
            chunks.append(chunk[:remaining])
            total += len(chunks[-1])
            if total >= MAX_BYTES:
                break
        raw = b"".join(chunks)
    finally:
        resp.close()

    if not raw:
        return ""

    try:
        html = raw.decode("utf-8", errors="replace")
        if not _looks_like_html(html):
            return ""
        # Respect a declared charset when present.
        match = _CHARSET_RE.search(resp.headers.get("Content-Type", ""))
        if match:
            try:
                html = raw.decode(match.group(1).strip(), errors="replace")
            except (LookupError, UnicodeError):
                pass
    except Exception:  # noqa: BLE001 - extraction is best-effort
        return ""

    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=False,
            include_formatting=True,
            include_links=True,
            favor_precision=True,
        )
    except Exception:  # noqa: BLE001 - optional dependency / parser failure
        return ""

    text = (extracted or "").strip()
    if len(text) < min_chars:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n…"
    return text


def is_public_http_url(url: str) -> bool:
    """Compatibility wrapper used by callers/tests."""
    return is_safe_public_http_url(url)


def looks_like_html(text: str) -> bool:
    """Small helper kept public for tests."""
    return _looks_like_html(text)
