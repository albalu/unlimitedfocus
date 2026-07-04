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
globalThis.chrome = {
  storage: {
    sync: { get: async () => stored },
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
  check(R.itemKey("/reel/ABC/") === "ABC", "itemKey /reel/ABC/");
  check(R.itemKey("/reels/ABC") === "ABC", "itemKey /reels/ABC");
  check(R.itemKey("/p/XYZ/liked_by/") === "XYZ", "itemKey ignores sub-pages");
  check(Number.isInteger(site.itemAllowance), "site has an itemAllowance");

  // Settings normalization
  let s = await globalThis.UFSettings.load();
  check(s.enabled === true, "default enabled");
  check(s.mode === "block", "default mode is block");
  check(s.maxScreens === 3, "default maxScreens");
  check(s.message === "With focus, anything is possible.", "default message");
  check(s.sites.instagram === true, "new site defaults to enabled");

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

  console.log(fails === 0 ? "ALL PASS" : `${fails} FAILURES`);
  process.exitCode = fails === 0 ? 0 : 1;
})();
