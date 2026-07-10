from __future__ import annotations

"""
Three-layer deduplication checked in ascending cost order:

  Layer 1 — Exact URL match        (O(1) SQLite lookup)
  Layer 2 — Title fuzzy match      (in-memory rapidfuzz against recent titles)
  Layer 3 — Content SHA-256 hash   (O(1) SQLite lookup)

Stop at the first match; cheaper checks come first.
"""

import hashlib
import logging
import re

from rapidfuzz import fuzz

from .db import Database
from .models import Article

logger = logging.getLogger(__name__)

# Title similarity threshold (0–100). 80 catches "same race, different outlet"
# rewrites while allowing genuinely different stories through.
FUZZY_THRESHOLD: int = 80

# How many days back to look when building the fuzzy comparison set.
RECENT_DAYS: int = 7


# ── Text helpers ───────────────────────────────────────────────────────────────

def normalize_title(title: str) -> str:
    """
    Lowercase, remove punctuation, collapse whitespace.
    This is the form stored in the DB and used for fuzzy comparison.
    """
    text = title.lower()
    # Remove full-width and half-width punctuation
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def content_hash(body: str) -> str:
    """SHA-256 of normalized body text (empty body → hash of empty string)."""
    normalized = re.sub(r"\s+", "", body.lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ── Main check ─────────────────────────────────────────────────────────────────

def is_duplicate(article: Article, db: Database) -> bool:
    """
    Returns True if the article should be skipped (already seen or too similar
    to a recently published story).
    """

    # ── Layer 1: exact URL ────────────────────────────────────────────────────
    if db.is_url_seen(article.url):
        logger.debug("[Dedup L1] URL already seen: %s", article.url)
        return True

    # ── Layer 2: title fuzzy match ────────────────────────────────────────────
    norm = normalize_title(article.title)
    recent_titles = db.get_recent_normalized_titles(days=RECENT_DAYS)
    for stored in recent_titles:
        score = fuzz.token_sort_ratio(norm, stored)
        if score >= FUZZY_THRESHOLD:
            logger.info(
                "[Dedup L2] Title too similar (score=%d): '%s'", score, article.title
            )
            return True

    # ── Layer 3: content hash (only if body is non-trivial) ───────────────────
    if len(article.body) > 20:
        chash = content_hash(article.body)
        if db.is_content_hash_seen(chash):
            logger.info("[Dedup L3] Content hash already seen: %s", article.url)
            return True

    return False
