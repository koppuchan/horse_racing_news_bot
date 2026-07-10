from __future__ import annotations

"""
AI rewriting module.

Sends each article to OpenAI GPT-4o-mini with a prompt that preserves
all factual data (horse names, race name, jockey, odds, result) while
producing an original 200–300 character Japanese text.

On any OpenAI error the module retries up to max_retries times with
exponential back-off, then falls back to the original title/body so the
pipeline never stops mid-run.
"""

import logging
import re
import time
from typing import Optional

from openai import OpenAI, RateLimitError, APIError, APIConnectionError

logger = logging.getLogger(__name__)

# ── Prompt ─────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
あなたは競馬専門のニュースライターです。
与えられたニュース記事を、以下のルールに従ってリライトしてください。

【ルール】
1. 馬名・レース名・着順・オッズ・騎手名・調教師名などの事実情報は変えないこと
2. 文章のトーンは読者が楽しめる、生き生きとした表現にすること
3. タイトルは30文字以内、本文は200〜300文字以内に収めること
4. 著作権に抵触しないよう、元の文章をそのまま使わず完全にリライトすること

【出力フォーマット（この形式を厳守）】
タイトル：（ここにタイトル）
本文：（ここに本文）\
"""

_USER_TEMPLATE = """\
以下の競馬ニュースをリライトしてください。

元タイトル：{title}

元記事：
{body}\
"""

# Max characters of the original body sent to the API (cost control)
_MAX_BODY_CHARS = 1200


# ── Response parser ────────────────────────────────────────────────────────────

def _parse_response(text: str) -> tuple[str, str]:
    """
    Extract title and body from the GPT response.
    Returns ("", "") if parsing fails — caller should fall back to originals.
    """
    title = ""
    body_parts: list[str] = []
    in_body = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        # Match both ：(full-width) and :(half-width) separators
        m_title = re.match(r"^タイトル[：:]\s*(.+)", line)
        m_body = re.match(r"^本文[：:]\s*(.*)", line)

        if m_title:
            title = m_title.group(1).strip()
            in_body = False
        elif m_body:
            first = m_body.group(1).strip()
            if first:
                body_parts.append(first)
            in_body = True
        elif in_body and line:
            body_parts.append(line)

    return title, "".join(body_parts)


# ── Rewriter ───────────────────────────────────────────────────────────────────

class Rewriter:
    """Wraps the OpenAI client with retry logic and response parsing."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        max_retries: int = 3,
        initial_backoff: float = 2.0,
    ) -> None:
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff

    def rewrite(self, title: str, body: str) -> tuple[str, str]:
        """
        Returns (rewritten_title, rewritten_body).
        Falls back to (original_title, original_body) on persistent failure
        so the pipeline can still mark the article as seen and move on.
        """
        # Truncate body to control token usage
        truncated_body = body[:_MAX_BODY_CHARS] if body else "(本文なし — タイトルのみリライト)"
        user_msg = _USER_TEMPLATE.format(title=title, body=truncated_body)

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user",   "content": user_msg},
                    ],
                    max_tokens=500,
                    temperature=0.75,
                )
                raw = (response.choices[0].message.content or "").strip()
                new_title, new_body = _parse_response(raw)

                if not new_title:
                    new_title = title
                if not new_body:
                    # Entire response used as body if format was unexpected
                    new_body = raw

                logger.info(
                    "[Rewriter] OK (attempt %d/%d) | %s → %s",
                    attempt, self.max_retries,
                    title[:40],
                    new_title[:40],
                )
                return new_title, new_body

            except RateLimitError as exc:
                wait = self.initial_backoff * (2 ** (attempt - 1))
                logger.warning(
                    "[Rewriter] Rate limit — waiting %.0fs (attempt %d/%d)",
                    wait, attempt, self.max_retries,
                )
                time.sleep(wait)
                last_exc = exc

            except (APIConnectionError, APIError) as exc:
                wait = self.initial_backoff * attempt
                logger.error(
                    "[Rewriter] API error (attempt %d/%d): %s — retrying in %.0fs",
                    attempt, self.max_retries, exc, wait,
                )
                time.sleep(wait)
                last_exc = exc

        logger.error(
            "[Rewriter] All %d attempts failed for '%s'. Using originals. Last error: %s",
            self.max_retries, title, last_exc,
        )
        return title, body
