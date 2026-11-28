// TrendLens trial sign-in (Nov 2026). POST {password} -> sets the tl_site cookie (180 days) checked by middleware.js.
// Env (Vercel, mark Sensitive): SITE_PASSWORD, SITE_SECRET (32+ random characters). Wrong guesses: 5 per 15 minutes per visitor.
const crypto = require("crypto");

const mem = new Map();
async function limited(name, max, sec) {
  const url = process.env.UPSTASH_REDIS_REST_URL, tok = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (url && tok) {
    try {
      const k = "tl:rl:" + name + ":" + Math.floor(Date.now() / 1000 / sec);
      const call = async c => (await (await fetch(url, { method: "POST", headers: { Authorization: "Bearer " + tok, "Content-Type": "application/json" }, body: JSON.stringify(c) })).json()).result;
      const n = Number(await call(["INCR", k])); if (n === 1) await call(["EXPIRE", k, String(sec)]);
      return n > max;
    } catch (e) { console.error("rate store", e.message); }
  }
  const now = Date.now(), a = (mem.get(name) || []).filter(t => now - t < sec * 1000); a.push(now); mem.set(name, a);
  if (mem.size > 5000) mem.clear();
  return a.length > max;
}
const clientIp = req => String(req.headers["x-real-ip"] || String(req.headers["x-forwarded-for"] || "").split(",")[0] || "unknown").trim();
function fromOwnSite(req) {
  const o = req.headers.origin || req.headers.referer || "";
  try { return Boolean(o) && new URL(o).host.toLowerCase() === String(req.headers.host || "").toLowerCase(); } catch { return false; }
}
const DAYS = 180;
module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  const pw = process.env.SITE_PASSWORD, secret = String(process.env.SITE_SECRET || "");
  if (req.method === "DELETE") {             // sign out
    res.setHeader("Set-Cookie", "tl_site=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax");
    return res.status(200).json({ ok: true });
  }
  if (req.method !== "POST") return res.status(405).json({ error: "method not allowed" });
  if (!pw || secret.length < 32) return res.status(503).json({ error: "the trial lock is not set up" });
  if (!fromOwnSite(req)) return res.status(403).json({ error: "forbidden" });
  if (await limited("site:" + clientIp(req), 5, 900)) return res.status(429).json({ error: "Too many attempts. Try again in 15 minutes." });
  let given;
  try {
    const raw = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
    if (raw.length > 600) return res.status(413).json({ error: "too large" });
    given = JSON.parse(raw).password;
  } catch { return res.status(400).json({ error: "bad request" }); }
  if (typeof given !== "string" || !given || given.length > 200) return res.status(400).json({ error: "bad request" });
  const a = crypto.createHash("sha256").update(given).digest(), b = crypto.createHash("sha256").update(pw).digest();
  if (!crypto.timingSafeEqual(a, b)) { await new Promise(r => setTimeout(r, 600)); return res.status(401).json({ error: "Wrong password." }); }
  const exp = Date.now() + DAYS * 864e5;
  const sig = crypto.createHmac("sha256", secret).update("tl-site." + exp).digest("base64url");
  res.setHeader("Set-Cookie", `tl_site=${exp}.${sig}; Path=/; Max-Age=${DAYS * 86400}; HttpOnly; Secure; SameSite=Lax`);
  return res.status(200).json({ ok: true, days: DAYS });
};
