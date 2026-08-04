#!/usr/bin/env python
"""
Find and repair already-published articles that are cut off mid-sentence.

These are posts that went live while the AI rewrite was failing: the bot fell
back to the raw RSS excerpt, which ends in "…". This script finds them, asks
Gemini to rebuild each into a complete article, and updates the post in place.

The permalink is never changed, so existing links and SEO stay intact.

Usage:
    python regenerate_truncated.py              # dry run — list only, no changes
    python regenerate_truncated.py --show       # dry run + show proposed new text
    python regenerate_truncated.py --apply      # write the changes to WordPress
    python regenerate_truncated.py --apply --limit 5     # do the first 5 only
    python regenerate_truncated.py --show --id 1234       # target one post

Always review a --show run before using --apply.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from src.publisher import WordPressClient
from src.rewriter import Rewriter, RewriteError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("regenerate")
logging.getLogger("httpx").setLevel(logging.WARNING)

_PROJECT_ROOT = Path(__file__).parent

# A body ending in any of these was cut off by the feed, not by our writer.
_TRUNCATION_MARKERS = ("…", "...", "‥", "···")

# Below this length a post is a stub regardless of how it ends.
_SUSPICIOUS_LENGTH = 90


def _plain_text(html: str) -> str:
    return BeautifulSoup(html or "", "lxml").get_text(separator=" ", strip=True)


def is_truncated(body_text: str) -> tuple[bool, str]:
    """Return (needs_repair, reason)."""
    text = body_text.strip()
    if not text:
        return True, "本文が空"
    for marker in _TRUNCATION_MARKERS:
        if text.endswith(marker):
            return True, f"末尾が「{marker}」で途切れている"
    if len(text) < _SUSPICIOUS_LENGTH:
        return True, f"本文が極端に短い（{len(text)}文字）"
    # A finished Japanese sentence ends with one of these.
    if not text.endswith(("。", "！", "？", "」", "）", "!", "?")):
        return True, "文が句点で終わっていない"
    return False, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually update WordPress")
    parser.add_argument("--show", action="store_true", help="print the proposed new text")
    parser.add_argument("--limit", type=int, default=0, help="process at most N posts")
    parser.add_argument("--id", type=int, action="append", help="only this post ID (repeatable)")
    args = parser.parse_args()

    load_dotenv()
    import os

    missing = [
        k for k in ("GEMINI_API_KEY", "WP_BASE_URL", "WP_USERNAME", "WP_APP_PASSWORD")
        if not os.environ.get(k, "").strip()
    ]
    if missing:
        logger.critical("Missing env vars: %s", ", ".join(missing))
        return 1

    with open(_PROJECT_ROOT / "config" / "sources.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    wp_config = config.get("wordpress", {})
    gemini_config = config.get("gemini", {})

    wp = WordPressClient(
        base_url=os.environ["WP_BASE_URL"],
        username=os.environ["WP_USERNAME"],
        app_password=os.environ["WP_APP_PASSWORD"],
        post_type=wp_config.get("post_type", "horse_racing_news"),
    )
    if not wp.verify_connection():
        return 1

    rewriter = Rewriter(
        api_key=os.environ["GEMINI_API_KEY"],
        model=gemini_config.get("model", "gemini-flash-lite-latest"),
        fallback_models=gemini_config.get("fallback_models", []),
    )

    if not args.apply:
        print("\n*** DRY RUN — WordPress will not be modified. Use --apply to write. ***\n")

    # ── Scan ──────────────────────────────────────────────────────────────────
    logger.info("Scanning published articles...")
    broken: list[dict] = []
    total = 0
    for post in wp.iter_posts():
        total += 1
        if args.id and post["id"] not in args.id:
            continue
        body_text = _plain_text(post.get("content", {}).get("rendered", ""))
        needs_repair, reason = is_truncated(body_text)
        if needs_repair:
            broken.append(
                {
                    "id": post["id"],
                    "title": _plain_text(post.get("title", {}).get("rendered", "")),
                    "body": body_text,
                    "date": post.get("date", ""),
                    "link": post.get("link", ""),
                    "reason": reason,
                }
            )

    logger.info("Scanned %d post(s) — %d need repair", total, len(broken))
    if not broken:
        print("\n途切れている記事は見つかりませんでした。")
        return 0

    print(f"\n{'='*70}\n修復対象: {len(broken)}件\n{'='*70}")
    for b in broken:
        print(f"  ID={b['id']}  {b['date'][:10]}  [{b['reason']}]")
        print(f"    {b['title'][:60]}")
    print()

    if args.limit:
        broken = broken[: args.limit]
        print(f"--limit {args.limit} により先頭{len(broken)}件のみ処理します。\n")

    # ── Repair ────────────────────────────────────────────────────────────────
    repaired = failed = 0
    for i, b in enumerate(broken, 1):
        logger.info("[%d/%d] ID=%d %s", i, len(broken), b["id"], b["title"][:40])
        try:
            new_title, new_body = rewriter.repair_truncated(b["title"], b["body"])
        except RewriteError as exc:
            logger.error("  → 再生成失敗: %s", exc)
            failed += 1
            continue

        still_bad, reason = is_truncated(new_body)
        if still_bad:
            # Never replace a broken article with another broken article.
            logger.error("  → 再生成結果も不完全のためスキップ (%s)", reason)
            failed += 1
            continue

        if args.show:
            print(f"\n  --- BEFORE (ID={b['id']}) ---")
            print(f"  題: {b['title']}")
            print(f"  文: {b['body']}")
            print(f"  --- AFTER ---")
            print(f"  題: {new_title}")
            print(f"  文: {new_body}\n")

        if args.apply:
            ok = wp.update_post(
                b["id"],
                title=new_title,
                content=new_body,
                excerpt=new_body[:120],
            )
            if ok:
                repaired += 1
                logger.info("  → 更新完了: %s", b["link"])
            else:
                failed += 1
        else:
            repaired += 1

    print(f"\n{'='*70}")
    if args.apply:
        print(f"完了 — 修復 {repaired}件 / 失敗 {failed}件")
    else:
        print(f"DRY RUN 完了 — 修復可能 {repaired}件 / 失敗 {failed}件")
        print("実際に反映するには --apply を付けて再実行してください。")
    print(f"{'='*70}\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
