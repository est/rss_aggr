"""Runtime status report and contract checks (dependency-free)."""
import html
import json
from datetime import datetime, timezone
from pathlib import Path

from src.article_state import (
    ARTICLE_STATE_FILE,
    load_articles,
    summarize_articles,
)
from src.feed_state import FEED_STATE_FILE, load_state, state_summary


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _output_stats(data_dir: str) -> dict:
    base = Path(data_dir)
    days = 0
    last_date = None
    index_exists = (base / "index.md").exists()
    if base.exists():
        for year_dir in base.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            for f in year_dir.glob("*.md"):
                try:
                    parsed = datetime.strptime(f"{year_dir.name}{f.stem}", "%Y%m%d")
                except ValueError:
                    continue
                days += 1
                if last_date is None or parsed > last_date:
                    last_date = parsed
    return {
        "days": days,
        "last_date": last_date.strftime("%Y-%m-%d") if last_date else "",
        "index_exists": index_exists,
    }


def _last_state_activity(state: dict) -> str:
    """Latest successful fetch time (never a failed attempt)."""
    latest = None
    for info in (state.get("feeds") or {}).values():
        dt = _parse_dt(info.get("last_ok"))
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return _iso(latest)


def _last_classified(articles: list[dict]) -> str:
    latest = None
    for article in articles:
        dt = _parse_dt(article.get("classified_at"))
        if dt is None:
            dt = _parse_dt((article.get("classification_meta") or {}).get("classified_at"))
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return _iso(latest)


def build_status(
    data_dir: str = "output",
    feed_state_path: str = FEED_STATE_FILE,
    article_state_path: str = ARTICLE_STATE_FILE,
    *,
    ai_enabled: bool = True,
    state_path: str | None = None,
    articles_path: str | None = None,
) -> dict:
    """Build a status snapshot from runtime state and the article cache.

    ``state_path`` and ``articles_path`` are legacy aliases for
    ``feed_state_path`` and ``article_state_path``. They only apply when the
    corresponding new argument is left at its default, so callers can migrate
    incrementally without changing behaviour.
    """
    if state_path is not None and feed_state_path == FEED_STATE_FILE:
        feed_state_path = state_path
    if articles_path is not None and article_state_path == ARTICLE_STATE_FILE:
        article_state_path = articles_path

    state = load_state(feed_state_path)
    articles = load_articles(article_state_path)
    feeds = state_summary(state)
    article_summary = summarize_articles(article_state_path)
    output = _output_stats(data_dir)
    warnings = []

    if feeds.get("total", 0) > 0:
        disabled_ratio = feeds.get("disabled", 0) / feeds["total"]
        if disabled_ratio >= 1:
            warnings.append("all feeds are disabled; fetching is effectively stopped")
        elif disabled_ratio > 0.7:
            warnings.append(
                f"{feeds['disabled']}/{feeds['total']} feeds disabled "
                f"({disabled_ratio:.0%}); check feed health"
            )
    if ai_enabled and article_summary.get("unclassified", 0) > 0:
        warnings.append(
            f"{article_summary['unclassified']} articles were written without AI "
            f"classification (title only)"
        )
    if article_summary["classified"] > 0 and not output["index_exists"]:
        warnings.append("output/index.md is missing but classified articles exist")

    return {
        "generated_at": _iso(datetime.now(timezone.utc)),
        "feeds": feeds,
        "articles": article_summary,
        "output": output,
        # Prefer run-level markers so pruning the article cache cannot make a
        # successful classification look stale.
        "last_fetch": state.get("last_fetch") or _last_state_activity(state),
        "last_classify": state.get("last_classify") or _last_classified(articles),
        "ai_enabled": ai_enabled,
        "warnings": warnings,
    }


def _render_status_md(status: dict) -> str:
    """Render a human-readable status page without emitting untrusted HTML."""
    feeds = status.get("feeds") or {}
    articles = status.get("articles") or {}
    output = status.get("output") or {}

    lines = [
        "# Runtime Status",
        "",
        f"> Generated at {html.escape(str(status.get('generated_at', '')))}",
        "",
        "| Section | Metric | Value |",
        "|---------|--------|-------|",
    ]
    rows = [
        ("Feeds", "Total", feeds.get("total", 0)),
        ("Feeds", "Healthy", feeds.get("healthy", 0)),
        ("Feeds", "Failing", feeds.get("failing", 0)),
        ("Feeds", "Disabled", feeds.get("disabled", 0)),
        ("Feeds", "Retry pending", feeds.get("retry_pending", 0)),
        ("Articles", "Total", articles.get("total", 0)),
        ("Articles", "Pending", articles.get("pending", 0)),
        ("Articles", "Classified", articles.get("classified", 0)),
        ("Articles", "Title only", articles.get("unclassified", 0)),
        ("Articles", "Skipped", articles.get("skipped", 0)),
        ("Articles", "Failed", articles.get("failed", 0)),
        ("Output", "Days", output.get("days", 0)),
        ("Output", "Last date", output.get("last_date", "") or "—"),
        ("Output", "Index exists", "yes" if output.get("index_exists") else "no"),
        ("Runtime", "Last fetch", status.get("last_fetch", "") or "—"),
        ("Runtime", "Last classify", status.get("last_classify", "") or "—"),
        ("Runtime", "AI enabled", "yes" if status.get("ai_enabled", True) else "no"),
    ]
    for section, metric, value in rows:
        lines.append(
            f"| {html.escape(section)} | {html.escape(metric)} | "
            f"{html.escape(str(value))} |"
        )
    lines.append("")

    warnings = status.get("warnings") or []
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {html.escape(str(warning))}")
        lines.append("")

    return "\n".join(lines)


