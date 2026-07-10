#!/usr/bin/env python3
"""
Setup verification script.

Run this once after configuration to confirm everything is working
before enabling the cron job.

    python check_setup.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import os
from dotenv import load_dotenv

load_dotenv()

OK = "[OK]"
NG = "[NG]"
errors = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global errors
    status = OK if ok else NG
    suffix = f"  ({detail})" if detail else ""
    print(f"  {status}  {label}{suffix}")
    if not ok:
        errors += 1


print("\n=== Horse Racing News Bot — Setup Check ===\n")

# ── 1. Python version ──────────────────────────────────────────────────────────
print("[ Python ]")
ver = sys.version_info
check(
    f"Python >= 3.11  (current: {ver.major}.{ver.minor}.{ver.micro})",
    ver >= (3, 11),
    "invoke with: python3 check_setup.py" if ver < (3, 11) else "",
)

# ── 2. Dependencies ────────────────────────────────────────────────────────────
print("\n[ Dependencies ]")
pkgs = {
    "feedparser":    "feedparser",
    "httpx":         "httpx",
    "bs4":           "beautifulsoup4",
    "openai":        "openai",
    "rapidfuzz":     "rapidfuzz",
    "dotenv":        "python-dotenv",
    "yaml":          "PyYAML",
}
for module, pip_name in pkgs.items():
    try:
        __import__(module)
        check(pip_name, True)
    except ImportError:
        check(pip_name, False, f"run: pip install {pip_name}")

# ── 3. Environment variables ───────────────────────────────────────────────────
print("\n[ Environment variables (.env) ]")
required_env = ["OPENAI_API_KEY", "WP_BASE_URL", "WP_USERNAME", "WP_APP_PASSWORD"]
env_ok = True
for key in required_env:
    val = os.environ.get(key, "")
    present = bool(val.strip())
    preview = (val[:6] + "…") if (present and len(val) > 6) else val
    check(key, present, preview if present else "not set")
    if not present:
        env_ok = False

# ── 4. Config file ─────────────────────────────────────────────────────────────
print("\n[ Config ]")
config_path = Path("config/sources.yaml")
check("config/sources.yaml exists", config_path.exists())

if config_path.exists():
    import yaml
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    rss_count = len(cfg.get("rss_feeds", []))
    scrape_count = len(cfg.get("scrape_sources", []))
    check(f"RSS feeds configured: {rss_count}", rss_count > 0)
    check(f"Scrape sources configured: {scrape_count}", True, "optional")

# ── 5. WordPress connection ────────────────────────────────────────────────────
print("\n[ WordPress ]")
if env_ok:
    try:
        from src.publisher import WordPressClient
        wp = WordPressClient(
            base_url=os.environ["WP_BASE_URL"],
            username=os.environ["WP_USERNAME"],
            app_password=os.environ["WP_APP_PASSWORD"],
        )
        wp_ok = wp.verify_connection()
        check("REST API connection + authentication", wp_ok)

        if wp_ok:
            cats = wp.list_categories()
            check(f"Categories reachable: {len(cats)} found", len(cats) > 0)
            if cats:
                print("\n    Available categories:")
                for c in cats:
                    print(f"      ID={c['id']}  name={c['name']}")
    except Exception as exc:
        check("WordPress client", False, str(exc))
else:
    print(f"  [--]  Skipped (env vars missing)")

# ── 6. OpenAI connection ───────────────────────────────────────────────────────
print("\n[ OpenAI ]")
if os.environ.get("OPENAI_API_KEY"):
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        models = client.models.list()
        model_ids = [m.id for m in models.data]
        has_mini = "gpt-4o-mini" in model_ids
        check("API key valid", True)
        check("gpt-4o-mini accessible", has_mini, "fallback: gpt-3.5-turbo" if not has_mini else "")
    except Exception as exc:
        check("OpenAI API key", False, str(exc))
else:
    print(f"  [--]  Skipped (OPENAI_API_KEY missing)")

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n{'='*44}")
if errors == 0:
    print("  All checks passed. Ready to run:\n")
    print("    python3 run.py --dry-run   # test without publishing")
    print("    python3 run.py             # live run")
else:
    print(f"  {errors} check(s) failed. Fix the issues above before running.")
print()
sys.exit(0 if errors == 0 else 1)
