"""Storage module: save results as markdown files (YYYY/MMDD.md) + index."""
import html
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.urls import canonical_url, is_safe_http_url

OLD_HEADER = "| Author | Title | Summary | Score |"
OLD_SEP = "|--------|-------|---------|-------|"
NEW_HEADER = "| Author | Title | Category | Summary | Score |"
NEW_SEP = "|--------|-------|----------|---------|-------|"

SUMMARY_MAX_CHARS = 280
CATEGORY_MAX_CHARS = 100


def _normalize_link(url: str) -> str:
    """Legacy normalizer: strip scheme and trailing slash."""
    if not url:
        return ""
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            url = url[len(scheme):]
            break
    return url.rstrip("/")


def _dedup_key(url: str) -> str:
    return canonical_url(url) or _normalize_link(url)


def _md_escape(text) -> str:
    """Escape untrusted text for safe Markdown table rendering.

    HTML is escaped first so titles/authors/summaries cannot inject raw tags;
    pipes are then escaped and newlines collapsed to keep table rows intact.
    """
    escaped = html.escape(str(text if text is not None else ""), quote=True)
    escaped = escaped.replace("\r", " ").replace("\n", " ")
    return escaped.replace("|", "\\|")


def _split_md_row(line: str) -> list[str]:
    """Split markdown table row by unescaped '|' and return trimmed cells."""
    if not line.startswith("|"):
        return []
    cells = []
    cur = []
    escaped = False
    for ch in line:
        if ch == "\\" and not escaped:
            escaped = True
            cur.append(ch)
            continue
        if ch == "|" and not escaped:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        escaped = False
    cells.append("".join(cur).strip())
    return cells


def _is_data_row(line: str) -> bool:
    if not line.startswith("|"):
        return False
    cells = _split_md_row(line)
    if len(cells) < 2:
        return False
    # Header/separator rows.
    if (cells[1].lower() in ("author", "feed")
            and len(cells) > 2
            and cells[2].lower() in ("title", "title link")):
        return False
    if all((not c) or set(c) <= {"-"} for c in cells[1:-1]):
        return False
    return True


def _upgrade_old_row_to_category(line: str) -> str:
    """Upgrade old 4-column row to 5-column row by inserting empty category."""
    if not _is_data_row(line):
        return line
    cols = _split_md_row(line)
    # old format: ['', author, title, summary, score, '']
    if len(cols) == 6:
        author = cols[1]
        title_link = cols[2]
        summary = cols[3]
        score = cols[4]
        return f"| {author} | {title_link} | — | {summary or '—'} | {score or '—'} |"
    return line


def _parse_dt(value) -> datetime | None:
    """Parse an ISO-ish date string to an aware UTC datetime."""
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


def _parse_published(published: str) -> datetime | None:
    """Parse published/cached date string to an aware UTC datetime."""
    return _parse_dt(published)


def _article_date(article: dict) -> datetime | None:
    """Published date, falling back to cached_at when published is invalid."""
    return _parse_published(article.get("published", "")) or _parse_published(
        article.get("cached_at", "")
    )


def _format_score(value) -> str:
    """Return a score clamped to 1..10, or an em dash when unusable."""
    if value is None or value == "":
        return "—"
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return "—"
    return str(max(1, min(10, score)))


def _article_title_cell(article: dict) -> str:
    """Render title as a link only for safe http(s) URLs."""
    title = _md_escape(article.get("title", "") or "—")
    link = (article.get("link") or "").strip()
    if link and is_safe_http_url(link):
        return f"[{title}]({link})"
    return title