def write_status(status: dict, root: str = "status.json", data_dir: str = "output") -> None:
    """Write status.json at the repo root and a readable output/status.md."""
    root_path = Path(root)
    root_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = root_path.with_name(root_path.name + ".tmp")
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(root_path)

    md_path = Path(data_dir) / "status.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_tmp = md_path.with_name(md_path.name + ".tmp")
    md_tmp.write_text(_render_status_md(status), encoding="utf-8")
    md_tmp.replace(md_path)


def print_summary(status: dict) -> str:
    """Print and return a compact Markdown summary table."""
    feeds = status.get("feeds") or {}
    articles = status.get("articles") or {}
    output = status.get("output") or {}
    lines = [
        "## RSS status",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Feeds healthy/failing/disabled | "
        f"{feeds.get('healthy', 0)}/{feeds.get('failing', 0)}/{feeds.get('disabled', 0)} |",
        f"| Articles pending/classified/title-only/skipped/failed | "
        f"{articles.get('pending', 0)}/{articles.get('classified', 0)}/"
        f"{articles.get('unclassified', 0)}/{articles.get('skipped', 0)}/"
        f"{articles.get('failed', 0)} |",
        f"| Output days / index | {output.get('days', 0)} / "
        f"{'yes' if output.get('index_exists') else 'no'} |",
        f"| Last fetch | {status.get('last_fetch', '') or '—'} |",
        f"| Last classify | {status.get('last_classify', '') or '—'} |",
        f"| AI enabled | {'yes' if status.get('ai_enabled', True) else 'no'} |",
        f"| Warnings | {len(status.get('warnings') or [])} |",
        "",
    ]
    text = "\n".join(lines)
    print(text, flush=True)
    return text


def _is_stale(value: str, freshness_hours: int, now: datetime) -> bool:
    dt = _parse_dt(value)
    if dt is None:
        return True
    age_hours = (now - dt).total_seconds() / 3600
    return age_hours > freshness_hours


def check_status(
    status: dict,
    max_backlog: int = 2000,
    freshness_hours: int = 24,
    ai_enabled: bool = True,
) -> list[str]:
    """Return hard contract violations; an empty list means healthy.

    A high disabled-feed ratio is reported as a warning by build_status (and
    is deliberately not a hard failure here). Pending-backlog and freshness
    breaches, plus a missing output index, are failures. When AI is disabled,
    pending rows are intentionally waiting for cleanup: they do not count as
    backlog and no fresh classification is required.
    """
    violations = []
    now = datetime.now(timezone.utc)
    articles = status.get("articles") or {}
    feeds = status.get("feeds") or {}
    output = status.get("output") or {}

    pending = int(articles.get("pending", 0) or 0)
    failed = int(articles.get("failed", 0) or 0)
    backlog = failed if not ai_enabled else pending + failed
    if max_backlog > 0 and backlog > max_backlog:
        detail = (
            f"{failed} failed"
            if not ai_enabled
            else f"{pending} pending + {failed} failed"
        )
        violations.append(
            f"article backlog too large: {detail} (limit {max_backlog})"
        )

    total_feeds = int(feeds.get("total", 0) or 0)
    disabled_feeds = int(feeds.get("disabled", 0) or 0)
    if total_feeds > 0 and disabled_feeds >= total_feeds:
        violations.append("all feeds are disabled; fetching is effectively stopped")
    elif total_feeds > 0 and _is_stale(
        status.get("last_fetch", ""), freshness_hours, now
    ):
        violations.append(
            f"last fetch is missing or older than {freshness_hours}h"
        )

    if ai_enabled and backlog > 0:
        if _is_stale(status.get("last_classify", ""), freshness_hours, now):
            violations.append(
                f"articles are pending/failed but last classify is missing or "
                f"older than {freshness_hours}h"
            )

    classified = int(articles.get("classified", 0) or 0)
    if classified > 0 and not output.get("index_exists"):
        violations.append("classified articles exist but output/index.md is missing")

    return violations
