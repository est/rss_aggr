"""AI-powered article classification and scoring. Supports OpenAI-compatible APIs and Claude."""
import os
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests


# =============================================================================
# Configuration (centralized for easy tuning)
# =============================================================================

@dataclass
class ClassifierConfig:
    """Classifier configuration."""
    # Batching
    max_batch_size: int = 10          # Max articles per batch (avoid semantic drift)
    max_input_tokens: int = 8000      # Target input token budget per batch
    max_article_chars: int = 10000    # Articles longer than this go solo

    # Token estimation overheads
    system_overhead: int = 500        # System prompt tokens
    article_overhead: int = 50        # Per-article formatting tokens (link, title, etc.)

    # API settings
    output_max_tokens: int = 4096     # Max output tokens
    timeout: int = 30                 # API timeout in seconds
    temperature: float = 0.3          # Sampling temperature

    # Token estimation ratios
    cn_tokens_per_char: int = 2       # Tokens per Chinese character
    en_tokens_per_word: float = 1.3   # Tokens per English word

    # Defaults
    default_categories: tuple = ("Tech", "Biz", "Life", "Society", "Insight", "Misc")


CONFIG = ClassifierConfig()


# =============================================================================
# Prompt
# =============================================================================

BATCH_PROMPT = """You are a blog article classifier.

The user will provide some blog URLs with content

For each article, respond in EXACTLY this format (one block per article, separated by blank lines):

https://article-url
category: xxx
score: 1-10 (10 = must-read, 1 = not interesting)
summary: one-sentence summary in zh-CN (max 280 chars)

Do NOT use JSON. Just plain text blocks like above.

For the category:
  - Usually one of {categories}
  - For certain blogs there's a special "category" requirement from the user's input, Evaluate it accordingly
"""


# =============================================================================
# Token estimation & batching
# =============================================================================

def _estimate_tokens(text: str) -> int:
    """Rough token estimation: ~2 tokens per Chinese char, ~1.3 tokens per English word."""
    if not text:
        return 0
    cn_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    en_words = len(text.split()) - cn_chars
    return cn_chars * CONFIG.cn_tokens_per_char + int(en_words * CONFIG.en_tokens_per_word)


def _dynamic_batch(articles: list[dict]) -> list[list[dict]]:
    """Dynamic batching: sort by length, fill batches up to token budget.

    Rules:
    - Max batch size: CONFIG.max_batch_size (default 10)
    - Long articles (> CONFIG.max_article_chars): single article per batch
    - No truncation: full content sent to AI
    """
    long_articles = []
    normal_articles = []

    for a in articles:
        content_len = a.get("content_length") or len(a.get("content") or "")
        if content_len > CONFIG.max_article_chars:
            long_articles.append(a)
        else:
            normal_articles.append(a)

    # Sort by length (shortest first for better packing)
    normal_articles.sort(key=lambda x: x.get("content_length") or len(x.get("content") or ""))

    batches = []
    current_batch = []
    current_tokens = 0

    for a in normal_articles:
        article_tokens = _estimate_tokens(a.get("content") or "") + CONFIG.article_overhead

        if current_batch and (
            current_tokens + article_tokens > CONFIG.max_input_tokens - CONFIG.system_overhead
            or len(current_batch) >= CONFIG.max_batch_size
        ):
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0

        current_batch.append(a)
        current_tokens += article_tokens

    if current_batch:
        batches.append(current_batch)

    # Long articles each get their own batch
    for a in long_articles:
        batches.append([a])

    return batches


# =============================================================================
# Response parsing
# =============================================================================

