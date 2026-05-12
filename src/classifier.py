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

    @abstractmethod
    def _call_api(self, messages: list[dict], max_tokens: int) -> str:
        pass

    def classify_batch(self, articles: list[dict], categories: list[str]) -> list[dict]:
        """Classify multiple articles in one API call."""
        prompt = BATCH_PROMPT.format(categories=", ".join(categories))
        items = []
        for i, a in enumerate(articles):
            content = (a.get("content") or "")[:1500]
            items.append(f"[{i}] Title: {a['title']}\n{content}")
        user_msg = "\n\n".join(items)

        text = self._call_api([
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_msg},
        ], max_tokens=len(articles) * 150)

        # Parse JSON array, handle markdown code blocks
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        results = json.loads(text)
        if isinstance(results, dict):
            results = list(results.values())
        return results


class OpenAIClassifier(BaseClassifier):
    def __init__(self, model: str = "gpt-4o-mini"):
        from openai import OpenAI
        self.client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.model = model

    def _call_api(self, messages, max_tokens):
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=0.3, max_tokens=max_tokens,
        )
        return resp.choices[0].message.content


class ClaudeClassifier(BaseClassifier):
    def __init__(self, model: str = "claude-3-haiku-20240307"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model

    def _call_api(self, messages, max_tokens):
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, messages=messages,
        )
        return resp.content[0].text


class OpenRouterClassifier(BaseClassifier):
    def __init__(self, model: str = "nvidia/nemotron-3-nano-30b-a3b:free"):
        from openai import OpenAI
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ.get("OPENROUTER_API_KEY"),
        )
        self.model = model

    def _call_api(self, messages, max_tokens):
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=0.3, max_tokens=max_tokens,
        )
        return resp.choices[0].message.content


def get_classifier(provider: str = "openai", model: str | None = None) -> BaseClassifier:
    if provider == "openai":
        return OpenAIClassifier(model=model or "gpt-4o-mini")
    elif provider == "claude":
        return ClaudeClassifier(model=model or "claude-3-haiku-20240307")
    elif provider == "openrouter":
        return OpenRouterClassifier(model=model or "nvidia/nemotron-3-nano-30b-a3b:free")
    else:
        raise ValueError(f"Unknown provider: {provider}")


def classify_articles(
    articles: list[dict],
    provider: str = "openai",
    model: str | None = None,
    categories: list[str] | None = None,
    batch_size: int = 10,
) -> list[dict]:
    """Classify articles in batches."""
    if not articles:
        return []

    default_cats = ["AI/ML", "Web Development", "Infrastructure",
                    "Programming", "Security", "Business", "General Tech"]
    cats = categories or default_cats
    classifier = get_classifier(provider, model)

    classified = []
    for i in range(0, len(articles), batch_size):
        batch = articles[i : i + batch_size]
        try:
            results = classifier.classify_batch(batch, cats)
            for j, a in enumerate(batch):
                if j < len(results):
                    a["classification"] = results[j]
                else:
                    a["classification"] = _fallback(a)
        except Exception as e:
            print(f"  Batch {i//batch_size + 1} failed: {e}")
            for a in batch:
                a["classification"] = _fallback(a, str(e))
        classified.extend(batch)

    return classified


def _fallback(article: dict, error: str = "") -> dict:
    return {
        "category": "Unclassified",
        "tags": [],
        "score": 5,
        "summary": article.get("title", ""),
        "reasoning": f"Classification failed: {error}" if error else "",
    }
