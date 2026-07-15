from __future__ import annotations

"""
Pipeline orchestrator.

Execution order:
  1. Load config + validate env vars
  2. Verify WordPress connection
  3. Fetch articles from all configured sources
  4. For each article:
       a. Dedup check (skip if already seen)
       b. AI rewrite via OpenAI
       c. Publish to WordPress
       d. Record in SQLite
  5. Log summary

Entry point: run() — called by run.py (cron) and check_setup.py (diagnostics).
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

from .classifier import classify_article
from .db import Database
from .dedup import is_duplicate, content_hash, normalize_title
from .fetcher import fetch_all
from .models import Article
from .publisher import WordPressClient
from .rewriter import Rewriter

# ── Logging setup ──────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).parent.parent
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_DIR / "bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── Config helpers ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    config_path = _PROJECT_ROOT / "config" / "sources.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _require_env(keys: list[str]) -> dict[str, str]:
    """Load and validate required environment variables. Exits on missing."""
    values: dict[str, str] = {}
    missing: list[str] = []
    for key in keys:
        val = os.environ.get(key, "").strip()
        if not val:
            missing.append(key)
        else:
            values[key] = val
    if missing:
        logger.critical("Missing required environment variables: %s", ", ".join(missing))
        logger.critical("Copy .env.example to .env and fill in the values.")
        sys.exit(1)
    return values


def _resolve_media_id(category_id: int, config: dict) -> Optional[int]:
    """Return the featured media ID for a given category, falling back to the default."""
    mapping: dict = config.get("category_image_map", {})
    wp_defaults: dict = config.get("wordpress", {})
    return mapping.get(category_id) or wp_defaults.get("default_featured_media_id")


# ── Pipeline ───────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    """
    Main pipeline entry point.

    Args:
        dry_run: When True, skip WordPress publishing and DB writes.
                 Useful for testing the fetch + rewrite steps without side effects.
    """
    load_dotenv()
    if dry_run:
        logger.info("=== DRY RUN MODE — no posts will be published ===")

    env = _require_env(["GEMINI_API_KEY", "WP_BASE_URL", "WP_USERNAME", "WP_APP_PASSWORD"])
    config = _load_config()
    wp_config: dict = config.get("wordpress", {})
    post_status: str = wp_config.get("default_status", "draft")

    # ── Service init ───────────────────────────────────────────────────────────
    db = Database(_PROJECT_ROOT / "data" / "seen_articles.db")
    gemini_model = config.get("gemini", {}).get("model", "gemini-flash-lite-latest")
    rewriter = Rewriter(api_key=env["GEMINI_API_KEY"], model=gemini_model)
    post_type = wp_config.get("post_type", "keiba_news")
    wp = WordPressClient(
        base_url=env["WP_BASE_URL"],
        username=env["WP_USERNAME"],
        app_password=env["WP_APP_PASSWORD"],
        post_type=post_type,
    )

    if not dry_run:
        if not wp.verify_connection():
            logger.critical("WordPress connection failed. Aborting run.")
            sys.exit(1)
        if not wp.verify_post_type():
            logger.critical(
                "Custom post type '%s' REST endpoint not found. "
                "Confirm it is registered with show_in_rest=True in WordPress.",
                post_type,
            )
            sys.exit(1)

    # ── Fetch ──────────────────────────────────────────────────────────────────
    articles: list[Article] = fetch_all(config)
    logger.info("Fetched %d article(s) total", len(articles))

    db_stats_before = db.stats()
    logger.info("DB stats before run: %s", db_stats_before)

    # ── Process ────────────────────────────────────────────────────────────────
    n_processed = 0
    n_skipped_dedup = 0
    n_failed = 0

    for article in articles:
        try:
            # Step 1: Classify article
            article.category_id = classify_article(article.title, article.body)

            # Step 2: dedup check
            if is_duplicate(article, db, rewriter):
                n_skipped_dedup += 1
                continue

            # Step 2: AI rewrite
            rewritten_title, rewritten_body = rewriter.rewrite(article.title, article.body)

            if dry_run:
                logger.info(
                    "[DRY RUN] Would publish: '%s' (source: %s)",
                    rewritten_title, article.source,
                )
                # Still mark as seen so repeated dry-runs don't duplicate logs
                db.mark_seen(
                    url=article.url,
                    title=article.title,
                    title_normalized=normalize_title(article.title),
                    content_hash=content_hash(article.body),
                    source=article.source,
                    wp_post_id=None,
                )
                n_processed += 1
                continue

            # Step 3: publish
            media_id = _resolve_media_id(article.category_id, config)
            post_id = wp.create_post(
                title=rewritten_title,
                content=rewritten_body,
                category_ids=[article.category_id],
                featured_media_id=media_id,
                status=post_status,
                excerpt=rewritten_body[:120] if len(rewritten_body) > 120 else rewritten_body,
            )

            # Step 4: record in DB (even if WP post failed, avoid retrying same article)
            db.mark_seen(
                url=article.url,
                title=article.title,
                title_normalized=normalize_title(article.title),
                content_hash=content_hash(article.body),
                source=article.source,
                wp_post_id=post_id,
            )
            n_processed += 1

        except Exception as exc:
            logger.error(
                "Unhandled error processing '%s' (%s): %s",
                article.url, article.source, exc,
                exc_info=True,
            )
            n_failed += 1

    # ── Monthly maintenance ────────────────────────────────────────────────────
    # Purge records older than 90 days once per run (cheap no-op if nothing qualifies)
    db.purge_old_records(keep_days=90)

    # ── Summary ────────────────────────────────────────────────────────────────
    logger.info(
        "Run complete — processed=%d  skipped(dedup)=%d  failed=%d",
        n_processed, n_skipped_dedup, n_failed,
    )
