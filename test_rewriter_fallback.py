#!/usr/bin/env python
"""
Regression test for the "article displayed truncated" bug.

Before the fix, a failed Gemini call made Rewriter.rewrite() return the
originals, and main.py published them — so readers saw the RSS excerpt, which
ends mid-sentence with "…". These tests pin the new contract: a failed rewrite
raises RewriteError and nothing gets published.

Run: python test_rewriter_fallback.py
"""
import sys
import types

# ── Stub google.genai so the test needs no API key / network ───────────────────

class ClientError(Exception):
    pass


class ServerError(Exception):
    pass


_errors = types.ModuleType("google.genai.errors")
_errors.ClientError = ClientError
_errors.ServerError = ServerError
_errors.APIError = Exception

_genai = types.ModuleType("google.genai")
_genai.errors = _errors
_genai.Client = lambda **kw: None

_google = types.ModuleType("google")
_google.genai = _genai

sys.modules.setdefault("google", _google)
sys.modules["google.genai"] = _genai
sys.modules["google.genai.errors"] = _errors

from src.rewriter import Rewriter, RewriteError  # noqa: E402
import src.rewriter as rewriter_mod  # noqa: E402

# Never actually sleep during tests.
rewriter_mod.time.sleep = lambda s: None
rewriter_mod._MIN_CALL_INTERVAL = 0.0


# ── Fake Gemini responses ──────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, text, finish_reason="STOP"):
        self.text = text
        candidate = types.SimpleNamespace(
            finish_reason=types.SimpleNamespace(name=finish_reason)
        )
        self.candidates = [candidate]


