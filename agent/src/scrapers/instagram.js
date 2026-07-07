#!/usr/bin/env node
// Instagram scraper (PoC) — the agent that scrolls so you don't have to.
//
// How it connects: attaches over CDP to a real Chrome you launched with
// agent/scripts/launch-chrome.sh (dedicated persistent profile — log into
// Instagram ONCE in that window and the session sticks for every future run).
// It opens its own tab, leaves the rest of your browser alone, and disconnects
// when done.
//
// What it does per run:
//   1. Stories: opens the tray, steps through stories, screenshots each,
//      records link + poster + timestamp.
//   2. Feed: scrolls the home feed, skipping Sponsored/Suggested content
//      (that's the product: you never see the ads — stats count what was
//      shielded), records each organic post.
//   3. Every new item -> claude CLI extraction (structured/brief/detail) ->
//      contact upsert + item insert in Butterbase -> node/edge MERGE in Neo4j.
//   4. Dedupe: (platform, external_id) unique in DB; already-seen items are
//      skipped cheaply (no LLM call). A streak of consecutive dupes means we
//      reached previously-scraped territory and the run ends.
//
// Failure policy (owner's rule): missing config or unreachable dependencies
// fail loudly up front. A single item failing extraction is logged + skipped —
// it is NOT marked seen, so the next run retries it.
//
// Fragility notes: selectors lean on stable-ish signals (aria-labels, URL
// shapes, time[datetime]) rather than class names, same philosophy as the
// extension. TODO(i18n): "Story by"/"Sponsored" literals assume English UI.

import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright-core';
import { AGENT_ROOT, env } from '../env.js';
import { SCRAPE, PLATFORMS } from '../config.js';
import {
  upsertContact,
  itemExists,
  insertItem,
  markGraphSynced,
  startRun,
  finishRun,
  lastCompletedRun,
} from '../lib/butterbase.js';
import { verifyGraph, ensureConstraints, syncItemToGraph, closeGraph } from '../lib/graph.js';
import { extractItem } from '../lib/extract.js';

const PLATFORM = PLATFORMS.INSTAGRAM;
const NO_GRAPH = process.argv.includes('--no-graph'); // explicit escape hatch: scrape + DB only

const DATA_DIR = path.join(AGENT_ROOT, 'data');
const MEDIA_DIR = path.join(DATA_DIR, 'media');
const RUNS_DIR = path.join(DATA_DIR, 'runs');

const log = (...args) => console.log(new Date().toISOString().slice(11, 19), ...args);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rand = (min, max) => min + Math.floor(Math.random() * (max - min));

main().catch((err) => {
  console.error('\nFATAL:', err.message);
  process.exit(1);
});

async function main() {
  env('BUTTERBASE_API_KEY'); // fail fast before touching the browser

  // Preflight the graph BEFORE scraping so a bad Neo4j config doesn't waste a crawl.
  if (!NO_GRAPH) {
    const addr = await verifyGraph();
    await ensureConstraints();
    log(`neo4j connected (${addr})`);
  } else {
    log('running with --no-graph: skipping Neo4j sync (items stay graph_synced=false, run `npm run graph:sync` later)');
  }

  const prev = await lastCompletedRun(PLATFORM);
  log(prev ? `last completed run: ${prev.started_at}` : 'first run for this platform');

  fs.mkdirSync(MEDIA_DIR, { recursive: true });
  fs.mkdirSync(RUNS_DIR, { recursive: true });

  const browser = await chromium.connectOverCDP(SCRAPE.cdpUrl).catch((err) => {
    throw new Error(
      `Cannot reach Chrome CDP at ${SCRAPE.cdpUrl}. Start it first:\n  agent/scripts/launch-chrome.sh\n(${err.message})`
    );
  });

  const run = await startRun(PLATFORM);
  const ctx = {
    runId: run.id,
    jsonlPath: path.join(RUNS_DIR, `${new Date().toISOString().replace(/[:.]/g, '-')}.jsonl`),
    stats: { newPosts: 0, newStories: 0, dupes: 0, adsShielded: 0, suggestedShielded: 0, errors: 0 },
  };
  log(`run ${run.id} started — log: ${ctx.jsonlPath}`);

  let page;
  try {
    const context = browser.contexts()[0];
    if (!context) throw new Error('No browser context found over CDP — is Chrome fully started?');
    page = await context.newPage();

    await page.goto('https://www.instagram.com/', { waitUntil: 'domcontentloaded' });
    await sleep(rand(3000, 5000));
    if (page.url().includes('/accounts/login')) {
      throw new Error('Instagram is not logged in. Log in once in the launched Chrome window, then rerun.');
    }
    await dismissDialogs(page);

    await scrapeStories(page, ctx);
    await scrapeFeed(page, ctx);

    await finishRun(run.id, { status: 'completed', stats: ctx.stats });
    printSummary(ctx);
  } catch (err) {
    ctx.stats.fatal = err.message;
    await finishRun(run.id, { status: 'failed', stats: ctx.stats, error: err.message }).catch(() => {});
    throw err;
  } finally {
    await page?.close().catch(() => {});
    await browser.close().catch(() => {}); // disconnects from CDP; your Chrome stays open
    await closeGraph().catch(() => {});
  }
}

