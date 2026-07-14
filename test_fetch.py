#!/usr/bin/env python
"""
Fetch-only test. No OpenAI or WordPress credentials needed.

    python test_fetch.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml
from src.fetcher import fetch_all

CONFIG_PATH = Path("config/sources.yaml")

def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    print("Fetching articles...\n")
    articles = fetch_all(config)

    if not articles:
        print("No articles fetched. Check sources.yaml and your internet connection.")
        sys.exit(1)

    print(f"=== {len(articles)} article(s) fetched ===\n")
    for i, a in enumerate(articles, 1):
        print(f"[{i}] {a.source}")
        print(f"    Title : {a.title}")
        print(f"    URL   : {a.url}")
        print(f"    Body  : {a.body[:80]}{'...' if len(a.body) > 80 else ''}")
        print(f"    Date  : {a.published_at}")
        print()

if __name__ == "__main__":
    main()
