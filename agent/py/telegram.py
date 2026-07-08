"""Send messages to Telegram via the Seasons bot (Bot API sendMessage).

Used for the end-of-run favorites round-up. Best-effort by design: the caller
wraps this so a Telegram outage never fails a scrape. Credentials come from the
repo-root .env:
    TELEGRAM_SEASONS_BOT_TOKEN
    TELEGRAM_SEASONS_CHAT_ID
"""
from __future__ import annotations

import os

import requests

from uf_env import env

TOKEN_VAR = "TELEGRAM_SEASONS_BOT_TOKEN"
CHAT_VAR = "TELEGRAM_SEASONS_CHAT_ID"

# Telegram rejects messages over 4096 chars; leave headroom for the API framing.
MAX_LEN = 4000


def configured() -> bool:
    """True only when both credentials are present, so callers can skip the
    report cleanly on machines that never set Telegram up."""
    return bool(os.environ.get(TOKEN_VAR) and os.environ.get(CHAT_VAR))


def send_message(text: str, *, disable_preview: bool = True) -> None:
    token = env(TOKEN_VAR)
    chat_id = env(CHAT_VAR)  # dotenv strips the surrounding quotes in .env
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text[:MAX_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        },
        timeout=20,
    )
    if not resp.ok:
        raise RuntimeError(f"Telegram sendMessage -> HTTP {resp.status_code}: {resp.text[:300]}")
