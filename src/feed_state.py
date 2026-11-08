"""Feed state tracking: per-feed status, failure counting, auto-disable.

This module stores runtime state for individual feeds (last attempt, failure
counters, backoff/disable bookkeeping). It is distinct from any other runtime
state (e.g. fetch/classify timestamps), which live in their own files.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

FEED_STATE_FILE = "feed_state.json"
LEGACY_FEED_STATE_FILE = "state.json"
# Kept for callers that imported the old constant.
STATE_FILE = FEED_STATE_FILE

STATE_VERSION = 1

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
    text = (error or "").strip().lower()

    # Explicit parse errors come from our own fetcher prefix. Do not infer
    # parse errors from the URL (e.g. ".../feed.xml" on a 500 response).
    if text.startswith("parse error") or "parse error" in text or "invalid feed" in text:
        return ERROR_PARSE

    # Only trust a status code at the start of the message. The URL often
    # contains status-looking slugs like /404-feed.xml; scanning the whole
    # string would misclassify a transient 500 as permanent.
    status_match = re.match(r"^(?:http\s+)?(\d{3})\b", text)
    if status_match:
        status = int(status_match.group(1))
        if status in (404, 410):
            return ERROR_PERMANENT
        if status in (401, 403):
            return ERROR_BLOCKED
        if status == 429:
            return ERROR_RATE_LIMIT
        if 500 <= status <= 599:
            return ERROR_TRANSIENT
        if 400 <= status <= 499:
            # Other client errors are unlikely to heal by themselves.
            return ERROR_PERMANENT
        return ERROR_TRANSIENT

    if "too many requests" in text:
        return ERROR_RATE_LIMIT
    if ("timeout" in text or "timed out" in text or "connection" in text
            or "connect" in text or "reset" in text or "dns" in text
            or "name resolution" in text or "temporarily unavailable" in text):
        return ERROR_TRANSIENT

    return ERROR_TRANSIENT


def load_state(path: str = FEED_STATE_FILE) -> dict:
    """Load feed state, falling back to the legacy ``state.json``.

    The fallback only applies when the requested path is the default/new file
    and it does not exist yet. An explicitly requested path is used as-is.
    Raises ``ValueError`` when the file is not structurally a feed-state object
    so corrupt state fails closed instead of being silently rewritten.
    """
    p = Path(path)
    if not p.exists() and path == FEED_STATE_FILE:
        legacy = Path(LEGACY_FEED_STATE_FILE)
        if legacy.exists():
            p = legacy
    if not p.exists():
        return {"feeds": {}}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{p}: feed state must be a JSON object")
    if "feeds" in data:
        feeds = data["feeds"]
        if not isinstance(feeds, dict):
            raise ValueError(f"{p}: 'feeds' must be a JSON object")
        for url, info in feeds.items():
            if not isinstance(info, dict):
                raise ValueError(
                    f"{p}: feed entry {url!r} must be a JSON object"
                )
    return data


def save_state(state: dict, path: str = FEED_STATE_FILE):
    """Save feed state to JSON (atomic write) with a schema version.

    All existing top-level keys are preserved; ``v`` is added/updated.
    """
    p = Path(path)
    data = {"v": STATE_VERSION, **state}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(p)


def prune_feeds(state: dict, known_urls: set[str]) -> int:
    """Drop feed entries whose URL is no longer configured.

    Top-level keys other than ``feeds`` are preserved. Returns the number of
    removed entries.
    """
    feeds = state.get("feeds")
    if not isinstance(feeds, dict):
        return 0
    unknown = [url for url in feeds if url not in known_urls]
    for url in unknown:
        del feeds[url]
    return len(unknown)


def forget_feed(state: dict, url: str) -> bool:
    """Remove a single feed entry; return whether it existed."""
    feeds = state.get("feeds")
    if not isinstance(feeds, dict) or url not in feeds:
        return False
    del feeds[url]
    return True


def collect_feed_urls(config_path: str | Path = "feeds.toml") -> set[str]:
    """Return every feed URL in a feeds TOML file, including ``skip = true``."""
    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    urls: set[str] = set()
    for cat in data.get("category", []):
        for feed in cat.get("feed", []):
            url = feed.get("url")
            if url:
                urls.add(url)
    return urls


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


def record_content_lengths(
    state: dict,
    url: str,
    lengths: list[int],
    *,
    min_chars: int = 500,
    max_short: int = 5,
    disable_days: int = 3650,
) -> bool:
    """Track whether a feed consistently publishes only short excerpts.

    Only *newly archived* article bodies are passed here, so a feed with no
    new posts does not age toward a disable. One substantive article resets
    the streak. After ``max_short`` consecutive short bodies the feed is
    disabled (for a long quarantine) with reason ``rss does not provide full
    text``; ``src.feed_state --forget URL`` or a successful manual fetch can
    re-enable it.
    """
    if not lengths:
        return False
    feed = _get_feed(state, url)
    short = sum(1 for length in lengths if length < min_chars)
    if short < len(lengths):
        # At least one full-text article: this feed does produce full content.
        if feed.get("short_content_streak"):
            feed["short_content_streak"] = 0
        return False

    streak = int(feed.get("short_content_streak") or 0) + short
    feed["short_content_streak"] = streak
    if max_short > 0 and streak >= max_short:
        disable_feed(
            state,
            url,
            "rss does not provide full text",
            disabled_until=_now() + timedelta(days=disable_days),
        )
        feed["error_kind"] = "no_full_text"
        feed["short_content_streak"] = 0
        return True
    return False


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

    Ordering is ``(last_attempt, priority)`` with the least recently attempted
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
        # Fairness first: never-attempted / oldest-attempted feeds go first.
        # Priority only breaks ties, otherwise a large high-priority group
        # would starve every lower-priority feed when max_feeds is hit.
        return (last.timestamp() if last else 0.0, f.get("priority", 99))

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


def _cmd_prune(args) -> int:
    known = collect_feed_urls(args.feeds_config)
    state = load_state(args.state_file)
    removed = prune_feeds(state, known)
    save_state(state, args.state_file)
    print(
        f"pruned {removed} feed(s) not in {args.feeds_config} "
        f"({len(known)} known, {len(state.get('feeds', {}))} remaining)"
    )
    return 0


def _cmd_forget(args) -> int:
    state = load_state(args.state_file)
    existed = forget_feed(state, args.forget)
    save_state(state, args.state_file)
    print(f"forgot {args.forget}" if existed else f"not found: {args.forget}")
    return 0 if existed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect and maintain feed runtime state."
    )
    parser.add_argument(
        "--state-file",
        default=FEED_STATE_FILE,
        help=f"state file to operate on (default: {FEED_STATE_FILE})",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--prune",
        action="store_true",
        help="remove feed entries not present in the feeds config",
    )
    group.add_argument(
        "--forget",
        metavar="URL",
        help="remove a single feed entry by URL",
    )
    parser.add_argument(
        "--feeds-config",
        default="feeds.toml",
        help="feeds TOML file used by --prune (default: feeds.toml)",
    )
    args = parser.parse_args(argv)

    if args.prune:
        return _cmd_prune(args)
    return _cmd_forget(args)


if __name__ == "__main__":
    sys.exit(main())
