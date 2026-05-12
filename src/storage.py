"""Storage module: save results as JSON files."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path


def save_daily_results(
    data: dict,
    data_dir: str = "output/data",
) -> Path:
    """Save results as a daily JSON file. Appends articles on same-day re-runs."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)

    file_path = path / f"{today}.json"

    existing = {}
    if file_path.exists():
        existing = json.loads(file_path.read_text(encoding="utf-8"))

    existing_guids = {a["guid"] for a in existing.get("articles", []) if "guid" in a}
    new_articles = [a for a in data.get("articles", []) if a.get("guid") not in existing_guids]

    merged = {**existing}
    merged["articles"] = existing.get("articles", []) + new_articles
    merged["feeds_count"] = data.get("feeds_count", existing.get("feeds_count", 0))
    merged["health"] = data.get("health", existing.get("health", []))
    merged["stats"] = data.get("stats", existing.get("stats", {}))
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()

    file_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")

    return file_path


def load_seen_guids(data_dir: str = "output/data") -> set[str]:
    """Load all previously seen GUIDs from past daily files."""
    path = Path(data_dir)
    if not path.exists():
        return set()

    guids = set()
    for f in sorted(path.glob("*.json")):
        try:
            daily = json.loads(f.read_text(encoding="utf-8"))
            for article in daily.get("articles", []):
                guids.add(article.get("guid", ""))
        except (json.JSONDecodeError, KeyError):
            continue
    return guids


def load_all_results(data_dir: str = "output/data") -> list[dict]:
    """Load all daily results, sorted by date descending."""
    path = Path(data_dir)
    if not path.exists():
        return []

    results = []
    for f in sorted(path.glob("*.json"), reverse=True):
        try:
            results.append(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return results


def cleanup_old_data(data_dir: str = "output/data", keep_days: int = 90) -> int:
    """Delete data files older than keep_days. Returns number of files removed."""
    path = Path(data_dir)
    if not path.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = 0
    for f in path.glob("*.json"):
        try:
            date_str = f.stem
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if file_date < cutoff:
                f.unlink()
                removed += 1
        except (ValueError, OSError):
            continue
    return removed
