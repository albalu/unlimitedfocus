// POST — ingest a digest produced by the RocketRide pipeline (or the local
// digest runner). Auth: none at the edge, protected by the X-UF-Secret shared
// secret instead — RocketRide is not a Butterbase principal.
//   body: { headline, body?, topics?, stats?, period_start?, period_end?, source? }

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export default async function handler(req: Request, ctx: any): Promise<Response> {
  const secret = req.headers.get("x-uf-secret");
  if (!secret || secret !== ctx.env.UF_INGEST_SECRET) {
    return json({ error: "forbidden" }, 403);
  }
  const b = await req.json().catch(() => ({}));
  if (!b.headline) return json({ error: "headline required" }, 400);

  const res = await ctx.db.query(
    `INSERT INTO digests (headline, body, topics, stats, period_start, period_end, source, status)
     VALUES ($1, $2, $3, $4, $5, $6, $7, 'hot') RETURNING id, created_at`,
    [
      String(b.headline).slice(0, 300),
      b.body ?? null,
      JSON.stringify(b.topics ?? []),
      JSON.stringify(b.stats ?? {}),
      b.period_start ?? null,
      b.period_end ?? null,
      b.source ?? "rocketride",
    ]
  );
  return json({ ok: true, id: res.rows?.[0]?.id });
}
