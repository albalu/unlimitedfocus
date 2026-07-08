// POST {question} — answers "what's going on with my friends?" grounded in
// scraped items (SQL) + the Neo4j knowledge graph (live Cypher traversal via
// the Aura Query API), synthesized by the Butterbase AI gateway.
// Auth: required (end-user JWT). Every question is logged to `interactions`
// for the preference-learning loop.

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function neo4jQuery(ctx: any, statement: string): Promise<any[]> {
  const auth = btoa(`${ctx.env.NEO4J_USERNAME}:${ctx.env.NEO4J_PASSWORD}`);
  const res = await fetch(`${ctx.env.NEO4J_HTTP_URL}/db/${ctx.env.NEO4J_DATABASE || "neo4j"}/query/v2`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Basic ${auth}`,
    },
    body: JSON.stringify({ statement }),
  });
  if (!res.ok) {
    console.error("neo4j query failed", res.status, (await res.text()).slice(0, 300));
    return [];
  }
  const out = await res.json();
  const fields: string[] = out?.data?.fields ?? [];
  const values: any[][] = out?.data?.values ?? [];
  return values.map((row) => Object.fromEntries(fields.map((f, i) => [f, row[i]])));
}

// Signup on this app is open and platform auth only proves *some* signed-up
// user — so the handler additionally requires the caller to BE the owner.
// UF_OWNER_EMAIL is injected at deploy time (deploy_backend.py), never
// committed; if it is unset the guard fails closed.
function ownerOnly(ctx: any): Response | null {
  if (!ctx.user) return json({ error: "unauthorized" }, 401);
  const owner = (ctx.env.UF_OWNER_EMAIL || "").trim().toLowerCase();
  const caller = (ctx.user.email || "").trim().toLowerCase();
  if (!owner || caller !== owner) return json({ error: "forbidden" }, 403);
  return null;
}

export default async function handler(req: Request, ctx: any): Promise<Response> {
  const denied = ownerOnly(ctx);
  if (denied) return denied;
  const { question } = await req.json().catch(() => ({}));
  if (!question) return json({ error: "question required" }, 400);

  // Recent items — flat rows from Postgres.
  const items = await ctx.db.query(
    `SELECT i.kind, i.url, i.topic, i.brief, i.posted_at, i.captured_at, c.handle
       FROM items i LEFT JOIN contacts c ON c.id = i.contact_id
      WHERE i.deleted_at IS NULL
        AND (c.snoozed_until IS NULL OR c.snoozed_until < now())
      ORDER BY coalesce(i.posted_at, i.captured_at) DESC
      LIMIT 40`
  );

  // Relationship context — graph traversal Postgres can't express as naturally:
  // who posts about what, and who they mention (2-hop Contact->Item->Contact).
  const graphActivity = await neo4jQuery(
    ctx,
    `MATCH (c:Contact)-[:POSTED]->(i:Item)
     OPTIONAL MATCH (i)-[:ABOUT]->(t:Topic)
     OPTIONAL MATCH (i)-[:MENTIONS]->(m:Contact)
     WITH c.handle AS contact, t.name AS topic,
          count(DISTINCT i) AS items,
          collect(DISTINCT m.handle)[..5] AS mentions
     ORDER BY items DESC LIMIT 15
     RETURN contact, topic, items, mentions`
  );

  const context = { recent_items: items.rows, graph_activity: graphActivity };
  const ai = await fetch(
    `${ctx.env.BUTTERBASE_API_URL}/v1/${ctx.env.BUTTERBASE_APP_ID}/chat/completions`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${ctx.env.BUTTERBASE_API_KEY}`,
      },
      body: JSON.stringify({
        model: "anthropic/claude-3-haiku", // cheapest capable tier — free-plan AI credits are tiny
        max_tokens: 400,
        temperature: 0.3,
        messages: [
          {
            role: "system",
            content:
              "You are Unlimited Focus — the agent that scrolled the feeds so the user didn't have to. " +
              "Answer ONLY from CONTEXT (their friends' scraped posts/stories + knowledge-graph activity). " +
              "Reference items by their url so the user can click through. Concise, warm, no filler.",
          },
          {
            role: "user",
            content: `CONTEXT:\n${JSON.stringify(context).slice(0, 12000)}\n\nQUESTION: ${question}`,
          },
        ],
      }),
    }
  );
  const out = await ai.json();
  const answer = out?.choices?.[0]?.message?.content ?? `(AI gateway error: ${JSON.stringify(out).slice(0, 200)})`;

  await ctx.db.query(
    `INSERT INTO interactions (action, context) VALUES ('asked_about', $1)`,
    [JSON.stringify({ question, ok: !!out?.choices })]
  );

  return json({ answer, items: items.rows.slice(0, 10) });
}