def save_daily_results(
    data: dict,
    data_dir: str = "output",
    last_fetched: str = "",
    keep_days: int = 0,
    last_classified: str = "",
    last_classify: str | None = None,
    last_fetch: str = "",
) -> list[Path]:
    """Save articles grouped by their published date. Returns list of written files.

    Args:
        keep_days: 0 means no age filtering (all articles saved).
        last_classified: timestamp shown on the index (``last_classify`` is a
            backward-compatible alias; ``last_fetch`` aliases ``last_fetched``).
    """
    if last_fetch and not last_fetched:
        last_fetched = last_fetch
    if last_classify and not last_classified:
        last_classified = last_classify

    now = datetime.now(timezone.utc)
    cutoff_old = (
        now - timedelta(days=keep_days)
        if keep_days > 0
        else datetime.min.replace(tzinfo=timezone.utc)
    )
    written = []

    # Group valid articles by date (published, else cached_at).
    by_date: dict[str, list[dict]] = {}
    skipped = 0
    for a in data.get("articles", []):
        dt = _article_date(a)
        if dt is None:
            skipped += 1
            continue
        if dt < cutoff_old:
            skipped += 1
            continue
        if dt > now + timedelta(hours=1):
            skipped += 1
            continue
        key = dt.strftime("%Y/%m%d")
        by_date.setdefault(key, []).append(a)

    if skipped:
        print(f"  Skipped {skipped} articles (no date / future)", flush=True)

    for key, articles in by_date.items():
        yyyy, mmdd = key.split("/")
        path = Path(data_dir) / yyyy
        path.mkdir(parents=True, exist_ok=True)
        file_path = path / f"{mmdd}.md"

        existing_links = set()
        existing_lines = []
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                if _is_data_row(line):
                    existing_lines.append(_upgrade_old_row_to_category(line))
                    m = re.search(r"\]\(([^)]+)\)", line)
                    if m:
                        existing_links.add(_dedup_key(m.group(1)))

        new_rows = []
        for a in articles:
            c = a.get("classification") or {}
            author = _md_escape(
                a.get("author", "") or a.get("feed_title", "") or "-"
            )
            title_cell = _article_title_cell(a)
            link = (a.get("link") or "").strip()
            category = _md_escape((c.get("category") or "")[:CATEGORY_MAX_CHARS])
            summary = _md_escape((c.get("summary") or "")[:SUMMARY_MAX_CHARS])
            score = _format_score(c.get("score"))
            key_for_link = _dedup_key(link)
            if key_for_link and key_for_link in existing_links:
                continue
            if key_for_link:
                existing_links.add(key_for_link)
            new_rows.append(
                f"| {author} | {title_cell} | {category or '—'} | "
                f"{summary or '—'} | {score} |"
            )

        all_rows = existing_lines + new_rows
        if not all_rows:
            continue

        dt = _parse_published(f"{yyyy}-{mmdd[:2]}-{mmdd[2:]}T00:00:00Z")
        display_date = dt.strftime("%Y-%m-%d") if dt else key

        lines = [
            f"# {display_date}",
            "",
            f"> {len(all_rows)} articles",
            "",
            NEW_HEADER,
            NEW_SEP,
        ]
        lines.extend(all_rows)
        lines.append("")

        tmp_path = file_path.with_name(file_path.name + ".tmp")
        tmp_path.write_text("\n".join(lines), encoding="utf-8")
        tmp_path.replace(file_path)
        written.append(file_path)

    # Always regenerate the index, even when there were no new articles.
    _update_index(
        data_dir, last_fetched=last_fetched, last_classified=last_classified
    )
    return written


def _update_index(
    data_dir: str = "output",
    last_fetched: str = "",
    last_classified: str = "",
):
    """Regenerate index.md listing all daily files, newest first."""
    base = Path(data_dir)
    base.mkdir(parents=True, exist_ok=True)

    entries = []
    for yyyy_dir in sorted(base.iterdir(), reverse=True):
        if not yyyy_dir.is_dir() or not yyyy_dir.name.isdigit():
            continue
        for f in sorted(yyyy_dir.glob("*.md"), reverse=True):
            mmdd = f.stem
            try:
                date = datetime.strptime(f"{yyyy_dir.name}{mmdd}", "%Y%m%d")
                display = date.strftime("%Y-%m-%d")
            except ValueError:
                display = f"{yyyy_dir.name}/{mmdd}"
            rel = f"{yyyy_dir.name}/{f.name}"
            entries.append((display, rel))

    now = last_fetched or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = f"{len(entries)} days | last fetched: <span id='lf'>{now}</span>"
    if last_classified:
        meta += f" | last classified: <span id='lc'>{last_classified}</span>"
    lines = [
        "# RSS Aggregator",
        "",
        f"> {meta}",
        "",
        "<script>",
        "try{var e=document.getElementById('lf'),d=new Date(e.textContent);",
        "if(!isNaN(d))e.textContent=d.toLocaleString()+' ('+((Date.now()-d)/3600000).toFixed(1)+'h ago)'}catch(x){}",
        "</script>",
        "",
        "| Date | Link |",
        "|------|------|",
    ]
    for display, rel in entries:
        lines.append(f"| {display} | [{rel}]({rel}) |")
    lines.append("")

    index_path = base / "index.md"
    tmp_path = index_path.with_name(index_path.name + ".tmp")
    tmp_path.write_text("\n".join(lines), encoding="utf-8")
    tmp_path.replace(index_path)


def load_seen_links(data_dir: str = "output") -> set[str]:
    """Load canonical article links from previously rendered markdown files."""
    path = Path(data_dir)
    if not path.exists():
        return set()

    seen = set()
    for f in path.rglob("*.md"):
        if f.name in ("index.md", "status.md"):
            continue
        try:
            content = f.read_text(encoding="utf-8")
            for m in re.finditer(r"\]\(([^)]+)\)", content):
                key = _dedup_key(m.group(1))
                if key:
                    seen.add(key)
        except OSError:
            continue
    return seen


# Legacy alias: the old name exposed raw links, the new one is canonical.
load_seen_guids = load_seen_links