// --- stories ---------------------------------------------------------------

async function scrapeStories(page, ctx) {
  log('— stories —');
  // Own story is labelled differently; "Story by <name>" matches only others'.
  const tray = page.locator('button[aria-label^="Story by"]');
  const trayCount = await tray.count();
  if (trayCount === 0) {
    log('no stories tray found (none available, or DOM/locale change) — skipping stories');
    return;
  }
  log(`stories tray: ${trayCount} visible ring(s)`);
  await tray.first().click();
  try {
    await page.waitForURL(/\/stories\//, { timeout: 15_000 });
  } catch {
    log('story viewer did not open — skipping stories');
    return;
  }

  let hops = 0;
  const maxHops = SCRAPE.maxStories * 8; // stories advance faster than they process
  while (ctx.stats.newStories < SCRAPE.maxStories && hops < maxHops) {
    hops++;
    await sleep(rand(1200, 2200));
    if (!page.url().includes('/stories/')) break; // viewer closed = tray finished

    const m = page.url().match(/\/stories\/([^/]+)\/(\d+)/);
    if (!m) {
      // Interstitial between users — advance.
      await page.keyboard.press('ArrowRight');
      continue;
    }
    const [, username, storyId] = m;

    if (await itemExists(PLATFORM, storyId)) {
      ctx.stats.dupes++;
      await page.keyboard.press('ArrowRight');
      continue;
    }

    try {
      await page.keyboard.press(' '); // pause so screenshot matches what we record (best-effort)
      const shot = path.join(MEDIA_DIR, `story_${storyId}.png`);
      await page.screenshot({ path: shot });
      const postedAt = await page
        .locator('time[datetime]')
        .first()
        .getAttribute('datetime', { timeout: 2000 })
        .catch(() => null);
      await processItem(ctx, {
        kind: 'story',
        username,
        externalId: storyId,
        url: `https://www.instagram.com/stories/${username}/${storyId}/`,
        screenshotPath: shot,
        rawText: null,
        postedAt,
      });
    } catch (err) {
      ctx.stats.errors++;
      log(`  ✗ story ${storyId} failed (will retry next run): ${err.message.slice(0, 160)}`);
    }
    await page.keyboard.press(' '); // resume
    await page.keyboard.press('ArrowRight');
  }
  log(`stories done: ${ctx.stats.newStories} new, ${ctx.stats.dupes} already known`);
}

// --- feed --------------------------------------------------------------------

async function scrapeFeed(page, ctx) {
  log('— home feed —');
  await page.goto('https://www.instagram.com/', { waitUntil: 'domcontentloaded' });
  await sleep(rand(2500, 4000));
  await dismissDialogs(page);

  const handled = new Set(); // shortcodes touched this run (Instagram virtualizes the feed DOM)
  let dupStreak = 0;
  let rounds = 0;

  while (
    ctx.stats.newPosts < SCRAPE.maxNewPosts &&
    rounds < SCRAPE.maxScrollRounds &&
    dupStreak < SCRAPE.dupStreakStop
  ) {
    rounds++;
    const articles = page.locator('article');
    const n = await articles.count();

    for (let i = 0; i < n && ctx.stats.newPosts < SCRAPE.maxNewPosts; i++) {
      const article = articles.nth(i);
      try {
        const href = await article
          .locator('a[href*="/p/"], a[href*="/reel/"]')
          .first()
          .getAttribute('href', { timeout: 1500 })
          .catch(() => null);
        const shortcode = href?.match(/\/(?:p|reel)\/([^/?]+)/)?.[1];
        if (!shortcode || handled.has(shortcode)) continue;
        handled.add(shortcode);

        const rawText = ((await article.innerText().catch(() => '')) || '').slice(0, 4000);

        // The shield: never process promotional content, just count it.
        if (/\bSponsored\b/.test(rawText)) {
          ctx.stats.adsShielded++;
          appendJsonl(ctx, { type: 'shielded', reason: 'sponsored', shortcode });
          log(`  🛡 shielded sponsored content (${ctx.stats.adsShielded} this run)`);
          continue;
        }
        if (/\bSuggested for you\b/.test(rawText)) {
          ctx.stats.suggestedShielded++;
          appendJsonl(ctx, { type: 'shielded', reason: 'suggested', shortcode });
          continue;
        }

        if (await itemExists(PLATFORM, shortcode)) {
          dupStreak++;
          ctx.stats.dupes++;
          continue;
        }
        dupStreak = 0;

        const profileHref = await article
          .locator('header a[href^="/"]')
          .first()
          .getAttribute('href', { timeout: 1500 })
          .catch(() => null);
        const username = profileHref?.match(/^\/([^/?]+)/)?.[1] ?? 'unknown';
        const postedAt = await article
          .locator('time[datetime]')
          .first()
          .getAttribute('datetime', { timeout: 1500 })
          .catch(() => null);

        await article.scrollIntoViewIfNeeded();
        await sleep(rand(400, 900));
        const shot = path.join(MEDIA_DIR, `post_${shortcode}.png`);
        await article.screenshot({ path: shot }).catch(() => page.screenshot({ path: shot }));

        await processItem(ctx, {
          kind: href.includes('/reel/') ? 'reel' : 'post',
          username,
          externalId: shortcode,
          url: `https://www.instagram.com${href}`,
          screenshotPath: shot,
          rawText,
          postedAt,
        });
      } catch (err) {
        // Virtualized feed: elements go stale as it scrolls. Log and move on.
        ctx.stats.errors++;
        log(`  ✗ article ${i} failed: ${err.message.slice(0, 160)}`);
      }
    }

    await page.mouse.wheel(0, rand(900, 1600));
    await sleep(rand(1500, 3200)); // human-ish pacing — be polite, avoid account flags
  }

  if (dupStreak >= SCRAPE.dupStreakStop) {
    log(`stopping: ${dupStreak} consecutive already-known posts (reached previously scraped territory)`);
  }
}

// --- shared ------------------------------------------------------------------

async function processItem(ctx, { kind, username, externalId, url, screenshotPath, rawText, postedAt }) {
  log(`  ⋯ ${kind} ${externalId} by @${username}`);
  const extraction = await extractItem({ kind, username, screenshotPath, rawText });

  const contact = await upsertContact({
    platform: PLATFORM,
    handle: username,
    profileUrl: `https://www.instagram.com/${username}/`,
  });

  const item = await insertItem({
    platform: PLATFORM,
    kind,
    external_id: externalId,
    url,
    contact_id: contact.id,
    media_type: extraction.media_type ?? 'unknown',
    topic: extraction.topic ?? null,
    structured: extraction,
    brief: extraction.brief ?? null,
    detail: extraction.detail ?? null,
    caption_raw: rawText,
    media_path: screenshotPath,
    posted_at: postedAt,
    captured_at: new Date().toISOString(),
  });

  if (!NO_GRAPH) {
    await syncItemToGraph({ contact, item, mentions: extraction.mentions });
    await markGraphSynced(item.id);
  }

  ctx.stats[kind === 'story' ? 'newStories' : 'newPosts']++;
  appendJsonl(ctx, {
    type: 'item',
    kind,
    username,
    externalId,
    url,
    topic: extraction.topic,
    brief: extraction.brief,
    noteworthy: extraction.noteworthy,
    postedAt,
  });
  log(`  ✓ [${extraction.topic ?? '—'}] ${extraction.brief ?? ''}`);
}

async function dismissDialogs(page) {
  // "Turn on notifications", "Save login info", etc. Best-effort.
  for (const label of ['Not Now', 'Not now', 'Cancel']) {
    await page
      .locator(`button:has-text("${label}"), div[role="button"]:has-text("${label}")`)
      .first()
      .click({ timeout: 1200 })
      .catch(() => {});
  }
}

function appendJsonl(ctx, obj) {
  fs.appendFileSync(ctx.jsonlPath, JSON.stringify({ ts: new Date().toISOString(), ...obj }) + '\n');
}

function printSummary(ctx) {
  const s = ctx.stats;
  console.log(`
──────────────────────────────────────────
 run complete
   new posts      ${s.newPosts}
   new stories    ${s.newStories}
   already known  ${s.dupes}
   🛡 ads shielded         ${s.adsShielded}
   🛡 suggested shielded   ${s.suggestedShielded}
   errors (retry next run) ${s.errors}
 inspect: npm run inspect   |   raw log: ${ctx.jsonlPath}
──────────────────────────────────────────`);
}
