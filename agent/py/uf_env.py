"""Strict env loading. Secrets live in the repo-root .env (gitignored — public repo).
NO fallbacks by design: missing/invalid config fails loudly (owner's rule)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

AGENT_ROOT = Path(__file__).resolve().parent.parent   # .../agent
REPO_ROOT = AGENT_ROOT.parent                          # .../unlimitedfocus
DATA_DIR = AGENT_ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"
RUNS_DIR = DATA_DIR / "runs"

load_dotenv(REPO_ROOT / ".env", override=True)


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing required env var {name} — set it in {REPO_ROOT / '.env'}")
    return v
