#!/usr/bin/env python3
# On Debian/Ubuntu: "python" is not installed by default. Use "python3" or
# install the "python-is-python3" package to create a "python" symlink.
"""
Cron entry point.

Normal run (publish/draft articles):
    python run.py

Dry-run (fetch + rewrite only, no WordPress writes):
    python run.py --dry-run

This file exists so cron can reference a single path without needing
to activate a virtualenv or pass -m flags:
    */30 * * * * cd /path/to/horse-racing-news-bot && .venv/bin/python run.py >> logs/cron.log 2>&1
"""

import sys
from pathlib import Path

# Ensure the project root is importable when called directly by cron
sys.path.insert(0, str(Path(__file__).parent))

from src.main import run

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run(dry_run=dry_run)
