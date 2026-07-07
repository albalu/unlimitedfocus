// POST {action, item_id?, url?, context?} — records user interactions
// (visited_link, digest_feedback, ...) for the preference-learning loop.
// digest_feedback additionally lands in `preferences` (and stamps the digest),
// which both the chat orchestrator and the RocketRide digest job read later.
// Auth: required (end-user JWT).

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const ALLOWED = new Set(["visited_link", "digest_feedback", "dismissed", "asked_about"]);

export default async function handler(req: Request, ctx: any): Promise<Response> {
  if (!ctx.user) return json({ error: "unauthorized" }, 401);
  const body = await req.json().catch(() => ({}));
  const { action, item_id = null, context = {} } = body;
  if (!ALLOWED.has(action)) return json({ error: `action must be one of ${[...ALLOWED].join(", ")}` }, 400);

  const res = await ctx.db.query(
    `INSERT INTO interactions (action, item_id, context) VALUES ($1, $2, $3) RETURNING id`,
    [action, item_id, JSON.stringify(context)]
  );

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

  return json({ ok: true, id: res.rows?.[0]?.id });
}
