"use strict";

/**
 * UFFeedBlocker — replaces the feed with a focus message, keeping the
 * stories tray.
 *
 * Everything works inside the page's <main> region (the semantic content
 * area — the nav sidebar, drawers, and dialogs live outside it and are never
 * touched). Two hiding strategies, both driven by attributes that blocker.css
 * keys off, so deactivating is just removing attributes:
 *
 *  1. If the stories tray can be found, hide only the sibling branches along
 *     the tray → <main> path (feed posts, suggestions sidebar, ...). The tray
 *     stays visible and clickable.
 *  2. Otherwise (Explore, Reels, tray not rendered yet) hide <main> entirely.
 *
 * The tray is located structurally: links to /stories/… (or, failing that,
 * the <canvas> story rings), reduced to their lowest common ancestor. If that
 * ancestor contains feed posts (<article>), the match is wrong and we fall
 * back to hiding everything — the failure mode is always "too much focus",
 * never a visible feed.
 *
 * A MutationObserver re-applies the marking as the SPA renders, so the tray
 * appears as soon as the site draws it. With the feed branches display:none,
 * nothing below the tray ever loads more content.
 *
 * The message overlay is appended to <html>, not <body>, so the site's
 * framework never sees (or removes) it, and it ignores pointer events so the
 * tray, drawers, and dialogs keep working.
 */
class UFFeedBlocker {
  constructor() {
    this.active = false;
    this.message = "";
    // When true, [role="dialog"] layers are hidden too. Used for the item
    // drift wall: post/reel viewers can be dialogs rendered outside <main>,
    // so hiding <main> alone would leave them browsable. Feed blocking keeps
    // this false so search/notification drawers stay usable.
    this.hideDialogs = false;
    this.overlay = null;
    this.textEl = null;
    this.captionEl = null;
    this.marked = new Set();
    this.observer = null;
    this.applyScheduled = false;
  }

  /** Idempotent; also refreshes the message and theme when already active. */
  activate() {
    if (!this.active) {
      this.active = true;
      this._apply();
      this.observer = new MutationObserver(() => this._scheduleApply());
      this.observer.observe(document.documentElement, { childList: true, subtree: true });
    }
    document.documentElement.toggleAttribute("data-uf-dialogs-hidden", this.hideDialogs);
    if (!this.overlay) this._build();
    if (!this.overlay.isConnected) document.documentElement.appendChild(this.overlay);
    this.textEl.textContent = this.message;
    this._applyTheme();
    this.overlay.style.display = "flex";
  }

  deactivate() {
    if (!this.active) return;
    this.active = false;
    this.observer.disconnect();
    this.observer = null;
    const root = document.documentElement;
    root.removeAttribute("data-uf-feed-hidden");
    root.removeAttribute("data-uf-main-hidden");
    root.removeAttribute("data-uf-dialogs-hidden");
    for (const el of this.marked) el.removeAttribute("data-uf-hide");
    this.marked.clear();
    if (this.overlay) this.overlay.style.display = "none";
  }

  _scheduleApply() {
    if (this.applyScheduled) return;
    this.applyScheduled = true;
    requestAnimationFrame(() => {
      this.applyScheduled = false;
      if (this.active) this._apply();
    });
  }

  _apply() {
    const root = document.documentElement;
    const main = document.querySelector("main");
    const tray = main ? this._findStoriesTray(main) : null;
    if (!tray) {
      root.setAttribute("data-uf-main-hidden", "");
      root.removeAttribute("data-uf-feed-hidden");
      return;
    }
    for (let el = tray; el !== main && el.parentElement; el = el.parentElement) {
      for (const sibling of el.parentElement.children) {
        if (sibling === el || sibling.hasAttribute("data-uf-hide")) continue;
        sibling.setAttribute("data-uf-hide", "");
        this.marked.add(sibling);
      }
    }
    root.setAttribute("data-uf-feed-hidden", "");
    root.removeAttribute("data-uf-main-hidden");
  }

  _findStoriesTray(main) {
    // Story links are the strong signal; canvas story rings are the fallback
    // (a single canvas alone is too weak). Markers inside feed posts don't
    // count — a post can link to a story too.
    let markers = [...main.querySelectorAll('a[href^="/stories/"]')].filter(
      (el) => !el.closest("article")
    );
    if (!markers.length) {
      markers = [...main.querySelectorAll("canvas")].filter((el) => !el.closest("article"));
      if (markers.length < 2) return null;
    }
    let lca = markers[0];
    for (const marker of markers) {
      while (lca && lca !== main && !lca.contains(marker)) lca = lca.parentElement;
    }
    if (!lca || lca === main || !main.contains(lca)) return null;
    // If the "tray" contains feed posts, the markers were spread across the
    // page and this is not the tray. Hide everything instead.
    if (lca.querySelector("article")) return null;
    return lca;
  }

  /** Picks text colors that read on the site's current light/dark theme. */
  _applyTheme() {
    let dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const bg = document.body && getComputedStyle(document.body).backgroundColor;
    const parts = bg ? bg.match(/[\d.]+/g) : null;
    if (parts && parts.length >= 3 && !(parts.length >= 4 && Number(parts[3]) === 0)) {
      const [r, g, b] = parts.map(Number);
      dark = 0.2126 * r + 0.7152 * g + 0.0722 * b < 128;
    }
    this.textEl.style.color = dark ? "rgba(245, 245, 245, 0.92)" : "rgba(38, 38, 38, 0.92)";
    this.captionEl.style.color = dark ? "rgba(245, 245, 245, 0.45)" : "rgba(38, 38, 38, 0.45)";
  }

  _build() {
    const overlay = document.createElement("div");
    Object.assign(overlay.style, {
      position: "fixed",
      inset: "0",
      // Low z-index on purpose: the site's own dialogs, drawers, and the
      // story viewer (which set high z-indexes) must paint above the message.
      zIndex: "1",
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      gap: "14px",
      padding: "0 48px",
      textAlign: "center",
      pointerEvents: "none",
    });

    const text = document.createElement("div");
    Object.assign(text.style, {
      font: "600 clamp(22px, 3.5vw, 32px)/1.35 -apple-system, system-ui, sans-serif",
      letterSpacing: "-0.01em",
      maxWidth: "24em",
    });

    const caption = document.createElement("div");
    caption.textContent = "Unlimited Focus";
    Object.assign(caption.style, {
      font: "500 11px/1 -apple-system, system-ui, sans-serif",
      textTransform: "uppercase",
      letterSpacing: "0.16em",
    });

    overlay.append(text, caption);
    this.overlay = overlay;
    this.textEl = text;
    this.captionEl = caption;
  }
}

globalThis.UFFeedBlocker = UFFeedBlocker;
