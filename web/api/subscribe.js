// Stores browser push subscriptions in Upstash Redis (set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN in Vercel).
// Security (Nov 2026 review): only from this site, only known browser push services, size and shape checked, rate limited,
// and the store is capped, so nobody can fill it with junk or make the notifier call arbitrary servers.
const PUSH_HOSTS = [/^fcm\.googleapis\.com$/, /^updates\.push\.services\.mozilla\.com$/, /^web\.push\.apple\.com$/, /\.notify\.windows\.com$/, /^android\.googleapis\.com$/];
const MAX_SUBS = 5000;
const mem = new Map();
function limited(ip, max, sec) {
  const now = Date.now(), a = (mem.get(ip) || []).filter(t => now - t < sec * 1000); a.push(now); mem.set(ip, a);
  if (mem.size > 5000) mem.clear();
  return a.length > max;
}
function fromOwnSite(req) {
  const o = req.headers.origin || req.headers.referer || "";
  try { return Boolean(o) && new URL(o).host.toLowerCase() === String(req.headers.host || "").toLowerCase(); } catch { return false; }
}
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
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST" && req.method !== "DELETE") return res.status(405).json({ error: "method not allowed" });
  if (!fromOwnSite(req)) return res.status(403).json({ error: "forbidden" });
  const ip = String(req.headers["x-real-ip"] || String(req.headers["x-forwarded-for"] || "").split(",")[0] || "unknown").trim();
  if (limited(ip, 10, 60)) return res.status(429).json({ error: "too many requests" });
  if (!process.env.UPSTASH_REDIS_REST_URL) return res.status(503).json({ error: "push not configured" });
  try {
    const raw = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
    if (raw.length > 2000) return res.status(413).json({ error: "too large" });
    const sub = JSON.parse(raw);
    let u; try { u = new URL(String(sub && sub.endpoint)); } catch { return res.status(400).json({ error: "bad subscription" }); }
    const keys = sub.keys || {};
    const b64 = s => typeof s === "string" && /^[A-Za-z0-9_\-=]{8,200}$/.test(s);
    if (u.protocol !== "https:" || !PUSH_HOSTS.some(p => p.test(u.hostname)) || !b64(keys.p256dh) || !b64(keys.auth))
      return res.status(400).json({ error: "bad subscription" });
    const val = JSON.stringify({ endpoint: u.href, keys: { p256dh: keys.p256dh, auth: keys.auth } });
    if (req.method === "DELETE") await redis(["SREM", "trendlens:subs", val]);
    else {
      const n = Number((await redis(["SCARD", "trendlens:subs"])).result || 0);
      if (n >= MAX_SUBS) return res.status(507).json({ error: "subscriber list is full" });
      await redis(["SADD", "trendlens:subs", val]);
    }
    res.status(200).json({ ok: true });
  } catch (e) { res.status(500).json({ error: "failed" }); }
};
