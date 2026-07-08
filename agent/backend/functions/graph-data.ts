// GET — the social graph, shaped for @neo4j-nvl rendering in the web app.
// Live Cypher aggregation over the knowledge graph (Aura Query API):
//   - Contact nodes sized by posting activity
//   - Topic nodes sized by how much of the feed they cover
//   - POSTS_ABOUT edges (person -> topic, width = item count)
//   - MENTIONS edges between PEOPLE, derived through their items
//     ((c1)-[:POSTED]->(i)-[:MENTIONS]->(c2) collapsed to c1 -> c2)
// Snoozed posters are excluded, same as everywhere else in the hub.
// Auth: required (end-user JWT). Neo4j credentials stay server-side.

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function neo4jQuery(ctx: any, statement: string): Promise<any[]> {
  const auth = btoa(`${ctx.env.NEO4J_USERNAME}:${ctx.env.NEO4J_PASSWORD}`);
  const res = await fetch(`${ctx.env.NEO4J_HTTP_URL}/db/neo4j/query/v2`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Basic ${auth}` },
    body: JSON.stringify({ statement }),
  });
  if (!res.ok) throw new Error(`neo4j ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const out = await res.json();
  const fields: string[] = out?.data?.fields ?? [];
  const values: any[][] = out?.data?.values ?? [];
  return values.map((row) => Object.fromEntries(fields.map((f, i) => [f, row[i]])));
}

const CONTACT_COLOR = "#7dd3fc";       // people who post
const MENTIONED_COLOR = "#8b96a5";     // people only seen via mentions
const TOPIC_COLOR = "#5eead4";
const MENTION_EDGE_COLOR = "#fca5a5";

export default async function handler(_req: Request, ctx: any): Promise<Response> {
  if (!ctx.user) return json({ error: "unauthorized" }, 401);

  const [posters, topicEdges, mentionEdges, snoozedRows] = await Promise.all([
    neo4jQuery(ctx, `
      MATCH (c:Contact)-[:POSTED]->(i:Item)
      RETURN c.handle AS handle, count(i) AS posts
      ORDER BY posts DESC LIMIT 60`),
    neo4jQuery(ctx, `
      MATCH (c:Contact)-[:POSTED]->(i:Item)-[:ABOUT]->(t:Topic)
      RETURN c.handle AS handle, t.name AS topic, count(i) AS n
      ORDER BY n DESC LIMIT 200`),
    neo4jQuery(ctx, `
      MATCH (c:Contact)-[:POSTED]->(i:Item)-[:MENTIONS]->(m:Contact)
      WHERE c <> m
      RETURN c.handle AS who, m.handle AS whom, count(i) AS n
      ORDER BY n DESC LIMIT 150`),
    ctx.db.query(`SELECT handle FROM contacts WHERE snoozed_until IS NOT NULL AND snoozed_until > now()`),
  ]);
  const muted = new Set((snoozedRows.rows ?? []).map((r: any) => r.handle));

  const nodes = new Map<string, any>();
  const rels: any[] = [];

  for (const p of posters) {
    if (muted.has(p.handle)) continue;
    nodes.set(`c:${p.handle}`, {
      id: `c:${p.handle}`,
      caption: `@${p.handle}`,
      size: 18 + Math.min(30, Number(p.posts) * 3),
      color: CONTACT_COLOR,
    });
  }
  for (const e of topicEdges) {
    if (muted.has(e.handle) || !nodes.has(`c:${e.handle}`)) continue;
    const tid = `t:${e.topic}`;
    const t = nodes.get(tid) ?? { id: tid, caption: e.topic, size: 12, color: TOPIC_COLOR };
    t.size = Math.min(34, t.size + Number(e.n) * 2);
    nodes.set(tid, t);
    rels.push({ id: `pa:${e.handle}:${e.topic}`, from: `c:${e.handle}`, to: tid,
                width: Math.min(6, 1 + Number(e.n)) });
  }
  for (const m of mentionEdges) {
    if (muted.has(m.who) || muted.has(m.whom)) continue;
    if (!nodes.has(`c:${m.who}`)) continue;
    if (!nodes.has(`c:${m.whom}`)) {
      nodes.set(`c:${m.whom}`, { id: `c:${m.whom}`, caption: `@${m.whom}`, size: 14, color: MENTIONED_COLOR });
    }
    rels.push({ id: `m:${m.who}:${m.whom}`, from: `c:${m.who}`, to: `c:${m.whom}`,
                caption: "mentions", color: MENTION_EDGE_COLOR,
                width: Math.min(5, 1 + Number(m.n)) });
  }

  return json({
    nodes: [...nodes.values()],
    rels,
    meta: { posters: posters.length, mention_edges: mentionEdges.length, muted: muted.size },
  });
}
