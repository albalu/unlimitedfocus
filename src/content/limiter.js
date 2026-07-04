"use strict";

/**
 * UFScrollLimiter — generic "stop scrolling after N screens" engine.
 *
 * Strategy: instead of fighting the site's feed internals (obfuscated class
 * names, GraphQL endpoints, IntersectionObserver sentinels — all of which
 * change without notice), we cap how far the page can be scrolled. The feed
 * physically can't reach its "load more" trigger, so infinite scroll ends.
 * Nothing else on the page is touched.
 *
 * It clamps the document scroller and any "main" scroller (an element that
 * fills most of the viewport, e.g. the reels feed). Smaller scrollable areas
 * — comment lists, chat threads, dropdowns — are deliberately ignored so
 * normal site features keep working.
 */
class UFScrollLimiter {
  constructor() {
    this.active = false;
    this.maxScreens = 3;
    this.extraScreens = 0; // granted via the banner's "one more screen" button
    this.banner = null;
    this.bannerHideTimer = 0;
    this._onScroll = this._onScroll.bind(this);
    this._onWheel = this._onWheel.bind(this);
  }

  /** Max scrollTop, in px, for limited scrollers. */
  get capPx() {
    return (this.maxScreens + this.extraScreens) * window.innerHeight;
  }

  activate() {
    if (this.active) return;
    this.active = true;
    // Capture phase: scroll events don't bubble, but they do pass through the
    // window during capture, so this sees every scroller on the page.
    window.addEventListener("scroll", this._onScroll, { capture: true, passive: true });
    window.addEventListener("wheel", this._onWheel, { capture: true, passive: false });
    this._clamp(document.scrollingElement);
  }

  deactivate() {
    if (!this.active) return;
    this.active = false;
    window.removeEventListener("scroll", this._onScroll, { capture: true });
    window.removeEventListener("wheel", this._onWheel, { capture: true });
    this._hideBanner();
  }

  /** Call on SPA navigation: each surface gets a fresh allowance. */
  resetForNavigation() {
    this.extraScreens = 0;
    this._hideBanner();
  }

  _isMainScroller(el) {
    if (el === document.scrollingElement) return true;
    const rect = el.getBoundingClientRect();
    return rect.width >= window.innerWidth * 0.6 && rect.height >= window.innerHeight * 0.8;
  }

  _isScrollable(el) {
    if (el === document.scrollingElement) return true;
    if (el.scrollHeight <= el.clientHeight + 1) return false;
    const overflowY = getComputedStyle(el).overflowY;
    return overflowY === "auto" || overflowY === "scroll";
  }

  _clamp(el) {
    if (!el) return false;
    const cap = this.capPx;
    if (el.scrollTop <= cap) return false;
    if (el.scrollTo) {
      el.scrollTo({ top: cap, behavior: "instant" });
    } else {
      el.scrollTop = cap;
    }
    return true;
  }

  _onScroll(event) {
    const el = event.target === document ? document.scrollingElement : event.target;
    if (!(el instanceof Element)) return;
    if (!this._isMainScroller(el)) return;
    if (this._clamp(el)) this._showBanner();
  }

  /**
   * Blocks downward wheel input once the cap is reached, so the page hard-stops
   * instead of rubber-banding. Inner scrollers (comments, chats) that can still
   * scroll get to consume the event as usual.
   */
  _onWheel(event) {
    if (event.deltaY <= 0) return;
    const cap = this.capPx;
    for (const node of event.composedPath()) {
      if (!(node instanceof Element)) continue;
      if (!this._isScrollable(node)) continue;
      if (this._isMainScroller(node)) {
        const maxTop = node.scrollHeight - node.clientHeight;
        if (node.scrollTop >= cap - 1 && maxTop > cap) {
          event.preventDefault();
          this._showBanner();
        }
        return;
      }
      // A smaller scroller that can still move down handles the wheel itself;
      // if it's at its bottom, scrolling chains up, so keep walking ancestors.
      if (node.scrollTop + node.clientHeight < node.scrollHeight - 1) {
        return;
      }
    }
  }

  _showBanner() {
    if (!this.banner) this.banner = this._buildBanner();
    if (!this.banner.isConnected) document.documentElement.appendChild(this.banner);
    this.banner.style.opacity = "1";
    this.banner.style.pointerEvents = "auto";
    clearTimeout(this.bannerHideTimer);
    this.bannerHideTimer = setTimeout(() => this._hideBanner(), 4000);
  }

  _hideBanner() {
    clearTimeout(this.bannerHideTimer);
    if (!this.banner) return;
    this.banner.style.opacity = "0";
    this.banner.style.pointerEvents = "none";
  }

  _buildBanner() {
    const banner = document.createElement("div");
    // Inline styles only: the page's CSS can't be trusted and we don't want
    // to ship a stylesheet into the host page.
    Object.assign(banner.style, {
      position: "fixed",
      left: "50%",
      bottom: "24px",
      transform: "translateX(-50%)",
      zIndex: "2147483647",
      display: "flex",
      alignItems: "center",
      gap: "12px",
      padding: "10px 16px",
      borderRadius: "999px",
      background: "rgba(22, 22, 34, 0.95)",
      color: "#fff",
      font: "13px/1.4 -apple-system, system-ui, sans-serif",
      boxShadow: "0 4px 24px rgba(0, 0, 0, 0.35)",
      opacity: "0",
      pointerEvents: "none",
      transition: "opacity 150ms ease",
    });

    const text = document.createElement("span");
    text.textContent = "End of your feed — Unlimited Focus";
    banner.appendChild(text);

    const button = document.createElement("button");
    button.textContent = "One more screen";
    Object.assign(button.style, {
      border: "none",
      borderRadius: "999px",
      padding: "6px 12px",
      background: "#4f46e5",
      color: "#fff",
      font: "inherit",
      fontWeight: "600",
      cursor: "pointer",
    });
    button.addEventListener("click", () => {
      this.extraScreens += 1;
      this._hideBanner();
    });
    banner.appendChild(button);

    return banner;
  }
}

globalThis.UFScrollLimiter = UFScrollLimiter;
