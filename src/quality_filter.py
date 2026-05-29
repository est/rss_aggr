"""Quality-aware fetch filter: skip known bad feeds, apply content farm detection."""
import re
from urllib.parse import urlparse


# Known content farm / low-quality domains (regex patterns)
CONTENT_FARM_PATTERNS = [
    r'juzisuan\.com',
    r'sohu\.com',
    r'163\.com',
    r'baijiahao\.baidu\.com',
    r'toutiao\.com',
    r'zztt\.org',
    r'ithome\.com$',  # news aggregator, not personal blog
    r'cnbeta\.',
    r'36kr\.com',
    r'jianshu\.com',  # medium clone, mixed quality
    r'csdn\.net',  # often scraped/reposted content
    r'blog\.csdn\.net',
    r'juejin\.cn',  # developer platform, mixed quality
]

# Patterns in title that suggest low value
LOW_VALUE_TITLE_PATTERNS = [
    r'^周[报刊]',
    r'^weekly\s+digest',
    r'^daily\s+links',
    r'^monthly',
    r'^月[报刊]',
    r'^每日速递',
    r'^link\s+list',
    r'^newsletter',
    r'^reading\s+list',
    r'^值得阅读',
    r'^本周推荐',
    r'^资源汇总',
    r'^合集',
]

# Patterns that suggest good quality personal blog content
HIGH_VALUE_TITLE_PATTERNS = [
    r'^.{10,}$',  # reasonably long title (not too short)
    r'实践|经验|踩坑|深入|原理|设计|架构|思考|复盘',
    r'(?:how|why|what)\s+(?:i|we|to)',
    r'build|deploy|migrate|refactor',
]


def is_content_farm_domain(url: str) -> bool:
    """Check if a URL belongs to a known content farm domain."""
    domain = urlparse(url).hostname or ""
    for pattern in CONTENT_FARM_PATTERNS:
        if re.search(pattern, domain, re.I):
            return True
    return False


def is_low_value_title(title: str) -> bool:
    """Check if a title suggests low-value content."""
    title_lower = title.lower().strip()
    for pattern in LOW_VALUE_TITLE_PATTERNS:
        if re.search(pattern, title_lower, re.I):
            return True
    return False


def score_article_quality_heuristic(title: str, content: str, link: str = "") -> dict:
    """Heuristic quality scoring without AI.
    
    Returns dict with score (1-10) and reasons.
    """
    reasons = []
    score = 5  # baseline

    # Domain check
    if link and is_content_farm_domain(link):
        score -= 3
        reasons.append("content_farm_domain")

    # Title checks
    if is_low_value_title(title):
        score -= 2
        reasons.append("low_value_title")

    # Content length
    content_text = re.sub(r'<[^>]+>', '', content or "")
    word_count = len(content_text)
    
    if word_count < 100:
        score -= 2
        reasons.append("too_short")
    elif word_count < 300:
        score -= 1
        reasons.append("short_content")
    elif word_count > 1000:
        score += 1
        reasons.append("substantial_content")
    elif word_count > 3000:
        score += 2
        reasons.append("long_form_content")

    # Chinese content depth
    cn_chars = len(re.findall(r'[\u4e00-\u9fff]', content_text))
    if cn_chars > 500:
        score += 1
        reasons.append("chinese_depth")

    # Code blocks suggest technical depth
    code_blocks = len(re.findall(r'```|`[^`]{10,}`', content_text))
    if code_blocks > 2:
        score += 1
        reasons.append("has_code")

    # Images suggest effort
    img_count = len(re.findall(r'!\[', content))
    if img_count > 2:
        score += 1
        reasons.append("has_images")

    # Clickbait detection
    clickbait_patterns = [
        r'震惊', r'惊呆', r'必看', r'速看', r'不看后悔',
        r'万万没想到', r'竟然', r'居然', r'揭秘',
        r'how to make money', r'passive income',
        r'you won.t believe', r'one weird trick',
    ]
    for p in clickbait_patterns:
        if re.search(p, title.lower()):
            score -= 2
            reasons.append("clickbait")
            break

    # Listicle detection (too many list items)
    list_items = len(re.findall(r'^\s*[\d\-\*•]\s*[\.、）\)]', content_text, re.M))
    if list_items > 15:
        score -= 1
        reasons.append("listicle")

    # Link-heavy (aggregator-like)
    link_count = len(re.findall(r'https?://', content_text))
    if link_count > 30:
        score -= 1
        reasons.append("link_heavy")

    score = max(1, min(10, score))
    return {"score": score, "reasons": reasons}


def should_fetch_feed(feed_url: str, feed_title: str = "") -> bool:
    """Determine if a feed is worth fetching based on known quality signals."""
    # Skip known content farms
    if is_content_farm_domain(feed_url):
        return False
    return True


def filter_entries(entries: list[dict], min_heuristic_score: int = 3) -> list[dict]:
    """Filter feed entries using heuristic quality scoring.
    
    Returns only entries that pass quality checks.
    """
    filtered = []
    for entry in entries:
        title = entry.get("title", "")
        content = entry.get("content", "")
        link = entry.get("link", "")

        quality = score_article_quality_heuristic(title, content, link)
        entry["heuristic_score"] = quality["score"]
        entry["quality_reasons"] = quality["reasons"]

        if quality["score"] >= min_heuristic_score:
            filtered.append(entry)

    return filtered
