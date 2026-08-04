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

# A usable rewrite must be at least this long. Anything shorter means the model
# returned a fragment / refusal rather than the 200-300 char body we asked for.
_MIN_BODY_CHARS = 60

# Consecutive quota (429) failures after which we stop calling the API for the
# rest of the run. Without this, an exhausted quota makes every article burn
# several minutes of backoff before failing anyway.
_QUOTA_CIRCUIT_THRESHOLD = 2

# If the whole model chain fails for this many articles in a row, Gemini is down
# (e.g. the 2026-08-04 503 "high demand" outage), not just flaky. Stop calling it
# for the rest of the run so a 30-minute cron job cannot overrun into the next one.
_OUTAGE_CIRCUIT_THRESHOLD = 2


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


# ── Errors ─────────────────────────────────────────────────────────────────────

class RewriteError(RuntimeError):
    """
    Raised when Gemini could not produce a usable rewrite.

    The caller must NOT publish the article in this case: the original body we
    receive from RSS is only a truncated excerpt (it ends in "…"), so falling
    back to it puts a half-finished sentence on the site — and republishing the
    source text verbatim breaches the feed providers' terms as well.
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


def _finish_reason(response) -> str:
    """Name of the candidate's finish reason ('STOP' when generation completed)."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "NO_CANDIDATE"
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return "STOP"
    return getattr(reason, "name", str(reason))


# ── Rewriter ───────────────────────────────────────────────────────────────────

