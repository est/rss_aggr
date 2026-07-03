"""AI-powered article classification and scoring. Supports OpenAI-compatible APIs and Claude."""
import os
import re
from abc import ABC, abstractmethod

import requests


def _smart_truncate(text: str, max_len: int) -> str:
    """Truncate text at sentence/paragraph boundary near max_len."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    # Try to cut at paragraph boundary
    last_para = truncated.rfind("\n\n")
    if last_para > max_len * 0.6:
        return truncated[:last_para]
    # Try to cut at sentence boundary (Chinese and English)
    for sep in ("。", ".", "\n", "！", "!", "？", "?"):
        last_sep = truncated.rfind(sep)
        if last_sep > max_len * 0.6:
            return truncated[:last_sep + 1]
    return truncated


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
            if line.lower().startswith("category:"):
                info["category"] = line.split(":", 1)[1].strip() or "Misc"
            elif line.lower().startswith("tags:"):
                info["tags"] = [t.strip() for t in line.split(":", 1)[1].split(",")]
            elif line.lower().startswith("score:"):
                try:
                    info["score"] = int(line.split(":", 1)[1].strip())
                except ValueError:
                    info["score"] = 5
            elif line.lower().startswith("summary:"):
                info["summary"] = line.split(":", 1)[1].strip()

        results[url] = info
    return results


def normalize_category(category: str) -> str:
    """Normalize classifier category labels for robust comparisons."""
    if not category:
        return ""
    s = category.strip().strip("`\"'").strip().lower()
    # Keep only leading token before obvious commentary markers.
    for sep in ("(", ":", "-", ",", ";"):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    return s


def is_skip_category(category: str) -> bool:
    """Whether a category should be treated as skip/removal."""
    return normalize_category(category) == "skip"


class BaseClassifier(ABC):
    BATCH_SIZE = 10
    TIMEOUT = 30
    MAX_TOKENS = 4096  # BATCH_SIZE * 200 tokens == around 4k should be enough
    ARTICLE_LEN = 1500

    @abstractmethod
    def _call_api(self, messages: list[dict], max_tokens: int, timeout: int) -> str:
        pass

    def classify_batch(self, articles: list[dict], categories: list[str], skip_prompt: str = "") -> dict[str, dict]:
        """Classify articles, returns {url: classification_dict}."""
        prompt = BATCH_PROMPT.format(
            categories=", ".join(categories),
        )
        items = []
        for a in articles:
            if not a.get('link'):
                continue
            content = _smart_truncate(a.get("content") or "", self.ARTICLE_LEN).replace('\n', '\n> ')
            items.append('\n'.join([
                a['link'],
                f"Title: {a['title']}",
                f"Special rule: {skip_prompt}. If the article matches this rule, set category to 'skip'." if skip_prompt else '',
                f"\n> {content}"
            ]))
        user_msg = "\n\n---\n\n".join(items)

        text = self._call_api([
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_msg},
        ], max_tokens=self.MAX_TOKENS, timeout=self.TIMEOUT)

        if text is None:
            raise ValueError("API returned None")
        text = text.strip()
        if not text:
            raise ValueError("API returned empty string")

        # Strip markdown code blocks if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        return _parse_blocks(text)


class OpenAICompatibleClassifier(BaseClassifier):
    """Classifier for any OpenAI-compatible API (OpenAI, OpenRouter, vLLM, Ollama, etc.)."""

    DEFAULT_BASE_URLS = {
        "openai": "https://api.openai.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
    }

    def __init__(self, model: str, base_url: str = "", api_key: str = ""):
        self.model = model
        # Resolve base URL: use provided or default based on provider hints
        if base_url:
            self.base_url = base_url.rstrip("/")
        else:
            self.base_url = "https://api.openai.com/v1"
        self.api_key = api_key
        self.session = requests.Session()
        # Only set Authorization header if api_key is provided
        # Some providers (e.g., local models) don't require authentication
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    def _call_api(self, messages: list[dict], max_tokens: int, timeout: int) -> str:
        import sys
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        resp = self.session.post(url, json=payload, timeout=timeout)
        if not resp.ok:
            print(f"  [openai-compatible] API error: {resp.status_code}", flush=True)
            print(f"  Response body: {resp.text}", flush=True)
            sys.exit(1)
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if not content:
            usage = data.get("usage", {})
            print(f"  [openai-compatible] empty response (tokens: {usage})", flush=True)
        return content


class ClaudeClassifier(BaseClassifier):
    """Classifier for Anthropic Claude API."""

    def __init__(self, model: str = "claude-3-haiku-20240307"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"), timeout=self.TIMEOUT)
        self.model = model

    def _call_api(self, messages, max_tokens, timeout):
        import sys
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=max_tokens, messages=messages, timeout=timeout,
            )
        except Exception as e:
            print(f"  [claude] API error: {e}", flush=True)
            sys.exit(1)
        r = resp.content[0].text
        if not r:
            print(f"  [claude] empty response", flush=True)
        return r


def get_classifier(provider: str = "openai", model: str | None = None,
                   base_url: str = "", api_key_env: str = "") -> BaseClassifier | None:
    """Get classifier instance based on provider configuration.

    Args:
        provider: "openai" (OpenAI-compatible) or "claude" (Anthropic)
        model: Model name (provider-specific default if empty)
        base_url: API base URL for OpenAI-compatible providers
        api_key_env: Environment variable name for API key
    """
    if provider == "claude":
        env_key = api_key_env or "ANTHROPIC_API_KEY"
        if not os.environ.get(env_key):
            print(f"  WARNING: {env_key} not set, skipping AI classification", flush=True)
            return None
        return ClaudeClassifier(model=model or "claude-3-haiku-20240307")

    # OpenAI-compatible providers (openai, openrouter, custom, etc.)
    # For custom provider, api_key is optional (some local APIs don't need auth)
    if provider == "custom":
        env_key = api_key_env or ""
    else:
        env_key = api_key_env or "OPENAI_API_KEY"
    api_key = os.environ.get(env_key, "") if env_key else ""

    # Resolve default base URL
    if not base_url:
        base_url = OpenAICompatibleClassifier.DEFAULT_BASE_URLS.get(provider, "https://api.openai.com/v1")

    # Resolve default model
    default_models = {
        "openai": "gpt-4o-mini",
        "openrouter": "nvidia/nemotron-3-nano-30b-a3b:free",
    }
    resolved_model = model or default_models.get(provider, "gpt-4o-mini")

    # For openai/openrouter, require api_key; for custom, it's optional
    if not api_key and provider in ("openai", "openrouter"):
        print(f"  WARNING: {env_key} not set, skipping AI classification", flush=True)
        return None

    return OpenAICompatibleClassifier(model=resolved_model, base_url=base_url, api_key=api_key)


def classify_articles(
    articles: list[dict],
    provider: str = "openai",
    model: str | None = None,
    categories: list[str] | None = None,
    batch_size: int = BaseClassifier.BATCH_SIZE,
    skip_prompt: str = "",
    base_url: str = "",
    api_key_env: str = "",
) -> list[dict]:
    """Classify articles in batches. On batch failure, retry each article individually."""
    if not articles:
        return []

    classifier = get_classifier(provider, model, base_url=base_url, api_key_env=api_key_env)
    if not classifier:
        return []

    default_cats = ["Tech", "Biz", "Life", "Society", "Insight", "Misc"]
    cats = categories or default_cats

    total_batches = (len(articles) + batch_size - 1) // batch_size
    print(f"  {len(articles)} articles, batch_size={batch_size}, {total_batches} batches, provider={provider}, model={model}", flush=True)

    ok_count = 0
    fail_count = 0

    for i in range(0, len(articles), batch_size):
        batch_num = i // batch_size + 1
        batch = articles[i : i + batch_size]
        try:
            results = classifier.classify_batch(batch, cats, skip_prompt=skip_prompt)
            matched = 0
            for a in batch:
                url = a.get("link", "")
                if url in results:
                    a["classification"] = results[url]
                    matched += 1
            ok_count += matched
            missed = len(batch) - matched
            status = f"OK {matched}/{len(batch)}" if not missed else f"OK {matched}/{len(batch)}, {missed} unmatched"
            print(f"  [{batch_num}/{total_batches}] {status}", flush=True)
        except Exception as e:
            # Batch failed — retry each article individually
            print(f"  [{batch_num}/{total_batches}] Batch failed ({type(e).__name__}: {e}), retrying individually...", flush=True)
            for a in batch:
                try:
                    results = classifier.classify_batch([a], cats, skip_prompt=skip_prompt)
                    url = a.get("link", "")
                    if url in results:
                        a["classification"] = results[url]
                        ok_count += 1
                    else:
                        fail_count += 1
                        print(f"    SKIP {a.get('title', '')[:50]}: unmatched", flush=True)
                except Exception as e2:
                    fail_count += 1
                    print(f"    SKIP {a.get('title', '')[:50]}: {type(e2).__name__}: {e2}", flush=True)

    print(f"  Summary: {ok_count} classified, {fail_count} failed", flush=True)
    return articles
