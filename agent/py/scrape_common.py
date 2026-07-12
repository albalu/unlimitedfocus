"""Machinery shared by the per-platform scrapers (scrape_instagram.py,
scrape_linkedin.py, ...).

Each scraper owns its walk phase — the site's DOM, its in-page JS, its pacing.
Everything around the walk is identical and lives here, keyed off
ctx["platform"]: logging, media downloads, the daily capture cache, the
claude-CLI extraction -> Butterbase upsert -> Neo4j MERGE commit path, the
Unlimited Focus pause bracket, the favorites report, and run bookkeeping.

Shape of a run (see either scraper's main()):
    ctx = begin_run(platform, ...)      # preflight, cache load, run row
    chrome.new_tab(...); ctx["tab_open"] = True
    pause_extension(ctx)
    ... walk, calling capture(ctx, rec) per item ...
    restore_extension(ctx); close_our_tab(ctx)
    process_pending(ctx)                # browser idle from here on
    finish_run(ctx, "completed")
    report_favorites(ctx)
    print_summary(ctx)
"""
from __future__ import annotations

import datetime as dt
import html
import json
import random
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


def set_verbose(v: bool) -> None:
    global VERBOSE
    VERBOSE = v


def log(*args):
    print(dt.datetime.now().strftime("%H:%M:%S"), *args, flush=True)


def vlog(*args):
    if VERBOSE:
        log(*args)


# ── capture side ──────────────────────────────────────────────────────────────

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


def log_snooze_skip(ctx: dict, username: str, kind: str) -> None:
    """First skip per poster logs at normal level (visible confirmation the
    snooze is acknowledged); repeats only with --verbose to avoid spam when
    one muted account has many items."""
    if username not in ctx["snooze_logged"]:
        ctx["snooze_logged"].add(username)
        log(f"  🔕 skipping @{username} ({kind}) — snoozed")
    else:
        vlog(f"  🔕 skipped another {kind} by snoozed @{username}")
    append_jsonl(ctx, {"type": "snoozed_skip", "handle": username, "kind": kind})


def capture(ctx: dict, rec: dict) -> None:
    """Register a capture in the daily cache + this run's pending set."""
    rec = {"platform": ctx["platform"], **rec}
    cache.append_captured(rec)
    ctx["cache_captured"][rec["external_id"]] = rec
    vlog(f"  ⊙ captured {rec['kind']} {rec['external_id']} by @{rec['username']}")


def preload_snoozed_handles(platform: str) -> set[str]:
    """Posters the user snoozed in the UI (contacts.snoozed_until in the
    future). The scraper honors this at capture time: no screenshot, no
    extraction tokens, no DB/graph writes for muted posters."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = bb.select("contacts", {
        "platform": f"eq.{platform}",
        "snoozed_until": f"gt.{now}",
        "select": "handle",
        "limit": 1000,
    })
    return {r["handle"] for r in rows}


# ── processing (browser idle) ─────────────────────────────────────────────────

# Fallback when a capture record carries no profile_url of its own (all
# instagram records, and linkedin records from before company actors existed).
_PROFILE_URL_TEMPLATES = {
    cfg.PLATFORM_INSTAGRAM: "https://www.instagram.com/{}/",
    cfg.PLATFORM_LINKEDIN: "https://www.linkedin.com/in/{}/",
}


def process_item(ctx: dict, *, kind: str, username: str, external_id: str, url: str,
                 image_path: str | None, raw_text: str | None, posted_at: str | None,
                 media_hint: str | None = None, display_name: str | None = None,
                 profile_url: str | None = None, **_ignored) -> None:
    platform = ctx["platform"]
    log(f"  ⋯ {kind} {external_id} by @{username}")
    x = extract_item(kind, username, image_path, raw_text,
                     platform_label=cfg.PLATFORM_LABELS.get(platform, platform))
    if media_hint and x.get("media_type") in (None, "unknown", "image"):
        x["media_type"] = media_hint

    if not profile_url:
        tpl = _PROFILE_URL_TEMPLATES.get(platform)
        profile_url = tpl.format(username) if tpl else None
    contact = bb.upsert_contact(platform, username, display_name=display_name,
                                profile_url=profile_url)
    item = bb.upsert_item({
        "platform": platform,
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
            cache.mark_committed(ctx["platform"], rec["external_id"])
            ctx["committed"].add(rec["external_id"])
        except Exception as exc:
            ctx["stats"]["errors"] += 1
            log(f"  ✗ {rec.get('kind')} {rec.get('external_id')} failed (stays cached, retries next run): {str(exc)[:200]}")


# ── driven tab + Unlimited Focus pause bracket ────────────────────────────────

def close_our_tab(ctx: dict) -> None:
    # Guarded: close_tab targets the LAST tab of window 1, so a second call
    # would hit one of the user's own tabs.
    if ctx["tab_open"]:
        chrome.close_tab()
        ctx["tab_open"] = False


def pause_extension(ctx: dict) -> None:
    """If Unlimited Focus is actively guarding this site, pause it for the
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
            "loaded build predates src/content/agent.js (or this site's manifest "
            "entry), and its blocker would hide everything this run tries to capture.\n"
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


