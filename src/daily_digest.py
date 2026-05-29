"""Generate daily reading digest from classified articles.

Reads output markdown files, scores articles, and produces a curated
daily digest with top picks organized by category.
"""
import re
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _parse_articles_from_md(filepath: Path) -> list[dict]:
    """Parse articles from a markdown table file."""
    articles = []
    if not filepath.exists():
        return articles

    lines = filepath.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 4:
            continue
        if cells[1].lower() in ("author", "feed", ""):
            continue
        if all(set(c) <= {"-", " "} for c in cells):
            continue

        # Try to extract link from title cell
        title = cells[2] if len(cells) > 2 else ""
        link_match = re.search(r'\[([^\]]+)\]\(([^)]+)\)', title)
        if link_match:
            title_text = link_match.group(1)
            link = link_match.group(2)
        else:
            title_text = title
            link = ""

        score_str = cells[-1] if cells else "5"
        try:
            score = int(re.search(r'\d+', score_str).group())
        except (AttributeError, ValueError):
            score = 5

        category = cells[3] if len(cells) > 3 else "Misc"
        summary = cells[4] if len(cells) > 4 else ""

        articles.append({
            "author": cells[1],
            "title": title_text,
            "link": link,
            "category": category,
            "summary": summary,
            "score": score,
        })

    return articles


def generate_digest(data_dir: str = "output", days_back: int = 1,
                    min_score: int = 7, output_dir: str = "output/digests") -> str:
    """Generate a daily reading digest.

    Args:
        data_dir: directory containing markdown article files
        days_back: how many days back to look
        min_score: minimum score to include in digest
        output_dir: where to save digest files

    Returns:
        Path to generated digest file
    """
    base = Path(data_dir)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days_back)

    all_articles = []
    # Scan markdown files in data_dir/YYYY/MMDD.md pattern
    for md_file in sorted(base.rglob("*.md")):
        if md_file.name.startswith(".") or "digest" in str(md_file):
            continue
        articles = _parse_articles_from_md(md_file)
        all_articles.extend(articles)

    if not all_articles:
        print("No articles found for digest")
        return ""

    # Deduplicate by link
    seen = set()
    unique = []
    for a in all_articles:
        key = a.get("link") or a.get("title")
        if key not in seen:
            seen.add(key)
            unique.append(a)

    # Filter by score
    top = [a for a in unique if a.get("score", 0) >= min_score]
    top.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Group by category
    by_cat = {}
    for a in top:
        cat = a.get("category", "Misc")
        by_cat.setdefault(cat, []).append(a)

    # Generate digest
    date_str = now.strftime("%Y-%m-%d")
    digest_path = out / f"{date_str}.md"

    lines = [
        f"# 📖 Daily Reading Digest - {date_str}",
        f"",
        f"**{len(top)} articles** with score >= {min_score} (out of {len(unique)} total)",
        f"",
    ]

    # Summary stats
    lines.append("## 📊 Stats")
    lines.append(f"- Total articles: {len(unique)}")
    lines.append(f"- Top picks: {len(top)}")
    for cat in sorted(by_cat.keys()):
        lines.append(f"- {cat}: {len(by_cat[cat])}")
    lines.append("")

    # Top 5 must-reads
    lines.append("## 🔥 Must Read")
    for a in top[:5]:
        lines.append(f"")
        lines.append(f"### [{a['title']}]({a['link']})")
        lines.append(f"**Score: {a['score']}/10** | {a['category']} | by {a['author']}")
        if a.get('summary'):
            lines.append(f"> {a['summary']}")
    lines.append("")

    # By category
    for cat in ["Tech", "Biz", "Insight", "Life", "Misc"]:
        if cat not in by_cat:
            continue
        lines.append(f"## {cat}")
        for a in by_cat[cat]:
            score_bar = "⭐" * min(a['score'], 10)
            lines.append(f"- [{a['title']}]({a['link']}) {score_bar}")
            if a.get('summary'):
                lines.append(f"  > {a['summary']}")
        lines.append("")

    content = "\n".join(lines)
    digest_path.write_text(content, encoding="utf-8")
    print(f"Digest saved: {digest_path} ({len(top)} articles)")
    return str(digest_path)


if __name__ == "__main__":
    generate_digest()
