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
 * One additional value lives in chrome.storage.local (this device only,
 * never synced, never written by save()):
 *   agentPausedUntil  epoch ms; while in the future the extension acts as if
 *                     disabled. Set by the user's own scraping agent through
 *                     the page bridge (content/agent.js). Always time-boxed,
 *                     so a crashed agent can only delay focus, never lose it.
 *                     The master `enabled` switch is deliberately untouched:
 *                     pausing an already-off extension changes nothing, and
 *                     resuming can never turn on what the user turned off.
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
  const MIN_AGENT_PAUSE_MINUTES = 1;
  const MAX_AGENT_PAUSE_MINUTES = 120;

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
    const [raw, local] = await Promise.all([
      chrome.storage.sync.get(null),
      chrome.storage.local.get("agentPausedUntil"),
    ]);
    const settings = withDefaults(raw);
    settings.agentPausedUntil =
      typeof local.agentPausedUntil === "number" ? local.agentPausedUntil : 0;
    return settings;
  }

  async function save(settings) {
    const { agentPausedUntil, ...synced } = settings;
    await chrome.storage.sync.set(synced);
  }

  /** Ms until the agent pause expires; 0 when not paused. */
  function agentPauseRemaining(settings) {
    return Math.max(0, (settings.agentPausedUntil || 0) - Date.now());
  }

  async function setAgentPause(minutes) {
    const mins = Math.min(
      Math.max(Math.floor(Number(minutes) || 0), MIN_AGENT_PAUSE_MINUTES),
      MAX_AGENT_PAUSE_MINUTES
    );
    await chrome.storage.local.set({ agentPausedUntil: Date.now() + mins * 60_000 });
  }

  async function clearAgentPause() {
    await chrome.storage.local.remove("agentPausedUntil");
  }

  /** Calls back with the fresh, normalized settings whenever they change. */
  function onChange(callback) {
    chrome.storage.onChanged.addListener((_changes, area) => {
      if (area !== "sync" && area !== "local") return;
      load().then(callback);
    });
  }

  return {
    MIN_SCREENS,
    MAX_SCREENS,
    MAX_MESSAGE_LENGTH,
    MAX_AGENT_PAUSE_MINUTES,
    load,
    save,
    onChange,
    agentPauseRemaining,
    setAgentPause,
    clearAgentPause,
  };
})();
