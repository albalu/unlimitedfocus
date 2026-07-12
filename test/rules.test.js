"use strict";

/**
 * Tests for the shared modules (site rules + settings normalization).
 * Run with: node test/rules.test.js
 */
const fs = require("fs");
const path = require("path");

const read = (rel) => fs.readFileSync(path.join(__dirname, "..", rel), "utf8");

// eslint-disable-next-line no-eval
eval(read("src/shared/sites.js"));
let stored = {};
let localStored = {};
globalThis.chrome = {
  storage: {
    sync: {
      get: async () => stored,
      set: async (obj) => Object.assign(stored, obj),
    },
    local: {
      get: async () => ({ ...localStored }),
      set: async (obj) => Object.assign(localStored, obj),
      remove: async (key) => {
        delete localStored[key];
      },
    },
    onChanged: { addListener() {} },
  },
};
// eslint-disable-next-line no-eval
eval(read("src/shared/settings.js"));

let fails = 0;
const check = (cond, name) => {
  if (!cond) {
    console.log("FAIL:", name);
    fails++;
  }
};

(async () => {
  const R = globalThis.UFSiteRules;
  const site = R.forHost("www.instagram.com");

  check(!!R.forHost("instagram.com"), "bare host matches");
  check(!R.forHost("evil-instagram.com"), "lookalike host does not match");

  // Limited paths: the feeds
  for (const [p, want] of [
    ["/", true],
    ["/explore/", true],
    ["/explore", true],
    ["/reels/", true],
    ["/direct/inbox/", false],
    ["/direct/t/123/", false],
    ["/some_username/", false],
    ["/stories/foo/1/", false],
    ["/reelsfoo", false],
    ["/explorer", false],
  ]) {
    check(R.isLimitedPath(site, p) === want, `limited ${p} -> ${want}`);
  }

  // Contained paths: specific items. NOTE: /reels/<id> matches both lists;
  // index.js checks contained first, so contained wins.
  for (const [p, want] of [
    ["/reel/ABC123/", true],
    ["/reels/ABC123/", true],
    ["/p/XYZ/", true],
    ["/p/XYZ/liked_by/", true],
    ["/reels/", false],
    ["/p/", false],
    ["/", false],
    ["/some_username/", false],
  ]) {
    check(R.isContainedPath(site, p) === want, `contained ${p} -> ${want}`);
  }

  // Item identity: /reel/X, /reels/X, and sub-pages are the same item
  check(R.itemKey(site, "/reel/ABC/") === "ABC", "itemKey /reel/ABC/");
  check(R.itemKey(site, "/reels/ABC") === "ABC", "itemKey /reels/ABC");
  check(R.itemKey(site, "/p/XYZ/liked_by/") === "XYZ", "itemKey ignores sub-pages");
  check(Number.isInteger(site.itemAllowance), "site has an itemAllowance");
  check(site.storiesTray === true, "instagram keeps its stories tray");

  // LinkedIn
  const li = R.forHost("www.linkedin.com");
  check(!!li && !!R.forHost("linkedin.com"), "linkedin hosts match");
  check(!R.forHost("evil-linkedin.com"), "lookalike linkedin host does not match");
  check(!li.storiesTray, "linkedin has no stories tray");

  for (const [p, want] of [
    ["/feed/", true],
    ["/feed", true],
    ["/", false],                       // logged-out landing / redirect page
    ["/feed/update/urn:li:activity:71/", false],
    ["/in/some-person/", false],
    ["/mynetwork/", false],
    ["/messaging/", false],
    ["/jobs/", false],
    ["/feedback/", false],
  ]) {
    check(R.isLimitedPath(li, p) === want, `linkedin limited ${p} -> ${want}`);
  }

  for (const [p, want] of [
    ["/feed/update/urn:li:activity:7123456789/", true],
    ["/posts/jane-doe_ai-activity-7123456789-Ab3d/", true],
    ["/feed/", false],
    ["/posts/", false],
    ["/pulse/some-article-title/", false],
    ["/in/some-person/", false],
  ]) {
    check(R.isContainedPath(li, p) === want, `linkedin contained ${p} -> ${want}`);
  }

  // Both permalink shapes of one post resolve to the same numeric id
  check(
    R.itemKey(li, "/feed/update/urn:li:activity:7123456789/") === "7123456789",
    "linkedin itemKey from urn permalink"
  );
  check(
    R.itemKey(li, "/posts/jane-doe_ai-activity-7123456789-Ab3d/") === "7123456789",
    "linkedin itemKey from share slug"
  );
  check(
    R.itemKey(li, "/feed/update/urn:li:ugcPost:555/") === "555",
    "linkedin itemKey from ugcPost urn"
  );

  // Settings normalization
  let s = await globalThis.UFSettings.load();
  check(s.enabled === true, "default enabled");
  check(s.mode === "block", "default mode is block");
  check(s.maxScreens === 3, "default maxScreens");
  check(s.message === "With focus, anything is possible.", "default message");
  check(s.sites.instagram === true, "new site defaults to enabled");
  check(s.sites.linkedin === true, "linkedin defaults to enabled");

  stored = { mode: "limit", message: "  Go build something.  ", maxScreens: 5 };
  s = await globalThis.UFSettings.load();
  check(s.mode === "limit", "stored mode preserved");
  check(s.message === "Go build something.", "message trimmed");
  check(s.maxScreens === 5, "stored maxScreens preserved");

  stored = { mode: "bogus", message: "   ", maxScreens: 999 };
  s = await globalThis.UFSettings.load();
  check(s.mode === "block", "bogus mode falls back");
  check(s.message === "With focus, anything is possible.", "blank message falls back");
  check(s.maxScreens === 3, "out-of-range maxScreens falls back");

  stored = { message: "x".repeat(500) };
  s = await globalThis.UFSettings.load();
  check(s.message.length === 140, "long message clamped");

  // Agent pause: time-boxed, storage.local only, never written to sync
  const S = globalThis.UFSettings;
  s = await S.load();
  check(s.agentPausedUntil === 0, "no pause by default");
  check(S.agentPauseRemaining(s) === 0, "no pause remaining by default");

  await S.setAgentPause(30);
  s = await S.load();
  const remaining = S.agentPauseRemaining(s);
  check(remaining > 29 * 60_000 && remaining <= 30 * 60_000, "pause set for ~30 min");

  await S.setAgentPause(9999);
  s = await S.load();
  check(
    S.agentPauseRemaining(s) <= S.MAX_AGENT_PAUSE_MINUTES * 60_000,
    "pause TTL clamped to max"
  );
  await S.setAgentPause("bogus");
  s = await S.load();
  const clampedLow = S.agentPauseRemaining(s);
  check(clampedLow > 0 && clampedLow <= 60_000, "bogus TTL clamps to minimum");

  localStored = { agentPausedUntil: Date.now() - 1000 };
  s = await S.load();
  check(S.agentPauseRemaining(s) === 0, "expired pause counts as not paused");

  await S.setAgentPause(30);
  await S.clearAgentPause();
  s = await S.load();
  check(S.agentPauseRemaining(s) === 0, "clearAgentPause resumes");

  stored = {};
  await S.setAgentPause(30);
  s = await S.load();
  await S.save(s);
  check(!("agentPausedUntil" in stored), "save() never leaks the pause into sync");

  console.log(fails === 0 ? "ALL PASS" : `${fails} FAILURES`);
  process.exitCode = fails === 0 ? 0 : 1;
})();
