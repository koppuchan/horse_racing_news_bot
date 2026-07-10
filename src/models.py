from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Article:
    """Raw article fetched from an RSS feed or scraped page."""
    url: str
    title: str
    body: str           # Excerpt or full body text; may be empty for title-only sources
    source: str         # Friendly name of the source (e.g. "netkeiba")
    published_at: Optional[datetime] = None
    category_id: int = 1


@dataclass
class ProcessedArticle:
    """Article after AI rewriting, ready for WordPress publishing."""
    original: Article
    rewritten_title: str
    rewritten_body: str
    featured_media_id: Optional[int]
    wp_post_id: Optional[int] = None
