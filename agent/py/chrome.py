"""Drive the user's REAL Google Chrome via AppleScript — same technique as
slick_reader/marketing/foothill_browse_articles.py: no Playwright, no separate
profile, the human session with all its logins.

Conventions (matching the proven foothill script):
  - all work happens in the LAST tab of window 1 (we open it, we close it)
  - JS is base64-wrapped through eval(atob(...)) so quoting can never break
  - every osascript call is wall-clock capped so a wedged tab can't hang a run

Prerequisite (one-time): Chrome menu View → Developer → Allow JavaScript from
Apple Events must be enabled — check.py verifies and tells you if not.

Caveat: don't open/close tabs in Chrome window 1 while a run is going — the
last-tab convention would start driving the wrong tab. TODO(hardening): pin the
tab by navigating with a unique marker and re-locating it by URL each call.
"""
from __future__ import annotations

import base64
import json
import subprocess
import time

OSASCRIPT_TIMEOUT_SECS = 45


def _as(script: str) -> str:
    """Run AppleScript; return stdout stripped. Raises on error/timeout."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"AppleScript call timed out after {OSASCRIPT_TIMEOUT_SECS}s — Chrome tab unresponsive"
        ) from None
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


def new_tab(url: str) -> None:
    """Open URL as a new tab at the end of Chrome window 1 (creates the window if needed)."""
    safe = url.replace("\\", "\\\\").replace('"', '\\"')
    _as(
        'tell application "Google Chrome"\n'
        "    activate\n"
        "    if (count windows) = 0 then make new window\n"
        f'    tell window 1 to make new tab at end of tabs with properties {{URL:"{safe}"}}\n'
        "end tell"
    )


def js(code: str) -> str:
    """Execute JS in the LAST tab of window 1; return its string result.

    Returns '' when JS yields null/undefined ('missing value' in AppleScript).
    """
    b64 = base64.b64encode(code.encode()).decode()
    out = _as(
        'tell application "Google Chrome"\n'
        "    tell window 1\n"
        "        set t to last tab\n"
        f"        set r to execute t javascript \"eval(atob('{b64}'))\"\n"
        "    end tell\n"
        "    return r\n"
        "end tell"
    )
    return "" if out == "missing value" else out


def js_json(code: str):
    """Execute JS that returns JSON.stringify(...) and parse it."""
    out = js(code)
    if not out:
        return None
    return json.loads(out)


def tab_url() -> str:
    return _as('tell application "Google Chrome" to return URL of last tab of window 1')


def close_tab() -> None:
    """Close the last tab of window 1 (never the whole window). Best-effort."""
    try:
        _as(
            'tell application "Google Chrome"\n'
            "    if (count windows) = 0 then return\n"
            "    tell window 1\n"
            "        if (count tabs) > 1 then close last tab\n"
            "    end tell\n"
            "end tell"
        )
    except Exception:
        pass


def assert_on(fragment: str) -> None:
    """Guard: the last tab must still be ours (URL contains fragment)."""
    u = tab_url()
    if fragment not in u:
        raise RuntimeError(
            f"last tab is on {u!r}, expected *{fragment}* — were tabs opened/closed during the run?"
        )


def wait_for(condition_js: str, timeout: float = 20.0) -> bool:
    """Poll until condition_js is truthy in the last tab, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if js(f"({condition_js}) ? 'yes' : 'no'") == "yes":
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False
