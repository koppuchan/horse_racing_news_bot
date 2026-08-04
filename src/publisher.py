from __future__ import annotations

"""
WordPress REST API client.

Authentication: Application Password (built into WP 5.6+).
  WP Admin > Users > Profile > Application Passwords > Add New

The generated password (with spaces) is passed as HTTP Basic Auth.
httpx handles encoding automatically.

Post target: custom post type "keiba_news"
  Endpoint: /wp-json/wp/v2/keiba_news
  (The custom post type must be registered with show_in_rest=True)
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30   # seconds per request


class WordPressClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        app_password: str,
        post_type: str = "horse_racing_news",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/wp-json/wp/v2"
        self.post_type = post_type          # custom post type slug
        self._auth = (username, app_password)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            auth=self._auth,
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    # ── Connection check ───────────────────────────────────────────────────────

    def verify_connection(self) -> bool:
        """
        Ping /wp-json/wp/v2/users/me to confirm credentials are valid.
        Call this once at pipeline startup before doing any real work.
        """
        try:
            with self._client() as client:
                resp = client.get(f"{self.api}/users/me")
            if resp.status_code == 200:
                name = resp.json().get("name", "?")
                logger.info("[WP] Connection OK — authenticated as '%s' on %s", name, self.base_url)
                return True
            logger.error(
                "[WP] Auth failed: HTTP %d — check WP_USERNAME / WP_APP_PASSWORD",
                resp.status_code,
            )
            return False
        except Exception as exc:
            logger.error("[WP] Cannot reach WordPress at %s: %s", self.base_url, exc)
            return False

    # ── Post creation ──────────────────────────────────────────────────────────

    def create_post(
        self,
        *,
        title: str,
        content: str,
        category_ids: list[int],
        featured_media_id: Optional[int] = None,
        status: str = "draft",
        tags: Optional[list[int]] = None,
        excerpt: str = "",
    ) -> Optional[int]:
        """
        Create a new post via the REST API.
        Returns the WP post ID on success, None on failure.

        Args:
            title:             Post title (plain text).
            content:           Post body (HTML or plain text).
            category_ids:      List of WP category IDs.
            featured_media_id: WP media library ID for the eye-catch image.
            status:            "draft" or "publish".
            tags:              Optional list of WP tag IDs.
            excerpt:           Short excerpt shown in list views.
        """
        payload: dict = {
            "title":      title,
            "content":    content,
            "status":     status,
            "categories": category_ids,
            "excerpt":    excerpt,
        }
        if featured_media_id:
            payload["featured_media"] = featured_media_id
        if tags:
            payload["tags"] = tags

        endpoint = f"{self.api}/{self.post_type}"
        try:
            with self._client() as client:
                resp = client.post(endpoint, json=payload)
            resp.raise_for_status()
            data = resp.json()
            post_id: int = data["id"]
            post_url: str = data.get("link", "")
            logger.info(
                "[WP] Post created: ID=%d status=%s url=%s | %s",
                post_id, status, post_url, title[:60],
            )
            return post_id

        except httpx.HTTPStatusError as exc:
            body_preview = exc.response.text[:300]
            logger.error(
                "[WP] HTTP %d creating post '%s': %s",
                exc.response.status_code, title[:60], body_preview,
            )
            return None

        except Exception as exc:
            logger.error("[WP] Unexpected error creating post '%s': %s", title[:60], exc)
            return None

    # ── Post reading / updating ────────────────────────────────────────────────

    def iter_posts(self, per_page: int = 50, max_pages: int = 100):
        """
        Yield every post of the configured post type, newest first.

        Used by regenerate_truncated.py to audit what is actually live on the
        site rather than trusting the local DB.
        """
        endpoint = f"{self.api}/{self.post_type}"
        for page in range(1, max_pages + 1):
            try:
                with self._client() as client:
                    resp = client.get(
                        endpoint,
                        params={
                            "per_page": per_page,
                            "page": page,
                            "status": "publish,draft",
                            "orderby": "date",
                            "order": "desc",
                        },
                    )
                if resp.status_code == 400:
                    # WP returns 400 "rest_post_invalid_page_number" past the end.
                    return
                resp.raise_for_status()
                batch = resp.json()
            except Exception as exc:
                logger.error("[WP] Failed to list posts (page %d): %s", page, exc)
                return

            if not batch:
                return
            for post in batch:
                yield post
            if len(batch) < per_page:
                return

    def update_post(
        self,
        post_id: int,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        excerpt: Optional[str] = None,
    ) -> bool:
        """
        Update an existing post in place.

        The slug is deliberately never sent, so the permalink stays valid even
        when the title changes.
        """
        payload: dict = {}
        if title is not None:
            payload["title"] = title
        if content is not None:
            payload["content"] = content
        if excerpt is not None:
            payload["excerpt"] = excerpt
        if not payload:
            return True

        try:
            with self._client() as client:
                resp = client.post(f"{self.api}/{self.post_type}/{post_id}", json=payload)
            resp.raise_for_status()
            logger.info("[WP] Post %d updated", post_id)
            return True
        except httpx.HTTPStatusError as exc:
            logger.error(
                "[WP] HTTP %d updating post %d: %s",
                exc.response.status_code, post_id, exc.response.text[:300],
            )
            return False
        except Exception as exc:
            logger.error("[WP] Unexpected error updating post %d: %s", post_id, exc)
            return False

    # ── Diagnostic helpers ─────────────────────────────────────────────────────

    def verify_post_type(self) -> bool:
        """Check that the custom post type REST endpoint is accessible."""
        endpoint = f"{self.api}/{self.post_type}"
        try:
            with self._client() as client:
                resp = client.get(endpoint, params={"per_page": 1})
            if resp.status_code in (200, 401):
                # 401 means endpoint exists but needs auth — that's fine here
                logger.info("[WP] Custom post type endpoint OK: %s", endpoint)
                return True
            logger.error(
                "[WP] Custom post type endpoint returned HTTP %d: %s",
                resp.status_code, endpoint,
            )
            return False
        except Exception as exc:
            logger.error("[WP] Cannot reach %s: %s", endpoint, exc)
            return False

    def list_categories(self) -> list[dict]:
        """Fetch all categories — useful for finding the right category IDs."""
        try:
            with self._client() as client:
                resp = client.get(f"{self.api}/categories", params={"per_page": 100})
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("[WP] Failed to fetch categories: %s", exc)
            return []
