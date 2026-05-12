"""AI-powered article classification and scoring. Supports OpenAI and Claude."""
import json
import os
from abc import ABC, abstractmethod


CLASSIFICATION_PROMPT = """You are a tech article classifier. Given an article's title and content snippet, provide:
1. Category: one of {categories}
2. Tags: 3-5 relevant tags
3. Score: 1-10 (10 = must-read, 1 = not interesting)
4. Summary: one-sentence summary (max 100 chars)
5. Reasoning: brief explanation for the score

Return ONLY valid JSON in this format:
{{
  "category": "...",
  "tags": ["tag1", "tag2"],
  "score": 8,
  "summary": "...",
  "reasoning": "..."
}}"""


class BaseClassifier(ABC):
    @abstractmethod
    def classify(self, title: str, content: str, categories: list[str]) -> dict:
        pass


class OpenAIClassifier(BaseClassifier):
    def __init__(self, model: str = "gpt-4o-mini"):
        from openai import OpenAI
        self.client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.model = model

    def classify(self, title: str, content: str, categories: list[str]) -> dict:
        prompt = CLASSIFICATION_PROMPT.format(categories=", ".join(categories))
        user_msg = f"Title: {title}\n\nContent: {content[:2000]}"

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=300,
        )
        text = resp.choices[0].message.content.strip()
        return json.loads(text)


class ClaudeClassifier(BaseClassifier):
    def __init__(self, model: str = "claude-3-haiku-20240307"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model

    def classify(self, title: str, content: str, categories: list[str]) -> dict:
        prompt = CLASSIFICATION_PROMPT.format(categories=", ".join(categories))
        user_msg = f"Title: {title}\n\nContent: {content[:2000]}"

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=300,
            messages=[{"role": "user", "content": f"{prompt}\n\n{user_msg}"}],
        )
        text = resp.content[0].text.strip()
        return json.loads(text)


def get_classifier(provider: str = "openai", model: str | None = None) -> BaseClassifier:
    """Factory to get the appropriate classifier."""
    if provider == "openai":
        return OpenAIClassifier(model=model or "gpt-4o-mini")
    elif provider == "claude":
        return ClaudeClassifier(model=model or "claude-3-haiku-20240307")
    else:
        raise ValueError(f"Unknown provider: {provider}")


def classify_articles(
    articles: list[dict],
    provider: str = "openai",
    model: str | None = None,
    categories: list[str] | None = None,
) -> list[dict]:
    """Classify a batch of articles, returning enriched versions."""
    if not articles:
        return []

    default_cats = ["AI/ML", "Web Development", "Infrastructure",
                    "Programming", "Security", "Business", "General Tech"]
    cats = categories or default_cats
    classifier = get_classifier(provider, model)

    classified = []
    for article in articles:
        try:
            result = classifier.classify(article["title"], article["content"], cats)
            article["classification"] = result
        except Exception as e:
            article["classification"] = {
                "category": "Unclassified",
                "tags": [],
                "score": 5,
                "summary": article.get("title", ""),
                "reasoning": f"Classification failed: {e}",
            }
        classified.append(article)

    return classified
