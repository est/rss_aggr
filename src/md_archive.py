"""Durable, append-only Markdown archive plus per-feed offset indexes.

Daily archive files live at ``<root>/archive/YYYY/MM/DD.md`` (or
``<root>/YYYY/MM/DD.md`` when ``root`` itself is named ``archive``). Each
article is framed by an HTML comment header containing compact JSON and a
closing marker. Offsets and lengths are UTF-8 *byte* offsets, so multibyte
content (CJK, emoji) is handled correctly.

Per-feed indexes live at ``<feeds_dir>/<feed_id>.jsonl``: the first line is a
feed header, followed by append-only article and annotation records. Indexes
are derived data and can be rebuilt from the daily archive files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlsplit

from src.urls import canonical_url

try:  # pragma: no cover - only absent on non-POSIX platforms
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

MAGIC = b"<!-- rss-aggr:article v1\n"
JSON_END = b"-->\n"
TRAILER = b"<!-- /rss-aggr -->\n"

ARTICLE_META_FIELDS = (
    "guid",
    "feed_id",
    "feed_url",
    "site_url",
    "feed_title",
    "feed_category",
    "link",
    "title",
    "author",
    "published",
    "fetched_at",
)

_DAY_FILE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}\.md$")
_JSON_DASHES = b"--"
_JSON_DASH_ESCAPE = b"\\u002d\\u002d"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
@contextmanager
def _locked(handle) -> Iterator[None]:
    """Exclusive advisory lock for an open file handle (best effort)."""
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _text(value) -> str:
    """Coerce metadata to a JSON-safe string without producing ``None``."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_dt(value) -> datetime | None:
    """Parse an ISO timestamp (or epoch number) into an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _article_day(meta: dict) -> str:
    """Published day (UTC), then fetched_at/cached_at, then today."""
    for field in ("published", "fetched_at", "cached_at"):
        parsed = _parse_dt(meta.get(field))
        if parsed is not None:
            return parsed.strftime("%Y-%m-%d")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def _encode_header(meta: dict) -> bytes:
    """Compact UTF-8 JSON with ``--`` escaped so the HTML comment stays intact."""
    raw = _json_line(meta).encode("utf-8")
    return raw.replace(_JSON_DASHES, _JSON_DASH_ESCAPE)


def _archive_dir(root: str | os.PathLike) -> Path:
    """Resolve the daily-file directory from a project root or archive root."""
    base = Path(root)
    if base.name == "archive":
        return base
    return base / "archive"


def _file_relpath(path: Path, archive_dir: Path) -> str:
    """Archive-relative path stored in index records (e.g. archive/2026/05/01.md)."""
    try:
        rel = path.relative_to(archive_dir)
    except ValueError:
        rel = Path(path.name)
    if archive_dir.name:
        return (Path(archive_dir.name) / rel).as_posix()
    return rel.as_posix()


def _body_bytes(body) -> bytes:
    if isinstance(body, bytes):
        return body
    if body is None:
        return b""
    return str(body).encode("utf-8")


def _record_meta(meta: dict, body: bytes) -> dict:
    """Build the JSON header for one article record."""
    record = {"v": 1}
    for field in ARTICLE_META_FIELDS:
        value = meta.get(field, "")
        if field == "fetched_at" and not value:
            value = meta.get("cached_at", "")
        record[field] = _text(value)
    if not record["feed_id"]:
        record["feed_id"] = feed_id(
            record["feed_url"], record["site_url"], record["title"]
        )
    record["body_len"] = len(body)
    record["body_sha256"] = _sha256(body)
    return record


def _build_record(meta: dict, body: bytes) -> tuple[dict, bytes, int, int]:
    """Return ``(header_meta, record_bytes, body_offset, body_len)``."""
    header_meta = _record_meta(meta, body)
    header = MAGIC + _encode_header(header_meta) + b"\n" + JSON_END
    separator = b"" if body.endswith(b"\n") else b"\n"
    record = header + body + separator + TRAILER
    return header_meta, record, len(header), len(body)


# ---------------------------------------------------------------------------
# Record scanning / reading / repair
# ---------------------------------------------------------------------------
def _scan_file(path: str | os.PathLike) -> tuple[list[dict], dict | None]:
    """Scan a daily file, returning complete records and a trailing error.

    ``error`` is ``None`` for a clean file. Any incomplete or unparseable
    trailing record stops the scan; already-complete records are preserved.
    """
    records: list[dict] = []
    error: dict | None = None
    try:
        handle = open(path, "rb")
    except OSError as exc:
        return [], {"offset": 0, "reason": f"cannot open: {exc}"}

    with handle:
        size = os.fstat(handle.fileno()).st_size
        pos = 0
        while pos < size:
            start = pos
            handle.seek(pos)
            magic = handle.readline()
            if magic != MAGIC:
                error = {"offset": start, "reason": "invalid or partial record header"}
                break

            json_line = handle.readline()
            if not json_line.endswith(b"\n"):
                error = {"offset": start, "reason": "partial record metadata"}
                break
            try:
                meta = json.loads(json_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                error = {"offset": start, "reason": "invalid record metadata JSON"}
                break
            if not isinstance(meta, dict):
                error = {"offset": start, "reason": "record metadata is not an object"}
                break
            body_len = meta.get("body_len")
            if not isinstance(body_len, int) or isinstance(body_len, bool) or body_len < 0:
                error = {"offset": start, "reason": "invalid body_len"}
                break

            delimiter = handle.readline()
            if delimiter != JSON_END:
                error = {"offset": start, "reason": "missing record header terminator"}
                break

            body_offset = handle.tell()
            body = handle.read(body_len)
            if len(body) != body_len:
                error = {"offset": start, "reason": "truncated body"}
                break

            after_body = body_offset + body_len
            handle.seek(after_body)
            marker = handle.read(len(TRAILER) + 1)
            if marker.startswith(TRAILER):
                end = after_body + len(TRAILER)
            elif marker.startswith(b"\n" + TRAILER):
                end = after_body + 1 + len(TRAILER)
            else:
                error = {"offset": start, "reason": "missing record terminator"}
                break

            try:
                body_text = body.decode("utf-8")
            except UnicodeDecodeError:
                error = {"offset": start, "reason": "body is not valid UTF-8"}
                break

            records.append(
                {
                    "meta": meta,
                    "body": body_text,
                    "offset": start,
                    "length": end - start,
                    "body_offset": body_offset,
                    "body_len": body_len,
                    "sha256": _sha256(body),
                }
            )
            pos = end
    return records, error


def iter_records(path: str | os.PathLike) -> Iterator[dict]:
    """Yield complete records; silently stop at an incomplete trailing record."""
    records, _error = _scan_file(path)
    yield from records


def _parse_record(data: bytes, offset: int, length: int | None) -> dict:
    """Parse one record from ``data`` starting at its first byte."""
    if not data.startswith(MAGIC):
        raise ValueError("not an rss-aggr article record")
    cursor = len(MAGIC)
    newline = data.find(b"\n", cursor)
    if newline < 0:
        raise ValueError("incomplete record metadata")
    try:
        meta = json.loads(data[cursor:newline].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid record metadata: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("record metadata is not an object")

    body_len = meta.get("body_len")
    if not isinstance(body_len, int) or isinstance(body_len, bool) or body_len < 0:
        raise ValueError("invalid body_len")

    if data[newline + 1 : newline + 1 + len(JSON_END)] != JSON_END:
        raise ValueError("missing record header terminator")
    body_offset = newline + 1 + len(JSON_END)
    body_end = body_offset + body_len
    if len(data) < body_end:
        raise ValueError("truncated body")

    body = data[body_offset:body_end]
    if len(body) != body_len:
        raise ValueError("body length mismatch")

    rest = data[body_end:]
    if rest.startswith(TRAILER):
        record_end = body_end + len(TRAILER)
    elif rest.startswith(b"\n" + TRAILER):
        record_end = body_end + 1 + len(TRAILER)
    else:
        raise ValueError("missing record terminator")

    actual_length = record_end
    if length is not None and actual_length != length:
        raise ValueError(
            f"record length mismatch: expected {length}, found {actual_length}"
        )

    try:
        body_text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("body is not valid UTF-8") from exc

    digest = _sha256(body)
    if digest != meta.get("body_sha256"):
        raise ValueError("body sha256 mismatch")

    return {
        "meta": meta,
        "body": body_text,
        "offset": offset,
        "length": actual_length,
        "body_offset": offset + body_offset,
        "body_len": body_len,
        "sha256": digest,
    }


def read_record(
    path: str | os.PathLike, offset: int, length: int | None = None
) -> dict:
    """Read and verify one record at ``offset`` (bytes), raising on mismatch."""
    if offset < 0:
        raise ValueError("offset must be non-negative")
    with open(path, "rb") as handle:
        handle.seek(offset)
        if length is None:
            data = handle.read()
        else:
            if length < 0:
                raise ValueError("length must be non-negative")
            data = handle.read(length)
            if len(data) != length:
                raise ValueError("short read: record is truncated")
    return _parse_record(data, offset, length)


def _has_valid_record_after(path: Path, offset: int) -> bool:
    """Return True when a parseable record starts after ``offset``.

    Used to distinguish a recoverable partial tail from mid-file corruption.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    cursor = data.find(MAGIC, max(0, offset + 1))
    while cursor >= 0:
        try:
            _parse_record(data[cursor:], cursor, None)
            return True
        except ValueError:
            cursor = data.find(MAGIC, cursor + 1)
    return False


