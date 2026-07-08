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

# Synchronous DOM markers the extension leaves on <html>:
#   data-uf-agent-bridge   stamped by agent.js at load, value = build version
#                          (only builds >= 0.5 have the bridge)
#   data-uf-feed-hidden /  set by the blocker while it is actively hiding
#   data-uf-main-hidden    this page (block mode on a feed path)
_PROBE_JS = """(function(){
  var root = document.documentElement;
  return JSON.stringify({
    bridge: root.getAttribute('data-uf-agent-bridge'),
    hidden: root.hasAttribute('data-uf-feed-hidden')
         || root.hasAttribute('data-uf-main-hidden'),
  });
})()"""

_UNHIDDEN_COND = ("!document.documentElement.hasAttribute('data-uf-feed-hidden') && "
                  "!document.documentElement.hasAttribute('data-uf-main-hidden')")


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


def probe() -> dict:
    """Cheap, synchronous DOM look: {'bridge': version-or-None, 'hidden': bool}.
    No message round-trip; safe on any page."""
    try:
        return chrome.js_json(_PROBE_JS) or {}
    except Exception:
        return {}


def detect() -> tuple[str, dict | None]:
    """Classify what's in this browser:
      ('ok', state)    bridge present and answering — state as from status()
      ('stale', None)  the extension is here (bridge marker or an actively
                       hiding blocker) but commands go unanswered — almost
                       always a loaded build that predates the bridge and
                       needs a reload at chrome://extensions
      ('absent', None) no sign of the extension on this page
    """
    p = probe()
    if p.get("bridge"):
        state = status()
        return ("ok", state) if state is not None else ("stale", None)
    return ("stale", None) if p.get("hidden") else ("absent", None)


def wait_until_unhidden(timeout: float = 10.0) -> bool:
    """After a pause ack, wait for the blocker to actually lift (its hiding
    markers to leave <html>). Trivially true when it wasn't hiding."""
    return chrome.wait_for(_UNHIDDEN_COND, timeout)


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
