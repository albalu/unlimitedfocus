"use strict";

/**
 * UFFeedBlocker — replaces the feed with a focus message, keeping the
 * stories tray.
 *
 * Everything works inside the page's <main> region (the semantic content
 * area — the nav sidebar, drawers, and dialogs live outside it and are never
 * touched). Three hiding strategies, all driven by attributes that
 * blocker.css keys off, so deactivating is just removing attributes:
 *
 *  1. If the site has a stories tray (keepStoriesTray, set from the site
 *     rule — Instagram) and it can be found, hide only the sibling branches
 *     along the tray → <main> path (feed posts, suggestions sidebar, ...).
 *     The tray stays visible and clickable.
 *  2. If the site rule provides findFeed (LinkedIn), hide only the feed
 *     branch it returns — sidebars and composer stay usable.
 *  3. Otherwise (Explore, Reels, feed not rendered yet, detection miss)
 *     hide <main> entirely — the failure mode is always more focus.
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
 * tray, drawers, and dialogs keep working. When a single feed branch is
 * hidden (findFeed mode), the overlay is narrowed to the horizontal hole
 * that branch left behind — between the still-visible sidebars — so the
 * message reads as centered over where the feed was, not over the viewport.
 */
class UFFeedBlocker {
  constructor() {
    this.active = false;
    this.message = "";
    // Set from the site rule (storiesTray). When false, tray detection is
    // skipped entirely.
    this.keepStoriesTray = false;
    // Set from the site rule (findFeed): (main) => the feed branch to hide,
    // or null. Re-run on every mutation batch, so the marked branch grows
    // with the feed (earlier, smaller marks end up inside it — harmless).
    this.findFeed = null;
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
    // The single branch hidden by findFeed mode, if any — the overlay
    // narrows to the horizontal hole it left. Null in tray/main modes.
    this.feedBranch = null;
    this._onResize = () => this._scheduleApply();
  }

  /** Idempotent; also refreshes the message and theme when already active. */
  activate() {
    if (!this.active) {
      this.active = true;
      this._apply();
      this.observer = new MutationObserver(() => this._scheduleApply());
      this.observer.observe(document.documentElement, { childList: true, subtree: true });
      window.addEventListener("resize", this._onResize);
    }
    document.documentElement.toggleAttribute("data-uf-dialogs-hidden", this.hideDialogs);
    if (!this.overlay) this._build();
    if (!this.overlay.isConnected) document.documentElement.appendChild(this.overlay);
    this.textEl.textContent = this.message;
    this._applyTheme();
    this._placeOverlay();
    this.overlay.style.display = "flex";
  }

  deactivate() {
    if (!this.active) return;
    this.active = false;
    this.observer.disconnect();
    this.observer = null;
    window.removeEventListener("resize", this._onResize);
    this.feedBranch = null;
    const root = document.documentElement;
    root.removeAttribute("data-uf-feed-hidden");
    root.removeAttribute("data-uf-main-hidden");
    root.removeAttribute("data-uf-dialogs-hidden");
    for (const el of this.marked) {
      el.removeAttribute("data-uf-hide");
      el.removeAttribute("data-uf-hide-children");
    }
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
    const tray = main && this.keepStoriesTray ? this._findStoriesTray(main) : null;
    if (tray) {
      for (let el = tray; el !== main && el.parentElement; el = el.parentElement) {
        for (const sibling of el.parentElement.children) {
          if (sibling === el) continue;
          this._mark(sibling);
        }
      }
      this.feedBranch = null;
      this._setMode(root, "feed");
      this._placeOverlay();
      return;
    }
    const feed = main && this.findFeed ? this.findFeed(main) : null;
    if (feed) {
      // Hide the branch's CHILDREN, not the branch: it keeps its flex/grid
      // box, so the visible columns around it never reflow, and its rect is
      // what the overlay centers on. blocker.css hides future children too.
      if (!feed.hasAttribute("data-uf-hide-children")) {
        feed.setAttribute("data-uf-hide-children", "");
        this.marked.add(feed);
      }
      this.feedBranch = feed;
      this._setMode(root, "feed");
      this._placeOverlay();
      return;
    }
    this.feedBranch = null;
    this._setMode(root, "main");
    this._placeOverlay();
  }

  _mark(el) {
    if (el.hasAttribute("data-uf-hide")) return;
    el.setAttribute("data-uf-hide", "");
    this.marked.add(el);
  }

  /** "feed": only [data-uf-hide] branches hide; "main": all of <main> does. */
  _setMode(root, mode) {
    root.toggleAttribute("data-uf-feed-hidden", mode === "feed");
    root.toggleAttribute("data-uf-main-hidden", mode === "main");
  }

  /**
   * Narrow the overlay to the feed branch's own box (findFeed mode) so the
   * message centers over where the feed was, or span the viewport (tray and
   * whole-main modes). The branch stays measurable because only its children
   * are hidden. A box too narrow for the message means the layout isn't the
   * expected columns — span the viewport instead.
   */
  _placeOverlay() {
    if (!this.overlay) return;
    let left = 0;
    let right = 0;
    if (this.feedBranch && this.feedBranch.isConnected) {
      const r = this.feedBranch.getBoundingClientRect();
      if (r.width >= 240) {
        left = Math.max(0, r.left);
        right = Math.max(0, window.innerWidth - r.right);
      }
    }
    this.overlay.style.left = `${left}px`;
    this.overlay.style.right = `${right}px`;
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
