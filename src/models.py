from __future__ import annotations

from dataclasses import dataclass
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
