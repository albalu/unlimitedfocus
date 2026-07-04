"use strict";

/**
 * Settings stored in chrome.storage.sync:
 *   enabled     master switch
 *   mode        "block" hides the feed behind a focus message,
 *               "limit" caps scrolling at maxScreens instead
 *   message     the focus message shown in block mode
 *   maxScreens  how many viewport-heights of scrolling a limited page allows
 *   sites       { [siteId]: boolean } — per-site opt-out
 *
 * Loaded both as a content script and by the popup. Requires UFSiteRules
 * (sites.js) to be loaded first so new sites default to enabled.
 */
globalThis.UFSettings = (() => {
  const DEFAULTS = Object.freeze({
    enabled: true,
    mode: "block",
    message: "With focus, anything is possible.",
    maxScreens: 3,
  });

  const MIN_SCREENS = 1;
  const MAX_SCREENS = 50;
  const MAX_MESSAGE_LENGTH = 140;

  function withDefaults(raw) {
    const settings = {
      enabled: typeof raw.enabled === "boolean" ? raw.enabled : DEFAULTS.enabled,
      mode: raw.mode === "limit" || raw.mode === "block" ? raw.mode : DEFAULTS.mode,
      message:
        typeof raw.message === "string" && raw.message.trim()
          ? raw.message.trim().slice(0, MAX_MESSAGE_LENGTH)
          : DEFAULTS.message,
      maxScreens: raw.maxScreens,
      sites: { ...(raw.sites || {}) },
    };
    if (
      !Number.isInteger(settings.maxScreens) ||
      settings.maxScreens < MIN_SCREENS ||
      settings.maxScreens > MAX_SCREENS
    ) {
      settings.maxScreens = DEFAULTS.maxScreens;
    }
    // Sites not yet present in storage (e.g. added in an update) start enabled.
    for (const site of globalThis.UFSiteRules.all) {
      if (typeof settings.sites[site.id] !== "boolean") {
        settings.sites[site.id] = true;
      }
    }
    return settings;
  }

  async function load() {
    const raw = await chrome.storage.sync.get(null);
    return withDefaults(raw);
  }

  async function save(settings) {
    await chrome.storage.sync.set(settings);
  }

  /** Calls back with the fresh, normalized settings whenever they change. */
  function onChange(callback) {
    chrome.storage.onChanged.addListener((_changes, area) => {
      if (area !== "sync") return;
      load().then(callback);
    });
  }

  return { MIN_SCREENS, MAX_SCREENS, MAX_MESSAGE_LENGTH, load, save, onChange };
})();
