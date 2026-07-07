// GET — the "hot" digests + a snapshot of recent activity for the UI's
// landing view. Auth: required (end-user JWT).

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export default async function handler(_req: Request, ctx: any): Promise<Response> {
  if (!ctx.user) return json({ error: "unauthorized" }, 401);

  const digests = await ctx.db.query(
    `SELECT id, headline, body, topics, stats, status, source, created_at
       FROM digests ORDER BY created_at DESC LIMIT 5`
  );
  const items = await ctx.db.query(
    `SELECT i.id, i.kind, i.url, i.topic, i.brief, i.posted_at, i.captured_at, c.handle
       FROM items i LEFT JOIN contacts c ON c.id = i.contact_id
      ORDER BY i.captured_at DESC LIMIT 20`
  );
  const shield = await ctx.db.query(
    `SELECT coalesce(sum((stats->>'ads_shielded')::int), 0) AS ads,
            coalesce(sum((stats->>'suggested_shielded')::int), 0) AS suggested,
            count(*) AS runs
       FROM scrape_runs WHERE status = 'completed'`
  );

  return json({
    digests: digests.rows,
    recent_items: items.rows,
    shield: shield.rows?.[0] ?? {},
  });
}
