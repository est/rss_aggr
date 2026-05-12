"""State tracking: per-feed status, failure counting, auto-disable."""
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


def _get_feed(state: dict, url: str) -> dict:
    return state.setdefault("feeds", {}).setdefault(url, {})


def mark_fed(state: dict, url: str):
    feed = _get_feed(state, url)
    feed["last_ok"] = datetime.now(timezone.utc).isoformat()
    feed["consecutive_failures"] = 0


def mark_failed(state: dict, url: str, error: str):
    feed = _get_feed(state, url)
    feed["last_ok"] = feed.get("last_ok")
    feed["last_error"] = error
    feed["failed_at"] = datetime.now(timezone.utc).isoformat()
    feed["consecutive_failures"] = feed.get("consecutive_failures", 0) + 1


def disable_feed(state: dict, url: str, reason: str = "too many failures"):
    feed = _get_feed(state, url)
    feed["disabled"] = True
    feed["disabled_at"] = datetime.now(timezone.utc).isoformat()
    feed["disabled_reason"] = reason


def is_disabled(state: dict, url: str) -> bool:
    return _get_feed(state, url).get("disabled", False)


def get_due_feeds(feeds: list[dict], state: dict, interval_hours: int = 24,
                  max_failures: int = 0) -> list[dict]:
    """Return feeds that haven't been fetched within interval_hours, skipping disabled."""
    now = datetime.now(timezone.utc)
    due = []
    for f in feeds:
        url = f["xml_url"]
        feed = _get_feed(state, url)

        if feed.get("disabled"):
            continue

        if max_failures > 0 and feed.get("consecutive_failures", 0) >= max_failures:
            disable_feed(state, url, f"failed {feed['consecutive_failures']}x consecutively")
            continue

        last_ok = feed.get("last_ok")
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


def disable_stats(state: dict) -> dict:
    """Return disabled feed statistics."""
    feeds = state.get("feeds", {})
    disabled = [url for url, info in feeds.items() if info.get("disabled")]
    failing = [url for url, info in feeds.items()
               if not info.get("disabled") and info.get("consecutive_failures", 0) > 0]
    return {
        "total": len(feeds),
        "disabled": len(disabled),
        "failing": len(failing),
        "disabled_urls": disabled,
        "failing_urls": failing,
    }
