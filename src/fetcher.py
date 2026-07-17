from __future__ import annotations

import calendar
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup

from .models import Article

logger = logging.getLogger(__name__)

_USER_AGENT = "HorseRacingNewsBot/1.0 (+https://github.com/kinbo1111/horse-racing-news-bot)"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _entry_datetime(entry: feedparser.FeedParserDict) -> Optional[datetime]:
    """Extract published/updated time from a feedparser entry as UTC datetime."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


def _strip_html(html: str) -> str:
    """Return plain text from an HTML fragment."""
    return BeautifulSoup(html, "lxml").get_text(separator=" ", strip=True)


# ── RSS fetcher ────────────────────────────────────────────────────────────────

class RSSFetcher:
    """Fetches articles from a single RSS/Atom feed via feedparser."""

    def __init__(
        self,
        name: str,
        url: str,
        category_id: int,
        timeout: int = 20,
        category_filter: Optional[str] = None,
    ) -> None:
        self.name = name
        self.url = url
        self.category_id = category_id
        self.timeout = timeout
        # When set, only keep entries whose category/title/summary contains
        # this keyword (used for all-genre feeds like 東スポWEB).
        self.category_filter = category_filter

    def _matches_filter(self, entry: feedparser.FeedParserDict) -> bool:
        if not self.category_filter:
            return True
        keyword = self.category_filter
        tag_terms = " ".join(
            (t.get("term") or "") for t in (getattr(entry, "tags", None) or [])
        )
        haystack = " ".join(
            [
                tag_terms,
                entry.get("title", "") or "",
                entry.get("summary", "") or "",
            ]
        )
        return keyword in haystack

    def fetch(self) -> list[Article]:
        logger.info("[RSS] Fetching %s — %s", self.name, self.url)
        try:
            feed = feedparser.parse(
                self.url,
                request_headers={"User-Agent": _USER_AGENT},
                agent=_USER_AGENT,
            )
        except Exception as exc:
            logger.error("[RSS] Network error for %s: %s", self.name, exc)
            return []

        if feed.bozo and not feed.entries:
            logger.warning(
                "[RSS] Malformed feed from %s: %s", self.name, feed.bozo_exception
            )
            return []

        articles: list[Article] = []
        for entry in feed.entries:
            url = (entry.get("link") or "").strip()
            title = (entry.get("title") or "").strip()
            if not url or not title:
                continue

            if not self._matches_filter(entry):
                continue

            raw_body = entry.get("summary") or entry.get("description") or ""
            body = _strip_html(raw_body) if raw_body else ""

            articles.append(
                Article(
                    url=url,
                    title=title,
                    body=body,
                    source=self.name,
                    published_at=_entry_datetime(entry),
                    category_id=self.category_id,
                )
            )

        logger.info("[RSS] %s → %d article(s)", self.name, len(articles))
        return articles


# ── HTML scraper ───────────────────────────────────────────────────────────────

class ScrapeFetcher:
    """
    Generic HTML scraper for sites that do not provide an RSS feed.

    The caller configures CSS selectors in sources.yaml; this class handles
    HTTP, rate limiting, and BeautifulSoup parsing.
    """

    def __init__(
        self,
        *,
        name: str,
        list_url: str,
        category_id: int,
        article_selector: str,
        title_selector: str,
        link_selector: str,
        body_selector: Optional[str] = None,
        max_articles: int = 20,
        request_delay: float = 3.0,
        timeout: int = 25,
    ) -> None:
        self.name = name
        self.list_url = list_url
        self.category_id = category_id
        self.article_selector = article_selector
        self.title_selector = title_selector
        self.link_selector = link_selector
        self.body_selector = body_selector
        self.max_articles = max_articles
        self.request_delay = request_delay
        self.timeout = timeout
        self._headers = {
            "User-Agent": _USER_AGENT,
            "Accept-Language": "ja,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        }

    def _get_soup(self, url: str) -> Optional[BeautifulSoup]:
        try:
            with httpx.Client(
                timeout=self.timeout,
                headers=self._headers,
                follow_redirects=True,
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return BeautifulSoup(resp.text, "lxml")
        except httpx.HTTPStatusError as exc:
            logger.error("[Scrape] HTTP %d for %s", exc.response.status_code, url)
        except Exception as exc:
            logger.error("[Scrape] Error fetching %s: %s", url, exc)
        return None

    def _resolve_url(self, href: str) -> str:
        if href.startswith("http"):
            return href
        return urljoin(self.list_url, href)

    def fetch(self) -> list[Article]:
        logger.info("[Scrape] Fetching list page: %s (%s)", self.name, self.list_url)
        soup = self._get_soup(self.list_url)
        if not soup:
            return []

        items = soup.select(self.article_selector)[: self.max_articles]
        if not items:
            logger.warning(
                "[Scrape] No elements matched selector '%s' on %s",
                self.article_selector,
                self.list_url,
            )
            return []

        articles: list[Article] = []
        for item in items:
            title_el = item.select_one(self.title_selector)
            link_el = item.select_one(self.link_selector)
            if not title_el or not link_el:
                continue

            title = title_el.get_text(strip=True)
            href = self._resolve_url(link_el.get("href", "").strip())
            if not title or not href:
                continue

            body = ""
            if self.body_selector and href:
                time.sleep(self.request_delay)
                detail = self._get_soup(href)
                if detail:
                    body_el = detail.select_one(self.body_selector)
                    if body_el:
                        body = body_el.get_text(separator=" ", strip=True)

            articles.append(
                Article(
                    url=href,
                    title=title,
                    body=body,
                    source=self.name,
                    published_at=None,
                    category_id=self.category_id,
                )
            )
            time.sleep(self.request_delay)

        logger.info("[Scrape] %s → %d article(s)", self.name, len(articles))
        return articles


# ── Factory ────────────────────────────────────────────────────────────────────

def _build_fetchers(config: dict) -> list[RSSFetcher | ScrapeFetcher]:
    fetchers: list[RSSFetcher | ScrapeFetcher] = []

    for src in config.get("rss_feeds", []):
        fetchers.append(
            RSSFetcher(
                name=src["name"],
                url=src["url"],
                category_id=int(src.get("category_id", 1)),
                category_filter=src.get("category_filter"),
            )
        )

    for src in config.get("scrape_sources", []):
        fetchers.append(
            ScrapeFetcher(
                name=src["name"],
                list_url=src["list_url"],
                category_id=int(src.get("category_id", 1)),
                article_selector=src["article_selector"],
                title_selector=src["title_selector"],
                link_selector=src["link_selector"],
                body_selector=src.get("body_selector"),
                request_delay=float(src.get("request_delay", 3.0)),
            )
        )

    return fetchers


def fetch_all(config: dict) -> list[Article]:
    """
    Entry point called by the pipeline.
    Runs all configured fetchers and returns a combined deduplicated-by-URL list.
    """
    fetchers = _build_fetchers(config)
    seen_urls: set[str] = set()
    all_articles: list[Article] = []
    
    blacklist: list[str] = config.get("blacklist_keywords", [])

    for fetcher in fetchers:
        try:
            for article in fetcher.fetch():
                # Apply blacklist filter (title or URL)
                is_blacklisted = False
                for keyword in blacklist:
                    if keyword in article.title or keyword in article.url:
                        logger.info("Skipped blacklisted article: '%s' (matched: %s)", article.title, keyword)
                        is_blacklisted = True
                        break
                
                if is_blacklisted:
                    continue

                if article.url not in seen_urls:
                    seen_urls.add(article.url)
                    all_articles.append(article)
        except Exception as exc:
            logger.error("Fetcher '%s' raised an unexpected error: %s", fetcher.name, exc, exc_info=True)

    logger.info("fetch_all complete: %d unique article(s) across %d source(s)", len(all_articles), len(fetchers))
    return all_articles
