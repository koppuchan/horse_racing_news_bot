from __future__ import annotations

"""
AI rewriting module — Google Gemini 1.5 Flash (free tier).

Free tier limits (as of 2026):
  - 15 RPM  (requests per minute)
  - 1 500 RPD (requests per day)
  - 1 000 000 TPM (tokens per minute)

To stay safely under 15 RPM we enforce a minimum 5-second gap between
API calls (= max 12 RPM). On 429 / resource-exhausted errors we back off
and retry up to max_retries times.
"""

import logging
import re
import time
from typing import Optional

from google import genai
from google.genai.errors import ClientError, ServerError

logger = logging.getLogger(__name__)

# Minimum seconds between consecutive Gemini calls (free-tier rate guard)
_MIN_CALL_INTERVAL = 5.0

# ── Prompt ─────────────────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """\
あなたは競馬専門のニュースライターです。
以下のニュース記事を、下記のルールに従ってリライトしてください。

【ルール】
1. 馬名・レース名・着順・タイム・オッズ・騎手名などの事実情報は一切変えないこと
2. 元の文章をそのままコピーせず、完全に独自の表現・言い回しで書くこと
3. タイトルは30文字以内に収めること（ただし、タイトルの中に「〇文字」などの文字数に関する記載は絶対に含めないでください）
4. 本文は200〜300文字以内に収めること
5. 読者が楽しめる、生き生きとした文体にすること

【出力フォーマット（厳守）】
タイトル：（ここにタイトル）
本文：（ここに本文）

---
元タイトル：{title}

元記事：
{body}
"""

_MAX_BODY_CHARS = 1200   # truncate long articles before sending (cost/token control)


_DEDUP_PROMPT_TEMPLATE = """\
あなたは競馬ニュースの重複判定アシスタントです。
以下の「新着記事のタイトル」が、「過去の投稿済みタイトルリスト」のいずれかと【全く同じ話題（事実）】を伝えているかどうかを判定してください。

異なるメディアが同じレース結果や同じ出来事（転厩、ケガ、引退など）を報じている場合は「重複」とみなします。

【判定基準】
- 重複している場合：「YES」
- 重複していない、または判断できない場合：「NO」
※必ず「YES」か「NO」のどちらかのみを出力してください。

---
新着記事のタイトル：
{new_title}

過去の投稿済みタイトルリスト：
{recent_titles_text}
"""


# ── Response parser ────────────────────────────────────────────────────────────

def _parse_response(text: str) -> tuple[str, str]:
    title = ""
    body_lines: list[str] = []
    in_body = False

    for raw in text.splitlines():
        line = raw.strip()
        m_title = re.match(r"^タイトル[：:]\s*(.+)", line)
        m_body  = re.match(r"^本文[：:]\s*(.*)",    line)

        if m_title:
            title = m_title.group(1).strip()
            # Remove hallucinations like 【28文字】 or (30文字) from the title
            title = re.sub(r"[【（\(]\s*\d+\s*文字\s*[】）\)]", "", title).strip()
            title = re.sub(r"^\d+文字\s*", "", title).strip()
            in_body = False
        elif m_body:
            first = m_body.group(1).strip()
            if first:
                body_lines.append(first)
            in_body = True
        elif in_body and line:
            body_lines.append(line)

    return title, "".join(body_lines)


# ── Rewriter ───────────────────────────────────────────────────────────────────

class Rewriter:
    """Wraps Gemini 1.5 Flash with rate-limit handling and retry logic."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-flash-lite-latest",
        max_retries: int = 3,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self.max_retries = max_retries
        self._last_call_at: float = 0.0

    def _rate_limit_wait(self) -> None:
        """Enforce minimum interval between API calls."""
        elapsed = time.monotonic() - self._last_call_at
        wait = _MIN_CALL_INTERVAL - elapsed
        if wait > 0:
            logger.debug("[Rewriter] Rate-limit wait: %.1fs", wait)
            time.sleep(wait)

    def rewrite(self, title: str, body: str) -> tuple[str, str]:
        """
        Returns (rewritten_title, rewritten_body).
        Falls back to originals on persistent failure so the pipeline keeps running.
        """
        body_input = body[:_MAX_BODY_CHARS] if body else "（本文なし）"
        prompt = _PROMPT_TEMPLATE.format(title=title, body=body_input)

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._rate_limit_wait()
            try:
                self._last_call_at = time.monotonic()
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=prompt,
                )
                raw = (response.text or "").strip()
                new_title, new_body = _parse_response(raw)

                if not new_title:
                    new_title = title
                if not new_body:
                    new_body = raw   # use full response if format was unexpected

                logger.info(
                    "[Rewriter] OK (attempt %d) | %s → %s",
                    attempt, title[:40], new_title[:40],
                )
                return new_title, new_body

            except ClientError as exc:
                if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                    wait = 60.0 * attempt
                    logger.warning(
                        "[Rewriter] Rate limit (429) — waiting %.0fs (attempt %d/%d)",
                        wait, attempt, self.max_retries,
                    )
                    time.sleep(wait)
                else:
                    logger.error("[Rewriter] Client error (attempt %d/%d): %s", attempt, self.max_retries, exc)
                    time.sleep(5.0 * attempt)
                last_exc = exc

            except ServerError as exc:
                wait = 10.0 * attempt
                logger.error(
                    "[Rewriter] Server error — waiting %.0fs (attempt %d/%d)",
                    wait, attempt, self.max_retries,
                )
                time.sleep(wait)
                last_exc = exc

            except Exception as exc:
                logger.error(
                    "[Rewriter] Unexpected error (attempt %d/%d): %s",
                    attempt, self.max_retries, exc,
                )
                time.sleep(5.0 * attempt)
                last_exc = exc

        logger.error(
            "[Rewriter] All %d attempts failed for '%s'. Using originals. Last: %s",
            self.max_retries, title, last_exc,
        )
        return title, body

    def is_semantic_duplicate(self, new_title: str, recent_titles: list[str]) -> bool:
        """
        Uses Gemini to determine if new_title reports the exact same news
        as any title in recent_titles.
        Returns True if AI says 'YES', False otherwise.
        """
        if not recent_titles:
            return False

        # Format recent titles into a bulleted list
        recent_text = "\n".join(f"- {t}" for t in recent_titles)
        prompt = _DEDUP_PROMPT_TEMPLATE.format(new_title=new_title, recent_titles_text=recent_text)

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._rate_limit_wait()
            try:
                self._last_call_at = time.monotonic()
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=prompt,
                )
                raw = (response.text or "").strip().upper()
                is_dup = "YES" in raw

                logger.info("[Rewriter] AI Dedup OK | '%s' -> %s", new_title[:30], "DUPLICATE" if is_dup else "UNIQUE")
                return is_dup

            except ClientError as exc:
                if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                    wait = 60.0 * attempt
                    logger.warning("[Rewriter] AI Dedup Rate limit (429) — waiting %.0fs (attempt %d/%d)", wait, attempt, self.max_retries)
                    time.sleep(wait)
                else:
                    logger.error("[Rewriter] AI Dedup Client error (attempt %d/%d): %s", attempt, self.max_retries, exc)
                    time.sleep(5.0 * attempt)
                last_exc = exc
            except Exception as exc:
                logger.error("[Rewriter] AI Dedup error (attempt %d/%d): %s", attempt, self.max_retries, exc)
                time.sleep(5.0 * attempt)
                last_exc = exc

        logger.error("[Rewriter] AI Dedup failed. Assuming NO duplicate. Last error: %s", last_exc)
        return False
