"""URL helpers: canonical dedup keys and safety checks for untrusted links."""
import ipaddress
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


_INTERNAL_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)
_INTERNAL_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def is_safe_public_http_url(url: str) -> bool:
    """Whether ``url`` is an http(s) URL pointing at a public internet host.

    This is a syntactic SSRF guard: it never resolves DNS. It rejects missing
    hosts, obvious internal hostnames (``localhost``, ``*.local``,
    ``metadata.google.internal``, ...) and IP literals in loopback, private,
    link-local, reserved, multicast or unspecified ranges.
    """
    if not is_safe_http_url(url):
        return False
    try:
        host = (urlsplit(url.strip()).hostname or "").strip().lower()
    except ValueError:
        return False
    host = host.rstrip(".")
    if not host:
        return False
    if host in _INTERNAL_HOSTNAMES or host.endswith(_INTERNAL_HOST_SUFFIXES):
        return False

    # Alternate IPv4 spellings (``2130706433``, ``0x7f000001``) are not
    # parsed as addresses by ipaddress but are resolved by many stacks.
    if host.isdigit() or host.startswith("0x"):
        return False

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A regular DNS name; DNS is deliberately not resolved here.
        return True

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )
