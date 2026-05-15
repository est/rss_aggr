"""Storage module: save results as markdown files (YYYY/MMDD.md) + index."""
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path


KEEP_DAYS = 7


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", "")


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
) -> list[Path]:
    """Save articles grouped by their published date. Returns list of written files."""
    now = datetime.now(timezone.utc)
    cutoff_old = now - timedelta(days=KEEP_DAYS)
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
        print(f"  Skipped {skipped} articles (no date / >{KEEP_DAYS}d old / future)", flush=True)

    for key, articles in by_date.items():
        yyyy, mmdd = key.split("/")
        path = Path(data_dir) / yyyy
        path.mkdir(parents=True, exist_ok=True)
        file_path = path / f"{mmdd}.md"

        existing_guids = set()
        existing_lines = []
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.startswith("|") and not line.startswith("|--") and not line.startswith("| Author"):
                    existing_lines.append(line)
                    m = re.search(r"\]\(([^)]+)\)", line)
                    if m:
                        existing_guids.add(m.group(1))

        new_rows = []
        for a in articles:
            c = a.get("classification") or {}
            author = _md_escape(a.get("author", "") or a.get("feed_title", "") or "-")
            title = _md_escape(a.get("title", ""))
            link = a.get("link", "")
            summary = _md_escape(c.get("summary", "") or "")
            score = c.get("score", "—")
            if link and link not in existing_guids:
                new_rows.append(f"| {author} | [{title}]({link}) | {summary or '—'} | {score} |")

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
            "| Author | Title | Summary | Score |",
            "|--------|-------|---------|-------|",
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


def load_seen_guids(data_dir: str = "output") -> set[str]:
    """Load all previously seen article links from past markdown files."""
    path = Path(data_dir)
    if not path.exists():
        return set()

    guids = set()
    for f in path.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            content = f.read_text(encoding="utf-8")
            for m in re.finditer(r"\]\(([^)]+)\)", content):
                guids.add(m.group(1))
        except OSError:
            continue
    return guids


def load_unclassified_links(data_dir: str = "output") -> set[str]:
    """Load links that have score '—' (unclassified) from output markdown."""
    links = set()
    base = Path(data_dir)
    if not base.exists():
        return links

    for f in base.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.startswith("|") or line.startswith("|--") or line.startswith("| Author"):
                    continue
                m = re.search(r"\]\(([^)]+)\)", line)
                if not m:
                    continue
                link = m.group(1)
                cols = [c.strip() for c in line.split("|")]
                if len(cols) >= 5 and cols[4] in ("—", ""):
                    links.add(link)
        except OSError:
            continue
    return links


def update_classifications(data_dir: str, updates: dict[str, dict]) -> int:
    """Update classification for articles in-place by link.

    Args:
        data_dir: output directory
        updates: {link: {category, tags, score, summary}}

    Returns:
        Number of lines updated.
    """
    base = Path(data_dir)
    if not base.exists():
        return 0

    updated = 0
    for f in base.rglob("*.md"):
        if f.name == "index.md":
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        new_lines = []
        for line in lines:
            if not line.startswith("|") or line.startswith("|--") or line.startswith("| Author"):
                new_lines.append(line)
                continue

            m = re.search(r"\]\(([^)]+)\)", line)
            if not m:
                new_lines.append(line)
                continue

            link = m.group(1)
            if link not in updates:
                new_lines.append(line)
                continue

            info = updates[link]
            cols = [c.strip() for c in line.split("|")]
            if len(cols) < 6:
                new_lines.append(line)
                continue

            # cols: ['', author, title_link, summary, score, '']
            author = cols[1]
            title_link = cols[2]
            summary = _md_escape(info.get("summary", "") or "")
            score = info.get("score", "—")
            new_line = f"| {author} | {title_link} | {summary or '—'} | {score} |"
            new_lines.append(new_line)
            updated += 1

        if updated:
            f.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return updated


def cleanup_old_data(data_dir: str = "output", keep_days: int = 90) -> int:
    """Delete markdown files older than keep_days."""
    path = Path(data_dir)
    if not path.exists():
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
