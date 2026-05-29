"""Feed and article quality scoring system.

Tracks per-feed quality metrics over time and provides heuristics for
detecting content farms, spam sites, and shallow clickbait.
"""
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

QUALITY_FILE = "output/quality.json"


def load_quality(path: str = QUALITY_FILE) -> dict:
    """Load quality tracking data."""
    p = Path(path)
    if not p.exists():
        return {"feeds": {}, "articles": {}, "updated_at": ""}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"feeds": {}, "articles": {}, "updated_at": ""}


def save_quality(data: dict, path: str = QUALITY_FILE):
    """Save quality tracking data."""
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _extract_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        host = urlparse(url).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def score_article_heuristic(title: str, content: str, link: str = "") -> dict:
    """Heuristic article quality scoring (no AI needed).

    Returns dict with:
      - depth_score: 1-10 (depth/originality)
      - is_clickbait: bool
      - is_content_farm: bool
      - word_count: int
      - read_time_min: float
      - signals: list of quality signals detected
    """
    signals = []
    title_lower = title.lower() if title else ""
    content_text = re.sub(r'<[^>]+>', '', content or "")
    word_count = len(content_text)
    read_time = max(0.5, word_count / 400)  # ~400 chars/min for Chinese

    depth_score = 5  # baseline
    is_clickbait = False
    is_content_farm = False

    # === Clickbait detection ===
    clickbait_patterns = [
        r'震惊', r'惊呆', r'必看', r'速看', r'不看后悔',
        r'万万没想到', r'竟然', r'居然', r'揭秘',
        r'how to make money', r'earn \$', r'passive income',
        r'\d+ tips to', r'mind.blowing', r'you won.t believe',
        r'one weird trick', r'free money', r'guaranteed',
    ]
    for p in clickbait_patterns:
        if re.search(p, title_lower):
            is_clickbait = True
            signals.append(f"clickbait_pattern:{p}")
            depth_score -= 2
            break

    # === Content farm detection ===
    # Very short content
    if word_count < 100:
        depth_score -= 2
        signals.append("very_short_content")
    elif word_count < 300:
        depth_score -= 1
        signals.append("short_content")

    # List-heavy content (listicles)
    list_items = len(re.findall(r'^\s*[\d\-\*•]\s*[\.、）\)]', content_text, re.M))
    if list_items > 10:
        depth_score -= 1
        signals.append("many_list_items")

    # Too many links (aggregator-like)
    link_count = len(re.findall(r'https?://', content_text))
    if link_count > 20:
        depth_score -= 1
        signals.append("link_heavy")

    # Code blocks suggest technical depth
    code_blocks = len(re.findall(r'```|`[^`]+`', content_text))
    if code_blocks > 2:
        depth_score += 1
        signals.append("has_code")

    # Long paragraphs suggest depth
    paragraphs = [p.strip() for p in content_text.split('\n\n') if len(p.strip()) > 100]
    if len(paragraphs) > 3:
        depth_score += 1
        signals.append("substantial_paragraphs")

    # Images suggest effort
    img_count = len(re.findall(r'!\[', content_text))
    if img_count > 2:
        signals.append("has_images")

    # Chinese content tends to be denser
    cn_chars = len(re.findall(r'[\u4e00-\u9fff]', content_text))
    if cn_chars > 500:
        depth_score += 1
        signals.append("long_chinese_content")

    # === Domain-level content farm signals ===
    domain = _extract_domain(link)
    farm_domains = [
        'juzisuan.com', 'sohu.com', '163.com', 'baijiahao',
        'toutiao.com', 'zhihu.com/zhuanlan',  # some zhihu is good but many listicles
    ]
    for fd in farm_domains:
        if fd in domain:
            is_content_farm = True
            signals.append(f"known_farm_domain:{fd}")
            depth_score -= 3
            break

    # Clamp score
    depth_score = max(1, min(10, depth_score))

    return {
        "depth_score": depth_score,
        "is_clickbait": is_clickbait,
        "is_content_farm": is_content_farm,
        "word_count": word_count,
        "read_time_min": round(read_time, 1),
        "signals": signals,
    }


