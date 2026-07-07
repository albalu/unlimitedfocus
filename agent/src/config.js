// Project-level (non-secret) configuration. The app id is public-shape config,
// not a credential — all access to it requires BUTTERBASE_API_KEY.
import './env.js';

export const BUTTERBASE_APP_ID = process.env.BUTTERBASE_APP_ID || 'app_desa1zwpsx43';
export const BUTTERBASE_API_BASE = `https://api.butterbase.ai/v1/${BUTTERBASE_APP_ID}`;

export const PLATFORMS = {
  INSTAGRAM: 'instagram',
  // TODO(phase-later): 'x', 'linkedin', 'facebook', 'threads', 'tiktok' — every
  // scraper implements the same contract (see scrapers/instagram.js) so new
  // platforms only add a scraper module, not new plumbing.
};

function int(name, dflt) {
  const v = process.env[name];
  return v ? parseInt(v, 10) : dflt;
}

// Scraper knobs. Conservative defaults: human-like pacing, modest per-run
// caps — an overnight loop is many small polite runs, not one giant crawl.
export const SCRAPE = {
  cdpUrl: process.env.UF_CDP_URL || 'http://localhost:9222',
  maxNewPosts: int('UF_MAX_POSTS', 15),
  maxStories: int('UF_MAX_STORIES', 15),
  // Stop scrolling after this many consecutive already-in-DB posts: we've
  // reached territory covered by a previous run.
  dupStreakStop: int('UF_DUP_STREAK', 8),
  maxScrollRounds: int('UF_MAX_SCROLLS', 40),
};