def _parse_blocks(text: str) -> dict[str, dict]:
    """Parse plain text blocks into {url: {category, tags, score, summary}}."""
    results = {}
    blocks = re.split(r"\n\s*\n", text.strip())

    for block in blocks:
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if not lines:
            continue

        url = lines[0].strip()
        if not url.startswith("http"):
            continue

        info = {"category": "Misc", "tags": [], "score": 5, "summary": ""}
        for line in lines[1:]:
            lower = line.lower()
            if lower.startswith("category:"):
                info["category"] = line.split(":", 1)[1].strip() or "Misc"
            elif lower.startswith("tags:"):
                info["tags"] = [t.strip() for t in line.split(":", 1)[1].split(",")]
            elif lower.startswith("score:"):
                try:
                    info["score"] = int(line.split(":", 1)[1].strip())
                except ValueError:
                    info["score"] = 5
            elif lower.startswith("summary:"):
                info["summary"] = line.split(":", 1)[1].strip()

        results[url] = info
    return results


def normalize_category(category: str) -> str:
    """Normalize classifier category labels for robust comparisons."""
    if not category:
        return ""
    s = category.strip().strip("`\"'").strip().lower()
    for sep in ("(", ":", "-", ",", ";"):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    return s


def is_skip_category(category: str) -> bool:
    """Whether a category should be treated as skip/removal."""
    return normalize_category(category) == "skip"


# =============================================================================
# Base classifier
# =============================================================================

class BaseClassifier(ABC):
    @abstractmethod
    def _call_api(self, messages: list[dict], max_tokens: int, timeout: int) -> str:
        pass

    def classify_batch(self, articles: list[dict], categories: list[str], skip_prompt: str = "") -> dict[str, dict]:
        """Classify articles, returns {url: classification_dict}."""
        prompt = BATCH_PROMPT.format(categories=", ".join(categories))

        items = []
        for a in articles:
            if not a.get('link'):
                continue
            content = (a.get("content") or "").replace('\n', '\n> ')
            item = [a['link'], f"Title: {a['title']}"]
            if skip_prompt:
                item.append(f"Special rule: {skip_prompt}. If the article matches this rule, set category to 'skip'.")
            item.append(f"\n> {content}")
            items.append('\n'.join(item))

        user_msg = "\n\n---\n\n".join(items)
        text = self._call_api([
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_msg},
        ], max_tokens=CONFIG.output_max_tokens, timeout=CONFIG.timeout)

        if text is None:
            raise ValueError("API returned None")
        text = text.strip()
        if not text:
            raise ValueError("API returned empty string")

        # Strip markdown code blocks if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        return _parse_blocks(text)


# =============================================================================
# OpenAI-compatible classifier
# =============================================================================

class OpenAICompatibleClassifier(BaseClassifier):
    """Classifier for any OpenAI-compatible API (OpenAI, OpenRouter, vLLM, Ollama, etc.)."""

    DEFAULT_BASE_URLS = {
        "openai": "https://api.openai.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
    }

    def __init__(self, model: str, base_url: str = "", api_key: str = ""):
        self.model = model
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    def _call_api(self, messages: list[dict], max_tokens: int, timeout: int) -> str:
        resp = self.session.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "temperature": CONFIG.temperature,
                "max_tokens": max_tokens,
            },
            timeout=timeout,
        )
        if not resp.ok:
            print(f"  [openai-compatible] API error: {resp.status_code}", flush=True)
            print(f"  Response body: {resp.text}", flush=True)
            sys.exit(1)
        content = resp.json()["choices"][0]["message"]["content"]
        if not content:
            print(f"  [openai-compatible] empty response", flush=True)
        return content


# =============================================================================
# Claude classifier
# =============================================================================

class ClaudeClassifier(BaseClassifier):
    """Classifier for Anthropic Claude API."""

    def __init__(self, model: str = "claude-3-haiku-20240307"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"), timeout=CONFIG.timeout)
        self.model = model

    def _call_api(self, messages: list[dict], max_tokens: int, timeout: int) -> str:
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=max_tokens, messages=messages, timeout=timeout,
            )
        except Exception as e:
            print(f"  [claude] API error: {e}", flush=True)
            sys.exit(1)
        content = resp.content[0].text
        if not content:
            print(f"  [claude] empty response", flush=True)
        return content