def repair_tail(path: str | os.PathLike) -> int:
    """Truncate only an incomplete/unparseable tail; return bytes removed.

    Mid-file corruption is never truncated: valid records after the error mean
    the file is not just a partial append, so the caller must fail closed and
    use a backup/rebuild instead.
    """
    target = Path(path)
    if not target.exists():
        return 0
    try:
        handle = target.open("r+b")
    except OSError:
        return 0
    with handle:
        with _locked(handle):
            size = os.fstat(handle.fileno()).st_size
            records, error = _scan_file(target)
            if error is None:
                return 0
            if _has_valid_record_after(target, error["offset"]):
                raise ValueError(
                    f"{target}:{error['offset']}: mid-file corruption; refusing to truncate"
                )
            last_good = records[-1]["offset"] + records[-1]["length"] if records else 0
            if size <= last_good:
                return 0
            handle.truncate(last_good)
            handle.flush()
            os.fsync(handle.fileno())
            return size - last_good


# ---------------------------------------------------------------------------
# Daily archive appends
# ---------------------------------------------------------------------------
def append_articles(
    root: str | os.PathLike, items: Iterable[tuple[dict, str]]
) -> list[dict]:
    """Append ``(article_meta, body_markdown)`` items to daily files.

    ``root`` is normally the project root (files go to ``root/archive/...``);
    when its final component is ``archive`` it is used directly. Returns one
    index record per appended article, in input order.
    """
    archive_dir = _archive_dir(root)
    groups: dict[Path, list[tuple[int, dict, bytes]]] = {}
    ordered: list[tuple[dict, str]] = list(items or [])

    for position, (meta, body) in enumerate(ordered):
        if not isinstance(meta, dict):
            continue
        day = _article_day(meta)
        path = archive_dir / day[:4] / day[5:7] / f"{day[8:10]}.md"
        groups.setdefault(path, []).append((position, meta, _body_bytes(body)))

    results: list[dict | None] = [None] * len(ordered)
    for path in sorted(groups):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size:
            repair_tail(path)
        with path.open("ab") as handle:
            with _locked(handle):
                for position, meta, body in groups[path]:
                    header_meta, record, body_offset, body_len = _build_record(
                        meta, body
                    )
                    handle.seek(0, os.SEEK_END)
                    offset = handle.tell()
                    handle.write(record)
                    handle.flush()
                    os.fsync(handle.fileno())

                    # Keep the index deliberately small; feed-level fields are
                    # in the first JSONL line, and body integrity is also
                    # recorded in the daily archive header.
                    index_record = {
                        "kind": "article",
                        "v": 1,
                        "feed_id": header_meta["feed_id"],
                        "file": _file_relpath(path, archive_dir),
                        "offset": offset,
                        "length": len(record),
                        "body_offset": offset + body_offset,
                        "body_len": body_len,
                        "sha256": header_meta["body_sha256"],
                        "guid": header_meta["guid"],
                        "link": header_meta["link"],
                        "title": header_meta["title"],
                        "author": header_meta["author"],
                        "published": header_meta["published"],
                    }
                    results[position] = index_record

    return [record for record in results if record is not None]


