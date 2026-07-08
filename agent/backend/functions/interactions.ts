// POST {action, item_id?, context?} — records user interactions AND handles
// curation. Auth: required (end-user JWT).
//
//   visited_link    — log only (learning signal)
//   asked_about     — log only
//   item_feedback   {item_id, context:{verdict: interesting|not_relevant|ad}}
//                   -> interactions + preferences (steers digests/chat later)
//   digest_feedback {context:{digest_id, verdict}} -> digests.feedback + preferences
//   deleted         {context:{kind:'item', ...}, item_id} or {context:{kind:'digest', digest_id}}
//     item:   TOMBSTONE — keep (platform, external_id, deleted_at) so the
//             scraper's dedupe never re-ingests it, null out heavy columns
//             (no bloat), and DETACH DELETE the (:Item) node in Neo4j.
//     digest: hard delete (no dedupe concern).

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Best-effort: a half-failed graph cleanup shouldn't block user curation —
// graph_sync-style repair can reconcile later.
async function neo4jRun(ctx: any, statement: string, parameters: Record<string, unknown>): Promise<void> {
  try {
    const auth = btoa(`${ctx.env.NEO4J_USERNAME}:${ctx.env.NEO4J_PASSWORD}`);
    const res = await fetch(`${ctx.env.NEO4J_HTTP_URL}/db/neo4j/query/v2`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Basic ${auth}` },
      body: JSON.stringify({ statement, parameters }),
    });
    if (!res.ok) console.error("neo4j run failed", res.status, (await res.text()).slice(0, 200));
  } catch (e) {
    console.error("neo4j unreachable during delete:", String(e).slice(0, 200));
  }
}

const ALLOWED = new Set(["visited_link", "asked_about", "item_feedback", "digest_feedback", "deleted", "snooze"]);
// snooze: {context:{contact_id, handle?, days: 1|3|7|30|'forever'|0}} — mutes a
// poster. 'forever' (ads/spam) pins snoozed_until to year 9999; 0 unsnoozes.
// Enforced in three places: UI queries filter it out, the digest ignores it,
// and the scraper preloads snoozed handles and skips them at capture time.

export default async function handler(req: Request, ctx: any): Promise<Response> {
  if (!ctx.user) return json({ error: "unauthorized" }, 401);
  const body = await req.json().catch(() => ({}));
  const { action, item_id = null, context = {} } = body;
  if (!ALLOWED.has(action)) return json({ error: `action must be one of ${[...ALLOWED].join(", ")}` }, 400);

  if (action === "item_feedback") {
    if (!item_id || !context?.verdict) return json({ error: "item_id and context.verdict required" }, 400);
    await ctx.db.query(`INSERT INTO preferences (key, value, context) VALUES ('item_feedback', $1, $2)`, [
      JSON.stringify({ item_id, verdict: context.verdict }),
      context.topic ?? null,
    ]);
  }

  if (action === "digest_feedback" && context?.digest_id) {
    await ctx.db.query(`UPDATE digests SET feedback = $1, status = 'seen' WHERE id = $2`, [
      context.verdict ?? "unknown",
      context.digest_id,
    ]);
    await ctx.db.query(`INSERT INTO preferences (key, value, context) VALUES ('digest_feedback', $1, $2)`, [
      JSON.stringify({ digest_id: context.digest_id, verdict: context.verdict }),
      context.headline ?? null,
    ]);
  }

  if (action === "snooze") {
    const { contact_id, days } = context ?? {};
    if (!contact_id || days === undefined) {
      return json({ error: "context.contact_id and context.days (1|3|7|30|'forever'|0) required" }, 400);
    }
    let until: string | null;
    if (days === 0 || days === "0") until = null; // unsnooze
    else if (days === "forever") until = "9999-12-31T00:00:00Z"; // permanent — ads/spam
    else if (Number(days) > 0) until = new Date(Date.now() + Number(days) * 86400000).toISOString();
    else return json({ error: "invalid days" }, 400);
    await ctx.db.query(`UPDATE contacts SET snoozed_until = $1 WHERE id = $2`, [until, contact_id]);
    await ctx.db.query(`INSERT INTO preferences (key, value, context) VALUES ('snooze', $1, $2)`, [
      JSON.stringify({ contact_id, days, until }),
      context.handle ?? null,
    ]);
  }

  if (action === "deleted") {
    if (context?.kind === "digest" && context?.digest_id) {
      await ctx.db.query(`DELETE FROM digests WHERE id = $1`, [context.digest_id]);
    } else if (context?.kind === "item" && item_id) {
      const row = await ctx.db.query(`SELECT platform, external_id FROM items WHERE id = $1`, [item_id]);
      await ctx.db.query(
        `UPDATE items SET deleted_at = now(), structured = NULL, detail = NULL,
                caption_raw = NULL, brief = NULL, media_path = NULL, topic = NULL
          WHERE id = $1`,
        [item_id]
      );
      const r = row.rows?.[0];
      if (r) {
        await neo4jRun(ctx, `MATCH (i:Item {platform: $platform, externalId: $externalId}) DETACH DELETE i`, {
          platform: r.platform,
          externalId: r.external_id,
        });
      }
    } else {
      return json({ error: "deleted needs context.kind 'item' (with item_id) or 'digest' (with context.digest_id)" }, 400);
    }
  }

  const res = await ctx.db.query(
    `INSERT INTO interactions (action, item_id, context) VALUES ($1, $2, $3) RETURNING id`,
    [action, action === "deleted" && context?.kind === "digest" ? null : item_id, JSON.stringify(context)]
  );

  return json({ ok: true, id: res.rows?.[0]?.id });
}
