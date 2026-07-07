// Neo4j knowledge graph sync.
//
// PoC graph model:
//   (:Contact {platform, handle})-[:POSTED]->(:Item {platform, externalId})
//   (:Item)-[:ABOUT]->(:Topic {name})
//   (:Item)-[:MENTIONS]->(:Contact)        // people referenced in the item
//
// TODO(phase-2): (:Event {kind: birthday|anniversary|...}) nodes extracted from
//   "noteworthy" facts, (:Place) nodes, GDS community detection over contacts.
// TODO(phase-4): feed noteworthy facts + user feedback into Cognee long-term memory.
import neo4j from 'neo4j-driver';
import { env } from '../env.js';

let driver;

export function getDriver() {
  if (!driver) {
    // Aura convention: neo4j+s://<instance-id>.databases.neo4j.io
    // If NEO4J_INSTANCE_NAME is the human name rather than the id, set
    // NEO4J_URI explicitly (Aura console → instance → "Connection URI").
    const uri =
      process.env.NEO4J_URI ?? `neo4j+s://${env('NEO4J_INSTANCE_NAME')}.databases.neo4j.io`;
    driver = neo4j.driver(uri, neo4j.auth.basic(env('NEO4J_USERNAME'), env('NEO4J_PASSWORD')));
  }
  return driver;
}

export async function verifyGraph() {
  const info = await getDriver().getServerInfo();
  return info.address;
}

export async function ensureConstraints() {
  const session = getDriver().session();
  try {
    await session.run(
      `CREATE CONSTRAINT contact_key IF NOT EXISTS FOR (c:Contact) REQUIRE (c.platform, c.handle) IS UNIQUE`
    );
    await session.run(
      `CREATE CONSTRAINT item_key IF NOT EXISTS FOR (i:Item) REQUIRE (i.platform, i.externalId) IS UNIQUE`
    );
    await session.run(
      `CREATE CONSTRAINT topic_key IF NOT EXISTS FOR (t:Topic) REQUIRE t.name IS UNIQUE`
    );
  } finally {
    await session.close();
  }
}

export async function syncItemToGraph({ contact, item, mentions = [] }) {
  const session = getDriver().session();
  try {
    await session.run(
      `MERGE (c:Contact {platform: $platform, handle: $handle})
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
       )`,
      {
        platform: item.platform,
        handle: contact.handle,
        displayName: contact.display_name ?? null,
        externalId: item.external_id,
        kind: item.kind,
        url: item.url ?? null,
        mediaType: item.media_type ?? null,
        topic: item.topic ?? null,
        brief: item.brief ?? null,
        postedAt: item.posted_at ?? null,
        capturedAt: item.captured_at ?? new Date().toISOString(),
        dbId: item.id,
        mentions: (mentions ?? []).filter((m) => typeof m === 'string' && m.length > 0),
      }
    );
  } finally {
    await session.close();
  }
}

export async function closeGraph() {
  if (driver) {
    await driver.close();
    driver = undefined;
  }
}
