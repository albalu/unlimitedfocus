"use strict";

/**
 * Site registry — the single place that describes which sites Unlimited Focus
 * knows about and which parts of them get the scroll limit.
 *
 * To add a new site:
 *   1. Add its URL patterns to "matches" in manifest.json so the content
 *      scripts are injected there.
 *   2. Add an entry to `all` below. Everything else (settings, popup UI,
 *      limiter behavior) picks it up automatically.
 *
 * Rule shape:
 *   id             stable key, used in storage — never rename once shipped
 *   label          human-readable name shown in the popup
 *   hosts          exact hostnames this rule applies to
 *   limitedPaths   regexes tested against location.pathname; a match means
 *                  the feed treatment (block or scroll limit) applies there
 *   containedPaths regexes for single-item views (a specific post or reel).
 *                  These take precedence over limitedPaths. The item can be
 *                  viewed (with the scroll limiter as a backstop), but moving
 *                  on to more than itemAllowance further items — the swipe-
 *                  to-next-reel / next-post drift that becomes an infinite
 *                  feed — hits the focus wall
 *   itemAllowance  how many additional items beyond the one opened are
 *                  viewable before the wall (default 2)
 *   itemKey        optional override for how a contained path maps to an item
 *                  identity (see itemKey below for the default)
 *   storiesTray    true when the site has a stories tray the blocker should
 *                  keep visible (block mode hides everything else in <main>)
 *
 * Any path matching neither list (DMs, profiles, stories, settings, ...) is
 * left completely alone.
 *
 * This file is loaded both as a content script and by the popup page, so it
 * must stay dependency-free and only define a global.
 */
globalThis.UFSiteRules = (() => {
  const all = [
    {
      id: "instagram",
      label: "Instagram",
      hosts: ["www.instagram.com", "instagram.com"],
      limitedPaths: [
        /^\/$/,               // home feed
        /^\/explore(\/|$)/,   // explore grid
        /^\/reels(\/|$)/,     // reels feed
      ],
      containedPaths: [
        /^\/reels?\/[^/]+/,   // a specific reel (/reel/<id> or /reels/<id>)
        /^\/p\/[^/]+/,        // a specific post
      ],
      itemAllowance: 2,
      storiesTray: true,
    },
    {
      id: "linkedin",
      label: "LinkedIn",
      hosts: ["www.linkedin.com", "linkedin.com"],
      limitedPaths: [
        /^\/feed\/?$/,        // home feed
      ],
      containedPaths: [
        /^\/feed\/update\//,  // a specific post (/feed/update/urn:li:activity:<id>)
        /^\/posts\/[^/]+/,    // a specific post (share slug ending in -activity-<id>-…)
      ],
      itemAllowance: 2,
      // Both permalink shapes embed the same numeric urn id — that id is the
      // item, so following a share link and landing on the urn permalink
      // count as one view, not two.
      itemKey(pathname) {
        const m = pathname.match(/(?:activity|ugcpost|share)[:-](\d+)/i);
        return m ? m[1] : pathname;
      },
    },
  ];

  function forHost(hostname) {
    return all.find((site) => site.hosts.includes(hostname)) || null;
  }

  function isLimitedPath(site, pathname) {
    return site.limitedPaths.some((re) => re.test(pathname));
  }

  function isContainedPath(site, pathname) {
    return (site.containedPaths || []).some((re) => re.test(pathname));
  }

  /**
   * Identity of the item a contained path shows. Default: the second segment,
   * so /reel/ABC and /reels/ABC (and /p/ABC/liked_by) all count as the same
   * item. Sites whose permalinks don't fit that shape override via itemKey.
   */
  function itemKey(site, pathname) {
    if (site && site.itemKey) return site.itemKey(pathname);
    const segments = pathname.split("/").filter(Boolean);
    return segments.length >= 2 ? segments[1] : null;
  }

  return { all, forHost, isLimitedPath, isContainedPath, itemKey };
})();
