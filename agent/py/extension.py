"""Bridge to the Unlimited Focus extension in the driven Chrome tab.

The extension's content script (src/content/agent.js) listens for
window.postMessage({type: "UF_AGENT", ...}) and acks through the
data-uf-agent-ack attribute on <html> — the DOM is shared between the page
and the content script, and page-world JS is all AppleScript can execute.

Commands are re-sent until acked: the content script loads at document_idle,
so on a fresh tab the first post usually beats it.

Best-effort by design: no extension (or an unresponsive one) returns None and
the scrape proceeds — the extension is a guard for humans, not a dependency
of the agent. The pause itself is time-boxed inside the extension, so even a
kill -9 mid-run only delays refocus, never loses it.
"""
from __future__ import annotations

import json
import time
import uuid

import chrome

_ACK_ATTR = "data-uf-agent-ack"


def _command(cmd: str, ttl_minutes: int | None = None, timeout: float = 10.0) -> dict | None:
    """Send one bridge command; return the extension's state dict
    ({enabled, siteEnabled, mode, pausedMinutes}), or None if nothing acked."""
    req_id = uuid.uuid4().hex[:12]
    msg: dict = {"type": "UF_AGENT", "id": req_id, "cmd": cmd}
    if ttl_minutes is not None:
        msg["ttlMinutes"] = ttl_minutes
    payload = json.dumps(msg)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chrome.js(f"window.postMessage({payload}, '*'); 'sent'")
            time.sleep(0.6)
            raw = chrome.js(
                f"document.documentElement.getAttribute('{_ACK_ATTR}') || ''")
        except Exception:
            time.sleep(0.6)
            continue
        if raw:
            try:
                ack = json.loads(raw)
            except ValueError:
                continue
            if ack.get("id") == req_id:
                return ack.get("state") or {}
    return None


def status() -> dict | None:
    return _command("status")


def pause(ttl_minutes: int) -> dict | None:
    """Time-boxed pause; the extension re-enables itself after ttl_minutes
    even if resume() never comes. Never touches the user's master switch."""
    return _command("pause", ttl_minutes=ttl_minutes)


def resume() -> dict | None:
    return _command("resume")


def is_blocking(state: dict | None) -> bool:
    """Would the extension interfere with a scrape right now? Both modes do:
    block hides the feed outright, limit caps the scroll walk."""
    return bool(state and state.get("enabled") and state.get("siteEnabled"))
