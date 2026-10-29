"""URL helpers: canonical dedup keys and safety checks for untrusted links."""
from urllib.parse import parse_qsl, urlencode, urlsplit

TRACKING_PARAMS = {"fbclid", "gclid", "ref", "source", "spm"}
TRACKING_PREFIXES = ("utm_",)
DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_tracking(name: str) -> bool:
    key = (name or "").strip().lower()
    return key in TRACKING_PARAMS or any(key.startswith(p) for p in TRACKING_PREFIXES)


_UNSAFE_CHARS = frozenset('<>"\'`|')


def _has_unsafe_chars(value: str) -> bool:
    return any(
        ch.isspace() or ord(ch) < 0x20 or ch in _UNSAFE_CHARS for ch in value
    )


def canonical_url(url: str) -> str:
    """Return a stable dedup key for an http(s) URL, or "" when invalid.

    The key intentionally drops the scheme (http and https are treated as the
    same article), the leading ``www.``, default ports, fragments and common
    tracking parameters. Remaining query parameters are sorted and a trailing
    slash is removed (except the root path).
    """
    if not isinstance(url, str):
        return ""
    value = url.strip()
    if not value or _has_unsafe_chars(value):
        return ""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return ""

    try:
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return ""
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""

    netloc = host
    if port is not None and port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    if not path:
        path = "/"

    query_pairs = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking(key)
    ]
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)

    key = f"{netloc}{path}"
    if query:
        key = f"{key}?{query}"
    return key


def is_safe_http_url(url: str) -> bool:
    """Whether ``url`` is an absolute http(s) URL with a host and no raw HTML.

    Rejects javascript:, data:, file:, protocol-relative, relative and
    whitespace/control-character variants often used to smuggle markup.
    """
    if not isinstance(url, str):
        return False
    value = url.strip()
    if not value or _has_unsafe_chars(value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    return True
