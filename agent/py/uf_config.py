"""Project-level (non-secret) configuration. The app id is public-shape config,
not a credential — all access requires BUTTERBASE_API_KEY."""
from __future__ import annotations

import os

import uf_env  # noqa: F401  (loads .env)

BUTTERBASE_APP_ID = os.environ.get("BUTTERBASE_APP_ID", "app_desa1zwpsx43")
BUTTERBASE_API_BASE = f"https://api.butterbase.ai/v1/{BUTTERBASE_APP_ID}"

PLATFORM_INSTAGRAM = "instagram"
# TODO(phase-later): 'x', 'linkedin', 'facebook', 'threads', 'tiktok' — each new
# platform is one new scrape_<platform>.py using the same chrome/butterbase/graph
# plumbing, nothing else changes.


def _int(name: str, dflt: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else dflt


# Conservative defaults: human-like pacing, modest per-run caps. An overnight
# loop is many small polite runs, not one giant crawl.
MAX_NEW_POSTS = _int("UF_MAX_POSTS", 15)
MAX_STORIES = _int("UF_MAX_STORIES", 15)
DUP_STREAK_STOP = _int("UF_DUP_STREAK", 8)   # consecutive known posts -> reached old territory
MAX_SCROLL_ROUNDS = _int("UF_MAX_SCROLLS", 40)
