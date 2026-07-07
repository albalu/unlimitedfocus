"""Neo4j knowledge-graph sync.

PoC graph model:
    (:Contact {platform, handle})-[:POSTED]->(:Item {platform, externalId})
    (:Item)-[:ABOUT]->(:Topic {name})
    (:Item)-[:MENTIONS]->(:Contact)

TODO(phase-2): (:Event) nodes from "noteworthy" facts, (:Place), GDS communities.
TODO(phase-4): promote noteworthy facts + user feedback into Cognee memory.
"""
from __future__ import annotations

from neo4j import GraphDatabase

from uf_env import env

_driver = None


def get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(env("NEO4J_URI"),
                                       auth=(env("NEO4J_USERNAME"), env("NEO4J_PASSWORD")))
    return _driver


def verify_graph() -> None:
    try:
        get_driver().verify_connectivity()
    except Exception as exc:
        raise SystemExit(
            f"Neo4j unreachable: {exc}\n"
            "fix: check NEO4J_URI in .env (Aura console → your instance → Connect → Connection URI), "
            "or rerun with --no-graph and backfill later via graph_sync.py"
        ) from None


def ensure_constraints() -> None:
    with get_driver().session() as s:
        s.run("CREATE CONSTRAINT contact_key IF NOT EXISTS FOR (c:Contact) REQUIRE (c.platform, c.handle) IS UNIQUE")
        s.run("CREATE CONSTRAINT item_key IF NOT EXISTS FOR (i:Item) REQUIRE (i.platform, i.externalId) IS UNIQUE")
        s.run("CREATE CONSTRAINT topic_key IF NOT EXISTS FOR (t:Topic) REQUIRE t.name IS UNIQUE")


_SYNC_CYPHER = """
MERGE (c:Contact {platform: $platform, handle: $handle})
  ON CREATE SET c.firstSeenAt = datetime($capturedAt)
SET c.displayName = coalesce($displayName, c.displayName),
    c.lastSeenAt  = datetime($capturedAt)
MERGE (i:Item {platform: $platform, externalId: $externalId})
SET i.kind       = $kind,
    i.url        = $url,
    i.mediaType  = $mediaType,
    i.topic      = $topic,
    i.brief      = $brief,
    i.capturedAt = datetime($capturedAt),
    i.postedAt   = CASE WHEN $postedAt IS NULL THEN null ELSE datetime($postedAt) END,
    i.dbId       = $dbId
MERGE (c)-[:POSTED]->(i)
FOREACH (_ IN CASE WHEN $topic IS NULL OR $topic = '' THEN [] ELSE [1] END |
  MERGE (t:Topic {name: toLower($topic)})
  MERGE (i)-[:ABOUT]->(t)
)
FOREACH (m IN $mentions |
  MERGE (mc:Contact {platform: $platform, handle: m})
  MERGE (i)-[:MENTIONS]->(mc)
)
"""


def sync_item_to_graph(contact: dict, item: dict, mentions: list | None = None) -> None:
    mentions = [m for m in (mentions or []) if isinstance(m, str) and m]
    with get_driver().session() as s:
        s.run(
            _SYNC_CYPHER,
            platform=item["platform"],
            handle=contact["handle"],
            displayName=contact.get("display_name"),
            externalId=item["external_id"],
            kind=item["kind"],
            url=item.get("url"),
            mediaType=item.get("media_type"),
            topic=item.get("topic"),
            brief=item.get("brief"),
            postedAt=item.get("posted_at"),
            capturedAt=item.get("captured_at"),
            dbId=item.get("id"),
            mentions=mentions,
        )


def close_graph() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None
