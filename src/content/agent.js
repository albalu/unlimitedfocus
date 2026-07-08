"use strict";

/**
 * Page bridge for the user's OWN agent (agent/py) — lets the script driving
 * this very browser pause Unlimited Focus for the duration of a scrape and
 * turn it back on afterwards, without ever touching the master switch.
 *
 * Why a bridge: AppleScript can only execute page-world JS, and the page
 * world has no chrome.storage. The two worlds do share the DOM and window
 * message events, so:
 *
 *   request  (page -> here)   window.postMessage({type:"UF_AGENT", id, cmd,
 *                             ttlMinutes?}) with cmd one of:
 *                               "status"  report state, no side effects
 *                               "pause"   time-boxed pause (storage.local)
 *                               "resume"  clear the pause
 *   reply    (here -> page)   JSON written to the data-uf-agent-ack
 *                             attribute on <html>; the page polls it and
 *                             matches on id.
 *
 * Deliberately weak "auth": any script on this host could post the same
 * message. Acceptable because the pause is (a) capped at
 * MAX_AGENT_PAUSE_MINUTES — an abusive page or a crashed agent can only
 * delay focus, never disable it — and (b) instantly visible: the feed
 * reappears and the popup says who paused it.
 */
(() => {
  const ACK_ATTR = "data-uf-agent-ack";

  async function handle(msg) {
    if (msg.cmd === "pause") {
      await globalThis.UFSettings.setAgentPause(msg.ttlMinutes);
    } else if (msg.cmd === "resume") {
      await globalThis.UFSettings.clearAgentPause();
    }
    const settings = await globalThis.UFSettings.load();
    const site = globalThis.UFSiteRules.forHost(location.hostname);
    const state = {
      enabled: settings.enabled,
      siteEnabled: !!site && settings.sites[site.id] !== false,
      mode: settings.mode,
      pausedMinutes: Math.ceil(
        globalThis.UFSettings.agentPauseRemaining(settings) / 60_000
      ),
    };
    document.documentElement.setAttribute(
      ACK_ATTR,
      JSON.stringify({ id: msg.id, state })
    );
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const msg = event.data;
    if (!msg || msg.type !== "UF_AGENT" || typeof msg.id !== "string") return;
    if (msg.cmd !== "status" && msg.cmd !== "pause" && msg.cmd !== "resume") return;
    handle(msg);
  });
})();
