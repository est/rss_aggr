"""Storage module: save results as markdown files (YYYY/MMDD.md) + index."""
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _md_escape(text: str) -> str:
    """Escape pipes and newlines for markdown table cells."""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", "")


def save_daily_results(
    data: dict,
    data_dir: str = "docs",
) -> Path:
    """Save articles as YYYY/MMDD.md markdown table. Appends on same-day re-runs."""
    now = datetime.now(timezone.utc)
    yyyy = now.strftime("%Y")
    mmdd = now.strftime("%m%d")

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

    new_articles = data.get("articles", [])
    rows = []
    for a in new_articles:
        c = a.get("classification") or {}
        author = _md_escape(a.get("author", "") or a.get("feed_title", "") or "-")
        title = _md_escape(a.get("title", ""))
        link = a.get("link", "")
        summary = _md_escape(c.get("summary", "") or "")
        score = c.get("score", "—")
        if link and link not in existing_guids:
            rows.append(f"| {author} | [{title}]({link}) | {summary or '—'} | {score} |")

    all_rows = existing_lines + rows

    lines = [
        f"# {now.strftime('%Y-%m-%d')}",
        "",
        f"> {len(all_rows)} articles | updated {now.strftime('%H:%M UTC')}",
        "",
        "| Author | Title | Summary | Score |",
        "|--------|-------|---------|-------|",
    ]
    lines.extend(all_rows)
    lines.append("")

    file_path.write_text("\n".join(lines), encoding="utf-8")

    _update_index(data_dir)
    return file_path


def _update_index(data_dir: str = "docs"):
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

    lines = [
        "# RSS Aggregator",
        "",
        f"> {len(entries)} days of collected articles",
        "",
        "| Date | Link |",
        "|------|------|",
    ]
    for display, rel in entries:
        lines.append(f"| {display} | [{rel}]({rel}) |")
    lines.append("")

    (base / "index.md").write_text("\n".join(lines), encoding="utf-8")


def load_seen_guids(data_dir: str = "docs") -> set[str]:
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


def cleanup_old_data(data_dir: str = "docs", keep_days: int = 90) -> int:
    """Delete markdown files older than keep_days. Returns number of files removed."""
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
