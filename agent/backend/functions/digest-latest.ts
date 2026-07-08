// GET — the "hot" digests + a snapshot of recent activity for the UI's
// landing view. Auth: required (end-user JWT).

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
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

export default async function handler(_req: Request, ctx: any): Promise<Response> {
  const denied = ownerOnly(ctx);
  if (denied) return denied;

  const digests = await ctx.db.query(
    `SELECT id, headline, body, topics, stats, status, source, created_at
       FROM digests ORDER BY created_at DESC LIMIT 8`
  );
  const items = await ctx.db.query(
    `SELECT i.id, i.kind, i.url, i.topic, i.brief, i.detail, i.posted_at, i.captured_at,
            c.handle, c.id AS contact_id
       FROM items i LEFT JOIN contacts c ON c.id = i.contact_id
      WHERE i.deleted_at IS NULL
        AND (c.snoozed_until IS NULL OR c.snoozed_until < now())
      ORDER BY coalesce(i.posted_at, i.captured_at) DESC LIMIT 60`
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
