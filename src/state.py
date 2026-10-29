"""State tracking: per-feed status, failure counting, auto-disable."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = "state.json"

# Error kinds used to decide retry/disable behaviour.
ERROR_PERMANENT = "permanent"
ERROR_TRANSIENT = "transient"
ERROR_BLOCKED = "blocked"
ERROR_RATE_LIMIT = "rate_limit"
ERROR_PARSE = "parse"

TRANSIENT_BASE_HOURS = 6
TRANSIENT_MAX_HOURS = 24 * 7
RATE_LIMIT_RETRY_HOURS = 2
BLOCKED_RETRY_DAYS = 7
DEFAULT_DISABLE_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value) -> datetime | None:
    """Parse an ISO timestamp (or pass through a datetime), returning UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify_error(error: str) -> str:
    """Map an error message to one of the ERROR_* kinds.

    Defaults to ``transient`` so unknown failures are retried rather than
    silently abandoning a feed.
    """
    text = (error or "").lower()

    # Specific HTTP statuses first: they are unambiguous when present.
    if re.search(r"\b(?:404|410)\b", text):
        return ERROR_PERMANENT
    if re.search(r"\b(?:401|403)\b", text):
        return ERROR_BLOCKED
    if re.search(r"\b429\b", text) or "too many requests" in text:
        return ERROR_RATE_LIMIT

    if "parse" in text or "xml" in text or "invalid feed" in text:
        return ERROR_PARSE

    if re.search(r"\b5\d\d\b", text) or "server error" in text:
        return ERROR_TRANSIENT
    if ("timeout" in text or "timed out" in text or "connection" in text
            or "connect" in text or "reset" in text or "dns" in text
            or "name resolution" in text or "temporarily unavailable" in text):
        return ERROR_TRANSIENT

    return ERROR_TRANSIENT


def load_state(path: str = STATE_FILE) -> dict:
    if not Path(path).exists():
        return {"feeds": {}}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_state(state: dict, path: str = STATE_FILE):
    """Save state to JSON file (atomic write)."""
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(p)


def _get_feed(state: dict, url: str) -> dict:
    return state.setdefault("feeds", {}).setdefault(url, {})


def _clear_keys(feed: dict, *keys: str):
    for key in keys:
        feed.pop(key, None)


def mark_ok(state: dict, url: str, status: str = "ok"):
    """Record a successful fetch, clearing all failure/disable bookkeeping."""
    feed = _get_feed(state, url)
    now = _iso(_now())
    feed["last_ok"] = now
    feed["last_attempt"] = now
    feed["last_status"] = status
    feed["consecutive_failures"] = 0
    _clear_keys(
        feed,
        "last_error",
        "error_kind",
        "retry_at",
        "disabled",
        "disabled_at",
        "disabled_reason",
        "disabled_until",
    )


def mark_failed(state: dict, url: str, error: str, kind: str | None = None):
    """Record a failed fetch and schedule the next allowed attempt.

    Transient, rate-limit and blocked failures are never disabled here; they
    are retried after ``retry_at`` so unattended runs keep recovering.
    """
    feed = _get_feed(state, url)
    now = _now()
    if kind not in (ERROR_PERMANENT, ERROR_TRANSIENT, ERROR_BLOCKED,
                    ERROR_RATE_LIMIT, ERROR_PARSE):
        kind = classify_error(error)

    feed["last_attempt"] = _iso(now)
    feed["last_error"] = error
    feed["error_kind"] = kind
    failures = feed.get("consecutive_failures", 0) + 1
    feed["consecutive_failures"] = failures

    if kind == ERROR_TRANSIENT:
        hours = TRANSIENT_BASE_HOURS * (2 ** (failures - 1))
        hours = min(hours, TRANSIENT_MAX_HOURS)
        feed["retry_at"] = _iso(now + timedelta(hours=hours))
    elif kind == ERROR_RATE_LIMIT:
        feed["retry_at"] = _iso(now + timedelta(hours=RATE_LIMIT_RETRY_HOURS))
    elif kind == ERROR_BLOCKED:
        feed["retry_at"] = _iso(now + timedelta(days=BLOCKED_RETRY_DAYS))
    else:
        # permanent / parse: no automatic retry.
        _clear_keys(feed, "retry_at")


