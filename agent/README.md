# unlimitedfocus agent

The other half of Unlimited Focus: the extension (repo root) blocks the
infinite scroll; this agent **does the scrolling for you** — reads your feeds
inside your own Chrome session, strips ads and suggested content, and turns
what your friends actually posted into structured data you can query, graph,
and get digests from.

## Architecture (hackathon)

```
your real Chrome (AppleScript control, logged-in session) ──> scraper (local, Python)
        │ stories + feed posts; Sponsored/Suggested shielded, never processed
        ▼
  claude CLI (Sonnet) — image+text -> {structured, brief, detail, noteworthy, mentions}
        ▼
  Butterbase (app_desa1zwpsx43) ── contacts / items / scrape_runs / interactions
        ▼
  Neo4j Aura ── (:Contact)-[:POSTED]->(:Item)-[:ABOUT]->(:Topic), [:MENTIONS]
        ▼
  RocketRide digest pipeline (phase 3) ──> "hot" digest topics
        ▼
  frontend chat on Butterbase (phase 2) + Cognee long-term memory (phase 4)
```

Browser control uses the AppleScript technique proven in
`slick_reader/marketing/foothill_browse_articles.py`: one new tab in your real
Chrome window 1, JS injected via `execute javascript "eval(atob(...))"`, tab
closed when done. No Playwright, no separate profile — your normal logins.

## Run the PoC

One-time prerequisite: Chrome menu **View → Developer → Allow JavaScript from
Apple Events** (the preflight verifies this).

The agent is a uv project (`agent/py/pyproject.toml`) — `uv run` resolves and
installs dependencies automatically, or run `uv sync` once explicitly:

```bash
cd agent/py

uv run check.py              # preflight: butterbase, neo4j, claude CLI, AppleScript
uv run scrape_instagram.py   # scrape (add --no-graph to defer Neo4j; --verbose for detail)
uv run inspect_data.py       # look at what it collected
uv run graph_sync.py         # backfill Neo4j for items captured with --no-graph
```

Secrets live in the repo-root `.env` (see `.env.example`). **No fallbacks by
design** — anything missing or invalid fails loudly.

Re-running is safe and incremental. Runs are capture-then-process backed by a
daily file cache (`agent/data/cache/YYYY-MM-DD/`): walks capture fast (stories
~2s/segment — never waits for videos to play), then everything
captured-but-uncommitted (including leftovers from earlier runs today, even
stories that have since expired) is extracted and upserted, and only then
marked committed. Dedupe is `(platform, external_id)`; a streak of
already-known posts ends the feed walk; cache days older than 7 are purged at
start. After a schema or prompt change, `--overwrite-today` re-extracts and
re-upserts today's captures in place. Don't open/close tabs in Chrome window 1
while a run is going (the driver targets the last tab).

`src/` contains the original Node/Playwright-CDP PoC of the same design —
superseded by `py/`; kept until the Python path is validated, then removed.

## Phases

- **P0 (now)** — Instagram scraper PoC: stories + feed, ad shielding, dedupe,
  extraction, Butterbase + Neo4j sync. *Inspect the data together, tune.*
- **P1** — hardening: media upload to Butterbase storage (not local paths),
  carousel/video handling, batch extraction to cut cost, i18n-proof selectors,
  scheduled overnight loop.
- **P2** — frontend chat on Butterbase (AI gateway + deployed frontend +
  auth): "what's new with my friends?", click-through to posts (recorded in
  `interactions` for preference learning).
- **P3** — RocketRide Cloud pipeline: periodic digest over Butterbase + Neo4j
  (graph traversals for trends: who's active, event clusters, communities),
  writes "hot" digest topics the chat surfaces next visit; user feedback on
  digests recorded as preferences.
- **P4** — Cognee long-term memory: promote `noteworthy` facts (birthdays,
  anniversaries, likes/dislikes) + accepted digest insights into durable agent
  memory.
- **P5** — payments on Butterbase (hackathon mandate), Telegram interface,
  more platforms (X, LinkedIn, Facebook, Threads, TikTok — each is one new
  `scrape_<platform>.py` on the same plumbing).

## Deployed stack (hackathon mandates)

| Requirement | Status |
|---|---|
| Butterbase database | ✅ 7 tables live on `app_desa1zwpsx43` (authenticated access mode) |
| Butterbase backend functions | ✅ `chat`, `interactions`, `digest-latest`, `digest-ingest` |
| Butterbase auth | ✅ email signup/login/verify wired into the frontend |
| Butterbase payments | ✅ Stripe Connect + "Focus Pro" $5/mo plan; subscribe button in UI (finish onboarding once) |
| Butterbase AI gateway | ✅ `chat` fn answers via `/chat/completions` (claude-3-haiku) |
| Butterbase frontend | ✅ https://unlimitedfocus.butterbase.dev |
| Neo4j actively traversed | ✅ `chat` fn (Aura Query API) + `digest_run.py` (top posters, topic clusters, mention edges) |
| RocketRide Cloud pipeline | ✅ `unlimitedfocus-digest` deployed, `@daily`, state=active |
| Cognee (bonus) | P4 — promote `noteworthy` facts to long-term memory |
| Daytona (bonus) | TBD |

### Backend / pipeline commands

```bash
cd agent/py
uv run deploy_backend.py            # schema + functions + CORS (idempotent); --billing for payments
uv run deploy_rocketride.py         # (re)deploy the digest pipeline to RocketRide Cloud
uv run digest_run.py                # gather context -> cloud pipeline -> 🔥 hot digest in the UI
```

## Notes / known limits (PoC)

- Selectors assume English Instagram UI (`Story by`, `Sponsored`) — TODO(i18n).
- Stories/videos: one frame + on-screen text only for now.
- Automating your own logged-in feed sits against Instagram's ToS and
  aggressive automation can flag accounts — pacing is deliberately slow and
  per-run caps small (`UF_MAX_POSTS`, `UF_MAX_STORIES`). Keep them modest.
- `media_path` points at local files under `agent/data/media/` for now.
