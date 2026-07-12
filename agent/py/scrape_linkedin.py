#!/usr/bin/env python3
"""LinkedIn scraper — the agent that scrolls so you don't have to.

Drives YOUR real Chrome via AppleScript (see chrome.py): opens one new tab in
window 1, browses linkedin.com/feed/ inside your normal logged-in session,
closes its tab when done. No Playwright, no separate profile.

Feed-only (LinkedIn retired stories in 2021), capture-then-process on the same
daily cache + shared run machinery as scrape_instagram.py (cache.py,
scrape_common.py):
  1. FEED WALK — fast: scroll the home feed collecting organic post cards;
     Promoted / "Suggested" are SHIELDED (counted, never processed).
  2. PROCESS   — browser idle: every captured-but-uncommitted record (incl.
     leftovers from earlier runs today) -> claude CLI extraction -> Butterbase
     upsert -> Neo4j MERGE -> marked committed in the cache.

Plays nice with the Unlimited Focus extension in the same browser: if it is
actively guarding linkedin, the run pauses it (time-boxed, master switch
untouched) for the walk and turns it back on at exit — success or failure.

Safe to rerun any time — same cache/dedupe/retry semantics as the Instagram
scraper (see its docstring).

Usage:
    uv run scrape_linkedin.py [--no-graph] [--overwrite-today] [--verbose]

Fragility notes: cards are located by their urn:li:* values — data-id/data-urn
first, falling back to a scan of every attribute under <main> (LinkedIn
shuffles attribute names across rollouts; the urn payload is what stays) —
and author by profile-link URL shape (/in/, /company/), not class names;
images are told apart from avatars by CDN URL shape. posted_at comes from the urn id
itself (LinkedIn ids are snowflakes — first 41 bits are epoch ms), so no DOM
timestamp is needed. TODO(i18n): "Promoted"/"Suggested" literals assume
English UI. TODO(phase-later): wire linkedin into the digest pipeline.
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import time

import butterbase as bb
import chrome
import graph
import scrape_common as sc
import uf_config as cfg
from scrape_common import log, vlog

# ── in-page JS (returns JSON strings; executed via chrome.js_json) ────────────

# Only genuine modal dialogs (premium upsells, "add to your feed" nags) — the
# docked messaging overlay is left alone.
_DISMISS_JS = r"""(function(){
  var d = document.querySelector('[role="dialog"] button[aria-label="Dismiss"]');
  if (d) { d.click(); return 'dismissed'; }
  return 'none';
})()"""

# One record per top-level feed card. Cards are addressed by their urn
# ("urn:li:activity:<id>" etc.) — the one part of LinkedIn's DOM that is
# load-bearing for the site itself and thus stable. Two strategies:
#   1. data-id / data-urn attributes (the classic feed DOM), then
#   2. a scan of EVERY attribute under <main> for an item urn — LinkedIn
#      shuffles attribute names across rollouts; the urn value is what stays.
# Returns {cards, diag} so a miss tells us what the page actually looks like.
#
# Author resolution: the first /in/ | /company/ | /school/ link that contains
# a real avatar image. The social-context header ("<connection> likes this")
# and body @mentions are text-only links, so requiring the avatar picks the
# actor block, not the connection who surfaced the post. Author identity is
# load-bearing downstream — a card with no resolvable author is SKIPPED.
_FEED_JS = r"""(function(){
  var CARD_SEL = '[data-id^="urn:li:"], [data-urn^="urn:li:"]';
  var URN = /urn:li:(activity|ugcPost|share):(\d+)/;
  function urnMatch(el){
    for (var a = 0; a < el.attributes.length; a++) {
      var m = String(el.attributes[a].value).match(URN);
      if (m) return m;
    }
    return null;
  }
  function isAggregate(el){  // suggestion clusters, never single posts
    for (var a = 0; a < el.attributes.length; a++)
      if (String(el.attributes[a].value).indexOf('urn:li:aggregate') === 0) return true;
    return false;
  }
  function collect(){
    var prim = document.querySelectorAll(CARD_SEL);
    if (prim.length) return {mode: 'data-attr', els: Array.prototype.slice.call(prim)};
    var scope = document.querySelector('main') || document.body;
    var all = scope.querySelectorAll('*'), els = [];
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (el.attributes && el.attributes.length && urnMatch(el)) els.push(el);
    }
    return {mode: 'attr-scan', els: els};
  }
  function outermost(els){
    // Top-level cards only: a reshare embeds the original post as a nested
    // urn-tagged element — that content belongs to the outer card's record.
    var out = [];
    for (var i = 0; i < els.length; i++) {
      var p = els[i].parentElement, nested = false;
      while (p && !nested) { if (els.indexOf(p) !== -1) nested = true; p = p.parentElement; }
      if (!nested) out.push(els[i]);
    }
    return out;
  }
  function authorOf(card, m) {
    var links = card.querySelectorAll('a[href*="/in/"], a[href*="/company/"], a[href*="/school/"]');
    var actor = null;
    for (var j = 0; j < links.length; j++) {
      var am = (links[j].getAttribute('href') || '').match(/\/(in|company|school)\/([^\/?#]+)/);
      if (!am) continue;
      var av = links[j].querySelector('img');
      if (!av || av.getBoundingClientRect().width < 24) continue;
      actor = am;
      break;
    }
    if (!actor) return;
    m.author = decodeURIComponent(actor[2]);
    m.profile_url = 'https://www.linkedin.com/' + actor[1] + '/' + actor[2] + '/';
    for (var k = 0; k < links.length; k++) {
      var h = links[k].getAttribute('href') || '';
      if (h.indexOf('/' + actor[1] + '/' + actor[2]) === -1) continue;
      var t = (links[k].innerText || '').split('\n')[0].trim();
      if (t) { m.author_name = t.slice(0, 120); return; }
    }
  }
  var found = collect();
  var cards = outermost(found.els);
  var out = [], seen = {};
  for (var i = 0; i < cards.length; i++) {
    var el = cards[i];
    if (isAggregate(el)) continue;
    var um = urnMatch(el);
    if (!um || seen[um[2]]) continue;
    seen[um[2]] = 1;
    var m = {urn: 'urn:li:' + um[1] + ':' + um[2], id: um[2]};
    authorOf(el, m);
    m.text = (el.innerText || '').slice(0, 4000);
    var best = null, imgs = el.querySelectorAll('img');
    for (var j2 = 0; j2 < imgs.length; j2++) {
      var im = imgs[j2], src = im.currentSrc || im.src || '';
      // avatars and logos are told apart by CDN URL shape, not size alone
      if (/profile-displayphoto|profile-framedphoto|company-logo/.test(src)) continue;
      if ((im.naturalWidth || 0) > ((best && best.naturalWidth) || 0)) best = im;
    }
    if (best && (best.naturalWidth || 0) >= 200) m.img = best.currentSrc || best.src;
    var vid = el.querySelector('video');
    if (vid) { m.video = true; if (!m.img && vid.poster) m.img = vid.poster; }
    out.push(m);
  }
  var se = document.scrollingElement;
  return JSON.stringify({cards: out, diag: {mode: found.mode, raw: found.els.length,
    ready: document.readyState, scrollTop: Math.round(se.scrollTop),
    scrollHeight: se.scrollHeight}});
})()"""

# Dumped once when the first look finds no cards: enough DOM facts to fix the
# selectors without another debugging session.
_DOM_DIAG_JS = r"""(function(){
  function cnt(s){ try { return document.querySelectorAll(s).length; } catch(e){ return -1; } }
  var se = document.scrollingElement;
  var out = {
    ready: document.readyState,
    scrollHeight: se.scrollHeight, clientHeight: se.clientHeight,
    dataIdUrn: cnt('[data-id^="urn:li:"]'), dataUrn: cnt('[data-urn^="urn:li:"]'),
    dataIdAny: cnt('[data-id]'), feedShared: cnt('.feed-shared-update-v2'),
    finiteScroll: cnt('.scaffold-finite-scroll__content'),
    roleArticle: cnt('[role="article"]'), dialogs: cnt('[role="dialog"]'),
    main: !!document.querySelector('main'),
    textHead: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 200)
  };
  var fs = document.querySelector('.scaffold-finite-scroll__content') || document.querySelector('main');
  if (fs) {
    out.children = [];
    for (var i = 0; i < Math.min(fs.children.length, 4); i++) {
      var el = fs.children[i], attrs = [];
      for (var a = 0; a < el.attributes.length; a++)
        attrs.push(el.attributes[a].name + '=' + String(el.attributes[a].value).slice(0, 60));
      out.children.push(el.tagName + '[' + attrs.join(' ') + ']');
    }
  }
  return JSON.stringify(out);
})()"""


def _scroll_js(dy: int) -> str:
    """Scroll the feed by dy px: the window if the document scrolls, else the
    dominant inner scroller (LinkedIn has shipped both layouts). Reports what
    it did so --verbose shows movement (or the lack of it) every round."""
    return """(function(){
  var dy = %d;
  var se = document.scrollingElement;
  var before = se.scrollTop;
  window.scrollBy(0, dy);
  if (se.scrollTop > before)
    return JSON.stringify({how: 'window', top: Math.round(se.scrollTop),
                           max: se.scrollHeight - se.clientHeight});
  var best = null;
  var all = document.querySelectorAll('*');
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    if (el.clientHeight < window.innerHeight * 0.5) continue;
    if (el.scrollHeight <= el.clientHeight + 100) continue;
    var st = getComputedStyle(el).overflowY;
    if (st !== 'auto' && st !== 'scroll') continue;
    if (!best || el.scrollHeight > best.scrollHeight) best = el;
  }
  if (!best) return JSON.stringify({how: 'none', top: Math.round(before),
                                    max: se.scrollHeight - se.clientHeight});
  var b = best.scrollTop;
  best.scrollTop = b + dy;
  return JSON.stringify({how: 'inner', cls: String(best.className).slice(0, 60),
                         top: Math.round(best.scrollTop), moved: best.scrollTop > b});
})()""" % dy


# ── helpers ───────────────────────────────────────────────────────────────────

def _posted_at_from_urn_id(post_id: str) -> str | None:
    """LinkedIn urn ids are snowflakes: the first 41 bits are the creation
    time in epoch ms. Sanity-bounded — an id-shape change yields None
    (posted_at stays unknown) rather than a bogus date."""
    try:
        ms = int(bin(int(post_id))[2:][:41], 2)
        ts = dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    now = dt.datetime.now(dt.timezone.utc)
    if not (dt.datetime(2005, 1, 1, tzinfo=dt.timezone.utc) <= ts <= now + dt.timedelta(days=1)):
        return None
    return ts.isoformat()


def _is_promoted(text: str) -> bool:
    # The label sits on its own line in the card header; line-anchored so a
    # post merely *mentioning* "Promoted" in its body isn't shielded.
    return any(line.strip() == "Promoted" for line in text[:500].splitlines())


def _is_suggested(text: str) -> bool:
    return any(line.strip() in ("Suggested", "Recommended for you")
               for line in text[:500].splitlines())


# ── feed (walk only — processing happens in sc.process_pending) ───────────────

def _preload_known_post_ids() -> set[str]:
    rows = bb.select("items", {
        "platform": f"eq.{cfg.PLATFORM_LINKEDIN}", "kind": "eq.post",
        "select": "external_id", "order": "captured_at.desc", "limit": 1000,
    })
    return {r["external_id"] for r in rows}


def scrape_feed(ctx: dict) -> None:
    log("— home feed (walk) —")
    if not chrome.wait_for("document.readyState === 'complete' && !!document.querySelector('main')", 20):
        log("⚠ feed page still not ready after 20s — walking anyway")

    known = _preload_known_post_ids()
    handled: set[str] = set()  # ids touched this run (feed DOM is virtualized)
    walked = dup_streak = rounds = scroll_stuck = 0

    while (walked < cfg.MAX_NEW_POSTS
           and rounds < cfg.MAX_SCROLL_ROUNDS
           and dup_streak < cfg.DUP_STREAK_STOP):
        rounds += 1
        chrome.assert_on("linkedin.com")
        res = chrome.js_json(_FEED_JS) or {}
        cards = res.get("cards") or []
        vlog(f"  round {rounds}: {len(cards)} card(s) in DOM — {res.get('diag')}")
        if rounds == 1 and not cards:
            # Selectors missed everything on the first look — dump the DOM
            # facts needed to fix them, in this run's log, right now.
            log("  ⚠ no feed cards on first look — DOM diagnostics:")
            try:
                log("    " + chrome.js(_DOM_DIAG_JS)[:900])
            except Exception as exc:
                log(f"    (diagnostics failed: {str(exc)[:120]})")

        for card in cards:
            if walked >= cfg.MAX_NEW_POSTS:
                break
            post_id = card.get("id")
            if not post_id or post_id in handled:
                continue
            handled.add(post_id)
            text = card.get("text") or ""

            # The shield: promotional content is counted, never processed.
            if _is_promoted(text):
                ctx["stats"]["ads_shielded"] += 1
                sc.append_jsonl(ctx, {"type": "shielded", "reason": "promoted", "external_id": post_id})
                log(f"  🛡 shielded promoted content ({ctx['stats']['ads_shielded']} this run)")
                continue
            if _is_suggested(text):
                ctx["stats"]["suggested_shielded"] += 1
                sc.append_jsonl(ctx, {"type": "shielded", "reason": "suggested", "external_id": post_id})
                continue

            if post_id in ctx["cache_captured"]:  # captured earlier today
                continue
            if post_id in known:
                dup_streak += 1
                ctx["stats"]["dupes"] += 1
                continue
            dup_streak = 0

            username = card.get("author")
            if not username:
                # Poster identity is load-bearing (friend vs stranger vs business
                # judgement later) — never record '@unknown'; retry next run.
                ctx["stats"]["author_unresolved"] += 1
                log(f"  ~ skipped {post_id}: could not resolve author (will retry next run)")
                vlog(f"    card text head: {text[:150]!r}")
                continue
            if username in ctx["snoozed"]:
                ctx["stats"]["snoozed_skipped"] += 1
                sc.log_snooze_skip(ctx, username, "post")
                continue

            known.add(post_id)
            image_path = sc.download_media(card.get("img"), f"li_post_{post_id}")
            sc.capture(ctx, dict(
                kind="post", username=username, external_id=post_id,
                url=f"https://www.linkedin.com/feed/update/{card['urn']}/",
                image_path=image_path, raw_text=text,
                posted_at=_posted_at_from_urn_id(post_id),
                media_hint="video" if card.get("video") else None,
                display_name=card.get("author_name"),
                profile_url=card.get("profile_url"),
            ))
            walked += 1

        scroll = chrome.js_json(_scroll_js(random.randint(900, 1600))) or {}
        vlog(f"    scroll: {scroll}")
        scroll_stuck = scroll_stuck + 1 if scroll.get("how") == "none" else 0
        if scroll_stuck >= 4:
            # Nothing on this page can scroll: the feed never grew (render
            # problem — see the round-1 diagnostics) or we hit its true end.
            # Either way, more rounds are the same page again.
            log("stopping: page cannot scroll any further")
            break
        time.sleep(random.uniform(1.5, 3.2))  # human-ish pacing — be polite, avoid account flags

    if dup_streak >= cfg.DUP_STREAK_STOP:
        log(f"stopping: {dup_streak} consecutive already-known posts (reached previously scraped territory)")
    log(f"feed walk done: {walked} captured")
    if not walked and rounds:
        log("  (0 captured — rerun with --verbose and check the per-round card counts "
            "and the round-1 DOM diagnostics above)")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-graph", action="store_true",
                    help="skip Neo4j sync (backfill later with graph_sync.py)")
    ap.add_argument("--overwrite-today", action="store_true",
                    help="re-extract + re-upsert everything captured today "
                         "(use after schema/prompt changes; DB rows are updated in place)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    ctx = sc.begin_run(
        cfg.PLATFORM_LINKEDIN,
        no_graph=args.no_graph, overwrite_today=args.overwrite_today,
        verbose=args.verbose,
        stats_keys=("new_posts", "dupes", "ads_shielded", "suggested_shielded",
                    "snoozed_skipped", "author_unresolved", "errors"),
    )

    chrome.new_tab("https://www.linkedin.com/feed/")
    time.sleep(random.uniform(3.0, 5.0))
    ctx["tab_open"] = True

    try:
        url = chrome.tab_url()
        if any(s in url for s in ("/login", "/authwall", "/uas/", "/checkpoint")):
            raise SystemExit("LinkedIn is not logged in in your Chrome — log in, then rerun.")
        sc.pause_extension(ctx)
        chrome.js(_DISMISS_JS)

        scrape_feed(ctx)
        sc.restore_extension(ctx)
        sc.close_our_tab(ctx)  # walk done — browser not needed for processing

        sc.process_pending(ctx)

        sc.finish_run(ctx, "completed")
        sc.report_favorites(ctx)  # last step: after everything else is done
        sc.print_summary(ctx)
    except BaseException as exc:
        try:
            sc.finish_run(ctx, "failed", error=str(exc)[:500])
        except Exception:
            pass
        raise
    finally:
        sc.restore_extension(ctx)
        sc.close_our_tab(ctx)
        graph.close_graph()


if __name__ == "__main__":
    main()
