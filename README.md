# Unlimited Focus

A browser extension that puts an end to infinite scroll on distracting feeds,
while leaving the rest of the site fully functional. On Instagram, the home
feed, Explore, and Reels are replaced by a focus message (or, optionally,
stop after a scroll allowance) — direct messages, individual posts, profiles,
and everything else work normally.

## How it works

Instead of fighting a site's internals (obfuscated class names, private API
endpoints, `IntersectionObserver` sentinels — all of which change without
notice), Unlimited Focus keys off two stable things only: **URL paths** (which
surfaces count as feeds) and coarse page structure. That makes it resilient
to redesigns. It has two modes:

**Hide the feed** (default) — on feed paths, the feed is hidden and a
centered message is shown instead (*"With focus, anything is possible."* —
editable in the popup). The stories tray stays visible and clickable: the
blocker finds it structurally (links to `/stories/…`, falling back to the
`<canvas>` story rings, reduced to their common ancestor) and hides only the
sibling branches around it inside `<main>` — feed posts, suggestions, and
all. If no tray exists on the surface (Explore, Reels) or detection ever
fails, the whole `<main>` region is hidden instead, so the failure mode is
always *more* focus, never a visible feed. The nav sidebar, drawers, and
dialogs live outside `<main>` and keep working; hidden content is
`display: none`, so nothing ever loads in the background either.

**Limit scrolling** — the feed works but is capped at a fixed allowance of N
viewport-heights ("screens") per visit, so it can never reach its "load more"
trigger. At the cap, a small banner offers a *"One more screen"* escape
hatch; each in-app navigation starts a fresh allowance. Only "main" scrollers
(the document, or elements filling most of the viewport) are capped — comment
lists, chat threads, and dropdowns are deliberately untouched.

**Specific posts and reels** (both modes) — opening a single item
(`/p/<id>`, `/reel/<id>`, `/reels/<id>`, e.g. from a DM or a profile) always
works: the item shows with the scroll limiter as a backstop, and its comments
scroll freely. But single-item viewers are also the on-ramp to infinite
consumption (swipe to the next reel, arrow to the next post — each is a URL
change, not a scroll, so no scroll cap can stop it). Unlimited Focus tracks
this drift: you can view the item you opened plus `itemAllowance` more
(default 2); after that the focus wall appears — covering item viewers even
when they render as dialogs. Returning to an already-viewed item lifts the
wall; visiting any normal page (profile, DMs, ...) resets the session.

## Install (Chrome / Edge / Brave)

1. Open `chrome://extensions`
2. Enable **Developer mode** (top right)
3. Click **Load unpacked** and select this folder

Click the toolbar icon to configure: master on/off, scroll allowance in
screens, and per-site toggles. Settings sync via `chrome.storage.sync` and
apply live — no page reload needed.

## Project structure

```
manifest.json          MV3 manifest — content scripts, permissions, popup
src/
  shared/              Loaded by BOTH content scripts and the popup
    sites.js           Site registry: which sites, which paths get limited
    settings.js        Settings schema, defaults, load/save/onChange
  content/
    blocker.js         "Hide the feed" engine: hides <main>, shows the message
    blocker.css        The attribute-gated rule that hides <main>
    limiter.js         "Limit scrolling" engine: scroll cap + banner
    index.js           Entry point: matches site rules, picks the engine for
                       the current mode, tracks SPA navigation and settings
  popup/               Toolbar popup UI (settings)
icons/                 Generated PNGs
test/                  Browser test harness (see below)
```

No build step, no dependencies. Shared modules are plain scripts that define
globals (`UFSiteRules`, `UFSettings`, `UFScrollLimiter`), loaded in order by
the manifest and by `popup.html`.

## Adding a new site

1. **`manifest.json`** — add the site's URL patterns to the content script's
   `"matches"` array.
2. **`src/shared/sites.js`** — add an entry to the registry: an `id` (stable,
   used as the storage key), a `label` for the popup, its `hosts`, and
   `limitedPaths` regexes for the surfaces that should stop scrolling.

That's it — settings, the popup toggle, and the limiter pick the new site up
automatically. Paths *not* matched by `limitedPaths` are never touched, so be
deliberate about excluding things like messages and search.

## Tests

- `node test/rules.test.js` — site rules (limited/contained path matching,
  item identity) and settings normalization.
- `test/blocker.html` — exercises the feed blocker against a mock feed DOM
  (stories tray, posts, suggestions sidebar, a dialog) in a real browser:
  serve the repo root (`python3 -m http.server 8765`) and open
  `http://localhost:8765/test/blocker.html` — it prints PASS/FAIL per check
  on the page and logs `UF_TEST_RESULT` to the console.

## Manual test checklist

Block mode (default):
- [ ] Home feed (`instagram.com/`), Explore, and Reels show the focus
      message instead of content
- [ ] Stories tray stays visible on the home feed; clicking an avatar
      opens and plays the story, and closing it returns to the blocked feed
- [ ] Sidebar navigation, search and notification drawers still work
- [ ] Editing the message in the popup updates the page live
- [ ] Message is readable in both light and dark theme

Limit mode:
- [ ] Home feed stops after N screens; banner appears
- [ ] "One more screen" extends the allowance once
- [ ] Navigating feed → profile → feed resets the allowance
- [ ] Changing the screens allowance applies without a reload

Both modes:
- [ ] DMs (`/direct/…`): scrolling old messages is unrestricted
- [ ] Opening a post and scrolling its comments is unrestricted
- [ ] Opening a reel link (e.g. shared in a DM) plays it; swiping on to a
      4th reel hits the focus wall; swiping back up shows the earlier ones
- [ ] Paging post-to-post (profile grid arrows) walls after the allowance,
      including when posts open as modal dialogs
- [ ] Visiting a profile page and reopening a reel resets the item allowance
- [ ] Toggling the extension off in the popup restores the page immediately
- [ ] Switching modes in the popup swaps behavior without a reload

## Roadmap ideas

- User-defined sites at runtime (`chrome.scripting.registerContentScripts` +
  optional host permissions) instead of manifest-listed sites
- Firefox support (`browser_specific_settings.gecko` + MV3 event page quirks)
- Per-site scroll allowances, scheduled quiet hours
