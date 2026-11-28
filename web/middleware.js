// TrendLens trial lock (Nov 2026). While SITE_PASSWORD is set in Vercel, every page, data file and API needs the trial
// password: the visitor signs in once on /login.html and a signed cookie keeps them in for 180 days.
// Env (Vercel, mark Sensitive): SITE_PASSWORD (the shared trial password), SITE_SECRET (32+ random characters, signs the cookie).
// With SITE_PASSWORD unset the site is open, exactly as before.
export const config = { matcher: "/:path*" };

const OPEN = new Set(["/login.html", "/api/site-login", "/legal.html", "/sw.js", "/manifest.json", "/icon-192.png", "/icon-512.png", "/favicon.ico", "/robots.txt"]);
const enc = new TextEncoder();
const b64url = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
async function sign(secret, msg) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64url(await crypto.subtle.sign("HMAC", key, enc.encode(msg)));
}
function same(a, b) {                       // constant-time string compare
  if (a.length !== b.length) return false;
  let d = 0; for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}
async function cookieOk(req, secret) {
  const m = (req.headers.get("cookie") || "").match(/(?:^|;\s*)tl_site=([^;]+)/);
  if (!m) return false;
  const [exp, sig] = decodeURIComponent(m[1]).split(".");
  const e = Number(exp);
  if (!Number.isFinite(e) || e < Date.now() || !sig) return false;
  return same(await sign(secret, "tl-site." + e), sig);
}
const pass = () => new Response(null, { headers: { "x-middleware-next": "1" } });   // continue to the page (same as next() in @vercel/functions)

export default async function middleware(req) {
  const pw = process.env.SITE_PASSWORD, secret = process.env.SITE_SECRET || "";
  if (!pw) return pass();                                   // not locked
  const url = new URL(req.url), path = url.pathname;
  if (OPEN.has(path)) return pass();
  if (secret.length >= 32) {
    if (req.headers.get("x-tl-internal") && same(req.headers.get("x-tl-internal"), secret)) return pass();   // Genie reading the site's own data files
    if (await cookieOk(req, secret)) return pass();
  }
  if (path.startsWith("/api/") || /\.(json|js|png|ico|txt|csv)$/.test(path))
    return new Response(JSON.stringify({ error: "sign in required" }), { status: 401, headers: { "content-type": "application/json", "cache-control": "no-store" } });
  const next = path + url.search + url.hash;
  return new Response(null, { status: 302, headers: { location: "/login.html?next=" + encodeURIComponent(next), "cache-control": "no-store" } });
}