def update_feed_quality(quality_data: dict, feed_url: str, article_scores: list[dict]):
    """Update aggregate quality metrics for a feed based on recent articles.

    Each item in article_scores should have:
      - score (AI score 1-10)
      - depth_score (heuristic 1-10)
      - category
    """
    feed = quality_data.setdefault("feeds", {}).setdefault(feed_url, {
        "total_articles": 0,
        "good_articles": 0,
        "skip_articles": 0,
        "avg_score": 5.0,
        "avg_depth": 5.0,
        "quality_trend": [],  # rolling weekly avg scores
        "last_updated": "",
        "tier": "unknown",  # gold/silver/bronze/unknown
    })

    for a in article_scores:
        feed["total_articles"] += 1
        score = a.get("score", 5)
        depth = a.get("depth_score", 5)
        cat = a.get("category", "")

        if cat == "skip" or score <= 2:
            feed["skip_articles"] += 1
        elif score >= 6:
            feed["good_articles"] += 1

        # Running average
        n = feed["total_articles"]
        feed["avg_score"] = round(((feed["avg_score"] * (n - 1)) + score) / n, 2)
        feed["avg_depth"] = round(((feed["avg_depth"] * (n - 1)) + depth) / n, 2)

    # Assign tier based on cumulative metrics
    feed["last_updated"] = datetime.now(timezone.utc).isoformat()
    total = feed["total_articles"]
    if total >= 5:
        good_ratio = feed["good_articles"] / total
        skip_ratio = feed["skip_articles"] / total
        if good_ratio > 0.6 and feed["avg_score"] >= 7:
            feed["tier"] = "gold"
        elif good_ratio > 0.4 and feed["avg_score"] >= 5:
            feed["tier"] = "silver"
        elif skip_ratio > 0.5 or feed["avg_score"] <= 3:
            feed["tier"] = "junk"
        else:
            feed["tier"] = "bronze"

    return feed


def get_feed_tier(feed_url: str, quality_data: dict) -> str:
    """Get quality tier for a feed."""
    feeds = quality_data.get("feeds", {})
    if feed_url in feeds:
        return feeds[feed_url].get("tier", "unknown")
    return "unknown"


def should_skip_feed(feed_url: str, quality_data: dict, min_articles: int = 10) -> bool:
    """Determine if a feed should be auto-disabled due to low quality."""
    feeds = quality_data.get("feeds", {})
    feed = feeds.get(feed_url)
    if not feed:
        return False
    total = feed.get("total_articles", 0)
    if total < min_articles:
        return False
    tier = feed.get("tier", "unknown")
    if tier == "junk":
        return True
    # Auto-skip if >70% articles are skipped and avg score < 3
    skip_ratio = feed.get("skip_articles", 0) / total
    if skip_ratio > 0.7 and feed.get("avg_score", 5) < 3:
        return True
    return False


def get_quality_summary(quality_data: dict) -> dict:
    """Get a summary of all feed quality tiers."""
    feeds = quality_data.get("feeds", {})
    tiers = {"gold": [], "silver": [], "bronze": [], "junk": [], "unknown": []}
    for url, info in feeds.items():
        tier = info.get("tier", "unknown")
        tiers.setdefault(tier, []).append({
            "url": url,
            "total": info.get("total_articles", 0),
            "avg_score": info.get("avg_score", 0),
            "avg_depth": info.get("avg_depth", 0),
            "good_ratio": round(info.get("good_articles", 0) / max(1, info.get("total_articles", 1)), 2),
        })

    # Sort each tier by avg_score desc
    for tier in tiers:
        tiers[tier].sort(key=lambda x: x["avg_score"], reverse=True)

    return {
        "total_feeds": len(feeds),
        "tiers": {k: len(v) for k, v in tiers.items()},
        "feeds": tiers,
    }
