from __future__ import annotations

"""
WordPress REST API client.

Authentication: Application Password (built into WP 5.6+).
  WP Admin > Users > Profile > Application Passwords > Add New

The generated password (with spaces) is passed as HTTP Basic Auth.
httpx handles encoding automatically.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30   # seconds per request


class WordPressClient:
    def __init__(self, base_url: str, username: str, app_password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/wp-json/wp/v2"
        # httpx Basic Auth encodes username:password in the Authorization header
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

        try:
            with self._client() as client:
                resp = client.post(f"{self.api}/posts", json=payload)
            resp.raise_for_status()
            post_id: int = resp.json()["id"]
            post_url: str = resp.json().get("link", "")
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

    # ── Diagnostic helpers ─────────────────────────────────────────────────────

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

    def upload_image(self, file_path: str, alt_text: str = "") -> Optional[int]:
        """
        Upload a local image file to the WP media library.
        Returns the media ID on success.

        Use this once per category image during initial setup — then store
        the returned IDs in config/sources.yaml under category_image_map.
        """
        import mimetypes
        from pathlib import Path

        path = Path(file_path)
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"

        try:
            with self._client() as client:
                with open(path, "rb") as f:
                    resp = client.post(
                        f"{self.api}/media",
                        content=f.read(),
                        headers={
                            "Content-Type": mime,
                            "Content-Disposition": f'attachment; filename="{path.name}"',
                        },
                    )
            resp.raise_for_status()
            media_id: int = resp.json()["id"]
            if alt_text:
                with self._client() as client:
                    client.post(
                        f"{self.api}/media/{media_id}",
                        json={"alt_text": alt_text},
                    )
            logger.info("[WP] Uploaded image: ID=%d (%s)", media_id, path.name)
            return media_id
        except Exception as exc:
            logger.error("[WP] Failed to upload %s: %s", file_path, exc)
            return None
