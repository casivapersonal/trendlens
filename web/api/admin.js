// TrendLens admin login (Nov 2026). Only the admin sees the full reasons and notes behind each idea on the site.
// POST /api/admin {password} -> {token, exp}   (token lasts 8 hours)
// GET  /api/admin  with "Authorization: Bearer <token>" -> 200 {ok:true} or 401
// Env, set in Vercel (mark both as Sensitive):
//   ADMIN_PASSWORD_HASH  "pbkdf2$<iterations>$<salt base64>$<hash base64>"  (made on the site: Admin -> Set up, in your own browser)
//   ADMIN_SECRET         a random string of at least 32 characters (signs the login tokens)
// The password itself is never stored or sent anywhere except in the login request over HTTPS.
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
const configured = () => Boolean(process.env.ADMIN_PASSWORD_HASH) && String(process.env.ADMIN_SECRET || "").length >= 32;
const sign = exp => crypto.createHmac("sha256", process.env.ADMIN_SECRET).update("tl-admin." + exp).digest("base64url");
function tokenOk(t) {
  if (!configured() || typeof t !== "string" || t.length > 200) return false;
  const [e, s] = t.split("."), exp = Number(e);
  if (!Number.isFinite(exp) || exp < Date.now() || exp > Date.now() + 9 * 3600e3) return false;
  const a = Buffer.from(sign(exp)), b = Buffer.from(String(s || ""));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
function passwordOk(pw) {
  const m = String(process.env.ADMIN_PASSWORD_HASH || "").trim().split("$");
  if (m.length !== 4 || m[0] !== "pbkdf2") return false;
  const it = Number(m[1]);
  if (!Number.isInteger(it) || it < 100000 || it > 5000000) return false;
  const salt = Buffer.from(m[2], "base64"), want = Buffer.from(m[3], "base64");
  if (salt.length < 16 || want.length < 32) return false;
  const got = crypto.pbkdf2Sync(Buffer.from(pw, "utf8"), salt, it, want.length, "sha256");
  return crypto.timingSafeEqual(got, want);
}
module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "GET" && req.method !== "POST") return res.status(405).json({ error: "method not allowed" });
  if (!configured()) return res.status(503).json({ error: "admin login is not set up" });
  if (req.method === "GET") {
    const ok = tokenOk(String(req.headers.authorization || "").replace(/^Bearer\s+/i, ""));
    return res.status(ok ? 200 : 401).json({ ok });
  }
  if (!fromOwnSite(req)) return res.status(403).json({ error: "forbidden" });
  if (await limited("admin:" + clientIp(req), 5, 900)) return res.status(429).json({ error: "too many attempts, try again in 15 minutes" });
  if (await limited("admin:all", 50, 3600)) return res.status(429).json({ error: "too many attempts, try again later" });
  let pw;
  try {
    const raw = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
    if (raw.length > 600) return res.status(413).json({ error: "too large" });
    pw = JSON.parse(raw).password;
  } catch { return res.status(400).json({ error: "bad request" }); }
  if (typeof pw !== "string" || pw.length < 1 || pw.length > 200) return res.status(400).json({ error: "bad request" });
  if (!passwordOk(pw)) { await new Promise(r => setTimeout(r, 600)); return res.status(401).json({ error: "wrong password" }); }
  const exp = Date.now() + 8 * 3600e3;
  return res.status(200).json({ token: exp + "." + sign(exp), exp });
};