def restore_extension(ctx: dict) -> None:
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


# ── favorites report (runs last, after processing) ────────────────────────────

def report_favorites(ctx: dict) -> None:
    """End-of-run round-up: Telegram the new post/story links of every favorite
    poster on this platform (top story link only per poster). 'New' = captured
    since the previous completed run (24h fallback on the first run).
    Best-effort — any failure is logged and swallowed so it never breaks a
    scrape."""
    if not telegram.configured():
        log("telegram not configured (TELEGRAM_SEASONS_*) — skipping favorites report")
        return
    try:
        favs = bb.select("contacts", {
            "platform": f"eq.{ctx['platform']}", "favorited": "eq.true",
            "select": "id,handle,display_name",
            "order": "handle.asc", "limit": 500,
        })
        if not favs:
            log("no favorite posters — skipping favorites report")
            return

        sections, links, empty = [], 0, 0
        for c in favs:
            items = bb.select("items", {
                "contact_id": f"eq.{c['id']}", "deleted_at": "is.null",
                "captured_at": f"gte.{ctx['fav_cutoff']}", "select": "kind,url",
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
                             f'<a href="{html.escape(it["url"])}">{it.get("kind") or "post"}</a>')
                links += 1
            sections.append("\n".join(lines))

        if not sections:
            log(f"favorites report: no new posts from {len(favs)} favorite(s) since last run")
            return
        label = cfg.PLATFORM_LABELS.get(ctx["platform"], ctx["platform"])
        date = dt.datetime.now().strftime("%a %b %d")
        body = f"❤️ <b>Favorites update</b> ({label}) — {date}\n\n" + "\n\n".join(sections)
        if empty:
            body += f"\n\n<i>({empty} other favorite(s) had no new posts)</i>"
        telegram.send_message(body)
        log(f"📮 favorites report sent to Telegram: {len(sections)} poster(s), {links} link(s)")
    except Exception as exc:
        log(f"⚠ favorites report failed (non-fatal): {str(exc)[:200]}")


# ── run bookkeeping ───────────────────────────────────────────────────────────

def begin_run(platform: str, *, no_graph: bool, overwrite_today: bool,
              verbose: bool, stats_keys: tuple[str, ...]) -> dict:
    """Everything before the browser: preflight, cache load, run row, ctx."""
    set_verbose(verbose)
    env("BUTTERBASE_API_KEY")  # fail fast before touching the browser
    if not no_graph:
        graph.verify_graph()
        graph.ensure_constraints()
        log("neo4j connected")
    else:
        log("--no-graph: items stay graph_synced=false; run `uv run graph_sync.py` later")

    purged = cache.purge_old()
    if purged:
        log(f"purged {purged} cache day(s) older than {cache.PURGE_DAYS} days")

    prev = bb.last_completed_run(platform)
    log(f"last completed run: {prev['started_at']}" if prev else "first run for this platform")

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    run = bb.start_run(platform)
    ctx = {
        "platform": platform,
        "run_id": run["id"],
        "no_graph": no_graph,
        # Favorites report scopes to content captured since the previous run
        # (so re-runs the same day don't re-send the same links); 24h on the
        # first run.
        "fav_cutoff": (prev["started_at"] if prev and prev.get("started_at")
                       else (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()),
        "jsonl_path": RUNS_DIR / (dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + ".jsonl"),
        "cache_captured": cache.load_captured(platform),
        "committed": set() if overwrite_today else cache.load_committed(platform),
        "snoozed": preload_snoozed_handles(platform),
        "snooze_logged": set(),
        "stats": {k: 0 for k in stats_keys},
        "tab_open": False,
        "uf_paused": False,
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
            f"({already} committed{', overwrite requested' if overwrite_today else ''})")
    log(f"run {run['id']} started")
    return ctx


def finish_run(ctx: dict, status: str, error: str | None = None) -> None:
    bb.finish_run(ctx["run_id"], status, ctx["stats"], error=error)


_STAT_LABELS = {
    "new_posts": "new posts",
    "new_stories": "new stories",
    "dupes": "already known",
    "ads_shielded": "🛡 ads shielded",
    "suggested_shielded": "🛡 suggested shielded",
    "snoozed_skipped": "🔕 snoozed skipped",
    "author_unresolved": "author unresolved",
    "errors": "errors",
}


def print_summary(ctx: dict) -> None:
    uncommitted = len([1 for e in ctx["cache_captured"] if e not in ctx["committed"]])
    rows = "\n".join(f"   {_STAT_LABELS.get(k, k):<22} {v}"
                     for k, v in ctx["stats"].items())
    print(f"""
──────────────────────────────────────────
 run complete ({ctx['platform']})
{rows}
   {'cached, not yet in DB':<22} {uncommitted}  (rerun to retry)
 inspect: uv run inspect_data.py   |   raw log: {ctx['jsonl_path']}
──────────────────────────────────────────""")
