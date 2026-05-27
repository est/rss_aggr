"""Storage module: save results as markdown files (YYYY/MMDD.md) + index."""
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

OLD_HEADER = "| Author | Title | Summary | Score |"
OLD_SEP = "|--------|-------|---------|-------|"
NEW_HEADER = "| Author | Title | Category | Summary | Score |"
NEW_SEP = "|--------|-------|----------|---------|-------|"

PENDING_CONTENT_FILE = ".pending_content.json"


def _normalize_link(url: str) -> str:
    """Normalize URL for dedup: strip scheme and trailing slash."""
    if not url:
        return ""
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            url = url[len(scheme):]
            break
    return url.rstrip("/")


def save_pending_content(articles: list[dict], data_dir: str = "output") -> Path:
    """Save article content for classify step (merge with existing). Returns path to the file."""
    base = Path(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = base / PENDING_CONTENT_FILE
    existing = load_pending_content(data_dir)
    for a in articles:
        link = a.get("link")
        if link:
            existing[link] = a.get("content", "")
    path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
    return path


def load_pending_content(data_dir: str = "output") -> dict[str, str]:
    """Load pending article content. Returns {link: content}."""
    path = Path(data_dir) / PENDING_CONTENT_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def clear_pending_content(data_dir: str = "output"):
    """Delete the pending content file."""
    path = Path(data_dir) / PENDING_CONTENT_FILE
    if path.exists():
        path.unlink()


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", "")


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
    if cells[1].lower() in ("author", "feed"):
        return False
    if all((not c) or set(c) <= {"-"} for c in cells[1:-1]):
        return False
    return True


def _extract_score(cols: list[str]) -> str:
    """Extract score cell from markdown row cells (supports old/new formats).

    New format (7 cols): ['', author, title, category, summary, score, '']
    Old format (6 cols): ['', author, title, summary, score, '']
    """
    if len(cols) >= 7:
        return cols[5]  # new format with category column
    if len(cols) >= 6:
        return cols[4]  # old format without category
    return ""


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


def _parse_published(published: str) -> datetime | None:
    """Parse published date string to datetime."""
    if not published:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(published, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _article_date(article: dict) -> datetime | None:
    """Get the publish date of an article."""
    return _parse_published(article.get("published", ""))


def save_daily_results(
    data: dict,
    data_dir: str = "output",
    last_fetched: str = "",
    keep_days: int = 14,
) -> list[Path]:
    """Save articles grouped by their published date. Returns list of written files."""
    now = datetime.now(timezone.utc)
    cutoff_old = now - timedelta(days=keep_days) if keep_days > 0 else datetime.min.replace(tzinfo=timezone.utc)
    written = []

    # Group valid articles by date
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
        print(f"  Skipped {skipped} articles (no date / >{keep_days}d old / future)", flush=True)

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
                        existing_links.add(_normalize_link(m.group(1)))

        new_rows = []
        for a in articles:
            c = a.get("classification") or {}
            author = _md_escape(a.get("author", "") or a.get("feed_title", "") or "-")
            title = _md_escape(a.get("title", ""))
            link = a.get("link", "")
            category = _md_escape(c.get("category", "") or "")
            summary = _md_escape(c.get("summary", "") or "")
            score = c.get("score", "—")
            if link and _normalize_link(link) not in existing_links:
                new_rows.append(f"| {author} | [{title}]({link}) | {category or '—'} | {summary or '—'} | {score} |")

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

        file_path.write_text("\n".join(lines), encoding="utf-8")
        written.append(file_path)

    _update_index(data_dir, last_fetched=last_fetched)
    return written


def _update_index(data_dir: str = "output", last_fetched: str = ""):
    """Regenerate index.md listing all daily files, newest first."""
    base = Path(data_dir)
    if not base.exists():
        return

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
    lines = [
        "# RSS Aggregator",
        "",
        f"> {len(entries)} days | last fetched: <span id='lf'>{now}</span>",
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

    (base / "index.md").write_text("\n".join(lines), encoding="utf-8")


def load_seen_links(data_dir: str = "output") -> set[str]:
    """Load all previously seen article links (normalized) from past markdown files."""
    path = Path(data_dir)
    if not path.exists():
        return set()

    links = set()
    for f in path.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            content = f.read_text(encoding="utf-8")
            for m in re.finditer(r"\]\(([^)]+)\)", content):
                links.add(_normalize_link(m.group(1)))
        except OSError:
            continue
    return links


def load_unclassified_links(data_dir: str = "output") -> set[str]:
    """Load normalized links that have score '—' (unclassified) from output markdown."""
    links = set()
    base = Path(data_dir)
    if not base.exists():
        return links

    for f in base.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not _is_data_row(line):
                    continue
                m = re.search(r"\]\(([^)]+)\)", line)
                if not m:
                    continue
                cols = _split_md_row(line)
                score = _extract_score(cols)
                if score in ("—", ""):
                    links.add(_normalize_link(m.group(1)))
        except OSError:
            continue
    return links


def load_unclassified_links_map(data_dir: str = "output") -> dict[str, set[str]]:
    """Load normalized unclassified links per file path."""
    result: dict[str, set[str]] = {}
    base = Path(data_dir)
    if not base.exists():
        return result

    for f in base.rglob("*.md"):
        if f.name == "index.md":
            continue
        links = set()
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not _is_data_row(line):
                    continue
                m = re.search(r"\]\(([^)]+)\)", line)
                if not m:
                    continue
                cols = _split_md_row(line)
                score = _extract_score(cols)
                if score in ("—", ""):
                    links.add(_normalize_link(m.group(1)))
        except OSError:
            continue
        if links:
            result[str(f)] = links
    return result


def collect_articles_for_links(data_dir: str, links: set[str]) -> list[dict]:
    """Collect unique {title, link, feed_title} rows from markdown files for given links.

    Args:
        links: set of links (will be normalized for matching).
    """
    if not links:
        return []

    # Normalize input links for matching
    norm_links = {_normalize_link(l) for l in links}

    articles = []
    seen_links = set()
    base = Path(data_dir)
    if not base.exists():
        return articles

    for f in base.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not _is_data_row(line):
                    continue
                cols = _split_md_row(line)
                m = re.search(r"\[([^\]]+)\]\(([^)]+)\)", line)
                if not m:
                    continue
                title = m.group(1)
                link = m.group(2)
                norm = _normalize_link(link)
                author = cols[1] if len(cols) > 1 else ""
                if norm not in norm_links or norm in seen_links:
                    continue
                seen_links.add(norm)
                articles.append({"title": title, "link": link, "author": author})
        except OSError:
            continue
    return articles


def update_classifications(data_dir: str, updates: dict[str, dict], only_links: set[str] | None = None) -> int:
    """Update classification for articles in-place by link.

    Args:
        data_dir: output directory
        updates: {normalized_link: {category, tags, score, summary}}
        only_links: set of normalized links to restrict updates to

    Returns:
        Number of lines updated.
    """
    base = Path(data_dir)
    if not base.exists():
        return 0

    # Build normalized -> original key mapping for updates
    updates_norm = {_normalize_link(k): v for k, v in updates.items()}

    updated = 0
    for f in base.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        header_changed = False
        new_lines = []
        for line in lines:
            if line.strip() == OLD_HEADER:
                new_lines.append(NEW_HEADER)
                header_changed = True
                continue
            if line.strip() == OLD_SEP:
                new_lines.append(NEW_SEP)
                header_changed = True
                continue
            if not _is_data_row(line):
                new_lines.append(line)
                continue

            m = re.search(r"\]\(([^)]+)\)", line)
            if not m:
                new_lines.append(line)
                continue

            link = m.group(1)
            norm = _normalize_link(link)
            if only_links is not None and norm not in only_links:
                new_lines.append(_upgrade_old_row_to_category(line))
                continue
            if norm not in updates_norm:
                new_lines.append(_upgrade_old_row_to_category(line))
                continue

            info = updates_norm[norm]
            if not info.get("category") or not info.get("summary") or "score" not in info:
                new_lines.append(_upgrade_old_row_to_category(line))
                continue
            cols = _split_md_row(line)
            if len(cols) < 5:
                new_lines.append(line)
                continue

            # old format: ['', author, title, summary, score, '']
            # new format: ['', author, title, category, summary, score, '']
            author = cols[1]
            title_link = cols[2]
            category = _md_escape(info.get("category", "") or "")
            summary = _md_escape(info.get("summary", "") or "")
            score = info.get("score", "—")
            new_line = f"| {author} | {title_link} | {category or '—'} | {summary or '—'} | {score} |"
            new_lines.append(new_line)
            updated += 1

        if updated or header_changed:
            f.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return updated


def remove_articles(data_dir: str, links: set[str]) -> int:
    """Remove articles by normalized link from output markdown files. Returns count removed."""
    base = Path(data_dir)
    if not base.exists() or not links:
        return 0

    removed = 0
    for f in base.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        new_lines = []
        for line in lines:
            if not _is_data_row(line):
                new_lines.append(line)
                continue
            m = re.search(r"\]\(([^)]+)\)", line)
            if m and _normalize_link(m.group(1)) in links:
                removed += 1
                continue
            new_lines.append(line)

        if len(new_lines) != len(lines):
            f.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return removed


def cleanup_old_data(data_dir: str = "output", keep_days: int = 0) -> int:
    """Delete markdown files older than keep_days. 0 = never delete."""
    path = Path(data_dir)
    if not path.exists() or keep_days <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = 0
    for f in path.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            yyyy = f.parent.name
            mmdd = f.stem
            file_date = datetime.strptime(f"{yyyy}{mmdd}", "%Y%m%d").replace(tzinfo=timezone.utc)
            if file_date < cutoff:
                f.unlink()
                removed += 1
        except (ValueError, OSError):
            continue

    if removed:
        _update_index(data_dir)
    return removed
