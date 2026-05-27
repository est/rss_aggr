"""AI-powered article classification and scoring. Supports OpenAI, Claude, and OpenRouter."""
import os
import re
from abc import ABC, abstractmethod


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

        info = {"category": "", "tags": [], "score": 5, "summary": ""}
        for line in lines[1:]:
            if line.lower().startswith("category:"):
                info["category"] = line.split(":", 1)[1].strip()
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
            content = (a.get("content") or "")[:self.ARTICLE_LEN].replace('\n', '\n> ')
            items.append('\n'.join([
                a['link'],
                f"Title: {a['title']}",
                f"Category: {skip_prompt}" if skip_prompt else '',
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


class OpenAIClassifier(BaseClassifier):
    def __init__(self, model: str = "gpt-4o-mini"):
        from openai import OpenAI
        self.client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"), timeout=self.TIMEOUT)
        self.model = model

    def _call_api(self, messages, max_tokens, timeout):
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=0.3, max_tokens=max_tokens, timeout=timeout,
        )
        r = resp.choices[0].message.content
        if not r:
            print(f"  [openai] empty response: {resp.to_json()}", flush=True)
        return r


class ClaudeClassifier(BaseClassifier):
    def __init__(self, model: str = "claude-3-haiku-20240307"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"), timeout=self.TIMEOUT)
        self.model = model

    def _call_api(self, messages, max_tokens, timeout):
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, messages=messages, timeout=timeout,
        )
        r = resp.content[0].text
        if not r:
            print(f"  [anthropic] empty response: {resp.to_json()}", flush=True)
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
            temperature=0.3, max_tokens=max_tokens, timeout=timeout,
        )
        r = resp.choices[0].message.content
        if not r:
            print(f"  [openrouter] empty response: {resp.to_json()}", flush=True)
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
    batch_size: int = BaseClassifier.BATCH_SIZE,
    skip_prompt: str = "",
) -> list[dict]:
    """Classify articles in batches."""
    if not articles:
        return []

    classifier = get_classifier(provider, model)
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
        except ValueError as e:
            fail_count += len(batch)
            print(f"  [{batch_num}/{total_batches}] SKIP  {e}", flush=True)
        except Exception as e:
            fail_count += len(batch)
            print(f"  [{batch_num}/{total_batches}] SKIP  {type(e).__name__}: {e}", flush=True)

    print(f"  Summary: {ok_count} classified, {fail_count} failed", flush=True)
    return articles
