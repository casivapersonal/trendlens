// Vercel serverless function: live NSE quotes for the TrendLens page.
// GET /api/quotes?s=RELIANCE,TCS,NIFTY%2050  ->  { at, quotes: { RELIANCE: {p, pc, h, l, v, t}, ... } }
// Source: Yahoo Finance chart endpoint (research use). Cached ~30s at Vercel's edge so many viewers = few upstream calls.
const MAP = { "NIFTY 50": "^NSEI", "NIFTY BANK": "^NSEBANK" };
async function one(sym) {
  const y = MAP[sym] || sym + ".NS";
  try {
    const r = await fetch(`https://query2.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(y)}?interval=1m&range=1d`,
      { headers: { "User-Agent": "Mozilla/5.0 TrendLens" } });
    if (!r.ok) return null;
    const m = (await r.json())?.chart?.result?.[0]?.meta;
    if (!m || m.regularMarketPrice == null) return null;
    return { p: m.regularMarketPrice, pc: m.chartPreviousClose ?? m.previousClose, h: m.regularMarketDayHigh,
             l: m.regularMarketDayLow, v: m.regularMarketVolume, t: m.regularMarketTime };
  } catch (e) { return null; }
}
// Security (Nov 2026 review): GET only, symbols checked against a strict pattern, at most 60 per call, rate limited per visitor.
const mem = new Map();
function limited(ip, max, sec) {
  const now = Date.now(), a = (mem.get(ip) || []).filter(t => now - t < sec * 1000); a.push(now); mem.set(ip, a);
  if (mem.size > 5000) mem.clear();
  return a.length > max;
}
const SYM = /^[A-Z0-9&.\- ]{1,20}$/;
module.exports = async (req, res) => {
  if (req.method !== "GET") return res.status(405).json({ error: "GET only" });
  const ip = String(req.headers["x-real-ip"] || String(req.headers["x-forwarded-for"] || "").split(",")[0] || "unknown").trim();
  if (limited(ip, 30, 60)) return res.status(429).json({ error: "too many requests" });
  const syms = [...new Set(String((req.query && req.query.s) || "").slice(0, 2000).split(",").map(s => s.trim().toUpperCase()).filter(s => SYM.test(s)))].slice(0, 60);
  const quotes = {};
  for (let i = 0; i < syms.length; i += 20) {
    const part = syms.slice(i, i + 20);
    const got = await Promise.all(part.map(one));
    part.forEach((s, k) => { if (got[k]) quotes[s] = got[k]; });
  }
  res.setHeader("Cache-Control", "public, s-maxage=30, stale-while-revalidate=30");
  res.status(200).json({ at: Math.floor(Date.now() / 1000), quotes });
};
