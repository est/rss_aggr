"""AI-powered article classification and scoring. Supports OpenAI, Claude, and OpenRouter."""
import json
import os
from abc import ABC, abstractmethod


BATCH_PROMPT = """You are a blog article classifier. For each article, provide:
1. Category: one of {categories}
2. Tags: 3-5 relevant tags
3. Score: 1-10 (10 = must-read, 1 = not interesting)
4. Summary: one-sentence summary (max 280 chars)

Return ONLY a JSON array, one object per article, in the same order:
[
  {{"category": "...", "tags": ["t1","t2"], "score": 8, "summary": "..."}},
  ...
]"""


class BaseClassifier(ABC):
    BATCH_SIZE = 10
    TIMEOUT = 30

    @abstractmethod
    def _call_api(self, messages: list[dict], max_tokens: int, timeout: int) -> str:
        pass

    def classify_batch(self, articles: list[dict], categories: list[str]) -> list[dict]:
        """Classify multiple articles in one API call. Raises on failure."""
        prompt = BATCH_PROMPT.format(categories=", ".join(categories))
        items = []
        for i, a in enumerate(articles):
            content = (a.get("content") or "")[:1500]
            items.append(f"[{i}] Title: {a['title']}\n{content}")
        user_msg = "\n\n".join(items)

        text = self._call_api([
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_msg},
        ], max_tokens=len(articles) * 150, timeout=self.TIMEOUT)

        if text is None:
            raise ValueError("API returned None (model unavailable or rate limited?)")
        text = text.strip()
        if not text:
            raise ValueError("API returned empty string")

        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        results = json.loads(text)
        if isinstance(results, dict):
            results = list(results.values())
        return results


class OpenAIClassifier(BaseClassifier):
    def __init__(self, model: str = "gpt-4o-mini"):
        from openai import OpenAI
        self.client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"), timeout=self.TIMEOUT)
        self.model = model

    def _call_api(self, messages, max_tokens, timeout):
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=0.3, max_tokens=max_tokens,
            timeout=timeout,
        )
        r = resp.choices[0].message.content
        if not r:
            print(f"  [openrouter] empty {resp}")
        return r


class ClaudeClassifier(BaseClassifier):
    def __init__(self, model: str = "claude-3-haiku-20240307"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"), timeout=self.TIMEOUT)
        self.model = model

    def _call_api(self, messages, max_tokens, timeout):
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, messages=messages,
            timeout=timeout,
        )
        r = resp.content[0].text
        if not r:
            print(f"  [anthropic] empty {resp}")
        return r


class OpenRouterClassifier(BaseClassifier):
    def __init__(self, model: str = "nvidia/nemotron-3-nano-30b-a3b:free"):
        from openai import OpenAI
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ.get("OPENROUTER_API_KEY"),
            timeout=self.TIMEOUT,
        )
        self.model = model

    def _call_api(self, messages, max_tokens, timeout):
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=0.3, max_tokens=max_tokens,
            timeout=timeout,
        )
        r = resp.choices[0].message.content
        if not r:
            print(f"  [openrouter] empty {resp}")
        return r


def _check_api_key(provider: str) -> bool:
    env_keys = {
        "openai": "OPENAI_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    return bool(os.environ.get(env_keys.get(provider, "")))


def get_classifier(provider: str = "openai", model: str | None = None) -> BaseClassifier | None:
    if not _check_api_key(provider):
        env_key = {"openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY",
                   "openrouter": "OPENROUTER_API_KEY"}.get(provider, "")
        print(f"  WARNING: {env_key} not set, skipping AI classification", flush=True)
        return None
    if provider == "openai":
        return OpenAIClassifier(model=model or "gpt-4o-mini")
    elif provider == "claude":
        return ClaudeClassifier(model=model or "claude-3-haiku-20240307")
    elif provider == "openrouter":
        return OpenRouterClassifier(model=model or "nvidia/nemotron-3-nano-30b-a3b:free")
    else:
        print(f"  WARNING: unknown provider '{provider}', skipping AI classification", flush=True)
        return None


def classify_articles(
    articles: list[dict],
    provider: str = "openai",
    model: str | None = None,
    categories: list[str] | None = None,
    batch_size: int = 10,
) -> list[dict]:
    """Classify articles in batches. Logs detailed errors for debugging."""
    if not articles:
        return []

    classifier = get_classifier(provider, model)
    if not classifier:
        return []

    default_cats = ["AI/ML", "Web Development", "Infrastructure",
                    "Programming", "Security", "Business", "General Tech"]
    cats = categories or default_cats

    total_batches = (len(articles) + batch_size - 1) // batch_size
    print(f"  {len(articles)} articles, batch_size={batch_size}, {total_batches} batches, provider={provider}, model={model}", flush=True)

    classified = []
    ok_count = 0
    fail_count = 0

    for i in range(0, len(articles), batch_size):
        batch_num = i // batch_size + 1
        batch = articles[i : i + batch_size]
        try:
            results = classifier.classify_batch(batch, cats)
            for j, a in enumerate(batch):
                if j < len(results):
                    a["classification"] = results[j]
            ok_count += len(batch)
            print(f"  [{batch_num}/{total_batches}] OK  {len(batch)} articles", flush=True)
        except json.JSONDecodeError as e:
            fail_count += len(batch)
            # log first 200 chars of raw response for debugging
            raw = getattr(e, "doc", "") or ""
            print(f"  [{batch_num}/{total_batches}] JSON_ERR  {e} | raw[:200]: {raw[:200]}", flush=True)
        except Exception as e:
            fail_count += len(batch)
            print(f"  [{batch_num}/{total_batches}] SKIP  {type(e).__name__}: {e}", flush=True)
        classified.extend(batch)

    print(f"  Summary: {ok_count} classified, {fail_count} failed", flush=True)
    return classified
