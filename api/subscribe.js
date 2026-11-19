// Stores browser push subscriptions in Upstash Redis (set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN in Vercel).
async function redis(cmd) {
  const r = await fetch(process.env.UPSTASH_REDIS_REST_URL, {
    method: "POST",
    headers: { Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify(cmd),
  });
  if (!r.ok) throw new Error("redis " + r.status);
  return r.json();
}
module.exports = async (req, res) => {
  if (!process.env.UPSTASH_REDIS_REST_URL) return res.status(503).json({ error: "push not configured" });
  try {
    const sub = typeof req.body === "string" ? JSON.parse(req.body) : req.body;
    if (!sub || !sub.endpoint || !String(sub.endpoint).startsWith("https://")) return res.status(400).json({ error: "bad subscription" });
    const val = JSON.stringify({ endpoint: sub.endpoint, keys: sub.keys });
    if (req.method === "DELETE") await redis(["SREM", "trendlens:subs", val]);
    else if (req.method === "POST") await redis(["SADD", "trendlens:subs", val]);
    else return res.status(405).end();
    res.status(200).json({ ok: true });
  } catch (e) { res.status(500).json({ error: "failed" }); }
};
