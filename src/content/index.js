"use strict";

/**
 * Content-script entry point. Decides, for the current host and path, whether
 * the scroll limiter should be on, and keeps that decision fresh across SPA
 * navigations and settings changes.
 */
(async function main() {
  const site = globalThis.UFSiteRules.forHost(location.hostname);
  if (!site) return;

  let settings = await globalThis.UFSettings.load();
  const limiter = new globalThis.UFScrollLimiter();
  const blocker = new globalThis.UFFeedBlocker();
  blocker.keepStoriesTray = !!site.storiesTray;

  // While on contained paths (a specific post/reel), the items the user may
  // view: the one they arrived on plus up to itemAllowance more. Swiping
  // further is feed drift and hits the focus wall; leaving contained paths
  // resets the session.
  let allowedItems = null;

  // While the user's agent holds a pause, act as if disabled — and schedule
  // a re-check for the moment it expires, so a crashed agent can never leave
  // focus off (no storage event fires on expiry; only this timer catches it).
  let pauseTimer = null;

  function applyState() {
    const rules = globalThis.UFSiteRules;
    limiter.maxScreens = settings.maxScreens;
    blocker.message = settings.message;
    const pauseMs = globalThis.UFSettings.agentPauseRemaining(settings);
    clearTimeout(pauseTimer);
    if (pauseMs > 0) {
      pauseTimer = setTimeout(applyState, pauseMs + 250);
    }
    const siteEnabled =
      settings.enabled && pauseMs === 0 && settings.sites[site.id] !== false;
    const path = location.pathname;

    let engine = "none";
    let drifted = false;
    if (siteEnabled && rules.isContainedPath(site, path)) {
      const key = rules.itemKey(site, path);
      const allowance = site.itemAllowance ?? 2;
      if (!allowedItems) {
        allowedItems = new Set([key]);
      } else if (!allowedItems.has(key) && allowedItems.size < allowance + 1) {
        allowedItems.add(key);
      }
      drifted = !allowedItems.has(key);
      engine = drifted ? "block" : "limit";
    } else {
      allowedItems = null;
      if (siteEnabled && rules.isLimitedPath(site, path)) {
        engine = settings.mode === "block" ? "block" : "limit";
      }
    }

    if (engine === "block") {
      limiter.deactivate();
      // Item views open as dialogs over another page (e.g. post modals on a
      // profile); the drift wall must cover those too. Feed blocking must
      // not, so search/notification drawers stay usable.
      blocker.hideDialogs = drifted;
      blocker.activate();
    } else if (engine === "limit") {
      blocker.deactivate();
      limiter.activate();
    } else {
      blocker.deactivate();
      limiter.deactivate();
    }
  }

  // These sites are SPAs: full page loads are rare, the URL changes via the
  // history API. Watching DOM mutations (throttled to one check per frame)
  // plus popstate catches every navigation without patching history in the
  // page's world.
  let lastPath = location.pathname;
  let checkScheduled = false;
  function scheduleNavCheck() {
    if (checkScheduled) return;
    checkScheduled = true;
    requestAnimationFrame(() => {
      checkScheduled = false;
      if (location.pathname === lastPath) return;
      lastPath = location.pathname;
      limiter.resetForNavigation();
      applyState();
    });
  }
  new MutationObserver(scheduleNavCheck).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
  window.addEventListener("popstate", scheduleNavCheck);

  globalThis.UFSettings.onChange((next) => {
    settings = next;
    applyState();
  });

  applyState();
})();
