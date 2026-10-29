"""Archive layer: per-day metadata JSONL and metadata-only data-branch snapshots.

Raw article bodies must never enter git. Only metadata is archived; the raw
``content``/``content_length`` fields stay in the Actions cache and in the
local ``articles.json`` working copy.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.urls import canonical_url

# Fields that are safe (and useful) to publish to the data branch.
METADATA_FIELDS = (
    "guid",
    "title",
    "link",
    "author",
    "published",
    "feed_title",
    "feed_category",
    "classification",
    "classification_meta",
    "status",
    "classified_at",
    "unclassified_reason",
    "unclassified_at",
    "cached_at",
    "canonical",
    "attempts",
    "skip_reason",
)

# Raw body fields that must never survive into an archive record.
RAW_FIELDS = ("content", "content_length")


def _parse_dt(value) -> datetime | None:
    """Parse an ISO timestamp (with ``Z`` support) into an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def metadata_record(article: dict) -> dict:
    """Return a metadata-only copy of ``article`` (never includes raw body)."""
    if not isinstance(article, dict):
        return {}
    record = {field: article[field] for field in METADATA_FIELDS if field in article}
    for field in RAW_FIELDS:
        record.pop(field, None)
    return record


def _article_key(article: dict) -> str:
    """Dedup key: canonical link, then guid, then the raw link."""
    link = (article.get("link") or "").strip()
    if link:
        canonical = canonical_url(link)
        if canonical:
            return canonical
    guid = str(article.get("guid") or "").strip()
    if guid:
        return guid
    canonical = (article.get("canonical") or "").strip()
    if canonical:
        return canonical_url(canonical) or canonical
    return link


def _record_day(article: dict) -> str:
    """Ingest day (UTC) from ``cached_at``, falling back to the current day."""
    dt = _parse_dt(article.get("cached_at"))
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _existing_keys(path: Path) -> set[str]:
    """Read dedup keys from an existing JSONL file, ignoring unreadable lines."""
    keys: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue
                key = _article_key(record)
                if key:
                    keys.add(key)
    except OSError:
        return keys
    return keys


def _ends_with_newline(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            f.seek(-1, 2)
            return f.read(1) == b"\n"
    except (OSError, ValueError):
        return True


def append_metadata(articles: list[dict], archive_dir: str = "archive") -> int:
    """Append metadata records to per-day ``YYYY-MM-DD.jsonl`` files.

    Records are deduped by canonical link (then guid, then raw link) against
    the existing content of the target file, so reruns do not duplicate rows.
    Returns the number of records appended.
    """
    by_day: dict[str, list[tuple[str, dict]]] = {}
    for article in articles or []:
        if not isinstance(article, dict):
            continue
        record = metadata_record(article)
        key = _article_key(record)
        if not key:
            continue
        by_day.setdefault(_record_day(article), []).append((key, record))

    if not by_day:
        return 0

    base = Path(archive_dir)
    base.mkdir(parents=True, exist_ok=True)
    appended = 0

    for day in sorted(by_day):
        path = base / f"{day}.jsonl"
        seen = _existing_keys(path) if path.exists() else set()
        new_records = []
        for key, record in by_day[day]:
            if key in seen:
                continue
            seen.add(key)
            new_records.append(record)
        if not new_records:
            continue

        needs_newline = path.exists() and path.stat().st_size > 0 and not _ends_with_newline(path)
        with path.open("a", encoding="utf-8") as f:
            if needs_newline:
                f.write("\n")
            for record in new_records:
                f.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
        appended += len(new_records)

    return appended


def _copy_tree(src: Path, dest: Path) -> int:
    """Copy every file under ``src`` to ``dest``; return the file count."""
    if not src.is_dir():
        return 0
    copied = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.name.endswith(".tmp"):
            continue
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def _atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(src.read_bytes())
    tmp.replace(dest)


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(path)


def _metadata_articles(path: Path) -> list[dict] | None:
    """Load metadata-only article records from ``path`` (``None`` if unusable)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, list):
        return None
    return [metadata_record(a) for a in data if isinstance(a, dict)]


def repair_archive_dir(archive_dir: str = "archive") -> int:
    """Drop partial/invalid lines and raw-content rows from JSONL archives.

    Returns the number of removed lines. Files with no remaining records are
    left as empty files rather than deleted, so the day remains visible.
    """
    removed = 0
    base = Path(archive_dir)
    if not base.exists():
        return 0
    for path in sorted(base.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        kept: list[str] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError, TypeError):
                removed += 1
                continue
            if (
                not isinstance(record, dict)
                or not _article_key(record)
                or "content" in record
                or "content_length" in record
            ):
                removed += 1
                continue
            kept.append(line)
        if len(kept) == len(lines):
            continue
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            "\n".join(kept) + ("\n" if kept else ""),
            encoding="utf-8",
        )
        tmp.replace(path)
    return removed


def validate_archive_dir(archive_dir: str = "archive") -> list[str]:
    """Return validation errors for per-day JSONL archive files."""
    errors: list[str] = []
    base = Path(archive_dir)
    if not base.exists():
        return errors
    for path in sorted(base.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError, TypeError):
                errors.append(f"{path}:{lineno}: invalid JSON")
                continue
            if not isinstance(record, dict):
                errors.append(f"{path}:{lineno}: record is not an object")
                continue
            if "content" in record or "content_length" in record:
                errors.append(f"{path}:{lineno}: raw content field")
            if not _article_key(record):
                errors.append(f"{path}:{lineno}: missing dedup key")
    return errors


def write_snapshot(dest_dir: str, root: str = ".") -> dict:
    """Prepare a metadata-only data-branch snapshot in ``dest_dir``.

    Copies ``output/``, ``archive/``, ``state.json`` and ``status.json`` when
    present, and writes a metadata-only ``articles.json``. Raw article bodies
    are never written.
    """
    root_path = Path(root)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    output_files = _copy_tree(root_path / "output", dest / "output")
    archive_files = _copy_tree(root_path / "archive", dest / "archive")

    for name in ("state.json", "status.json"):
        src = root_path / name
        if src.is_file():
            _atomic_copy(src, dest / name)

    articles = _metadata_articles(root_path / "articles.json")
    if articles is not None:
        _atomic_write_json(dest / "articles.json", articles)

    return {
        "articles": len(articles) if articles is not None else 0,
        "archive_files": archive_files,
        "output_files": output_files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSS aggregator archive tools")
    parser.add_argument(
        "--snapshot",
        metavar="DEST",
        help="Write a metadata-only data-branch snapshot to DEST",
    )
    parser.add_argument(
        "--validate",
        metavar="DIR",
        help="Validate per-day JSONL archive files under DIR",
    )
    parser.add_argument(
        "--repair",
        metavar="DIR",
        help="Drop partial/invalid lines from per-day JSONL archive files",
    )
    args = parser.parse_args(argv)

    if args.repair:
        removed = repair_archive_dir(args.repair)
        print(json.dumps({"removed": removed}, sort_keys=True), flush=True)
        return 0

    if args.validate:
        errors = validate_archive_dir(args.validate)
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0

    if not args.snapshot:
        parser.error("--snapshot DEST, --validate DIR or --repair DIR is required")

    manifest = write_snapshot(args.snapshot)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
