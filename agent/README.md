# unlimitedfocus agent

The Chrome extension at the repo root **blocks** the infinite scroll. This agent
is the other half: it **does the scrolling for you**. Overnight, on your own
machine, it walks your feeds inside your real logged-in Chrome, throws away the
ads and "suggested" content, and turns what your friends actually posted into
structured data — a searchable history, a knowledge graph of who's connected to
what, and a daily digest you read in a few seconds instead of doom-scrolling for
an hour.

You interact with it in one of two ways: a **web app** (ask "what's new with my
close friends?", click through to the few posts you actually care about) and a
**daily digest** (a short "here's what happened" written for you each morning).

---

## The three things you'll do

1. **Scrape** — run a local script that drives your Chrome and records your feed.
2. **Digest** — run one command that turns the last few days into a digest.
3. **Interact** — open the web app, read the digest, ask questions, click through.

Steps 1 and 2 run on your machine (your Instagram session never leaves it).
Step 3 is a hosted web app anyone you share it with can open.

---

## Step 0 — one-time setup

**Prerequisites**

- macOS with **Google Chrome**, logged into Instagram in the normal way.
- Chrome menu **View → Developer → Allow JavaScript from Apple Events** (turn it
  on once — the preflight checks this).
- [`uv`](https://docs.astral.sh/uv/) (the Python runner) and the
  [`claude`](https://claude.com/claude-code) CLI, logged in.
- A repo-root `.env` (copy `.env.example`). Required keys — **no fallbacks by
  design, a missing/invalid one fails loudly:**

  ```
  BUTTERBASE_API_KEY=      # backend: db, functions, AI gateway, payments
  NEO4J_URI=               # Aura console → your instance → Connect → "Connection URI"
  NEO4J_USERNAME=
  NEO4J_PASSWORD=
  NEO4J_INSTANCE_NAME=
  ROCKET_RIDE_API_KEY=     # the cloud digest pipeline
  UF_OWNER_EMAIL=          # your account (for humans; not enforced directly)
  UF_OWNER_USER_ID=        # your Butterbase app-user id — the real guard
  ```

  **Single-owner lockdown (`UF_OWNER_USER_ID`).** Butterbase's signup endpoint is
  open and can't be switched off — anyone who finds your app can register and get
  a valid token. So the backend admits exactly one identity, your app-user id, at
  **two layers** (both set up by `deploy_backend.py`, both failing closed if the
  id is unset):

  1. **Functions** — every backend function 403s any caller whose `ctx.user.id`
     isn't yours. (The token carries only the id, not the email — that's why the
     id, not `UF_OWNER_EMAIL`, is what's enforced.)
  2. **Data API** — owner-only **RLS** on every table, so the auto-generated
     `GET /v1/<app>/<table>` returns nothing to anyone else. Without this a
     stranger's token could read your tables directly, bypassing the functions.

  Your own service key (the scraper, this deploy script) bypasses RLS as
  designed, so scraping keeps working. **Non-owner accounts can still be created,
  but they're inert: no data, no AI spend.**

  **Getting your `UF_OWNER_USER_ID`:** sign up once in the web app with
  `UF_OWNER_EMAIL`, then read the `user.id` field from the login response (or ask
  Butterbase support / the MCP `manage_auth_users` list). Put it in `.env` and
  run `deploy_backend.py`.

**Verify everything is wired up**

```bash
cd agent/py
uv run check.py
```

This checks all five services in one shot — Butterbase reachable, Neo4j
connected, `claude` CLI able to generate, and AppleScript allowed to drive
Chrome — and prints an exact fix hint for anything that's red.

---

## Step 1 — scrape your Instagram (local)

Make sure Chrome is open and logged into Instagram, then:

```bash
uv run scrape_instagram.py            # add --verbose to watch each step
```

What happens: the script opens **one new tab** in your real Chrome, walks your
**stories** (fast — grabs the first frame + who/when, then hits Next, so it
never sits through a video) and your **home feed**, skipping anything marked
*Sponsored* or *Suggested for you* — that's the whole point, you never see the
ads. For each genuine post/story from a friend it downloads the image, asks the
local `claude` CLI to describe it (topic, a one-line brief, a fuller
description, plus any milestones like birthdays/weddings/travel), and saves it.
It closes its tab when done; the rest of your browser is untouched.

**It's safe to run anytime.** Everything captured lands in a daily file cache
(`agent/data/cache/YYYY-MM-DD/`) first, and only reaches the database after it's
been processed. Re-running skips anything already seen, retries anything that
failed, and stops the feed walk once it hits posts from a previous run. Leave it
running overnight; run it again tomorrow — it just adds what's new.

> Don't open/close tabs in Chrome window 1 while a scrape is running (the driver
> works on the last tab). Per-run caps are deliberately small and pacing is slow
> — automating your own logged-in feed is against Instagram's ToS and aggressive
> speed can flag an account. Tune with `UF_MAX_POSTS` / `UF_MAX_STORIES` in
> `.env`.

**See what it collected:**

```bash
uv run inspect_data.py
```

Prints your recent runs, the contacts it's building up, and the latest items
with their brief + link.

**LinkedIn too:** `uv run scrape_linkedin.py` walks your LinkedIn home feed
the same way (feed-only — LinkedIn retired stories), shielding *Promoted* and
*Suggested* posts, on the same daily cache, database, and graph. The shared
run machinery lives in `scrape_common.py`; each platform script only owns its
walk.

**Scrape while you're away:** `uv run lockwatch.py install` sets up a
launchd agent that runs both scrapers (sequentially) every time the screen
locks — at most once per 10 minutes (`UF_LOCKWATCH_GAP_MIN`). Unlocking
mid-run interrupts the scraper gracefully, so Chrome is yours the moment you
sit back down. After installing, run `uv run lockwatch.py fire` once while
unlocked to approve the macOS automation prompts. `status` / `uninstall` /
log at `agent/data/lockwatch.log`. Caveat: a closed lid means the Mac
sleeps, so no runs happen then — `caffeinate -i` only guards against idle
sleep during a run.

---

## Step 2 — digest the last few days

```bash
uv run digest_run.py                  # --days N to widen the window (default 2)
```

This gathers the recent items **and runs Neo4j graph queries** (who's been most
active, which topics are clustering, who's mentioning whom), sends that context
to the **digest pipeline running on RocketRide Cloud**, and stores the result as
a 🔥 "hot" digest. Next time you open the web app, it's on the front page.

The pipeline is already deployed and also runs on a daily schedule; `digest_run.py`
is how you trigger one on demand right after a scrape.

---

## Step 3 — interact on the web

Open **https://unlimitedfocus.butterbase.dev**, sign up (email + password), and
you get:

- **🔥 Hot digest** — the short write-up from Step 2. Thumbs-up / thumbs-down
  each one; your feedback is remembered and steers future digests toward what
  you actually care about.
- **💬 Ask about your circles** — natural-language questions ("what's new with
  my close friends this week?"). Answers are grounded in your scraped items plus
  live graph traversals, and cite the posts so you can open them.
- **🧑‍🤝‍🧑 Latest from your feed (ad-free)** — the real posts, no ads, no
  suggestions. **Clicking a post is recorded** — that's the signal the system
  learns from about what's worth your attention.
- **🛡 The shield** — a running count of how many ads and "suggested" posts it
  scrolled past *so you didn't have to*.
- **Go Pro** — a subscription (Butterbase payments) for the always-on version.

---

## Everyday loop, in short

```bash
cd agent/py
uv run scrape_instagram.py     # overnight or whenever
uv run digest_run.py           # turn it into a digest
# → open https://unlimitedfocus.butterbase.dev and read / ask / click
```

---

## Tech stack — what and why

| Layer | Tech | Why this one |
|---|---|---|
| **Feed capture** | Your real Chrome, driven by **AppleScript** | Instagram's private APIs and class names change constantly and automation gets flagged. Driving *your own logged-in browser* (one tab, JS via `execute javascript "eval(atob(...))"`) means no separate login, no fragile reverse-engineering, and your session/cookies never leave your machine. Same technique proven in `slick_reader/marketing/foothill_browse_articles.py`. |
| **Understanding posts** | Local **`claude` CLI** (Sonnet) | Vision + text extraction runs through your local Claude Code login instead of a metered API, so overnight runs over hundreds of items stay cheap. Turns an image + caption into `{topic, brief, detail, noteworthy, mentions}`. |
| **Backend** | **Butterbase** — Postgres, serverless functions, auth, AI gateway, payments, static hosting | One managed backend with zero DevOps: the database (`contacts`, `items`, `scrape_runs`, `interactions`, `digests`, `preferences`), the functions the web app calls (`chat`, `interactions`, `digest-latest`, `digest-ingest`), end-user auth, the AI model gateway the chat + digest use, Stripe-Connect payments, and the deployed frontend — all in one place. |
| **Relationships** | **Neo4j Aura** | The interesting questions are about *connections* — who posts about what, who mentions whom, which friends cluster around a topic or event. That's graph traversal, not table joins. The graph is `(:Contact)-[:POSTED]->(:Item)-[:ABOUT]->(:Topic)` with `[:MENTIONS]` edges, and both the chat function and the digest actively query it with Cypher (top posters, topic clusters, mention edges) — not as a key-value store. |
| **Digest pipeline** | **RocketRide Cloud** | The digest is a real AI pipeline (`webhook → llm → response`) deployed to RocketRide Cloud as a managed, scheduled endpoint — not a local cron. Its LLM node is pointed at the **Butterbase AI gateway**, so the pipeline calls Butterbase for inference (no separate model key). Secrets are substituted client-side and never stored in the cloud. |
| **Long-term memory** (bonus, in progress) | **Cognee** | Milestones worth remembering across time — birthdays, anniversaries, a friend's move or launch — and which digests you found interesting get promoted into durable agent memory, so the assistant gets to know your circle over months, not just the last scrape. |

### How the pieces connect

```
your real Chrome (logged in)
        │  AppleScript: one tab, walk stories + feed, shield ads/suggested
        ▼
scrape_instagram.py ──local claude CLI──> {topic, brief, detail, noteworthy, mentions}
        │
        ├─────────────► Butterbase Postgres   (contacts / items / scrape_runs / interactions)
        └─────────────► Neo4j Aura            ((:Contact)-[:POSTED]->(:Item)-[:ABOUT]->(:Topic), [:MENTIONS])

digest_run.py ── reads Butterbase + traverses Neo4j ──► RocketRide Cloud pipeline ──► Butterbase AI gateway
        └────────────────────────────────────────────► digests table  (🔥 hot digest)

web app (Butterbase-hosted, Butterbase auth)
   digest-latest / chat / interactions functions ── read items + traverse Neo4j + call AI gateway
        └─ your clicks & 👍/👎 ──► interactions / preferences  (what to learn from next)
```

---

## Operating the backend (deploy / redeploy)

These are already deployed; rerun after a change (all idempotent):

```bash
cd agent/py
uv run deploy_backend.py         # schema + owner-only RLS + functions + CORS; add --billing for the Stripe plan
uv run deploy_rocketride.py      # (re)deploy the digest pipeline to RocketRide Cloud
uv run graph_sync.py             # backfill Neo4j for any items captured with --no-graph
```

To update the frontend, edit `agent/frontend/index.html` and redeploy it to
Butterbase static hosting.

---

## Roadmap

- **Now** — Instagram + LinkedIn scrapers, backend, graph, web app, and
  RocketRide digest all working end-to-end (LinkedIn not yet wired into the
  digest pipeline).
- **Next** — media to Butterbase storage (not local files), carousel/video
  handling, batch extraction, i18n-proof selectors, a scheduled overnight loop.
- **Then** — Cognee memory promotion; a Telegram interface; more platforms (X,
  Facebook, Threads, TikTok — each is one new `scrape_<platform>.py`
  on the same plumbing).

## Known limits (PoC)

- Selectors assume the **English** UI (`Story by`, `Sponsored`, `Promoted`).
- Stories/videos capture **one frame** + on-screen text.
- `media_path` points at local files under `agent/data/media/` for now.
- The RocketRide **daily scheduled** run needs `ROCKETRIDE_BB_KEY` set in its
  cloud project env; the on-demand `digest_run.py` path resolves it locally and
  works today.
- The older Node/Playwright PoC in `agent/src/` is superseded by `agent/py/` and
  will be removed.
