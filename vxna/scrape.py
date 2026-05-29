#!/usr/bin/env python3
"""Scrape V2EX VXNA node for RSS feed submission posts and add new feeds to feeds.toml.

Usage:
    python3 vxna/scrape.py [--token TOKEN] [--pages N]

The token can also be set via VXNA_TOKEN environment variable.
"""

import argparse
import json
import os
import re
import subprocess
import sys


def fetch_page(token: str, page: int) -> list:
    """Fetch a single page of VXNA topics via V2EX API v2."""
    # Support token with or without "Bearer " prefix
    auth = token if token.startswith("Bearer ") else f"Bearer {token}"
    result = subprocess.run(
        ["curl", "-s", "-H", f"Authorization: {auth}",
         f"https://www.v2ex.com/api/v2/nodes/vxna/topics?p={page}"],
        capture_output=True, text=True, timeout=30
    )
    try:
        data = json.loads(result.stdout)
        return data.get("result", [])
    except (json.JSONDecodeError, KeyError):
        return []


def extract_rss_feeds(topic: dict) -> list[tuple[str, str, str]]:
    """Extract RSS feed URLs from a VXNA topic.
    
    Returns list of (clean_title, rss_url, site_url).
    """
    content = topic.get("content", "")
    title = topic.get("title", "")

    # Find all URLs in content
    urls = re.findall(r'https?://[^\s\)">\n]+', content)

    # Filter for RSS-like URLs
    rss_urls = [u.rstrip(',.)]') for u in urls
                if re.search(r'(rss|feed|atom|\.xml)', u, re.I)]

    # Also look for explicit "RSS: <url>" patterns
    rss_explicit = re.findall(r'(?:RSS|rss|订阅源)[：:\s]*(https?://[^\s\)">\n]+)', content)
    for u in rss_explicit:
        u = u.rstrip(',.)]')
        if u not in rss_urls:
            rss_urls.append(u)

    feeds = []
    for url in rss_urls:
        site_match = re.search(r'https?://([^/]+)', url)
        site = f'https://{site_match.group(1)}' if site_match else ''
        clean_title = re.sub(r'申请(收录|更换|移除)|个人博客|～|~', '', title).strip(': ：')
        feeds.append((clean_title, url, site))

    return feeds


def load_existing_urls(feeds_file: str) -> set:
    """Load existing feed URLs from feeds.toml."""
    try:
        with open(feeds_file) as f:
            content = f.read()
        return set(re.findall(r'url = "([^"]+)"', content))
    except FileNotFoundError:
        return set()


def add_feeds_to_toml(feeds_file: str, new_feeds: list[tuple[str, str, str]]):
    """Append new feeds to feeds.toml."""
    lines = []
    for title, url, site in new_feeds:
        safe_title = title.replace('"', "'")[:60] if title else url.split('//')[-1].split('/')[0]
        lines.append(f'\n[[category.feed]]')
        lines.append(f'title = "{safe_title}"')
        lines.append(f'url = "{url}"')
        lines.append(f'site = "{site}"')

    with open(feeds_file, 'a') as f:
        f.write('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description="Scrape VXNA for RSS feeds")
    parser.add_argument("--token", default=os.environ.get("VXNA_TOKEN", ""),
                        help="V2EX API bearer token")
    parser.add_argument("--pages", type=int, default=50, help="Max pages to scrape")
    parser.add_argument("--feeds-file", default="feeds.toml", help="Path to feeds.toml")
    parser.add_argument("--dry-run", action="store_true", help="Don't write, just print")
    args = parser.parse_args()

    if not args.token:
        print("Error: VXNA_TOKEN not set. Use --token or set VXNA_TOKEN env var.")
        sys.exit(1)

    existing_urls = load_existing_urls(args.feeds_file)
    print(f"Existing feeds: {len(existing_urls)}")

    all_feeds = []
    for page in range(1, args.pages + 1):
        topics = fetch_page(args.token, page)
        if not topics:
            print(f"Page {page}: empty - stopping")
            break
        print(f"Page {page}: {len(topics)} topics")
        for t in topics:
            all_feeds.extend(extract_rss_feeds(t))

    # Deduplicate by URL
    seen = set()
    unique = []
    for title, url, site in all_feeds:
        if url not in seen:
            seen.add(url)
            unique.append((title, url, site))

    # Filter out existing
    new_feeds = [(t, u, s) for t, u, s in unique if u not in existing_urls]
    print(f"\nNew feeds found: {len(new_feeds)}")

    if args.dry_run:
        for t, u, s in new_feeds:
            print(f"  + {u}")
    else:
        add_feeds_to_toml(args.feeds_file, new_feeds)
        print(f"Added {len(new_feeds)} new feeds to {args.feeds_file}")
        for t, u, s in new_feeds:
            print(f"  + {u}")


if __name__ == "__main__":
    main()