class FakeModels:
    """Returns queued outcomes; an Exception instance is raised instead."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, *, model, contents):
        self.calls.append(model)
        outcome = self._outcomes.pop(0) if self._outcomes else self._outcomes
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_rewriter(outcomes, fallback_models=None):
    r = Rewriter(api_key="x", model="primary", fallback_models=fallback_models)
    models = FakeModels(outcomes)
    r._client = types.SimpleNamespace(models=models)
    return r, models


GOOD = FakeResponse(
    "タイトル：バファート師が米3冠の重みを語る\n"
    "本文：チャーチルダウンズ社とNYRAが2027年から新シリーズを創設すると発表した。"
    "全6戦のシリーズにはプリークネスSが名を連ねる。名伯楽バファート師は3冠の価値を"
    "改めて強調し、その存在は競馬界に不可欠だと語った。ファンの期待は高まるばかりだ。"
)

# The exact shape of the bug: truncated RSS excerpt.
ORIGINAL_TITLE = 'バファート師が"米３冠"の重みを強調「競馬における最大の偉業はやはり３冠」「不可欠な存在」'
ORIGINAL_BODY = (
    "3日、チャーチルダウンズ社およびニューヨーク競馬協会（NYRA）が2027年から新たに"
    "「サラブレッド・チャンピオンシップ・シリーズ」を創設することを発表した。"
    "全6戦からなる新シリーズには、米3冠競走の第2戦、プリークネスSが含まれてお…"
)

failures = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


# ── 1. Happy path ──────────────────────────────────────────────────────────────
r, models = make_rewriter([GOOD])
title, body = r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
check("success returns rewritten title", title == "バファート師が米3冠の重みを語る", title)
check("success body is the rewrite, not the excerpt", not body.endswith("…"), body[-30:])
check("success body is full length", len(body) > 100, str(len(body)))

# ── 2. THE BUG: persistent failure must not return the truncated original ──────
r, models = make_rewriter([ClientError("500 internal")] * 3)
try:
    title, body = r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
    check("persistent failure raises instead of publishing excerpt", False,
          f"returned body ending {body[-20:]!r}")
except RewriteError:
    check("persistent failure raises instead of publishing excerpt", True)

# ── 3. Truncated generation (MAX_TOKENS) must not be published ────────────────
cut_off = FakeResponse("タイトル：バファート師が語る\n本文：チャーチルダウンズ社が新シリーズを創設すると発表し、プリークネスSが含まれてお",
                       finish_reason="MAX_TOKENS")
r, models = make_rewriter([cut_off, cut_off, cut_off])
try:
    r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
    check("MAX_TOKENS output is rejected", False, "it was accepted")
except RewriteError:
    check("MAX_TOKENS output is rejected", True)

# ── 4. Too-short / unparseable body is rejected ───────────────────────────────
r, models = make_rewriter([FakeResponse("すみません、できません。")] * 3)
try:
    r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
    check("short/unparseable body is rejected", False, "it was accepted")
except RewriteError:
    check("short/unparseable body is rejected", True)

# ── 5. Fallback model chain ───────────────────────────────────────────────────
r, models = make_rewriter(
    [ClientError("404 model not found"), GOOD],
    fallback_models=["backup"],
)
title, body = r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
check("falls through to backup model", models.calls == ["primary", "backup"], str(models.calls))
check("backup result is used", title == "バファート師が米3冠の重みを語る", title)

# ── 6. Quota circuit breaker ──────────────────────────────────────────────────
r, models = make_rewriter([ClientError("429 RESOURCE_EXHAUSTED")] * 10)
for _ in range(2):
    try:
        r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
    except RewriteError:
        pass
check("circuit breaker trips on repeated 429", r._ai_disabled, "not tripped")
calls_before = len(models.calls)
try:
    r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
except RewriteError:
    pass
check("no API calls once quota is exhausted", len(models.calls) == calls_before,
      f"{len(models.calls)} vs {calls_before}")
check("dedup also short-circuits", r.is_semantic_duplicate("t", ["a"]) is False)

# ── 6b. THE REAL 2026-08-04 INCIDENT: 503 "high demand" on every model ────────
# The bot published 7 raw articles because the primary model returned 503 and
# there was no fallback chain and no fallback-free skip.
outage = ServerError("503 UNAVAILABLE. This model is currently experiencing high demand.")
r, models = make_rewriter([outage] * 40, fallback_models=["backup1", "backup2"])
try:
    r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
    check("503 outage does not publish the excerpt", False, "it returned a body")
except RewriteError:
    check("503 outage does not publish the excerpt", True)
check("503 falls through the whole model chain",
      set(models.calls) == {"primary", "backup1", "backup2"}, str(models.calls))
check("503 retries at most twice per model", len(models.calls) <= 6, str(len(models.calls)))

# A second failed article must trip the outage breaker so a 30-min cron job
# cannot overrun into the next one.
try:
    r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
except RewriteError:
    pass
check("outage breaker trips after 2 failed articles", r._ai_disabled, "not tripped")
calls_before = len(models.calls)
try:
    r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
except RewriteError:
    pass
check("no further API calls during outage", len(models.calls) == calls_before,
      f"{len(models.calls)} vs {calls_before}")

# ── 6c. A recovery resets the breaker ────────────────────────────────────────
r, models = make_rewriter([outage, outage, outage, outage, outage, outage, GOOD],
                          fallback_models=["backup1", "backup2"])
try:
    r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)   # article 1 fails everywhere
except RewriteError:
    pass
title, _ = r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)  # article 2 succeeds
check("success resets the failure counter", r._chain_failures == 0, str(r._chain_failures))
check("not disabled after a recovery", not r._ai_disabled)

# ── 7. Missing title alone is recoverable ─────────────────────────────────────
body_only = FakeResponse(
    "本文：チャーチルダウンズ社とNYRAが2027年から新シリーズを創設すると発表した。"
    "全6戦のシリーズにはプリークネスSが名を連ねる。名伯楽バファート師は3冠の価値を"
    "改めて強調し、その存在は競馬界に不可欠だと語った。"
)
r, models = make_rewriter([body_only])
title, body = r.rewrite(ORIGINAL_TITLE, ORIGINAL_BODY)
check("missing title falls back to original headline", title == ORIGINAL_TITLE, title)
check("body still the rewrite", not body.endswith("…"))

print()
if failures:
    print(f"{len(failures)} test(s) FAILED: {failures}")
    sys.exit(1)
print("All tests passed.")
