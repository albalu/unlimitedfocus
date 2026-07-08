#!/usr/bin/env python3
"""Preflight: verifies every dependency the scraper needs, with exact fix hints.
Run once before your first scrape:  uv run check.py"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

failures = 0


def ok(name, detail=""):
    print(f"  ✓ {name}" + (f" — {detail}" if detail else ""))


def bad(name, detail, hint=""):
    global failures
    failures += 1
    print(f"  ✗ {name} — {detail}" + (f"\n      fix: {hint}" if hint else ""))


print("unlimitedfocus preflight (python)\n")

# 1. env vars
from uf_env import REPO_ROOT  # noqa: E402  (loads .env)

for name in ["BUTTERBASE_API_KEY", "NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD",
             "NEO4J_INSTANCE_NAME", "ROCKET_RIDE_API_KEY"]:
    if os.environ.get(name):
        ok(f"env {name}")
    else:
        bad(f"env {name}", "missing", f"add it to {REPO_ROOT / '.env'}")

# 2. Butterbase
try:
    import butterbase as bb
    import uf_config as cfg

    bb.select("contacts", {"limit": 1})
    ok("butterbase", f"app {cfg.BUTTERBASE_APP_ID} reachable, schema live")
except Exception as exc:
    bad("butterbase", str(exc)[:200])

# 3. Neo4j
try:
    import graph

    graph.get_driver().verify_connectivity()
    ok("neo4j", "connected")
    graph.close_graph()
except SystemExit as exc:
    bad("neo4j", str(exc)[:400])
except Exception as exc:
    bad("neo4j", str(exc)[:200],
        "add NEO4J_URI=neo4j+s://<instance-id>.databases.neo4j.io to .env "
        "(Aura console → your instance → Connect → Connection URI)")

# 4. claude CLI (extraction engine)
try:
    r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=15)
    ok("claude CLI", r.stdout.strip())
except Exception:
    bad("claude CLI", "not on PATH", "install Claude Code (https://claude.com/claude-code)")

# 4b. claude headless generation — proves auth actually works in -p mode
# (a 401 here + ANTHROPIC_API_KEY exported in your shell means that key was
#  hijacking auth; extract.clean_env strips it for real runs)
try:
    from extract import run_claude

    out = run_claude("reply with exactly: ok", timeout=90).strip()
    ok("claude headless (-p)", out[:60] or "(empty reply)")
except Exception as exc:
    bad("claude headless (-p)", str(exc)[:400],
        "run `claude -p 'say ok'` in your terminal — if that works but this fails, "
        "tell Claude the exact error text above")

# 5. AppleScript control of Chrome (opens + closes one about:blank tab)
try:
    import chrome

    chrome.new_tab("about:blank")
    result = chrome.js("1 + 1")
    chrome.close_tab()
    if result == "2":
        ok("chrome via AppleScript", "JavaScript from Apple Events enabled")
    else:
        bad("chrome via AppleScript", f"unexpected JS result {result!r}")
except Exception as exc:
    bad("chrome via AppleScript", str(exc)[:200],
        'in Chrome enable: menu View → Developer → "Allow JavaScript from Apple Events" '
        "(and grant Terminal automation permission if macOS asks)")

# 6. Unlimited Focus extension bridge (optional — but a blocker that can't be
# paused hides the feed and the scraper captures nothing, so verify loudly)
try:
    import time

    import chrome
    import extension

    chrome.new_tab("https://www.instagram.com/")
    time.sleep(6)  # content scripts inject at document_idle
    p = extension.probe()
    verdict, state = extension.detect()
    chrome.close_tab()
    if verdict == "ok":
        guard = ("actively guarding — scraper will pause/resume it"
                 if extension.is_blocking(state) else "installed, currently off")
        ok("unlimited focus bridge", f"v{p.get('bridge')} — {guard}")
    elif verdict == "stale":
        bad("unlimited focus bridge",
            "extension present but its agent bridge is not answering "
            "(loaded build predates src/content/agent.js?)",
            "chrome://extensions → Unlimited Focus → reload (↻), then rerun this check")
    else:
        ok("unlimited focus extension", "not detected — nothing the scraper needs to pause")
except Exception as exc:
    bad("unlimited focus bridge", str(exc)[:200])

print("\nall required checks passed — ready to scrape" if failures == 0
      else f"\n{failures} required check(s) failed")
sys.exit(0 if failures == 0 else 1)
