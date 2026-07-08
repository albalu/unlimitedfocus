#!/usr/bin/env python3
"""Instagram scraper (PoC) — the agent that scrolls so you don't have to.

Drives YOUR real Chrome via AppleScript (same technique as slick_reader's
foothill_browse_articles.py): opens one new tab in window 1, browses
instagram.com inside your normal logged-in session, closes its tab when done.
No Playwright, no separate profile.

Capture-then-process, backed by a daily file cache (see cache.py):
  1. STORY WALK  — fast: per segment grab first-frame media + metadata, hit
     Next immediately (~2s/segment; never sits through videos). Multi-segment
     stories yield one record per segment.
  2. FEED WALK   — fast: scroll home collecting organic post cards; Sponsored /
     "Suggested for you" are SHIELDED (counted, never processed).
  3. PROCESS     — browser idle: every captured-but-uncommitted record (incl.
     leftovers from earlier runs today) -> claude CLI extraction -> Butterbase
     upsert -> Neo4j MERGE -> marked committed in the cache.

Plays nice with the Unlimited Focus extension in the same browser: if it is
actively guarding instagram, the run pauses it (time-boxed, master switch
untouched) for the walks and turns it back on at exit — success or failure.
An extension the user turned off themselves is left off.

Safe to rerun any time: capture skips anything in today's cache or the DB;
processing picks up whatever previous runs left uncommitted (even stories that
have since expired from Instagram). Cache dirs older than 7 days purge at
start. After a schema/prompt change, --overwrite-today re-extracts and
re-upserts everything captured today.

Usage:
    uv run scrape_instagram.py [--no-graph] [--overwrite-today] [--verbose]

Failure policy (owner's rule): missing config / unreachable dependencies fail
loudly up front. --no-graph is the one explicit escape hatch (backfill later
with graph_sync.py).

Fragility notes: selectors lean on aria-labels, URL shapes and time[datetime],
not class names. TODO(i18n): "Story by"/"Sponsored" literals assume English UI.
TODO(hardening): stories/videos capture one frame; carousels capture the
visible slide only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import random
import re
import time

import requests

import butterbase as bb
import cache
import chrome
import extension
import graph
import telegram
import uf_config as cfg
from extract import extract_item
from uf_env import MEDIA_DIR, RUNS_DIR, env

VERBOSE = False


def log(*args):
    print(dt.datetime.now().strftime("%H:%M:%S"), *args, flush=True)


def vlog(*args):
    if VERBOSE:
        log(*args)


# ── in-page JS (returns JSON strings; executed via chrome.js_json) ────────────

_DISMISS_JS = r"""(function(){
  var els = document.querySelectorAll('button, div[role="button"]');
  for (var i = 0; i < els.length; i++) {
    var t = (els[i].innerText || '').trim();
    if (t === 'Not Now' || t === 'Not now' || t === 'Cancel') { els[i].click(); return 'dismissed'; }
  }
  return 'none';
})()"""

# Find and click the first story ring that isn't your own. Three strategies
# (Instagram's DOM shifts often — same philosophy as the extension's blocker):
# aria-label, /stories/ links, then canvas rings -> nearest clickable ancestor.
# Returns diagnostics so a miss tells us what the tray actually looks like.
_TRAY_CLICK_JS = r"""(function(){
  function label(el){
    if (!el || !el.getAttribute) return '';
    return (el.getAttribute('aria-label') || '').toLowerCase();
  }
  var diag = {aria: 0, links: 0, canvases: 0};
  var cands = [];
  var aria = document.querySelectorAll('[aria-label^="Story by"]');
  diag.aria = aria.length;
  for (var i = 0; i < aria.length; i++) cands.push(aria[i]);
  if (!cands.length) {
    var links = document.querySelectorAll('main a[href^="/stories/"]');
    diag.links = links.length;
    for (var j = 0; j < links.length; j++) cands.push(links[j]);
  }
  if (!cands.length) {
    var canv = document.querySelectorAll('main canvas');
    diag.canvases = canv.length;
    for (var k = 0; k < canv.length; k++) {
      var p = canv[k].closest('[role="button"], button, a');
      if (p && cands.indexOf(p) === -1) cands.push(p);
    }
  }
  for (var c = 0; c < cands.length; c++) {
    var el = cands[c];
    var lb = label(el) + ' ' + label(el.parentElement);
    if (lb.indexOf('your story') !== -1 || lb.indexOf('add to') !== -1) continue;
    el.scrollIntoView({block: 'center'});
    el.click();
    return JSON.stringify({count: cands.length, clicked: true, diag: diag});
  }
  return JSON.stringify({count: 0, clicked: false, diag: diag});
})()"""

# Media grab must target the ACTIVE story: the viewer keeps neighbor users'
# cards mounted (smaller, off-center) and briefly shows the PREVIOUS segment's
# image while the new one loads. So prefer the largest img whose center sits in
# the middle band of the viewport; the caller additionally rejects an img URL
# identical to the previous segment's (stale frame) and re-polls.
_STORY_STATE_JS = r"""(function(){
  var m = {url: location.href};
  var t = document.querySelector('time[datetime]');
  if (t) m.datetime = t.getAttribute('datetime');
  var cx = window.innerWidth / 2;
  var best = null, bestW = 0, centered = null, centeredW = 0;
  var imgs = document.querySelectorAll('img');
  for (var i = 0; i < imgs.length; i++) {
    var im = imgs[i];
    var w = im.naturalWidth || 0;
    if (w < 200) continue;
    var r = im.getBoundingClientRect();
    if (r.width < 100) continue;
    if (w > bestW) { best = im; bestW = w; }
    var mid = r.left + r.width / 2;
    if (Math.abs(mid - cx) < window.innerWidth * 0.25 && w > centeredW) {
      centered = im; centeredW = w;
    }
  }
  var pick = centered || best;
  if (pick) m.img = pick.currentSrc || pick.src;
  var vid = document.querySelector('video');
  if (vid) {
    m.video = true;
    if (!m.img && vid.poster) m.img = vid.poster;
    var s = vid.currentSrc || vid.src || '';
    if (s && s.indexOf('blob:') !== 0) m.video_src = s;
  }
  m.text = (document.body ? document.body.innerText : '').slice(0, 1200);
  return JSON.stringify(m);
})()"""

# Advance the story viewer by CLICKING like a human. Shared machinery: a full
# pointer+mouse sequence with real coordinates and detail=1 (bare .click()
# doesn't exist on SVGs; React needs a believable sequence), always fired on
# the TOPMOST element at the click point — the same element a real cursor
# would hit, overlays included. Also handles the "View story" gate shown on
# direct /stories/<user>/ navigation.
#
# _NEXT_JS  — prefers the explicit Next control, tap zone only as fallback.
# _TAP_JS   — ignores the control and clicks the right-edge tap zone of the
#             story surface (used when the control turns out to be inert).
_ADVANCE_LIB = r"""
  function fire(el, x, y){
    var types = ['pointerover','pointerenter','pointermove','pointerdown','mousedown','pointerup','mouseup','click'];
    for (var i = 0; i < types.length; i++) {
      var t = types[i];
      try {
        var E = (t.indexOf('pointer') === 0 && window.PointerEvent) ? PointerEvent : MouseEvent;
        el.dispatchEvent(new E(t, {bubbles:true, cancelable:true, composed:true, view:window,
                                   detail:1, clientX:x, clientY:y, button:0,
                                   buttons:(t === 'pointerdown' || t === 'mousedown') ? 1 : 0,
                                   pointerId:1, isPrimary:true}));
      } catch(e) {}
    }
    try { if (typeof el.click === 'function') el.click(); } catch(e) {}
  }
  function fireAt(x, y, fallback){
    var tgt = document.elementFromPoint(x, y) || fallback;
    if (!tgt) return null;
    fire(tgt, x, y);
    return tgt.tagName.toLowerCase();
  }
  function visibleNextCtl(){
    // IG keeps hidden icon copies in the DOM whose rect is 0x0 at the origin —
    // only a Next control with a real on-screen rect counts.
    var els = document.querySelectorAll('[aria-label="Next"]');
    for (var i = 0; i < els.length; i++) {
      var b = els[i].closest('button, [role="button"]') || els[i];
      var r = b.getBoundingClientRect();
      if (r.width >= 8 && r.height >= 8 &&
          r.right > 0 && r.bottom > 0 &&
          r.left < window.innerWidth && r.top < window.innerHeight) {
        return b;
      }
    }
    return null;
  }
  function gate(out){
    var btns = document.querySelectorAll('button, div[role="button"]');
    for (var g = 0; g < btns.length; g++) {
      if ((btns[g].innerText || '').trim().toLowerCase() === 'view story') {
        var r = btns[g].getBoundingClientRect();
        out.did.push('gate:' + fireAt(r.left + r.width/2, r.top + r.height/2, btns[g]));
        return true;
      }
    }
    return false;
  }
  function surface(){
    var surf = document.querySelector('video'), best = 0;
    if (!surf) {
      var imgs = document.querySelectorAll('img');
      for (var i = 0; i < imgs.length; i++) {
        var w = imgs[i].getBoundingClientRect().width;
        if (w > best) { best = w; surf = imgs[i]; }
      }
    }
    return surf;
  }
  function tap(out){
    var surf = surface();
    if (!surf) { out.did.push('no-surface'); return; }
    var r = surf.getBoundingClientRect();
    var x = r.right - Math.max(24, r.width * 0.1), y = r.top + r.height / 2;
    out.did.push('tap:' + fireAt(x, y, surf));
  }
