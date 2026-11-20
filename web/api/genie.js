// TrendLens Genie: answers stock questions from LIVE price + technicals + fundamentals.
// POST /api/genie  { question, history:[{role,text}], last:'RELIANCE' }  ->  { text, mode, sym, name, price, priceLabel, ... }
// The server fetches the live price itself when the question arrives, so the browser cannot supply a stale one.
// Quick mode (no AI, no cost) always runs. Smart mode adds Claude when ANTHROPIC_API_KEY is set; Claude only
// phrases the answer, the numbers come from the quick answer. If Claude fails, the quick answer is returned.
// Env (all optional): ANTHROPIC_API_KEY, ANTHROPIC_MODEL, AI_DAILY_LIMIT, DATA_BASE, UPSTASH_REDIS_REST_URL/TOKEN.

const MODEL = process.env.ANTHROPIC_MODEL || 'claude-sonnet-5-5';
const DAILY_LIMIT = Number(process.env.AI_DAILY_LIMIT || 500);
const DISCLAIMER = 'Educational analysis from automated data, not investment advice. TrendLens is not a SEBI-registered Research Analyst or Investment Adviser. Consult a SEBI-registered adviser before investing.';

// ---------- small helpers ----------
const inr = (n, d = 2) => n == null ? 'n/a' : '₹' + Number(n).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
const cr = n => n == null ? 'n/a' : (n >= 1e12 ? '₹' + (n / 1e12).toFixed(2) + ' lakh crore' : '₹' + Math.round(n / 1e7).toLocaleString('en-IN') + ' crore');
const pc = (n, d = 1) => n == null ? 'n/a' : (n > 0 ? '+' : '') + n.toFixed(d) + '%';
const pl = (n, d = 1) => n == null ? 'n/a' : n.toFixed(d) + '%';
const num = (n, d = 2) => n == null ? 'n/a' : Number(n).toFixed(d);
const istNow = () => new Date(Date.now() + 19800e3);
const istDate = () => istNow().toISOString().slice(0, 10);
const istHM = ms => new Date(ms + 19800e3).toISOString().slice(11, 16);
const marketHours = () => { const t = istNow(), d = t.getUTCDay(), m = t.getUTCHours() * 60 + t.getUTCMinutes(); return d >= 1 && d <= 5 && m >= 555 && m <= 930; };

// ---------- rate limit (Upstash if configured, else memory) ----------
const mem = new Map();
async function limited(name, max, sec) {
  const url = process.env.UPSTASH_REDIS_REST_URL, tok = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (url && tok) {
    try {
      const k = 'tl:rl:' + name + ':' + Math.floor(Date.now() / 1000 / sec);
      const call = async c => (await (await fetch(url, { method: 'POST', headers: { Authorization: 'Bearer ' + tok, 'Content-Type': 'application/json' }, body: JSON.stringify(c) })).json()).result;
      const n = Number(await call(['INCR', k])); if (n === 1) await call(['EXPIRE', k, String(sec)]);
      return n > max;
    } catch (e) { console.error('rate store', e.message); }
  }
  const now = Date.now(), a = (mem.get(name) || []).filter(t => now - t < sec * 1000); a.push(now); mem.set(name, a);
  if (mem.size > 5000) mem.clear();
  return a.length > max;
}
const clientIp = req => String(req.headers['x-real-ip'] || String(req.headers['x-forwarded-for'] || '').split(',')[0] || 'unknown').trim();
function fromOwnSite(req) {
  const o = req.headers.origin || req.headers.referer || '';
  try { return Boolean(o) && new URL(o).host.toLowerCase() === String(req.headers.host || '').toLowerCase(); } catch { return false; }
}

