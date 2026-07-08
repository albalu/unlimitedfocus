#!/usr/bin/env python3
"""Digest run: compile what happened across the user's circles and turn it
into a "hot" digest via the RocketRide Cloud pipeline.

Flow:
  1. Butterbase: recent items, shield stats, past digest feedback (preference steering).
  2. Neo4j: graph traversals — top posters, topic clusters, mention edges.
  3. RocketRide Cloud: run the digest_pipeline (webhook -> ai_chat -> response).
  4. POST the result to the Butterbase `digest-ingest` function -> `digests`
     table -> surfaces as 🔥 hot digest in the frontend chat UI.

Usage:  uv run digest_run.py [--days 2]
Run it after scrapes (or nightly). Deploy the pipeline first: deploy_rocketride.py.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os

import requests

import butterbase as bb
import graph
import uf_config as cfg
from uf_env import AGENT_ROOT, env

PIPELINE_FILE = AGENT_ROOT / "pipeline" / "digest_pipeline.json"


def gather_context(days: int) -> dict:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()

    items = bb.select("items", {
        "captured_at": f"gte.{since}",
        "deleted_at": "is.null",
        "select": "kind,topic,brief,url,posted_at,structured,contact_id",
        "order": "captured_at.desc", "limit": 100,
    })
    contact_rows = bb.select("contacts", {"select": "id,handle,snoozed_until", "limit": 500})
    contacts = {c["id"]: c["handle"] for c in contact_rows}
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    snoozed_ids = {c["id"] for c in contact_rows if (c.get("snoozed_until") or "") > now_iso}
    items = [i for i in items if i.get("contact_id") not in snoozed_ids]
    for i in items:
        i["handle"] = contacts.get(i.pop("contact_id"), "?")
        i["noteworthy"] = (i.pop("structured") or {}).get("noteworthy") or []

    runs = bb.select("scrape_runs", {"status": "eq.completed", "order": "started_at.desc", "limit": 20})
    shield = {
        "ads_shielded": sum((r.get("stats") or {}).get("ads_shielded", 0) for r in runs),
        "suggested_shielded": sum((r.get("stats") or {}).get("suggested_shielded", 0) for r in runs),
    }
    feedback = bb.select("preferences", {"key": "eq.digest_feedback", "order": "created_at.desc", "limit": 20})

    # Graph traversals — the relationship layer (this is what Neo4j is FOR):
    graph.verify_graph()
    with graph.get_driver().session() as s:
        top_posters = s.run(
            """MATCH (c:Contact)-[:POSTED]->(i:Item)
               WHERE i.capturedAt >= datetime($since)
               RETURN c.handle AS handle, count(i) AS items
               ORDER BY items DESC LIMIT 10""", since=since).data()
        topic_map = s.run(
            """MATCH (c:Contact)-[:POSTED]->(i:Item)-[:ABOUT]->(t:Topic)
               WHERE i.capturedAt >= datetime($since)
               RETURN t.name AS topic, count(i) AS items, collect(DISTINCT c.handle)[..6] AS by
               ORDER BY items DESC LIMIT 12""", since=since).data()
        mention_edges = s.run(
            """MATCH (c:Contact)-[:POSTED]->(i:Item)-[:MENTIONS]->(m:Contact)
               WHERE i.capturedAt >= datetime($since)
               RETURN c.handle AS who, m.handle AS mentioned, count(i) AS times
               ORDER BY times DESC LIMIT 15""", since=since).data()
    graph.close_graph()

    return {
        "window_days": days,
        "recent_items": items[:60],
        "shield": shield,
        "user_feedback_on_past_digests": [f.get("value") for f in feedback],
        "graph": {"top_posters": top_posters, "topics": topic_map, "mentions": mention_edges},
    }


# The llm_openai_api node has no system-prompt field, so the full instruction
# rides in the payload text (it becomes the model's user message).
INSTRUCTION = (
    "You are the digest writer for Unlimited Focus, an agent that scrolls social feeds so its "
    "user doesn't have to. Below is a JSON context: recent items from the user's friends, "
    "knowledge-graph trends (top posters, topic clusters, who-mentions-whom), shield stats, and "
    "the user's past digest feedback (respect it — skip themes they marked not_interesting). "
    "Respond with ONLY minified JSON, no markdown fences:\n"
    '{"headline":"<one catchy line: the single most noteworthy thing>",'
    '"body":"<5-10 sentences: trends, life events, who is active, who mentioned whom; group by '
    'person/topic; warm, concrete, no filler>",'
    '"topics":["<3-6 topic tags>"]}\n\nCONTEXT:\n'
)


async def run_pipeline(context: dict) -> str:
    from rocketride import RocketRideClient
    from rocketride.schema import Question

    # The SDK substitutes ${ROCKETRIDE_*} placeholders from its env at run time
    # (secrets stay client-side, never stored in the cloud). The pipeline's LLM
    # node authenticates to the Butterbase gateway with this — reuse our
    # Butterbase key so no separate model key is needed.
    os.environ["ROCKETRIDE_BB_KEY"] = env("BUTTERBASE_API_KEY")

    payload = INSTRUCTION + json.dumps(context, default=str)
    uri = os.environ.get("ROCKETRIDE_URI", "https://api.rocketride.ai")
    async with RocketRideClient(uri=uri, auth=env("ROCKET_RIDE_API_KEY")) as client:
        result = await client.use(filepath=str(PIPELINE_FILE), source="webhook_1")
        token = result["token"]
        try:
            # chat() is the request/response path (send() only uploads the input
            # object and returns its receipt). expectJson nudges structured out.
            q = Question(expectJson=True)
            q.addQuestion(payload)
            resp = await client.chat(token=token, question=q)
        finally:
            await client.terminate(token)

    answers = (resp or {}).get("answers") or []
    if not answers:
        raise SystemExit(f"pipeline returned no answers: {json.dumps(resp, default=str)[:300]}")
    first = answers[0]
    return first if isinstance(first, str) else json.dumps(first, default=str)


def parse_digest(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise SystemExit(f"pipeline returned no JSON digest: {raw[:300]}")
    d = json.loads(raw[start:end + 1])
    if not d.get("headline"):
        raise SystemExit(f"digest missing headline: {raw[:300]}")
    return d


def ingest(digest: dict, days: int) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    r = requests.post(
        f"https://api.butterbase.ai/v1/{cfg.BUTTERBASE_APP_ID}/fn/digest-ingest",
        headers={"Content-Type": "application/json", "X-UF-Secret": env("UF_INGEST_SECRET")},
        json={
            "headline": digest["headline"],
            "body": digest.get("body"),
            "topics": digest.get("topics", []),
            "stats": {"window_days": days},
            "period_start": (now - dt.timedelta(days=days)).isoformat(),
            "period_end": now.isoformat(),
            "source": "rocketride",
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise SystemExit(f"digest-ingest failed: HTTP {r.status_code}: {r.text[:300]}")
    print(f"✓ digest ingested ({r.json().get('id')}): {digest['headline']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=2, help="look-back window (default 2)")
    args = ap.parse_args()

    print("gathering context (butterbase + neo4j traversals) …")
    context = gather_context(args.days)
    n = len(context["recent_items"])
    if n == 0:
        raise SystemExit("no items in window — run the scraper first")
    print(f"  {n} items, {len(context['graph']['top_posters'])} active contacts, "
          f"{len(context['graph']['mentions'])} mention edges")

    print("running RocketRide Cloud digest pipeline …")
    raw = asyncio.run(run_pipeline(context))
    digest = parse_digest(raw)
    ingest(digest, args.days)


if __name__ == "__main__":
    main()
