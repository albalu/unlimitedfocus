#!/usr/bin/env bash
# Launches a real Google Chrome with remote debugging (CDP) enabled so the
# scraper can attach to it and open its own tab.
#
# Why a dedicated profile dir instead of your main one? Chrome 136+ refuses
# remote debugging on the default profile (security hardening), so we keep a
# separate persistent profile. Log into Instagram ONCE in this window — the
# session (cookies, device trust) persists across every future run, so
# overnight runs stay authenticated with your real browser fingerprint.
set -euo pipefail

PROFILE_DIR="${UF_CHROME_PROFILE:-$HOME/.unlimitedfocus/chrome-profile}"
PORT="${UF_CDP_PORT:-9222}"

mkdir -p "$PROFILE_DIR"
echo "Chrome profile: $PROFILE_DIR"
echo "CDP endpoint:   http://localhost:$PORT"
echo "First time? Log into instagram.com in this window, then run: npm run scrape:instagram"

exec "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  --window-size=1280,1000 \
  "https://www.instagram.com/"
