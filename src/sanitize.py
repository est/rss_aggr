"""Sanitize untrusted feed HTML and convert it to durable Markdown.

Feed HTML is hostile input: it may contain scripts, forms, event handlers,
``javascript:`` URLs, tracking pixels, broken markup and non-UTF-8 leftovers.
This module strips all of that before handing the result to ``html2text``, so
the archived Markdown is safe to render later and stable across runs.
"""
from __future__ import annotations

import html as html_lib
import re

import bleach
import html2text

# Tags that carry normal article content and are safe to keep.
ALLOWED_TAGS = [
    "p",
    "br",
    "strong",
    "b",
    "em",
    "i",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "img",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
    "sup",
    "sub",
    "del",
    "ins",
]

ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title", "width", "height"],
    "th": ["align", "colspan", "rowspan"],
    "td": ["align", "colspan", "rowspan"],
}

# Only web/mail links survive. This also rejects javascript:, data:, vbscript:
# and file: URLs because bleach validates the protocol of every URL attribute.
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

# Elements whose complete contents are never article text.
_DROP_BLOCKS = (
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "form",
    "noscript",
    "template",
    "svg",
    "math",
    "button",
)

# Separate elements that are unambiguously non-content and self-closing-ish.
_DROP_TAGS = ("input", "link", "meta", "base")

_DROP_BLOCK_RE = re.compile(
    r"<(?P<tag>" + "|".join(_DROP_BLOCKS) + r")\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
# An unclosed <script>/<style> is treated as running to the end of the input;
# leaving its body behind would embed executable-looking text in the Markdown.
_DROP_UNCLOSED_RE = re.compile(
    r"<(script|style)\b[^>]*>.*\Z", re.IGNORECASE | re.DOTALL
)
_DROP_OPEN_RE = re.compile(
    r"</?(?:" + "|".join(_DROP_BLOCKS) + r")\b[^>]*>", re.IGNORECASE
)
_DROP_TAG_RE = re.compile(
    r"</?(?:" + "|".join(_DROP_TAGS) + r")\b[^>]*>", re.IGNORECASE
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_UNCLOSED_COMMENT_RE = re.compile(r"<!--.*\Z", re.DOTALL)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_DIMENSION_RE = re.compile(r"\b(?:width|height)\s*=\s*[\"']?(\d+)", re.IGNORECASE)
_HIDDEN_STYLE_RE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.IGNORECASE
)
_CRLF_RE = re.compile(r"\r\n?")
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")
# Defense in depth: even if html2text turned escaped code into raw markup, the
# archived Markdown must never contain an executable element.
_DANGEROUS_MARKUP_RE = re.compile(
    r"<\s*/?\s*(?:script|style|iframe|object|embed|form|input|button)\b",
    re.IGNORECASE,
)
_DANGEROUS_LINK_RE = re.compile(
    r"(\]\(\s*)(?:javascript|data|vbscript|file)\s*:[^)]*\)",
    re.IGNORECASE,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _drop_tracking_pixels(match: re.Match[str]) -> str:
    """Return an empty string for 1x1/0x0 or hidden images, else the tag."""
    tag = match.group(0)
    if _HIDDEN_STYLE_RE.search(tag):
        return ""
    dimensions = [int(value) for value in _DIMENSION_RE.findall(tag)]
    if dimensions and all(value <= 1 for value in dimensions):
        return ""
    return tag


def _pre_clean(html: str) -> str:
    """Remove elements bleach cannot safely neutralize on its own."""
    text = _COMMENT_RE.sub("", html)
    text = _UNCLOSED_COMMENT_RE.sub("", text)
    # Paired blocks first (repeatedly, to handle nesting of similar tags).
    for _ in range(3):
        cleaned = _DROP_BLOCK_RE.sub("", text)
        if cleaned == text:
            break
        text = cleaned
    text = _DROP_UNCLOSED_RE.sub("", text)
    text = _DROP_OPEN_RE.sub("", text)
    text = _DROP_TAG_RE.sub("", text)
    text = _IMG_RE.sub(_drop_tracking_pixels, text)
    return text


def _normalize(markdown: str) -> str:
    """Normalize line endings/blank runs and guarantee one final newline."""
    text = _CRLF_RE.sub("\n", markdown)
    text = _CONTROL_RE.sub("", text)
    text = _EXCESS_NEWLINES_RE.sub("\n\n", text)
    text = text.strip()
    if not text:
        return ""
    return text + "\n"


def _scrub_markdown(markdown: str) -> str:
    """Remove executable markup/link protocols that slipped through as text."""
    text = _DANGEROUS_MARKUP_RE.sub(lambda match: "&lt;" + match.group(0)[1:], markdown)
    text = _DANGEROUS_LINK_RE.sub(r"\1#)", text)
    return text


def _plain_text_fallback(raw: str) -> str:
    """Last-resort conversion: escaped, tag-free plain text."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", "", raw)
    text = re.sub(r"<[^>]*>", " ", text)
    text = html_lib.unescape(text)
    return _normalize(html_lib.escape(text, quote=False))


def sanitize_text(value: str) -> str:
    """Strip markup from a short metadata field (title/author/feed name).

    Titles are stored in the JSON archive header and later HTML-escaped; this
    keeps the archive itself free of raw tags and control characters.
    """
    if value is None:
        return ""
    text = str(value)
    text = _CRLF_RE.sub(" ", text)
    text = _CONTROL_RE.sub("", text)
    text = bleach.clean(
        text,
        tags=[],
        attributes={},
        protocols=[],
        strip=True,
        strip_comments=True,
    )
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def sanitize_html(html: str, base_url: str = "") -> str:
    """Sanitize untrusted feed HTML and return deterministic Markdown.

    Relative links and images are resolved against ``base_url``. The result
    always ends with exactly one newline (or is empty) and never contains
    scripts, event handlers or ``javascript:``/``data:`` link targets.
    Malformed input never raises; conversion failures degrade to escaped text.
    """
    if html is None:
        return ""
    if not isinstance(html, str):
        html = str(html)
    raw = _CRLF_RE.sub("\n", html)
    if not raw.strip():
        return ""

    try:
        cleaned = bleach.clean(
            _pre_clean(raw),
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            protocols=ALLOWED_PROTOCOLS,
            strip=True,
            strip_comments=True,
        )

        converter = html2text.HTML2Text()
        converter.body_width = 0
        converter.baseurl = base_url or ""
        converter.ignore_links = False
        converter.ignore_images = False
        converter.ignore_emphasis = False
        converter.protect_links = False
        converter.unicode_snob = True
        converter.skip_internal_links = False
        converter.single_line_break = False
        converter.wrap_links = False
        converter.decode_errors = "replace"
        markdown = converter.handle(cleaned)
    except Exception:  # noqa: BLE001 - hostile input must never break ingest
        return _plain_text_fallback(raw)

    return _normalize(_scrub_markdown(markdown))