# ---------------------------------------------------------------------------
# Stable feed identifiers
# ---------------------------------------------------------------------------
def feed_id(feed_url: str, site_url: str = "", title: str = "") -> str:
    """Return a stable, filesystem-safe per-feed identifier.

    The identifier is ``<host slug>-<12 hex chars>`` where the digest covers
    the canonical feed URL (falling back to the site URL, then the title).
    """
    feed_url = _text(feed_url).strip()
    site_url = _text(site_url).strip()
    title = _text(title).strip()

    seed = canonical_url(feed_url) or feed_url
    if not seed:
        seed = canonical_url(site_url) or site_url
    if not seed:
        seed = title or "feed"
    digest = _sha256(seed.encode("utf-8"))[:12]

    host = ""
    for url in (feed_url, site_url):
        if not url:
            continue
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        if host:
            break
    if host.startswith("www."):
        host = host[4:]
    slug = re.sub(r"[^a-z0-9.-]+", "-", host).strip(".-")
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:64] or "feed"
    return f"{slug}-{digest}"


# ---------------------------------------------------------------------------
# Per-feed JSONL indexes
# ---------------------------------------------------------------------------
def _feed_identifier(feed_meta: dict, index_record: dict | None = None) -> str:
    index_record = index_record or {}
    existing = _text(feed_meta.get("feed_id")) or _text(index_record.get("feed_id"))
    if existing:
        return existing
    return feed_id(
        _text(feed_meta.get("feed_url")) or _text(index_record.get("feed_url")),
        _text(feed_meta.get("site_url")) or _text(index_record.get("site_url")),
        _text(feed_meta.get("title")) or _text(index_record.get("title")),
    )