# Backwards-compatible alias used by older callers.
mark_fed = mark_ok


def disable_feed(state: dict, url: str, reason: str = "too many failures",
                 disabled_until=None):
    """Disable a feed, by default for seven days (then auto re-enabled)."""
    feed = _get_feed(state, url)
    now = _now()
    until = _parse_dt(disabled_until) or (now + timedelta(days=DEFAULT_DISABLE_DAYS))
    feed["disabled"] = True
    feed["disabled_at"] = _iso(now)
    feed["disabled_reason"] = reason
    feed["disabled_until"] = _iso(until)


def is_disabled(state: dict, url: str) -> bool:
    return _get_feed(state, url).get("disabled", False)


def _is_auto_re_enable_due(feed: dict, now: datetime) -> bool:
    until = _parse_dt(feed.get("disabled_until"))
    if until is not None:
        return until <= now
    # Legacy state (pre-ADR-015) has disabled=true but no disabled_until.
    # Re-probe it after the default quarantine instead of keeping it disabled
    # forever.
    disabled_at = _parse_dt(feed.get("disabled_at"))
    if disabled_at is not None:
        return disabled_at + timedelta(days=DEFAULT_DISABLE_DAYS) <= now
    return True


def get_due_feeds(feeds: list[dict], state: dict, interval_hours: int = 24,
                  max_failures: int = 3) -> list[dict]:
    """Return feeds that should be fetched now.

    Ordering is ``(priority, last_attempt)`` with the least recently attempted
    feeds first, which prevents low-priority feeds from starving when the
    per-run feed limit is smaller than the feed count.
    """
    now = _now()
    due = []

    for f in feeds:
        url = f["xml_url"]
        feed = _get_feed(state, url)

        if feed.get("disabled"):
            if _is_auto_re_enable_due(feed, now):
                _clear_keys(
                    feed,
                    "disabled",
                    "disabled_at",
                    "disabled_reason",
                    "disabled_until",
                    "last_error",
                    "error_kind",
                    "retry_at",
                )
                feed["consecutive_failures"] = 0
                due.append(f)
                continue
            continue

        failures = feed.get("consecutive_failures", 0)
        kind = feed.get("error_kind")
        if (max_failures > 0 and failures >= max_failures
                and kind in (ERROR_PERMANENT, ERROR_PARSE)):
            disable_feed(
                state,
                url,
                f"{kind} error failed {failures}x consecutively",
                disabled_until=now + timedelta(days=DEFAULT_DISABLE_DAYS),
            )
            continue

        retry_at = _parse_dt(feed.get("retry_at"))
        if retry_at and retry_at > now:
            continue

        last_attempt = _parse_dt(feed.get("last_attempt"))
        if last_attempt is None:
            # Also honour legacy state that only recorded last_ok.
            last_attempt = _parse_dt(feed.get("last_ok"))
        if last_attempt is None:
            due.append(f)
            continue
        hours_since = (now - last_attempt).total_seconds() / 3600
        if hours_since >= interval_hours:
            due.append(f)

    def _sort_key(f: dict):
        feed = state.get("feeds", {}).get(f["xml_url"], {})
        last = _parse_dt(feed.get("last_attempt")) or _parse_dt(feed.get("last_ok"))
        return (f.get("priority", 99), last.timestamp() if last else 0.0)

    return sorted(due, key=_sort_key)


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


def state_summary(state: dict) -> dict:
    """Aggregate health counters, suitable for a status report."""
    feeds = state.get("feeds", {})
    now = _now()
    summary = {
        "total": len(feeds),
        "healthy": 0,
        "failing": 0,
        "disabled": 0,
        "retry_pending": 0,
        "error_kinds": {},
    }
    for info in feeds.values():
        kind = info.get("error_kind")
        if kind:
            summary["error_kinds"][kind] = summary["error_kinds"].get(kind, 0) + 1

        if info.get("disabled"):
            summary["disabled"] += 1
        elif info.get("consecutive_failures", 0) > 0:
            summary["failing"] += 1
        else:
            summary["healthy"] += 1

        retry_at = _parse_dt(info.get("retry_at"))
        if retry_at and retry_at > now and not info.get("disabled"):
            summary["retry_pending"] += 1

    return summary
