"""Storage module: save results as markdown files (YYYYMM/DD.md)."""
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _md_escape(text: str) -> str:
    """Escape pipes and newlines for markdown table cells."""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", "")


def save_daily_results(
    data: dict,
    data_dir: str = "output",
) -> Path:
    """Save articles as YYYYMM/DD.md markdown table. Appends on same-day re-runs."""
    now = datetime.now(timezone.utc)
    yyyymm = now.strftime("%Y%m")
    dd = now.strftime("%d")

    path = Path(data_dir) / yyyymm
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / f"{dd}.md"

    existing_guids = set()
    existing_lines = []
    if file_path.exists():
        content = file_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.startswith("|") and not line.startswith("|--") and not line.startswith("| Author"):
                existing_lines.append(line)
                # extract guid from link
                m = re.search(r"\]\(([^)]+)\)", line)
                if m:
                    existing_guids.add(m.group(1))

    new_articles = data.get("articles", [])
    rows = []
    for a in new_articles:
        c = a.get("classification", {})
        author = _md_escape(a.get("author", "") or a.get("feed_title", "") or "-")
        title = _md_escape(a.get("title", ""))
        link = a.get("link", "")
        summary = _md_escape(c.get("summary", "") or a.get("title", ""))
        score = c.get("score", "?")
        if link and link not in existing_guids:
            rows.append(f"| {author} | [{title}]({link}) | {summary} | {score} |")

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
    return file_path


def load_seen_guids(data_dir: str = "output") -> set[str]:
    """Load all previously seen article links from past markdown files."""
    path = Path(data_dir)
    if not path.exists():
        return set()

    guids = set()
    for f in path.rglob("*.md"):
        try:
            content = f.read_text(encoding="utf-8")
            for m in re.finditer(r"\]\(([^)]+)\)", content):
                guids.add(m.group(1))
        except OSError:
            continue
    return guids


def cleanup_old_data(data_dir: str = "output", keep_days: int = 90) -> int:
    """Delete markdown files older than keep_days. Returns number of files removed."""
    path = Path(data_dir)
    if not path.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = 0
    for f in path.rglob("*.md"):
        try:
            yyyymm = f.parent.name
            dd = f.stem
            file_date = datetime.strptime(f"{yyyymm}{dd}", "%Y%m%d").replace(tzinfo=timezone.utc)
            if file_date < cutoff:
                f.unlink()
                removed += 1
        except (ValueError, OSError):
            continue
    return removed