"""

_NEXT_JS = r"""(function(){""" + _ADVANCE_LIB + r"""
  var out = {did: []};
  if (gate(out)) return JSON.stringify(out);
  var b = visibleNextCtl();
  if (b) {
    var r = b.getBoundingClientRect();
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    fire(b, cx, cy);
    out.did.push('ctl@' + Math.round(cx) + ',' + Math.round(cy) + ':' + b.tagName.toLowerCase());
  } else {
    out.did.push('no-visible-ctl');
    tap(out);
  }
  return JSON.stringify(out);
})()"""

_TAP_JS = r"""(function(){""" + _ADVANCE_LIB + r"""
  var out = {did: []};
  if (gate(out)) return JSON.stringify(out);
  tap(out);
  return JSON.stringify(out);
})()"""

# Tray usernames in order, read from the FEED before opening the viewer —
# lets the walk jump to the next user's story by URL when in-story clicks
# refuse to advance (degraded mode: first segment per user, but keeps moving).
_TRAY_LIST_JS = r"""(function(){
  var seen = {}, out = [];
  var links = document.querySelectorAll('a[href^="/stories/"]');
  for (var i = 0; i < links.length; i++) {
    var m = (links[i].getAttribute('href') || '').match(/^\/stories\/([^/]+)/);
    if (m && !seen[m[1]]) { seen[m[1]] = 1; out.push(m[1]); }
  }
  return JSON.stringify(out);
})()"""

# Diagnostic: what interactive labels does the viewer actually expose? Logged
# when we cannot advance, so the next iteration can fix the selector for real.
_ARIA_DUMP_JS = r"""(function(){
  var els = document.querySelectorAll('[aria-label]');
  var out = [];
  for (var i = 0; i < els.length && out.length < 40; i++) {
    out.push(els[i].tagName + ':' + els[i].getAttribute('aria-label'));
  }
  return JSON.stringify(out);
})()"""

# One record per visible feed <article>: permalink, author, timestamp,
# innerText, and the largest non-avatar image.
# Author resolution (m.author) tries, in order:
#   1. avatar alt text — "<username>'s profile picture"
#   2. first single-segment profile link (/username/), skipping app routes
#   3. first innerText line, when it looks like a handle
# The poster identity is load-bearing downstream (friend vs stranger vs
# business), so a card with no resolvable author is SKIPPED, not '@unknown'.
_FEED_JS = r"""(function(){
  var ROUTES = ['p','reel','reels','explore','stories','direct','accounts','tv'];
  function authorOf(a) {
    var av = a.querySelector('img[alt$="profile picture"]');
    if (av) {
      var mm = (av.getAttribute('alt') || '').match(/^(.+?)'s profile picture$/);
      if (mm) return mm[1];
    }
    var links = a.querySelectorAll('a[href^="/"]');
    for (var j = 0; j < links.length; j++) {
      var h = links[j].getAttribute('href') || '';
      var m2 = h.match(/^\/([A-Za-z0-9._]+)\/?(?:[?#].*)?$/);
      if (m2 && ROUTES.indexOf(m2[1].toLowerCase()) === -1) return m2[1];
    }
    var first = ((a.innerText || '').split('\n')[0] || '').trim();
    if (/^[A-Za-z0-9._]{1,30}$/.test(first)) return first;
    return null;
  }
  var out = [], arts = document.querySelectorAll('article');
  for (var i = 0; i < arts.length; i++) {
    var a = arts[i], m = {};
    var link = a.querySelector('a[href*="/p/"], a[href*="/reel/"]');
    if (!link) continue;
    m.href = link.getAttribute('href');
    m.author = authorOf(a);
    var t = a.querySelector('time[datetime]');
    if (t) m.datetime = t.getAttribute('datetime');
    m.text = (a.innerText || '').slice(0, 4000);
    var best = null, imgs = a.querySelectorAll('img');
    for (var j2 = 0; j2 < imgs.length; j2++) {
      var im = imgs[j2];
      if ((im.getAttribute('alt') || '').indexOf('profile picture') !== -1) continue;
      if ((im.naturalWidth || 0) > ((best && best.naturalWidth) || 0)) best = im;
    }
    if (best && (best.naturalWidth || 0) >= 200) m.img = best.currentSrc || best.src;
    var vid = a.querySelector('video');
    if (vid) { m.video = true; if (!m.img && vid.poster) m.img = vid.poster; }
    out.push(m);
  }
  return JSON.stringify(out);
})()"""


# ── helpers ───────────────────────────────────────────────────────────────────

def download_media(url: str | None, stem: str) -> str | None:
    """Fetch a media CDN URL to MEDIA_DIR; None (-> text-only extraction) on miss."""
    if not url or url.startswith("blob:"):
        return None
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
        p = MEDIA_DIR / f"{stem}{ext}"
        p.write_bytes(r.content)
        return str(p)
    except Exception as exc:
        vlog(f"    (media download failed, text-only extraction: {str(exc)[:100]})")
        return None


def append_jsonl(ctx: dict, obj: dict) -> None:
    obj = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), **obj}
    with open(ctx["jsonl_path"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _log_snooze_skip(ctx: dict, username: str, kind: str) -> None:
    """First skip per poster logs at normal level (visible confirmation the
    snooze is acknowledged); repeats only with --verbose to avoid spam when
    one muted account has many stories."""
    if username not in ctx["snooze_logged"]:
        ctx["snooze_logged"].add(username)
        log(f"  🔕 skipping @{username} ({kind}) — snoozed")
    else:
        vlog(f"  🔕 skipped another {kind} by snoozed @{username}")
    append_jsonl(ctx, {"type": "snoozed_skip", "handle": username, "kind": kind})


def capture(ctx: dict, rec: dict) -> None:
    """Register a capture in the daily cache + this run's pending set."""
    cache.append_captured(rec)
    ctx["cache_captured"][rec["external_id"]] = rec
    vlog(f"  ⊙ captured {rec['kind']} {rec['external_id']} by @{rec['username']}")


def process_item(ctx: dict, *, kind: str, username: str, external_id: str, url: str,
                 image_path: str | None, raw_text: str | None, posted_at: str | None,
                 media_hint: str | None = None, **_ignored) -> None:
    log(f"  ⋯ {kind} {external_id} by @{username}")
    x = extract_item(kind, username, image_path, raw_text)
    if media_hint and x.get("media_type") in (None, "unknown", "image"):
        x["media_type"] = media_hint

    contact = bb.upsert_contact(cfg.PLATFORM_INSTAGRAM, username,
                                profile_url=f"https://www.instagram.com/{username}/")
    item = bb.upsert_item({
        "platform": cfg.PLATFORM_INSTAGRAM,
        "kind": kind,
        "external_id": external_id,
        "url": url,
        "contact_id": contact["id"],
        "media_type": x.get("media_type") or "unknown",
        "topic": x.get("topic"),
        "structured": x,
        "brief": x.get("brief"),
        "detail": x.get("detail"),
        "caption_raw": raw_text,
        "media_path": image_path,
        "posted_at": posted_at,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })

    if not ctx["no_graph"]:
        graph.sync_item_to_graph(contact, item, x.get("mentions"))
        bb.mark_graph_synced(item["id"])

    ctx["stats"]["new_stories" if kind == "story" else "new_posts"] += 1
    append_jsonl(ctx, {"type": "item", "kind": kind, "username": username,
                       "external_id": external_id, "url": url, "topic": x.get("topic"),
                       "brief": x.get("brief"), "noteworthy": x.get("noteworthy"),
                       "posted_at": posted_at})
    log(f"  ✓ [{x.get('topic') or '—'}] {x.get('brief') or ''}")


# ── stories (walk only — processing happens in process_pending) ───────────────

def _preload_snoozed_handles() -> set[str]:
    """Posters the user snoozed in the UI (contacts.snoozed_until in the
    future). The scraper honors this at capture time: no screenshot, no
    extraction tokens, no DB/graph writes for muted posters."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = bb.select("contacts", {
        "platform": f"eq.{cfg.PLATFORM_INSTAGRAM}",
        "snoozed_until": f"gt.{now}",
        "select": "handle",
        "limit": 1000,
    })
    return {r["handle"] for r in rows}


def _preload_known_story_ids() -> set[str]:
    """One query instead of a per-segment HTTP dedupe check (stories only live
    24h, so the most recent rows more than cover the tray)."""
    rows = bb.select("items", {
        "platform": f"eq.{cfg.PLATFORM_INSTAGRAM}", "kind": "eq.story",
        "select": "external_id", "order": "captured_at.desc", "limit": 500,
    })
    return {r["external_id"] for r in rows}


def scrape_stories(ctx: dict) -> None:
    log("— stories (walk) —")
    tray_users = chrome.js_json(_TRAY_LIST_JS) or []  # for URL-jump fallback
    vlog(f"  tray users (link strategy): {tray_users}")
    res = chrome.js_json(_TRAY_CLICK_JS)
    if not res or not res.get("clicked"):
        log(f"no stories tray found — skipping stories (diagnostics: {(res or {}).get('diag')})")
        return
    log(f"stories tray: {res['count']} candidate ring(s)")
    if not chrome.wait_for("location.pathname.indexOf('/stories/') === 0", 15):
        log("story viewer did not open — skipping stories")
        return

    known = _preload_known_story_ids()
    walked = 0
    hops, max_hops = 0, cfg.MAX_STORIES * 8
    last_id, stuck = None, 0
    last_img_url = None  # stale-frame guard: previous segment's media URL

    def advance(tap: bool = False):
        # The VISIBLE Next control is what demonstrably advances (hidden 0x0
        # icon copies are filtered out; clicks land at its real coordinates).
        # The right-edge tap zone is the retry variant.
        r = chrome.js_json(_TAP_JS if tap else _NEXT_JS) or {}
        vlog(f"    advance{'(tap)' if tap else ''}: {r.get('did')}")

    def jump_next_user(current: str) -> bool:
        """Degraded mode: in-story clicks refuse to advance — hop to the next
        tray user's story by URL (captures their current segment at least)."""
        if current in tray_users:
            rest = tray_users[tray_users.index(current) + 1:]
        else:
            rest = tray_users
        nxt = rest[0] if rest else None
        if not nxt:
            return False
        log(f"  … cannot advance within @{current}'s story — jumping to @{nxt} by URL")
        chrome.js(f"location.href = 'https://www.instagram.com/stories/{nxt}/'; 'ok'")
        return True

    while walked < cfg.MAX_STORIES and hops < max_hops:
        hops += 1
        time.sleep(random.uniform(0.5, 0.9))  # DOM settle only — we don't watch stories
        url = chrome.tab_url()
        if "/stories/" not in url:
            break  # viewer closed = tray finished
        m = re.search(r"/stories/([^/]+)/(\d+)", url)
        if not m:
            advance()  # interstitial between users, or a "View story" gate
            continue
        username, story_id = m.group(1), m.group(2)

        if story_id == last_id:
            # URL unchanged -> our advance didn't register. Retry the control,
            # then the raw tap zone, then jump to the next user by URL, and as
            # a last resort dump the aria-labels and stop stories.
            stuck += 1
            if stuck == 2:
                advance()
            elif stuck in (3, 4):
                advance(tap=True)
            elif stuck == 5:
                if jump_next_user(username):
                    last_id, stuck = None, 0
                continue
            elif stuck >= 7:
                labels = ""
                try:
                    labels = chrome.js(_ARIA_DUMP_JS)
                except Exception:
                    pass
                log(f"  ✗ cannot advance stories — viewer aria-labels: {labels[:400]}")
                break
            continue
        last_id, stuck = story_id, 0

        if username in ctx["snoozed"]:
            ctx["stats"]["snoozed_skipped"] += 1
            _log_snooze_skip(ctx, username, "story")
            advance()
            continue
        if story_id in ctx["cache_captured"]:  # captured earlier today — data already on disk
            advance()
            continue
        if story_id in known:
            ctx["stats"]["dupes"] += 1
            advance()
            continue
        known.add(story_id)

        try:
            state = chrome.js_json(_STORY_STATE_JS) or {}
            # Stale-frame guard: a NEW segment can never have the SAME media URL
            # as the previous one — if it does, the viewer is still showing the
            # old image (this produced identical wrong briefs across many
            # items). Re-poll while it loads; give up to text-only extraction
            # rather than describing the wrong image.
            img_url = state.get("img")
            if img_url and img_url == last_img_url:
                for _ in range(3):
                    time.sleep(0.45)
                    state = chrome.js_json(_STORY_STATE_JS) or state
                    img_url = state.get("img")
                    if img_url != last_img_url:
                        break
                if img_url == last_img_url:
                    log(f"  ~ story {story_id}: image still stale after retries — capturing text-only")
                    img_url = None
            if img_url:
                last_img_url = img_url

            image_path = download_media(img_url, f"story_{story_id}")
            capture(ctx, dict(
                kind="story", username=username, external_id=story_id,
                url=f"https://www.instagram.com/stories/{username}/{story_id}/",
                image_path=image_path, raw_text=state.get("text"),
                posted_at=state.get("datetime"),
                media_hint="video" if state.get("video") else None,
            ))
            walked += 1
        except Exception as exc:
            ctx["stats"]["errors"] += 1
            log(f"  ✗ story {story_id} capture failed (will retry next run): {str(exc)[:160]}")
        advance()  # move on immediately; extraction happens after the walks

    log(f"story walk done: {walked} captured, {ctx['stats']['dupes']} already known")


# ── feed (walk only — processing happens in process_pending) ──────────────────

def _preload_known_post_ids() -> set[str]:
    rows = bb.select("items", {
        "platform": f"eq.{cfg.PLATFORM_INSTAGRAM}", "kind": "in.(post,reel)",
        "select": "external_id", "order": "captured_at.desc", "limit": 1000,
    })
    return {r["external_id"] for r in rows}


def scrape_feed(ctx: dict) -> None:
    log("— home feed (walk) —")
    chrome.js("location.href = 'https://www.instagram.com/'; 'ok'")
    time.sleep(random.uniform(2.5, 4.0))
    chrome.js(_DISMISS_JS)

    known = _preload_known_post_ids()
    handled: set[str] = set()  # shortcodes touched this run (feed DOM is virtualized)
    walked = dup_streak = rounds = 0

    while (walked < cfg.MAX_NEW_POSTS
           and rounds < cfg.MAX_SCROLL_ROUNDS
           and dup_streak < cfg.DUP_STREAK_STOP):
        rounds += 1
        chrome.assert_on("instagram.com")
        cards = chrome.js_json(_FEED_JS) or []

        for card in cards:
            if walked >= cfg.MAX_NEW_POSTS:
                break
            sc_match = re.search(r"/(?:p|reel)/([^/?]+)", card.get("href") or "")
            if not sc_match:
                continue
            shortcode = sc_match.group(1)
            if shortcode in handled:
                continue
            handled.add(shortcode)
            text = card.get("text") or ""

            # The shield: promotional content is counted, never processed.
            if re.search(r"\bSponsored\b", text):
                ctx["stats"]["ads_shielded"] += 1
                append_jsonl(ctx, {"type": "shielded", "reason": "sponsored", "external_id": shortcode})
                log(f"  🛡 shielded sponsored content ({ctx['stats']['ads_shielded']} this run)")
                continue
            if "Suggested for you" in text:
                ctx["stats"]["suggested_shielded"] += 1
                append_jsonl(ctx, {"type": "shielded", "reason": "suggested", "external_id": shortcode})
                continue

            if shortcode in ctx["cache_captured"]:  # captured earlier today
                continue
            if shortcode in known:
                dup_streak += 1
                ctx["stats"]["dupes"] += 1
                continue
            dup_streak = 0

            username = card.get("author")
            if not username:
                # Poster identity is load-bearing (friend vs stranger vs business
                # judgement later) — never record '@unknown'; retry next run.
                ctx["stats"]["author_unresolved"] += 1
                log(f"  ~ skipped {shortcode}: could not resolve author (will retry next run)")
                vlog(f"    card text head: {text[:150]!r}")
                continue
            if username in ctx["snoozed"]:
                ctx["stats"]["snoozed_skipped"] += 1
                _log_snooze_skip(ctx, username, "post")
                continue

            known.add(shortcode)
            kind = "reel" if "/reel/" in card["href"] else "post"
            image_path = download_media(card.get("img"), f"post_{shortcode}")
            capture(ctx, dict(
                kind=kind, username=username, external_id=shortcode,
                url=f"https://www.instagram.com{card['href']}",
                image_path=image_path, raw_text=text,
                posted_at=card.get("datetime"),
                media_hint="video" if card.get("video") else None,
            ))
            walked += 1

        chrome.js(f"window.scrollBy(0, {random.randint(900, 1600)}); 'ok'")
        time.sleep(random.uniform(1.5, 3.2))  # human-ish pacing — be polite, avoid account flags

    if dup_streak >= cfg.DUP_STREAK_STOP:
        log(f"stopping: {dup_streak} consecutive already-known posts (reached previously scraped territory)")
    log(f"feed walk done: {walked} captured")


# ── processing (browser idle) ─────────────────────────────────────────────────

def process_pending(ctx: dict) -> None:
    pending = [rec for ext_id, rec in ctx["cache_captured"].items()
               if ext_id not in ctx["committed"]]
    if not pending:
        log("nothing pending to process")
        return
    log(f"— processing {len(pending)} pending item(s) (incl. any left over from earlier runs today) —")
    for rec in pending:
        try:
            process_item(ctx, **rec)
            cache.mark_committed(rec["external_id"])
            ctx["committed"].add(rec["external_id"])
        except Exception as exc:
            ctx["stats"]["errors"] += 1
            log(f"  ✗ {rec.get('kind')} {rec.get('external_id')} failed (stays cached, retries next run): {str(exc)[:200]}")


# ── favorites report (runs last, after processing) ────────────────────────────

def report_favorites(ctx: dict, cutoff_iso: str) -> None:
    """End-of-run round-up: Telegram the new post/story links of every favorite
    poster (top story link only per poster). 'New' = captured since the previous
    completed run (24h fallback on the first run). Best-effort — any failure is
    logged and swallowed so it never breaks a scrape."""
    if not telegram.configured():
        log("telegram not configured (TELEGRAM_SEASONS_*) — skipping favorites report")
        return
    try:
        favs = bb.select("contacts", {
            "favorited": "eq.true", "select": "id,handle,display_name",
            "order": "handle.asc", "limit": 500,
        })
        if not favs:
            log("no favorite posters — skipping favorites report")
            return

        sections, links, empty = [], 0, 0
        for c in favs:
            items = bb.select("items", {
                "contact_id": f"eq.{c['id']}", "deleted_at": "is.null",
                "captured_at": f"gte.{cutoff_iso}", "select": "kind,url",
                "order": "captured_at.desc", "limit": 100,
            })
            items = [it for it in items if it.get("url")]
            if not items:
                empty += 1
                continue
            stories = [it for it in items if it.get("kind") == "story"]
            others = [it for it in items if it.get("kind") != "story"]

            name = c.get("display_name")
            head = f"❤️ <b>@{html.escape(c['handle'])}</b>"
            if name and name != c["handle"]:
                head += f" · {html.escape(name)}"
            lines = [head]
            if stories:  # top (most recent) story only
                surl = html.escape(stories[0]["url"])
                lines.append(f'• 📖 <a href="{surl}">story</a>')
                links += 1
            seen = set()
            for it in others:
                if it["url"] in seen:
                    continue
                seen.add(it["url"])
                reel = it.get("kind") == "reel"
                lines.append(f'• {"🎬" if reel else "📷"} '
                             f'<a href="{html.escape(it["url"])}">{"reel" if reel else "post"}</a>')
                links += 1
            sections.append("\n".join(lines))

        if not sections:
            log(f"favorites report: no new posts from {len(favs)} favorite(s) since last run")
            return
        date = dt.datetime.now().strftime("%a %b %d")
        body = f"❤️ <b>Favorites update</b> — {date}\n\n" + "\n\n".join(sections)
        if empty:
            body += f"\n\n<i>({empty} other favorite(s) had no new posts)</i>"
        telegram.send_message(body)
        log(f"📮 favorites report sent to Telegram: {len(sections)} poster(s), {links} link(s)")
    except Exception as exc:
        log(f"⚠ favorites report failed (non-fatal): {str(exc)[:200]}")


# ── main ──────────────────────────────────────────────────────────────────────

def print_summary(ctx: dict) -> None:
    s = ctx["stats"]
    uncommitted = len([1 for e in ctx["cache_captured"] if e not in ctx["committed"]])
    print(f"""
──────────────────────────────────────────
 run complete
   new posts      {s['new_posts']}
   new stories    {s['new_stories']}
   already known  {s['dupes']}
   🛡 ads shielded         {s['ads_shielded']}
   🛡 suggested shielded   {s['suggested_shielded']}
   🔕 snoozed skipped      {s['snoozed_skipped']}
   author unresolved       {s['author_unresolved']}
   errors                  {s['errors']}
   cached, not yet in DB   {uncommitted}  (rerun to retry)
 inspect: uv run inspect_data.py   |   raw log: {ctx['jsonl_path']}
──────────────────────────────────────────""")


def main() -> None:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-graph", action="store_true",
                    help="skip Neo4j sync (backfill later with graph_sync.py)")
    ap.add_argument("--overwrite-today", action="store_true",
                    help="re-extract + re-upsert everything captured today "
                         "(use after schema/prompt changes; DB rows are updated in place)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    env("BUTTERBASE_API_KEY")  # fail fast before touching the browser
    if not args.no_graph:
        graph.verify_graph()
        graph.ensure_constraints()
        log("neo4j connected")
    else:
        log("--no-graph: items stay graph_synced=false; run `uv run graph_sync.py` later")

    purged = cache.purge_old()
    if purged:
        log(f"purged {purged} cache day(s) older than {cache.PURGE_DAYS} days")

    prev = bb.last_completed_run(cfg.PLATFORM_INSTAGRAM)
    log(f"last completed run: {prev['started_at']}" if prev else "first run for this platform")
    # Favorites report scopes to content captured since the previous run (so
    # re-runs the same day don't re-send the same links); 24h on the first run.
    fav_cutoff = (prev["started_at"] if prev and prev.get("started_at")
                  else (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat())

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    run = bb.start_run(cfg.PLATFORM_INSTAGRAM)
    ctx = {
        "no_graph": args.no_graph,
        "jsonl_path": RUNS_DIR / (dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + ".jsonl"),
        "cache_captured": cache.load_captured(),
        "committed": set() if args.overwrite_today else cache.load_committed(),
        "snoozed": _preload_snoozed_handles(),
        "snooze_logged": set(),
        "stats": {"new_posts": 0, "new_stories": 0, "dupes": 0,
                  "ads_shielded": 0, "suggested_shielded": 0, "snoozed_skipped": 0,
                  "author_unresolved": 0, "errors": 0},
    }
    if ctx["snoozed"]:
        # Log WHO is being honored (not just a count) so acknowledgment is
        # always verifiable — full list lands in the run's jsonl.
        names = sorted(ctx["snoozed"])
        shown = ", ".join("@" + h for h in names[:15])
        more = f" … +{len(names) - 15} more (full list in run log)" if len(names) > 15 else ""
        log(f"🔕 honoring {len(names)} snoozed poster(s): {shown}{more}")
        append_jsonl(ctx, {"type": "snoozed_honored", "handles": names})
    if ctx["cache_captured"]:
        already = len([1 for e in ctx["cache_captured"] if e in ctx["committed"]])
        log(f"daily cache: {len(ctx['cache_captured'])} captured today "
            f"({already} committed{', overwrite requested' if args.overwrite_today else ''})")
    log(f"run {run['id']} started")

    chrome.new_tab("https://www.instagram.com/")
    time.sleep(random.uniform(3.0, 5.0))
    ctx["tab_open"] = True
    ctx["uf_paused"] = False

    def close_our_tab():
        # Guarded: close_tab targets the LAST tab of window 1, so a second call
        # would hit one of the user's own tabs.
        if ctx["tab_open"]:
            chrome.close_tab()
            ctx["tab_open"] = False

    def pause_extension():
        """If Unlimited Focus is actively guarding instagram, pause it for the
        walks. Only pauses what is ON — a user-disabled extension stays off,
        because the pause layer never touches the master switch. A build too
        old to have the bridge fails the run loudly (owner's rule): its
        blocker keeps the feed display:none and every capture comes up empty."""
        verdict, state = extension.detect()
        if verdict == "absent":
            log("unlimited focus extension not detected — nothing to pause")
        elif verdict == "stale":
            raise SystemExit(
                "Unlimited Focus is installed but not answering its agent bridge — the "
                "loaded build predates src/content/agent.js, and its blocker would hide "
                "everything this run tries to capture.\n"
                "Fix: chrome://extensions → Unlimited Focus → reload (↻), then rerun. "
                "Verify with: uv run check.py"
            )
        elif extension.is_blocking(state):
            if extension.pause(cfg.EXTENSION_PAUSE_MINUTES) is not None:
                ctx["uf_paused"] = True
                log(f"🧘 unlimited focus paused for this run "
                    f"(crash backstop: re-enables itself in {cfg.EXTENSION_PAUSE_MINUTES} min)")
                if not extension.wait_until_unhidden():
                    log("⚠ feed still hidden after the pause ack — captures may come up empty")
                time.sleep(random.uniform(1.5, 2.5))  # let the unblocked feed render
            else:
                log("⚠ could not pause unlimited focus — proceeding, but the feed may be hidden")
        else:
            log("unlimited focus is already off — leaving it off")

    def restore_extension():
        # Needs our tab: the bridge lives in its content script. Hence called
        # BEFORE close_our_tab on every path.
        if not ctx["uf_paused"]:
            return
        ctx["uf_paused"] = False
        if extension.resume() is not None:
            log("🧘 unlimited focus back on")
        else:
            log(f"⚠ could not turn unlimited focus back on — it re-enables itself "
                f"within {cfg.EXTENSION_PAUSE_MINUTES} min")

    try:
        if "/accounts/login" in chrome.tab_url():
            raise SystemExit("Instagram is not logged in in your Chrome — log in, then rerun.")
        pause_extension()
        chrome.js(_DISMISS_JS)

        scrape_stories(ctx)
        scrape_feed(ctx)
        restore_extension()
        close_our_tab()  # walks done — browser not needed for processing

        process_pending(ctx)

        bb.finish_run(run["id"], "completed", ctx["stats"])
        report_favorites(ctx, fav_cutoff)  # last step: after everything else is done
        print_summary(ctx)
    except BaseException as exc:
        try:
            bb.finish_run(run["id"], "failed", ctx["stats"], error=str(exc)[:500])
        except Exception:
            pass
        raise
    finally:
        restore_extension()
        close_our_tab()
        graph.close_graph()


if __name__ == "__main__":
    main()