def _feed_header(feed_meta: dict, identifier: str) -> dict:
    return {
        "v": 1,
        "kind": "feed",
        "feed_id": identifier,
        "feed_url": _text(feed_meta.get("feed_url")),
        "site_url": _text(feed_meta.get("site_url")),
        "title": _text(feed_meta.get("title") or feed_meta.get("feed_title")),
    }


def _article_keys(record: dict) -> list[str]:
    """Dedup keys: canonical link and guid (both when available)."""
    keys: list[str] = []
    link = _text(record.get("link")).strip()
    if link:
        keys.append("link:" + (canonical_url(link) or link))
    guid = _text(record.get("guid")).strip()
    if guid:
        keys.append("guid:" + guid)
    return keys


def _parse_jsonl_lines(raw: bytes | str) -> list[dict]:
    """Parse complete JSONL objects, ignoring blank and partial/corrupt lines."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _truncate_partial_line(raw: bytes) -> bytes:
    """Drop a trailing line that is not complete JSON (an interrupted append)."""
    if raw and not raw.endswith(b"\n"):
        cut = raw.rfind(b"\n") + 1
        candidate = raw[cut:]
        try:
            json.loads(candidate.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return raw[:cut]
    return raw


def _append_feed_lines(
    feeds_dir: str | os.PathLike,
    feed_meta: dict,
    records: list[dict],
    dedup: bool,
) -> None:
    """Append lines to a feed index under one lock, creating the header."""
    identifier = _feed_identifier(feed_meta, records[0] if records else None)
    directory = Path(feeds_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{identifier}.jsonl"

    with path.open("a+b") as handle:
        with _locked(handle):
            handle.seek(0)
            raw = handle.read()
            raw = _truncate_partial_line(raw)
            needs_separator = bool(raw) and not raw.endswith(b"\n")
            # Apply the truncation to disk if a partial line was found.
            size = os.fstat(handle.fileno()).st_size
            if len(raw) < size:
                handle.truncate(len(raw))

            existing = _parse_jsonl_lines(raw)
            lines: list[dict] = []
            if not existing:
                lines.append(_feed_header(feed_meta, identifier))
            elif existing[0].get("kind") != "feed":
                # A pre-existing index without a header is left append-only; a
                # rebuild is the supported way to repair it.
                pass

            seen: set[str] = set()
            if dedup:
                for record in existing:
                    if record.get("kind", "article") != "article":
                        continue
                    seen.update(_article_keys(record))

            for record in records:
                if dedup:
                    keys = _article_keys(record)
                    if any(key in seen for key in keys):
                        continue
                    seen.update(keys)
                lines.append(record)

            if not lines:
                return
            handle.seek(0, os.SEEK_END)
            if needs_separator:
                handle.write(b"\n")
            for record in lines:
                handle.write((_json_line(record) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())


def append_feed_record(
    feeds_dir: str | os.PathLike, feed_meta: dict, index_record: dict
) -> None:
    """Append one article index record, deduped by guid/canonical link."""
    record = dict(index_record or {})
    identifier = _feed_identifier(feed_meta, record)
    record["v"] = 1
    record["kind"] = "article"
    record["feed_id"] = identifier
    _append_feed_lines(feeds_dir, feed_meta, [record], dedup=True)


def append_feed_annotation(
    feeds_dir: str | os.PathLike, feed_meta: dict, annotation: dict
) -> None:
    """Append an annotation record (append-only; latest wins, no dedup)."""
    source = annotation or {}
    record = {
        "v": 1,
        "kind": "annotation",
        "guid": _text(source.get("guid")),
        "file": _text(source.get("file")),
        "offset": source.get("offset"),
        "category": _text(source.get("category")),
        "summary": _text(source.get("summary")),
        "score": source.get("score"),
        "classified_at": _text(source.get("classified_at")),
    }
    _append_feed_lines(feeds_dir, feed_meta, [record], dedup=False)


def read_feed_index(path: str | os.PathLike) -> tuple[dict, list[dict]]:
    """Return ``(feed_header, article+annotation records)``, ignoring junk."""
    target = Path(path)
    if not target.exists():
        return {}, []
    try:
        raw = target.read_bytes()
    except OSError:
        return {}, []
    # A line without its trailing newline is an interrupted append.
    raw = _truncate_partial_line(raw)
    records = _parse_jsonl_lines(raw)
    if not records:
        return {}, []
    header = records[0]
    if header.get("kind") != "feed":
        raise ValueError(f"{target}: first record is not a feed header")
    return header, records[1:]


# ---------------------------------------------------------------------------
# Rebuild / validate
# ---------------------------------------------------------------------------
def _atomic_write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(_json_line(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _resolve_archive_root(value: str | os.PathLike) -> Path:
    """Accept either an archive directory or a project root containing one."""
    directory = Path(value)
    if directory.name != "archive":
        nested = directory / "archive"
        if nested.is_dir():
            return nested
    return directory


def _archive_files(archive_root: str | os.PathLike) -> list[Path]:
    directory = _resolve_archive_root(archive_root)
    if not directory.exists():
        return []
    files: list[Path] = []
    for path in sorted(directory.rglob("*.md")):
        try:
            rel = path.relative_to(directory).as_posix()
        except ValueError:
            continue
        if _DAY_FILE_RE.match(rel):
            files.append(path)
    return files


def rebuild_indexes(
    archive_root: str | os.PathLike, feeds_dir: str | os.PathLike
) -> dict:
    """Rebuild feed indexes from daily archive files (annotations are lost)."""
    archive_dir = _resolve_archive_root(archive_root)
    files = _archive_files(archive_dir)

    grouped: dict[str, dict] = {}
    duplicates = 0
    for path in files:
        for record in iter_records(path):
            meta = record["meta"]
            identifier = _text(meta.get("feed_id")) or feed_id(
                _text(meta.get("feed_url")),
                _text(meta.get("site_url")),
                _text(meta.get("title")),
            )
            group = grouped.setdefault(
                identifier,
                {
                    "meta": feed_meta_from_meta(meta),
                    "articles": [],
                    "keys": set(),
                },
            )
            index_record = {
                "kind": "article",
                "v": 1,
                "feed_id": identifier,
                "file": _file_relpath(path, archive_dir),
                "offset": record["offset"],
                "length": record["length"],
                "body_offset": record["body_offset"],
                "body_len": record["body_len"],
                "sha256": record["sha256"],
                "guid": _text(meta.get("guid")),
                "link": _text(meta.get("link")),
                "title": _text(meta.get("title")),
                "author": _text(meta.get("author")),
                "published": _text(meta.get("published")),
            }
            keys = _article_keys(index_record)
            if any(key in group["keys"] for key in keys):
                duplicates += 1
                continue
            group["keys"].update(keys)
            group["articles"].append(index_record)

    feeds_path = Path(feeds_dir)
    annotations_dropped = 0
    existing_headers: dict[str, dict] = {}
    if feeds_path.exists():
        for old_path in feeds_path.glob("*.jsonl"):
            try:
                old_header, old_records = read_feed_index(old_path)
            except (OSError, ValueError):
                continue
            if old_header.get("feed_id"):
                existing_headers[_text(old_header["feed_id"])] = old_header
            annotations_dropped += sum(
                1 for record in old_records if record.get("kind") == "annotation"
            )

    written = 0
    for identifier in sorted(grouped):
        group = grouped[identifier]
        header_meta = dict(group["meta"])
        previous = existing_headers.get(identifier)
        if previous:
            for field in ("feed_url", "site_url", "title"):
                if not header_meta.get(field) and previous.get(field):
                    header_meta[field] = previous[field]
        records = [_feed_header(header_meta, identifier)] + group["articles"]
        _atomic_write_jsonl(feeds_path / f"{identifier}.jsonl", records)
        written += len(group["articles"])

    return {
        "feeds": len(grouped),
        "articles": written,
        "files": len(files),
        "duplicates": duplicates,
        "annotations_dropped": annotations_dropped,
    }


def feed_meta_from_meta(meta: dict) -> dict:
    """Extract feed-level fields from an article header.

    New records carry ``feed_title`` in the daily header; legacy records may
    leave it empty.
    """
    return {
        "feed_id": _text(meta.get("feed_id")),
        "feed_url": _text(meta.get("feed_url")),
        "site_url": _text(meta.get("site_url")),
        "title": _text(meta.get("feed_title")),
    }


def _repair_feed_meta(meta: dict) -> dict:
    """Feed metadata derived from one archive record header."""
    title = _text(meta.get("feed_title"))
    return {
        "feed_id": _text(meta.get("feed_id")),
        "feed_url": _text(meta.get("feed_url")),
        "site_url": _text(meta.get("site_url")),
        "feed_title": title,
        "title": title,
    }


def _read_index_state(path: Path) -> tuple[dict | None, list[dict]]:
    """Return ``(feed_header_or_None, records)`` even for a headerless index.

    ``records`` excludes the header when one is present. Junk/partial lines are
    ignored, matching :func:`read_feed_index`.
    """
    if not path.exists():
        return None, []
    try:
        raw = path.read_bytes()
    except OSError:
        return None, []
    records = _parse_jsonl_lines(_truncate_partial_line(raw))
    if not records:
        return None, []
    if records[0].get("kind") == "feed":
        return records[0], records[1:]
    return None, records


def _repair_index_header(
    path: Path,
    identifier: str,
    feed_meta: dict,
    records: list[dict],
) -> dict:
    """Atomically prepend a feed header to a headerless index."""
    header_meta = dict(feed_meta)
    # Fill gaps from the records that survived without a header.
    for field in ("feed_url", "site_url", "title"):
        if header_meta.get(field):
            continue
        for record in records:
            value = _text(record.get(field)).strip()
            if value:
                header_meta[field] = value
                break
    header = _feed_header(header_meta, identifier)
    _atomic_write_jsonl(path, [header, *records])
    return header


def repair_indexes(
    archive_root: str | os.PathLike, feeds_dir: str | os.PathLike
) -> dict:
    """Append only the article records missing from feed indexes.

    Existing indexes are normally never rewritten or shrunk: article records
    already present (by guid or canonical link) are left untouched and
    annotations stay in place. Missing records for one feed are appended in a
    single batched write (one read/fsync) instead of one write per record.

    A headerless index (records but no ``kind="feed"`` first line) is repaired
    by atomically prepending a header derived from the archive rows while
    preserving every existing record and annotation. Running it twice appends
    nothing and rewrites nothing the second time.

    Returns counts for ``articles``, ``feeds``, ``appended``, ``skipped`` and
    ``headers_repaired``.
    """
    archive_dir = _resolve_archive_root(archive_root)
    files = _archive_files(archive_dir)
    groups: dict[str, dict] = {}

    for path in files:
        for record in iter_records(path):
            meta = record["meta"]
            identifier = _text(meta.get("feed_id")) or feed_id(
                _text(meta.get("feed_url")),
                _text(meta.get("site_url")),
                _text(meta.get("title")),
            )
            group = groups.setdefault(
                identifier,
                {"meta": _repair_feed_meta(meta), "articles": []},
            )
            candidate = feed_meta_from_meta(meta)
            for field in ("feed_url", "site_url", "title"):
                if not group["meta"].get(field) and candidate.get(field):
                    group["meta"][field] = candidate[field]
            group["articles"].append(
                {
                    "kind": "article",
                    "v": 1,
                    "feed_id": identifier,
                    "file": _file_relpath(path, archive_dir),
                    "offset": record["offset"],
                    "length": record["length"],
                    "body_offset": record["body_offset"],
                    "body_len": record["body_len"],
                    "sha256": record["sha256"],
                    "guid": _text(meta.get("guid")),
                    "link": _text(meta.get("link")),
                    "title": _text(meta.get("title")),
                    "author": _text(meta.get("author")),
                    "published": _text(meta.get("published")),
                }
            )

    directory = Path(feeds_dir)
    stats = {
        "articles": 0,
        "feeds": len(groups),
        "appended": 0,
        "skipped": 0,
        "headers_repaired": 0,
    }
    for identifier in sorted(groups):
        group = groups[identifier]
        path = directory / f"{identifier}.jsonl"
        header, existing = _read_index_state(path)

        if existing and header is None:
            _repair_index_header(path, identifier, group["meta"], existing)
            stats["headers_repaired"] += 1

        seen: set[str] = set()
        for record in existing:
            if record.get("kind", "article") == "article":
                seen.update(_article_keys(record))

        missing: list[dict] = []
        for index_record in group["articles"]:
            stats["articles"] += 1
            keys = _article_keys(index_record)
            if any(key in seen for key in keys):
                stats["skipped"] += 1
                continue
            seen.update(keys)
            missing.append(index_record)

        if missing:
            # One read/dedup/fsync for the whole batch instead of one per record.
            _append_feed_lines(directory, group["meta"], missing, dedup=True)
            stats["appended"] += len(missing)
    return stats


def _index_record_path(archive_dir: Path, value: str) -> Path:
    path = Path(value or "")
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == "archive":
        return archive_dir.parent / path
    return archive_dir / path


def reconcile_indexes(
    archive_root: str | os.PathLike, feeds_dir: str | os.PathLike
) -> dict:
    """Drop feed-index records whose archive offset/length no longer verifies.

    Annotations are kept only for articles that remain valid. This is the
    recovery path for a missing/truncated daily file or a stale cache; the
    caller should also clear the corresponding archive refs in
    article_state.json so fetch can re-archive the body.
    """
    archive_dir = _resolve_archive_root(archive_root)
    directory = Path(feeds_dir)
    stats = {
        "feeds": 0,
        "articles": 0,
        "annotations": 0,
        "dropped_articles": 0,
        "dropped_annotations": 0,
    }
    if not directory.is_dir():
        return stats

    for index_path in sorted(directory.glob("*.jsonl")):
        try:
            header, records = read_feed_index(index_path)
        except (OSError, ValueError):
            continue
        stats["feeds"] += 1
        kept_articles: list[dict] = []
        annotations: list[dict] = []
        kept_guids: set[str] = set()
        kept_links: set[str] = set()

        for record in records:
            kind = record.get("kind", "article")
            if kind == "article":
                file = _text(record.get("file"))
                offset = record.get("offset")
                length = record.get("length")
                valid = (
                    file
                    and isinstance(offset, int)
                    and not isinstance(offset, bool)
                    and offset >= 0
                    and isinstance(length, int)
                    and not isinstance(length, bool)
                    and length > 0
                )
                if valid:
                    try:
                        read_record(
                            _index_record_path(archive_dir, file), offset, length
                        )
                    except (OSError, ValueError):
                        valid = False
                if not valid:
                    stats["dropped_articles"] += 1
                    continue
                kept_articles.append(record)
                stats["articles"] += 1
                guid = _text(record.get("guid")).strip()
                if guid:
                    kept_guids.add(guid)
                link = _text(record.get("link")).strip()
                if link:
                    kept_links.add(canonical_url(link) or link)
            elif kind == "annotation":
                annotations.append(record)

        kept_annotations = []
        for annotation in annotations:
            guid = _text(annotation.get("guid")).strip()
            file = _text(annotation.get("file")).strip()
            link = _text(annotation.get("link")).strip()
            if guid and guid not in kept_guids:
                stats["dropped_annotations"] += 1
                continue
            if link and (canonical_url(link) or link) not in kept_links:
                stats["dropped_annotations"] += 1
                continue
            if not guid and not link and file and not any(
                _text(a.get("file")) == file for a in kept_articles
            ):
                stats["dropped_annotations"] += 1
                continue
            kept_annotations.append(annotation)
            stats["annotations"] += 1

        _atomic_write_jsonl(
            index_path, [_feed_header_from(header), *kept_articles, *kept_annotations]
        )
    return stats


def _feed_header_from(header: dict) -> dict:
    return {
        "v": 1,
        "kind": "feed",
        "feed_id": _text(header.get("feed_id")),
        "feed_url": _text(header.get("feed_url")),
        "site_url": _text(header.get("site_url")),
        "title": _text(header.get("title")),
    }


def validate_archive(archive_root: str | os.PathLike) -> list[str]:
    """Return errors for truncated records, bad framing or checksum mismatches."""
    errors: list[str] = []
    for path in _archive_files(archive_root):
        records, error = _scan_file(path)
        if error is not None:
            errors.append(f"{path}@{error['offset']}: {error['reason']}")
        for record in records:
            meta = record["meta"]
            if meta.get("v") not in (None, 1):
                errors.append(f"{path}@{record['offset']}: unsupported record version")
            if _text(meta.get("body_sha256")) != record["sha256"]:
                errors.append(f"{path}@{record['offset']}: body sha256 mismatch")
            if meta.get("body_len") != record["body_len"]:
                errors.append(f"{path}@{record['offset']}: body_len mismatch")
    return errors


def repair_archive(archive_root: str | os.PathLike) -> int:
    """Repair incomplete tails in every daily archive file."""
    removed = 0
    for path in _archive_files(archive_root):
        removed += repair_tail(path)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Markdown archive tools")
    parser.add_argument("--validate", metavar="ARCHIVE_ROOT")
    parser.add_argument("--repair", metavar="ARCHIVE_ROOT")
    parser.add_argument("--rebuild-indexes", metavar="ARCHIVE_ROOT")
    parser.add_argument("--reconcile-indexes", metavar="ARCHIVE_ROOT")
    parser.add_argument("--repair-indexes", metavar="ARCHIVE_ROOT")
    parser.add_argument("--feeds", default="feeds")
    args = parser.parse_args(argv)

    if args.validate:
        errors = validate_archive(args.validate)
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0
    if args.repair:
        try:
            removed = repair_archive(args.repair)
        except ValueError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            return 1
        print(json.dumps({"removed": removed}, sort_keys=True))
        return 0
    if args.rebuild_indexes:
        print(
            json.dumps(
                rebuild_indexes(args.rebuild_indexes, args.feeds), sort_keys=True
            )
        )
        return 0
    if args.reconcile_indexes:
        print(
            json.dumps(
                reconcile_indexes(args.reconcile_indexes, args.feeds), sort_keys=True
            )
        )
        return 0
    if args.repair_indexes:
        print(
            json.dumps(
                repair_indexes(args.repair_indexes, args.feeds), sort_keys=True
            )
        )
        return 0
    parser.error(
        "one of --validate, --repair, --rebuild-indexes, --reconcile-indexes "
        "or --repair-indexes is required"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
