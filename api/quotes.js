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
module.exports = async (req, res) => {
  const syms = String((req.query && req.query.s) || "").split(",").map(s => s.trim()).filter(Boolean).slice(0, 80);
  const quotes = {};
  for (let i = 0; i < syms.length; i += 20) {
    const part = syms.slice(i, i + 20);
    const got = await Promise.all(part.map(one));
    part.forEach((s, k) => { if (got[k]) quotes[s] = got[k]; });
  }
  res.setHeader("Cache-Control", "public, s-maxage=30, stale-while-revalidate=30");
  res.status(200).json({ at: Math.floor(Date.now() / 1000), quotes });
};
