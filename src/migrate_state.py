"""One-shot migration from legacy ``state.json``/``articles.json``.

The new layout uses ``feed_state.json`` and ``article_state.json``. The loaders
already fall back to the legacy files; this command makes the migration explicit
for CI and writes the new files before validation/snapshot.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from src.article_state import (
    ARTICLE_STATE_FILE,
    LEGACY_ARTICLE_STATE_FILE,
    load_articles,
    save_articles,
)
from src.feed_state import (
    FEED_STATE_FILE,
    LEGACY_FEED_STATE_FILE,
    load_state,
    save_state,
)


def migrate(remove_legacy: bool = False) -> tuple[int, int]:
    state = load_state()
    articles = load_articles()
    save_state(state)
    save_articles(articles)
    if remove_legacy:
        for name in (LEGACY_FEED_STATE_FILE, LEGACY_ARTICLE_STATE_FILE):
            path = Path(name)
            if path.exists():
                path.unlink()
    return len(state.get("feeds") or {}), len(articles)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate state.json/articles.json to feed_state/article_state."
    )
    parser.add_argument(
        "--remove-legacy",
        action="store_true",
        help="delete state.json/articles.json after writing the new files",
    )
    args = parser.parse_args(argv)
    feeds, articles = migrate(remove_legacy=args.remove_legacy)
    print(
        f"migrated {feeds} feed state entries and {articles} article rows "
        f"to {FEED_STATE_FILE} / {ARTICLE_STATE_FILE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
