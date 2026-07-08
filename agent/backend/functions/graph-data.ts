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

async function neo4jQuery(ctx: any, statement: string, parameters: Record<string, unknown> = {}): Promise<any[]> {
  const auth = btoa(`${ctx.env.NEO4J_USERNAME}:${ctx.env.NEO4J_PASSWORD}`);
  const res = await fetch(`${ctx.env.NEO4J_HTTP_URL}/db/${ctx.env.NEO4J_DATABASE || "neo4j"}/query/v2`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Basic ${auth}` },
    body: JSON.stringify({ statement, parameters }),
  });
  if (!res.ok) throw new Error(`neo4j ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const out = await res.json();
  const fields: string[] = out?.data?.fields ?? [];
  const values: any[][] = out?.data?.values ?? [];
  return values.map((row) => Object.fromEntries(fields.map((f, i) => [f, row[i]])));
}

// POST body: { op: 'delete'|'rename', node_id: 'c:<handle>'|'t:<topic>', display_name? }
//   delete (contact) — EVERYWHERE (built for ad/spam accounts):
//       1. Neo4j: the contact node + all their Item nodes, DETACH DELETEd
//       2. Postgres: all their items tombstoned (dedupe keys kept so the
//          scraper never re-ingests those posts; heavy columns nulled)
//       3. Future collection stopped: contact permanently snoozed — the
//          scraper, hub, chat, digest and graph all honor it
//   delete (topic)   — graph-scoped only (a topic isn't a data source; it
//                      reappears if new items are about it)
//   rename — sets displayName ONLY; the id (handle / topic name) never
//            changes. Contact renames also sync contacts.display_name in
//            Postgres so the whole hub shows the new name.
async function mutate(req: Request, ctx: any): Promise<Response> {
  const b = await req.json().catch(() => ({}));
  const m = /^([ct]):(.+)$/.exec(b.node_id || "");
  if (!m || !["delete", "rename"].includes(b.op)) {
    return json({ error: "op (delete|rename) and node_id (c:<handle> | t:<name>) required" }, 400);
  }
  if (b.op === "rename" && !b.display_name) return json({ error: "display_name required for rename" }, 400);
  const [, kind, key] = m;
  const name = b.op === "rename" ? String(b.display_name).slice(0, 80) : null;

  if (kind === "c") {
    if (b.op === "delete") {
      await neo4jQuery(ctx,
        `MATCH (c:Contact {handle: $k})
         OPTIONAL MATCH (c)-[:POSTED]->(i:Item)
         DETACH DELETE i, c`, { k: key });
      await ctx.db.query(
        `UPDATE items SET deleted_at = now(), structured = NULL, detail = NULL,
                caption_raw = NULL, brief = NULL, media_path = NULL, topic = NULL
          WHERE deleted_at IS NULL
            AND contact_id IN (SELECT id FROM contacts WHERE handle = $1)`,
        [key]
      );
      await ctx.db.query(
        `UPDATE contacts SET snoozed_until = '9999-12-31T00:00:00Z' WHERE handle = $1`,
        [key]
      );
    } else {
      await neo4jQuery(ctx, `MATCH (c:Contact {handle: $k}) SET c.displayName = $d`, { k: key, d: name });
      await ctx.db.query(`UPDATE contacts SET display_name = $1 WHERE handle = $2`, [name, key]);
    }
  } else {
    if (b.op === "delete") {
      await neo4jQuery(ctx, `MATCH (t:Topic {name: $k}) DETACH DELETE t`, { k: key });
    } else {
      await neo4jQuery(ctx, `MATCH (t:Topic {name: $k}) SET t.displayName = $d`, { k: key, d: name });
    }
  }
  await ctx.db.query(`INSERT INTO interactions (action, context) VALUES ('graph_edit', $1)`, [JSON.stringify(b)]);
  return json({ ok: true });
}

const CONTACT_COLOR = "#7dd3fc";       // people who post
const MENTIONED_COLOR = "#8b96a5";     // people only seen via mentions
const TOPIC_COLOR = "#5eead4";
const MENTION_EDGE_COLOR = "#fca5a5";

// Signup on this app is open and platform auth only proves *some* signed-up
// user — so the handler additionally requires the caller to BE the owner.
// ctx.user carries only { id } (no email), so we match on the owner's
// app-user id, injected at deploy time (deploy_backend.py) and never
// committed. Belt to the RLS suspenders: the tables themselves only admit
// this same id, so the raw Data API is closed to strangers too. Fails closed
// if UF_OWNER_USER_ID is unset.
function ownerOnly(ctx: any): Response | null {
  if (!ctx.user) return json({ error: "unauthorized" }, 401);
  const owner = (ctx.env.UF_OWNER_USER_ID || "").trim();
  if (!owner || ctx.user.id !== owner) return json({ error: "forbidden" }, 403);
  return null;
}

export default async function handler(req: Request, ctx: any): Promise<Response> {
  const denied = ownerOnly(ctx);
  if (denied) return denied;
  if (req.method === "POST") return mutate(req, ctx);

  const [posters, topicEdges, mentionEdges, snoozedRows] = await Promise.all([
    neo4jQuery(ctx, `
      MATCH (c:Contact)-[:POSTED]->(i:Item)
      RETURN c.handle AS handle, coalesce(c.displayName, '@' + c.handle) AS caption, count(i) AS posts
      ORDER BY posts DESC LIMIT 60`),
    neo4jQuery(ctx, `
      MATCH (c:Contact)-[:POSTED]->(i:Item)-[:ABOUT]->(t:Topic)
      RETURN c.handle AS handle, t.name AS topic,
             coalesce(t.displayName, t.name) AS tcaption, count(i) AS n
      ORDER BY n DESC LIMIT 200`),
    neo4jQuery(ctx, `
      MATCH (c:Contact)-[:POSTED]->(i:Item)-[:MENTIONS]->(m:Contact)
      WHERE c <> m
      RETURN c.handle AS who, m.handle AS whom,
             coalesce(m.displayName, '@' + m.handle) AS whomCaption, count(i) AS n
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
      caption: p.caption,
      size: 18 + Math.min(30, Number(p.posts) * 3),
      color: CONTACT_COLOR,
    });
  }
  for (const e of topicEdges) {
    if (muted.has(e.handle) || !nodes.has(`c:${e.handle}`)) continue;
    const tid = `t:${e.topic}`;
    const t = nodes.get(tid) ?? { id: tid, caption: e.tcaption, size: 12, color: TOPIC_COLOR };
    t.size = Math.min(34, t.size + Number(e.n) * 2);
    nodes.set(tid, t);
    rels.push({ id: `pa:${e.handle}:${e.topic}`, from: `c:${e.handle}`, to: tid,
                width: Math.min(6, 1 + Number(e.n)) });
  }
  for (const m of mentionEdges) {
    if (muted.has(m.who) || muted.has(m.whom)) continue;
    if (!nodes.has(`c:${m.who}`)) continue;
    if (!nodes.has(`c:${m.whom}`)) {
      nodes.set(`c:${m.whom}`, { id: `c:${m.whom}`, caption: m.whomCaption, size: 14, color: MENTIONED_COLOR });
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