# =============================================================================
# Factory
# =============================================================================

def get_classifier(provider: str = "openai", model: str | None = None,
                   base_url: str = "", api_key_env: str = "") -> BaseClassifier | None:
    """Get classifier instance based on provider configuration."""
    if provider == "claude":
        env_key = api_key_env or "ANTHROPIC_API_KEY"
        if not os.environ.get(env_key):
            print(f"  WARNING: {env_key} not set, skipping AI classification", flush=True)
            return None
        return ClaudeClassifier(model=model or "claude-3-haiku-20240307")

    # OpenAI-compatible providers
    env_key = api_key_env or ("OPENAI_API_KEY" if provider != "custom" else "")
    api_key = os.environ.get(env_key, "") if env_key else ""

    if not api_key and provider in ("openai", "openrouter"):
        print(f"  WARNING: {env_key} not set, skipping AI classification", flush=True)
        return None

    base_url = base_url or OpenAICompatibleClassifier.DEFAULT_BASE_URLS.get(provider, "https://api.openai.com/v1")
    model = model or {"openai": "gpt-4o-mini", "openrouter": "nvidia/nemotron-3-nano-30b-a3b:free"}.get(provider, "gpt-4o-mini")

    return OpenAICompatibleClassifier(model=model, base_url=base_url, api_key=api_key)


# =============================================================================
# Main entry point
# =============================================================================

def classify_articles(
    articles: list[dict],
    provider: str = "openai",
    model: str | None = None,
    categories: list[str] | None = None,
    batch_size: int = CONFIG.max_batch_size,
    skip_prompt: str = "",
    base_url: str = "",
    api_key_env: str = "",
) -> list[dict]:
    """Classify articles using dynamic batching based on content length."""
    if not articles:
        return []

    classifier = get_classifier(provider, model, base_url=base_url, api_key_env=api_key_env)
    if not classifier:
        return []

    cats = list(categories or CONFIG.default_categories)

    # Override max_batch_size if provided
    old_max = CONFIG.max_batch_size
    if batch_size != old_max:
        CONFIG.max_batch_size = batch_size

    batches = _dynamic_batch(articles)
    CONFIG.max_batch_size = old_max

    total_batches = len(batches)
    print(f"  {len(articles)} articles, {total_batches} batches (dynamic), provider={provider}, model={model}", flush=True)

    ok_count = 0
    fail_count = 0

    for batch_num, batch in enumerate(batches, 1):
        try:
            results = classifier.classify_batch(batch, cats, skip_prompt=skip_prompt)
            matched = sum(1 for a in batch if a.get("link", "") in results)
            for a in batch:
                if a.get("link", "") in results:
                    a["classification"] = results[a["link"]]
            ok_count += matched
            missed = len(batch) - matched
            status = f"OK {matched}/{len(batch)}" if not missed else f"OK {matched}/{len(batch)}, {missed} unmatched"
            print(f"  [{batch_num}/{total_batches}] {status}", flush=True)
        except Exception as e:
            print(f"  [{batch_num}/{total_batches}] Batch failed ({type(e).__name__}: {e}), retrying individually...", flush=True)
            for a in batch:
                try:
                    results = classifier.classify_batch([a], cats, skip_prompt=skip_prompt)
                    if a.get("link", "") in results:
                        a["classification"] = results[a["link"]]
                        ok_count += 1
                    else:
                        fail_count += 1
                        print(f"    SKIP {a.get('title', '')[:50]}: unmatched", flush=True)
                except Exception as e2:
                    fail_count += 1
                    print(f"    SKIP {a.get('title', '')[:50]}: {type(e2).__name__}: {e2}", flush=True)

    print(f"  Summary: {ok_count} classified, {fail_count} failed", flush=True)
    return articles