class Rewriter:
    """Wraps Gemini 1.5 Flash with rate-limit handling and retry logic."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-flash-lite-latest",
        max_retries: int = 3,
        fallback_models: Optional[list[str]] = None,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        # Tried in order; a model that is out of quota or retired falls through
        # to the next one instead of failing the whole article.
        self._models = [model] + [m for m in (fallback_models or []) if m != model]
        self.max_retries = max_retries
        self._last_call_at: float = 0.0
        self._quota_failures = 0
        self._chain_failures = 0
        self._ai_disabled = False
        self._disabled_reason = ""

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

        Raises RewriteError if no model in the chain produced a usable rewrite.
        We deliberately do NOT fall back to the originals: the RSS body is a
        truncated excerpt, so publishing it shows a cut-off article.
        """
        body_input = body[:_MAX_BODY_CHARS] if body else "（本文なし）"
        prompt = _PROMPT_TEMPLATE.format(title=title, body=body_input)
        new_title, new_body = self._generate(
            prompt, min_body_chars=_MIN_BODY_CHARS, label=title
        )
        # The body is what readers see, so a missing title is recoverable —
        # reuse the original headline.
        return (new_title or title), new_body

    def _generate(
        self, prompt: str, *, min_body_chars: int, label: str
    ) -> tuple[str, str]:
        """
        Run a prompt through the model chain and return the parsed
        (title, body). The title may be empty; the body is guaranteed to be at
        least min_body_chars long. Raises RewriteError if nothing usable came back.
        """
        if self._ai_disabled:
            raise RewriteError(
                f"Gemini disabled earlier in this run ({self._disabled_reason})"
            )

        last_error: str = "unknown"
        for model in self._models:
            for attempt in range(1, self.max_retries + 1):
                self._rate_limit_wait()
                try:
                    self._last_call_at = time.monotonic()
                    response = self._client.models.generate_content(
                        model=model,
                        contents=prompt,
                    )
                    self._quota_failures = 0
                    self._chain_failures = 0

                    reason = _finish_reason(response)
                    if reason != "STOP":
                        # MAX_TOKENS / SAFETY here means the text we got is cut
                        # off mid-sentence — exactly what we must not publish.
                        last_error = f"generation did not complete ({reason})"
                        logger.warning(
                            "[Rewriter] %s (%s attempt %d/%d)",
                            last_error, model, attempt, self.max_retries,
                        )
                        continue

                    raw = (response.text or "").strip()
                    new_title, new_body = _parse_response(raw)

                    if len(new_body) < min_body_chars:
                        last_error = f"body too short ({len(new_body)} chars)"
                        logger.warning(
                            "[Rewriter] %s (%s attempt %d/%d) — raw: %r",
                            last_error, model, attempt, self.max_retries, raw[:200],
                        )
                        continue

                    logger.info(
                        "[Rewriter] OK (%s attempt %d) | %s → %s (%d chars)",
                        model, attempt, label[:40], new_title[:40], len(new_body),
                    )
                    return new_title, new_body

                except ClientError as exc:
                    if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                        self._quota_failures += 1
                        last_error = f"rate limited (429) on {model}"
                        if self._quota_failures >= _QUOTA_CIRCUIT_THRESHOLD:
                            # Quota is gone; stop burning minutes of backoff on
                            # every remaining article in this run.
                            self._disable(
                                f"quota exhausted after {self._quota_failures} "
                                "consecutive 429s"
                            )
                            raise RewriteError(last_error) from exc
                        wait = 60.0 * attempt
                        logger.warning(
                            "[Rewriter] Rate limit (429) — waiting %.0fs (%s attempt %d/%d)",
                            wait, model, attempt, self.max_retries,
                        )
                        time.sleep(wait)
                    else:
                        last_error = f"client error: {exc}"
                        logger.error(
                            "[Rewriter] Client error (%s attempt %d/%d): %s",
                            model, attempt, self.max_retries, exc,
                        )
                        # 404 (model retired) or 400 won't fix itself on retry —
                        # move straight to the next model in the chain.
                        break

                except ServerError as exc:
                    last_error = f"server error: {exc}"
                    # 503 means *this* model is overloaded right now, so another
                    # model in the chain is far more likely to answer than the
                    # same one. Retry once, then move on instead of burning 60s.
                    logger.error(
                        "[Rewriter] Server error (%s attempt %d/%d): %s",
                        model, attempt, self.max_retries, str(exc)[:120],
                    )
                    if attempt >= 2:
                        break
                    time.sleep(5.0)

                except Exception as exc:
                    last_error = f"unexpected error: {exc}"
                    logger.error(
                        "[Rewriter] Unexpected error (%s attempt %d/%d): %s",
                        model, attempt, self.max_retries, exc,
                    )
                    time.sleep(5.0 * attempt)

            if model != self._models[-1]:
                logger.warning("[Rewriter] Model '%s' failed — trying next model", model)

        self._chain_failures += 1
        if self._chain_failures >= _OUTAGE_CIRCUIT_THRESHOLD:
            self._disable(
                f"every model failed for {self._chain_failures} articles in a row "
                f"({last_error})"
            )
        raise RewriteError(
            f"all {len(self._models)} model(s) failed for '{label[:60]}': {last_error}"
        )

    def _disable(self, reason: str) -> None:
        """Stop calling Gemini for the remainder of this run."""
        self._ai_disabled = True
        self._disabled_reason = reason
        logger.error(
            "[Rewriter] Disabling AI for the rest of this run — %s", reason
        )

    def is_semantic_duplicate(self, new_title: str, recent_titles: list[str]) -> bool:
        """
        Uses Gemini to determine if new_title reports the exact same news
        as any title in recent_titles.
        Returns True if AI says 'YES', False otherwise.
        """
        if not recent_titles or self._ai_disabled:
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
                    model=self._models[0],
                    contents=prompt,
                )
                self._quota_failures = 0
                raw = (response.text or "").strip().upper()
                is_dup = "YES" in raw

                logger.info("[Rewriter] AI Dedup OK | '%s' -> %s", new_title[:30], "DUPLICATE" if is_dup else "UNIQUE")
                return is_dup

            except ClientError as exc:
                if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                    self._quota_failures += 1
                    if self._quota_failures >= _QUOTA_CIRCUIT_THRESHOLD:
                        self._disable(
                            f"quota exhausted after {self._quota_failures} "
                            "consecutive 429s (dedup)"
                        )
                        return False
                    wait = 60.0 * attempt
                    logger.warning("[Rewriter] AI Dedup Rate limit (429) — waiting %.0fs (attempt %d/%d)", wait, attempt, self.max_retries)
                    time.sleep(wait)
                else:
                    logger.error("[Rewriter] AI Dedup Client error (attempt %d/%d): %s", attempt, self.max_retries, exc)
                    time.sleep(5.0 * attempt)
                last_exc = exc

            except ServerError as exc:
                # Dedup is a best-effort extra layer (L1-L3 still ran), so don't
                # spend 30s retrying an overloaded model — just let it through.
                logger.warning(
                    "[Rewriter] AI Dedup unavailable (%s) — relying on L1-L3 dedup",
                    str(exc)[:120],
                )
                return False
            except Exception as exc:
                logger.error("[Rewriter] AI Dedup error (attempt %d/%d): %s", attempt, self.max_retries, exc)
                time.sleep(5.0 * attempt)
                last_exc = exc

        logger.error("[Rewriter] AI Dedup failed. Assuming NO duplicate. Last error: %s", last_exc)
        return False