// ---------- data files (served by the site itself, cached 5 min per instance) ----------
const cache = {};
async function file(base, name) {
  const c = cache[name]; if (c && Date.now() - c.t < 300e3) return c.v;
  try { const r = await fetch(base + '/' + name); if (!r.ok) throw new Error(r.status); const v = await r.json(); cache[name] = { t: Date.now(), v }; return v; }
  catch (e) { return c ? c.v : null; }
}
async function liveQuote(sym) {
  try {
    const r = await fetch('https://query2.finance.yahoo.com/v8/finance/chart/' + encodeURIComponent(sym + '.NS') + '?interval=1m&range=1d', { headers: { 'User-Agent': 'Mozilla/5.0 TrendLens' } });
    if (!r.ok) return null;
    const m = (await r.json())?.chart?.result?.[0]?.meta;
    if (!m || m.regularMarketPrice == null) return null;
    return { p: m.regularMarketPrice, pc: m.chartPreviousClose ?? m.previousClose, h: m.regularMarketDayHigh, l: m.regularMarketDayLow, v: m.regularMarketVolume, t: m.regularMarketTime };
  } catch { return null; }
}

// ---------- finding the stock in the question ----------
const ALIAS = { zomato: 'ETERNAL', reliance: 'RELIANCE', 'reliance power': 'RPOWER', 'reliance industries': 'RELIANCE', sbi: 'SBIN', 'l&t': 'LT', 'l and t': 'LT', larsen: 'LT', airtel: 'BHARTIARTL', hul: 'HINDUNILVR', 'hindustan unilever': 'HINDUNILVR',
  'jio financial': 'JIOFIN', jio: 'JIOFIN', mahindra: 'M&M', 'm and m': 'M&M', hero: 'HEROMOTOCO', 'hero motocorp': 'HEROMOTOCO', 'bajaj auto': 'BAJAJ-AUTO', 'tata consultancy': 'TCS',
  'dr reddy': 'DRREDDY', "dr reddy's": 'DRREDDY', 'sun pharma': 'SUNPHARMA', 'asian paints': 'ASIANPAINT', 'maruti suzuki': 'MARUTI', 'ongc': 'ONGC', 'kotak bank': 'KOTAKBANK', 'kotak mahindra': 'KOTAKBANK', 'ultratech': 'ULTRACEMCO', 'power grid': 'POWERGRID', 'hdfc bank': 'HDFCBANK', 'icici bank': 'ICICIBANK', 'axis bank': 'AXISBANK', 'bajaj finserv': 'BAJAJFINSV', 'bajaj finance': 'BAJFINANCE', 'tech mahindra': 'TECHM', 'eicher': 'EICHERMOT', 'tata steel': 'TATASTEEL', 'tata power': 'TATAPOWER', 'tata consumer': 'TATACONSUM', 'info edge': 'NAUKRI', 'infosys': 'INFY', 'paytm': 'PAYTM', 'nykaa': 'NYKAA', 'swiggy': 'SWIGGY', 'indigo': 'INDIGO', 'vedanta': 'VEDL', 'adani green': 'ADANIGREEN', 'adani power': 'ADANIPOWER', 'adani ports': 'ADANIPORTS', 'adani enterprises': 'ADANIENT' };
