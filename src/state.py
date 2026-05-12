"""State tracking: per-feed last fetch time, round-robin scheduling."""
import json
from datetime import datetime, timezone
from pathlib import Path


STATE_FILE = "state.json"


def load_state(path: str = STATE_FILE) -> dict:
    if not Path(path).exists():
        return {"feeds": {}}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_state(state: dict, path: str = STATE_FILE):
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_fed(state: dict, url: str):
    state.setdefault("feeds", {})[url] = {
        "last_ok": datetime.now(timezone.utc).isoformat(),
    }


def mark_failed(state: dict, url: str, error: str):
    state.setdefault("feeds", {})[url] = {
        "last_ok": state.get("feeds", {}).get(url, {}).get("last_ok"),
        "last_error": error,
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }


def get_due_feeds(feeds: list[dict], state: dict, interval_hours: int = 24) -> list[dict]:
    """Return feeds that haven't been fetched within interval_hours."""
    now = datetime.now(timezone.utc)
    feed_state = state.get("feeds", {})
    due = []
    for f in feeds:
        url = f["xml_url"]
        info = feed_state.get(url, {})
        last_ok = info.get("last_ok")
        if not last_ok:
            due.append(f)
            continue
        try:
            last_dt = datetime.fromisoformat(last_ok)
            hours_since = (now - last_dt).total_seconds() / 3600
            if hours_since >= interval_hours:
                due.append(f)
        except (ValueError, TypeError):
            due.append(f)
    return due


def prioritize_feeds(feeds: list[dict]) -> list[dict]:
    """Sort by priority (lower number = higher priority)."""
    return sorted(feeds, key=lambda f: f.get("priority", 99))
