from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_articles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url              TEXT    UNIQUE NOT NULL,
    title            TEXT    NOT NULL,
    title_normalized TEXT    NOT NULL,
    content_hash     TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    wp_post_id       INTEGER,
    processed_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_url          ON seen_articles (url);
CREATE INDEX IF NOT EXISTS idx_content_hash ON seen_articles (content_hash);
CREATE INDEX IF NOT EXISTS idx_processed_at ON seen_articles (processed_at);
"""


class Database:
    """
    SQLite wrapper that tracks every article the bot has seen.
    Used by the deduplication layer to avoid re-publishing the same story.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._apply_schema()
        logger.info("Database ready: %s", self.db_path)

    # ── Internal helpers ───────────────────────────────────────────────────────

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads while writing
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _apply_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ── Query methods ──────────────────────────────────────────────────────────

    def is_url_seen(self, url: str) -> bool:
        """Layer 1 dedup: exact URL match."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_articles WHERE url = ? LIMIT 1", (url,)
            ).fetchone()
        return row is not None

    def is_content_hash_seen(self, content_hash: str) -> bool:
        """Layer 3 dedup: SHA-256 hash of normalized body text."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_articles WHERE content_hash = ? LIMIT 1",
                (content_hash,),
            ).fetchone()
        return row is not None

    def get_recent_original_titles(self, days: int = 1) -> list[str]:
        """Return original titles of articles seen in the last N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT title FROM seen_articles WHERE processed_at >= ?",
                (cutoff,)
            )
            return [row["title"] for row in cursor.fetchall()]

    def get_recent_normalized_titles(self, days: int = 7) -> list[str]:
        """
        Layer 2 dedup: returns normalized titles from the past N days
        so the caller can run fuzzy matching against them.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT title_normalized FROM seen_articles WHERE processed_at >= ?",
                (cutoff,),
            ).fetchall()
        return [row["title_normalized"] for row in rows]

    # ── Write methods ──────────────────────────────────────────────────────────

    def mark_seen(
        self,
        *,
        url: str,
        title: str,
        title_normalized: str,
        content_hash: str,
        source: str,
        wp_post_id: Optional[int] = None,
    ) -> None:
        """Record that an article has been processed (published or attempted)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO seen_articles
                    (url, title, title_normalized, content_hash, source, wp_post_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (url, title, title_normalized, content_hash, source, wp_post_id),
            )

    # ── Maintenance ────────────────────────────────────────────────────────────

    def purge_old_records(self, keep_days: int = 90) -> int:
        """
        Delete records older than keep_days to prevent unbounded DB growth.
        Returns number of rows deleted.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM seen_articles WHERE processed_at < ?", (cutoff,)
            )
        deleted = cur.rowcount
        if deleted:
            logger.info("Purged %d records older than %d days", deleted, keep_days)
        return deleted

    def stats(self) -> dict:
        """Return row counts for logging/diagnostics."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM seen_articles").fetchone()[0]
            week = conn.execute(
                "SELECT COUNT(*) FROM seen_articles WHERE processed_at >= ?",
                ((datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
            ).fetchone()[0]
        return {"total": total, "last_7_days": week}