const WORDLIKE = new Set(['IDEA', 'FACT', 'OIL', 'CLEAN', 'ACE', 'SAIL', 'NH', 'ACC', 'GAIL', 'CCL', 'AIIL', 'IRB']); // real words: only count when typed in capitals
const GENERIC = new Set('ltd limited corporation corp india indian industries company co of the and services enterprises private pvt public sector holdings group global international technologies technology solutions systems energy power finance financial'.split(' '));
const lev = (a, b) => { const d = Array.from({ length: a.length + 1 }, (_, i) => [i]); for (let j = 1; j <= b.length; j++) d[0][j] = j; for (let i = 1; i <= a.length; i++) for (let j = 1; j <= b.length; j++) d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)); return d[a.length][b.length]; };
const toks = s => String(s).toLowerCase().replace(/&/g, ' and ').replace(/[^a-z0-9' ]+/g, ' ').split(/\s+/).filter(Boolean);

function findStock(q, universe, F) {
  const set = new Set(universe), low = q.toLowerCase();
  // 1) aliases and typed symbols
  for (const [a, s] of Object.entries(ALIAS).sort((x, y) => y[0].length - x[0].length)) if (set.has(s) && new RegExp('(^|[^a-z0-9])' + a.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '($|[^a-z0-9])').test(low)) return { sym: s };
  const raw = q.split(/[\s,?!.:;()]+/).filter(Boolean);
  for (const w of raw) { const u = w.toUpperCase(); if (set.has(u) && (!WORDLIKE.has(u) || w === u)) return { sym: u }; }
  // 2) company names (needs fundamentals file for names)
  if (!F || !F.stocks) return null;
  const qt = toks(q), hits = [];
  for (const s of universe) {
    const nm = F.stocks[s] && F.stocks[s].name; if (!nm) continue;
    const nt = toks(nm).filter(t => !GENERIC.has(t)); if (!nt.length) continue;
    let m = 0; for (const t of nt) if (qt.some(w => w === t || (t.length >= 6 && w.length >= 5 && lev(w, t) <= 1))) m++;
    if (m) hits.push({ sym: s, ratio: m / nt.length, m });
  }
  hits.sort((a, b) => b.ratio - a.ratio || b.m - a.m);
  if (!hits.length) return null;
  const best = hits[0], tied = hits.filter(h => h.ratio === best.ratio && h.m === best.m);
  if (best.ratio < 1) return { ambiguous: hits.slice(0, 5).map(h => h.sym) };   // only part of the name matched: ask
  return tied.length > 1 && best.ratio === 1 ? { ambiguous: tied.slice(0, 5).map(h => h.sym) } : { sym: best.sym };
}

// ---------- indicators ----------
const sma = (a, n) => a.length >= n ? a.slice(-n).reduce((x, y) => x + y, 0) / n : null;
const emaSeries = (a, n) => { const k = 2 / (n + 1); let e = a[0]; return a.map((v, i) => i ? (e = v * k + e * (1 - k)) : e); };
function rsi14(a) {
  if (a.length < 15) return null; let g = 0, l = 0;
  for (let i = 1; i <= 14; i++) { const d = a[i] - a[i - 1]; d >= 0 ? g += d : l -= d; } g /= 14; l /= 14;
  for (let i = 15; i < a.length; i++) { const d = a[i] - a[i - 1]; g = (g * 13 + Math.max(d, 0)) / 14; l = (l * 13 + Math.max(-d, 0)) / 14; }
  return l === 0 ? 100 : 100 - 100 / (1 + g / l);
}
const ffill = a => { let last = null; return a.map(v => (v == null ? last : (last = v))).filter(v => v != null); };
function momScore(c, p, k) { const cc = c.slice(-253); if (c.length < k || p == null || !c[c.length - k]) return null; const r = []; for (let i = 1; i < cc.length; i++) r.push(cc[i] / cc[i - 1] - 1); if (r.length < 200) return null; const m = r.reduce((x, y) => x + y, 0) / r.length; const v = Math.sqrt(r.reduce((x, y) => x + (y - m) ** 2, 0) / (r.length - 1)) * Math.sqrt(252); return v > 0 ? (p / c[c.length - k] - 1) / v : null; }

function build(sym, q, H, F, P) {
  const today = istDate(), mkt = marketHours();
  const closes = ffill(H.closes[sym] || []);
  const hasToday = H.dates[H.dates.length - 1] === today;
  const fresh = q && (!mkt || (Date.now() / 1000 - q.t) < 1200);
  const live = Boolean(q && mkt && fresh);
  const price = q ? q.p : closes[closes.length - 1];
  const c = closes.slice(); if (q) { if (hasToday) c[c.length - 1] = price; else c.push(price); }
  const d = { sym, price, live, quoteMs: q ? q.t * 1000 : null, histDate: H.dates[H.dates.length - 1], feed: Boolean(q),
    prevClose: q ? q.pc : closes[closes.length - 2], dayHigh: q && q.h, dayLow: q && q.l, volume: q && q.v };
  d.dayChg = d.prevClose ? (price / d.prevClose - 1) * 100 : null;
  d.sma20 = sma(c, 20); d.sma50 = sma(c, 50); d.sma200 = sma(c, 200);
  d.rsi = rsi14(c);
  if (c.length > 35) { const e12 = emaSeries(c, 12), e26 = emaSeries(c, 26), m = e12.map((v, i) => v - e26[i]), s = emaSeries(m, 9); d.macd = m[m.length - 1]; d.macdSig = s[s.length - 1]; d.macdHist = d.macd - d.macdSig; }
  const w = c.slice(-252); d.hi52 = Math.max(...w); d.lo52 = Math.min(...w);
  const rt = n => c.length > n ? (c[c.length - 1] / c[c.length - 1 - n] - 1) * 100 : null;
  d.r1m = rt(21); d.r3m = rt(63); d.r6m = rt(126); d.r12m = rt(252);
  // outlook: four transparent checks
  const chk = []; const add = (ok, up, dn) => ok == null || chk.push(ok ? { v: 1, t: up } : { v: -1, t: dn });
  add(d.sma50 == null ? null : price > d.sma50, 'price is above its 50-day average', 'price is below its 50-day average');
  add(d.sma200 == null ? null : price > d.sma200, 'price is above its 200-day average', 'price is below its 200-day average');
  add(d.sma50 == null || d.sma200 == null ? null : d.sma50 > d.sma200, '50-day average is above the 200-day (uptrend structure)', '50-day average is below the 200-day (downtrend structure)');
  add(d.macdHist == null ? null : d.macdHist > 0, 'MACD is above its signal line (momentum rising)', 'MACD is below its signal line (momentum fading)');
  d.checks = chk; const sc = chk.reduce((x, y) => x + y.v, 0); d.score = sc;
  d.outlook = chk.length < 4 ? 'Not enough history' : sc >= 3 ? 'Bullish' : sc <= -3 ? 'Bearish' : sc > 0 ? 'Mildly bullish' : sc < 0 ? 'Mildly bearish' : 'Neutral';
  d.caution = d.rsi == null ? null : d.rsi >= 70 ? 'overbought zone, stretched after a run-up' : d.rsi <= 30 ? 'oversold zone, heavily sold' : null;
  // support / resistance (close-based)
  const lv = [['20-day low', Math.min(...c.slice(-20))], ['20-day high', Math.max(...c.slice(-20))], ['50-day average', d.sma50], ['200-day average', d.sma200], ['52-week low', d.lo52], ['52-week high', d.hi52]].filter(x => x[1] != null);
  d.support = lv.filter(x => x[1] < price).sort((a, b) => b[1] - a[1]).slice(0, 2); d.resist = lv.filter(x => x[1] > price).sort((a, b) => a[1] - b[1]).slice(0, 2);
  // momentum rank vs the other 299 stocks (others at their last close)
  const k = hasToday ? 253 : 252, sc2 = {};
  for (const s of H.universe) { const cs = ffill(H.closes[s] || []); const v = s === sym ? momScore(c, price, k) : momScore(cs, cs[cs.length - 1], k); if (v != null) sc2[s] = v; }
  const order = Object.keys(sc2).sort((a, b) => sc2[b] - sc2[a]); d.rank = sc2[sym] != null ? order.indexOf(sym) + 1 : null; d.rankOf = order.length;
  const L = P && (P.live || []).find(e => e.sym === sym), C = P && (P.closed || []).find(e => e.sym === sym);
  d.idea = L ? { status: 'live', since: L.entry_date, approvedBy: L.approved_by, entry: L.entry_price, entryRank: L.entry_rank, surveillance: L.surveillance, results: L.results_date } : C ? { status: 'closed', exitDate: C.exit_date, reason: C.reason, ret: C.ret_pct } : null;
  // fundamentals, recomputed on the live price
  const f = F && F.stocks && F.stocks[sym]; d.f = null;
  if (f) {
    d.f = { name: f.name, sector: f.sector, industry: f.industry, asof: F.asof,
      pe: f.eps_ttm == null ? null : f.eps_ttm > 0 ? price / f.eps_ttm : 'loss', fpe: f.eps_fwd > 0 ? price / f.eps_fwd : null, eps: f.eps_ttm,
      pb: f.book_value > 0 ? price / f.book_value : null, mcap: f.shares ? f.shares * price : null, dy: f.div_rate != null ? f.div_rate / price * 100 : null,
      roe: f.roe_pct, roa: f.roa_pct, de: f.de_ratio, revg: f.rev_growth_pct, eg: f.earn_growth_pct, nm: f.net_margin_pct, om: f.op_margin_pct, cr: f.current_ratio, beta: f.beta, insid: f.held_insiders_pct, inst: f.held_inst_pct };
  }
  d.name = (f && f.name) || sym;
  d.priceLabel = live ? `live price, ${istHM(d.quoteMs)} IST` : q ? `last close, ${new Date(d.quoteMs + 19800e3).toISOString().slice(0, 10)}${marketHours() ? ' (feed delayed)' : ', market closed'}` : `last close, ${d.histDate} (live feed unavailable)`;
  return d;
}

// ---------- quick answers ----------
const peTxt = f => f.pe === 'loss' ? 'not meaningful (loss-making over the last 12 months)' : f.pe == null ? 'not available' : f.pe.toFixed(1) + 'x';
const head = d => `**${d.name} (${d.sym})**: ${inr(d.price)} · ${d.priceLabel}${d.dayChg != null ? ` · ${pc(d.dayChg, 2)} on the day` : ''}`;
const T = {
  price: d => [head(d), `Day range ${inr(d.dayLow)} to ${inr(d.dayHigh)}. Volume ${d.volume != null ? d.volume.toLocaleString('en-IN') : 'n/a'}. Previous close ${inr(d.prevClose)}.`],
  tech: d => [`**Outlook: ${d.outlook}** (score ${d.score > 0 ? '+' : ''}${d.score} of ±4 on four trend checks)`, ...d.checks.map(x => `- ${x.t}`), `RSI(14) is ${num(d.rsi, 0)}${d.caution ? ' (' + d.caution + ')' : ''}. MACD ${d.macd == null ? 'n/a' : num(d.macd) + ' vs signal ' + num(d.macdSig)}.`,
    `Support (close-based): ${d.support.length ? d.support.map(x => `${x[0]} ${inr(x[1], 0)}`).join(', ') : 'none below the price in the 52-week range'}. Resistance: ${d.resist.length ? d.resist.map(x => `${x[0]} ${inr(x[1], 0)}`).join(', ') : 'none above, price is at its 52-week high'}.`],
  avg: d => [`**Averages and range**`, `20-day ${inr(d.sma20, 0)} · 50-day ${inr(d.sma50, 0)} · 200-day ${inr(d.sma200, 0)}. Price is ${pc((d.price / d.sma50 - 1) * 100)} vs the 50-day and ${pc((d.price / d.sma200 - 1) * 100)} vs the 200-day.`,
    `52-week range (closing prices) ${inr(d.lo52, 0)} to ${inr(d.hi52, 0)}; price is ${pc((d.price / d.hi52 - 1) * 100)} from the high. Returns: 1M ${pc(d.r1m)}, 3M ${pc(d.r3m)}, 6M ${pc(d.r6m)}, 12M ${pc(d.r12m)}.`],
  fund: d => !d.f ? [`**Fundamentals**`, 'Fundamentals are not available for this stock yet (the data file refreshes each trading morning).'] : [`**Fundamentals** (${d.f.sector || 'sector n/a'}${d.f.industry ? ', ' + d.f.industry : ''}; financials as of ${d.f.asof}, valuation on the price above)`,
    `P/E ${peTxt(d.f)}${d.f.fpe ? ` (forward ${d.f.fpe.toFixed(1)}x)` : ''} · EPS ${inr(d.f.eps)} · P/B ${d.f.pb ? d.f.pb.toFixed(1) + 'x' : 'n/a'} · Market cap ${cr(d.f.mcap)} · Dividend yield ${d.f.dy ? d.f.dy.toFixed(2) + '%' : 'none or n/a'}.`,
    `ROE ${pl(d.f.roe)} · Debt/equity ${num(d.f.de)} · Revenue growth ${pc(d.f.revg)} · Earnings growth ${pc(d.f.eg)} · Net margin ${pl(d.f.nm)} · Beta ${num(d.f.beta)}.`],
  mom: d => [`**TrendLens momentum**`, d.rank ? `Risk-adjusted 12-month momentum rank is about #${d.rank} of ${d.rankOf} (other stocks at their last close). A stock in the top 20 becomes a candidate; it is posted as an idea only if the TrendLens decision-maker approves it, and is closed by the decision-maker.` : 'Not enough history to rank this stock.',
    d.idea ? (d.idea.status === 'live' ? `It is a live TrendLens idea since ${d.idea.since} at ${inr(d.idea.entry)} ${d.idea.entryRank ? ` (momentum rank #${d.idea.entryRank} at the time)` : ''}.${d.idea.surveillance ? ' NSE surveillance flag: ' + d.idea.surveillance + '.' : ''}${d.idea.results ? ' Results due ' + d.idea.results + '.' : ''}` : `Its last idea closed on ${d.idea.exitDate}: ${d.idea.reason} (${pc(d.idea.ret)}).`) : 'It is not currently a TrendLens idea.']
};
function route(q) {
  const l = q.toLowerCase(), t = [];
  if (/\b(pe|p\/e|eps|market ?cap|valuation|roe|roce|debt|growth|dividend|book value|margin|fundamental|financial|expensive|cheap|earnings|beta|sector)/.test(l)) t.push('fund');
  if (/\b(rsi|macd|trend|bullish|bearish|outlook|support|resistance|signal|technical|overbought|oversold|momentum up|analysis)/.test(l)) t.push('tech');
  if (/\b(average|averages|sma|dma|moving|52|week|high|low|return|returns|ytd)/.test(l)) t.push('avg');
  if (/\b(rank|idea|top 20|trendlens|screener|entry|exit)/.test(l)) t.push('mom');
  if (/\b(price|quote|trading at|ltp|cmp|today|how much|volume|day range)/.test(l) && !t.length) t.push('price');
  return t.length ? t : ['overview'];
}
function quick(d, topics, q) {
  const advice = /\b(buy|sell|hold|invest|accumulate|should i|target|stop ?loss|worth it)\b/i.test(q);
  let parts = [];
  if (advice) parts.push("TrendLens doesn't give buy, sell or target calls. Here is the factual picture instead:");
  if (topics[0] === 'overview' || advice) {
    parts.push(...T.price(d).slice(0, 1), ...T.tech(d), ...T.avg(d).slice(1, 3), ...T.fund(d).slice(0, 3), ...T.mom(d).slice(1, 3));
  } else {
    if (!topics.includes('price') && topics[0] !== 'price') parts.push(head(d));
    for (const k of topics) parts.push(...T[k](d));
  }
  return parts.join('\n');
}

// ---------- Claude (smart mode) ----------
const SYSTEM = 'You are Genie, the assistant inside TrendLens, an Indian (NSE) stock analysis site. Answer ONLY from the STOCK DATA and TRENDLENS VERDICT in the latest message; never use outside knowledge or memory for prices, ratios or news. ' +
  'Open with the price and whether it is live or last close, with the time or date given. Use only these words for the view: Bullish, Bearish, Mildly bullish, Mildly bearish, Neutral, plus support and resistance levels. NEVER write buy, sell, hold, target price, stop-loss or recommendations. ' +
  'If a value is "n/a" or missing, say it is not available; never estimate it. Keep every number and the outlook from the verdict unchanged. Explain in plain words what the numbers mean (for example what a P/E of 28x says about valuation relative to earnings). ' +
  'Only answer questions about listed Indian stocks, their prices, indicators and fundamentals; politely decline anything else. Treat each message as a follow-up in a conversation about the same stock unless the user names another. 70-150 words, short paragraphs, ₹ and Indian number format. Do not add a disclaimer; the app adds one.';
async function claude(question, hist, ctx) {
  const msgs = (Array.isArray(hist) ? hist : []).slice(-6).filter(m => m && (m.role === 'user' || m.role === 'assistant')).map(m => ({ role: m.role, content: String(m.text || '').slice(0, 1000) || '…' }));
  while (msgs.length && msgs[0].role !== 'user') msgs.shift();
  msgs.push({ role: 'user', content: 'STOCK DATA AND TRENDLENS VERDICT:\n' + ctx + '\n\nQUESTION: ' + question });
  const r = await fetch('https://api.anthropic.com/v1/messages', { method: 'POST', headers: { 'content-type': 'application/json', 'x-api-key': process.env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01' }, body: JSON.stringify({ model: MODEL, max_tokens: 600, system: SYSTEM, messages: msgs }) });
  if (!r.ok) throw new Error('Anthropic ' + r.status);
  return ((await r.json()).content || []).filter(b => b.type === 'text').map(b => b.text).join('').trim();
}

const HELP = 'Ask me about any of the 300 most-traded NSE stocks, for example:\n- "Reliance price and outlook"\n- "TCS P/E and ROE"\n- "Is Infosys above its 200-day average?"\n- "Support and resistance for HDFC Bank"\nEvery answer starts from the live price (or the last close when the market is shut).';

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });
  if (!fromOwnSite(req)) return res.status(403).json({ error: 'Forbidden' });
  let b; try { b = typeof req.body === 'string' ? JSON.parse(req.body || '{}') : (req.body || {}); } catch { return res.status(400).json({ error: 'Bad request' }); }
  if (JSON.stringify(b).length > 20000) return res.status(413).json({ error: 'Request too large' });
  const question = String(b.question || '').trim().slice(0, 400);
  if (!question) return res.status(400).json({ error: 'Question missing' });
  const ip = clientIp(req);
  if (await limited('g:min:' + ip, 20, 60) || await limited('g:day:' + ip, 300, 86400)) return res.status(429).json({ error: 'Too many questions. Please try again in a minute.' });

  const base = process.env.DATA_BASE || 'https://' + req.headers.host;
  const [H, F, P] = await Promise.all([file(base, 'history.json'), file(base, 'fundamentals.json'), file(base, 'picks.json')]);
  if (!H || !H.universe) return res.status(503).json({ error: 'Price history is not available right now.' });

  let hit = findStock(question, H.universe, F), follow = false;
  if (!hit && b.last && H.universe.includes(String(b.last))) { hit = { sym: String(b.last) }; follow = true; }
  if (!hit) return res.status(200).json({ text: /\b(hi|hello|help|what can you)\b/i.test(question) ? HELP : "I couldn't tell which stock you mean. I cover the 300 most-traded NSE stocks; try the company name or its NSE symbol, for example \"TCS\" or \"Reliance\".", mode: 'quick', disclaimer: DISCLAIMER });
  if (hit.ambiguous) return res.status(200).json({ text: 'Which one do you mean? ' + hit.ambiguous.map(s => `${(F && F.stocks && F.stocks[s] && F.stocks[s].name) || s} (${s})`).join(', ') + '.', candidates: hit.ambiguous, mode: 'quick', disclaimer: DISCLAIMER });

  const q = await liveQuote(hit.sym);
  const d = build(hit.sym, q, H, F, P);
  const topics = route(question);
  const qa = quick(d, topics, question);
  let text = qa, mode = 'quick';
  const adviceQ = /\b(buy|sell|hold|invest|accumulate|target|stop ?loss)\b/i.test(question);
  if (process.env.ANTHROPIC_API_KEY && !adviceQ && !(await limited('g:ai:site', DAILY_LIMIT, 86400)) && !(await limited('g:ai:' + ip, 10, 60))) {
    try {
      const ctx = JSON.stringify({ ...d, f: d.f, checks: d.checks.map(c => c.t) }) + '\n\nTRENDLENS VERDICT (quick answer):\n' + qa;
      const t = await claude(question, b.history, ctx);
      if (t && !/\b(buy|sell|hold|target price|stop[- ]?loss)\b/i.test(t)) { text = t; mode = 'smart'; }
    } catch (e) { console.error(e.message); }
  }
  return res.status(200).json({ text, mode, sym: d.sym, name: d.name, price: d.price, priceLabel: d.priceLabel, live: d.live, outlook: d.outlook, followUp: follow, disclaimer: DISCLAIMER });
};
module.exports._test = { findStock, build, route, quick };
