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
async function liveQuote(sym, raw) {
  try {
    const r = await fetch('https://query2.finance.yahoo.com/v8/finance/chart/' + encodeURIComponent(raw ? sym : sym + '.NS') + '?interval=1m&range=1d', { headers: { 'User-Agent': 'Mozilla/5.0 TrendLens' }, signal: AbortSignal.timeout(6000) });
    if (!r.ok) return null;
    const m = (await r.json())?.chart?.result?.[0]?.meta;
    if (!m || m.regularMarketPrice == null) return null;
    return { p: m.regularMarketPrice, pc: m.chartPreviousClose ?? m.previousClose, h: m.regularMarketDayHigh, l: m.regularMarketDayLow, v: m.regularMarketVolume, t: m.regularMarketTime };
  } catch { return null; }
}

// ---------- finding the stock in the question ----------
const ALIAS = { zomato: 'ETERNAL', reliance: 'RELIANCE', 'reliance power': 'RPOWER', 'reliance industries': 'RELIANCE', sbi: 'SBIN', 'l&t': 'LT', 'l and t': 'LT', larsen: 'LT', airtel: 'BHARTIARTL', hul: 'HINDUNILVR', 'hindustan unilever': 'HINDUNILVR',
  'jio financial': 'JIOFIN', jio: 'JIOFIN', mahindra: 'M&M', 'm and m': 'M&M', hero: 'HEROMOTOCO', 'hero motocorp': 'HEROMOTOCO', 'bajaj auto': 'BAJAJ-AUTO', 'tata consultancy': 'TCS',
  'dr reddy': 'DRREDDY', "dr reddy's": 'DRREDDY', 'sun pharma': 'SUNPHARMA', 'asian paints': 'ASIANPAINT', 'maruti suzuki': 'MARUTI', 'ongc': 'ONGC', 'kotak bank': 'KOTAKBANK', 'kotak mahindra': 'KOTAKBANK', 'ultratech': 'ULTRACEMCO', 'power grid': 'POWERGRID', 'hdfc bank': 'HDFCBANK', 'icici bank': 'ICICIBANK', 'axis bank': 'AXISBANK', 'bajaj finserv': 'BAJAJFINSV', 'bajaj finance': 'BAJFINANCE', 'tech mahindra': 'TECHM', 'eicher': 'EICHERMOT', 'tata steel': 'TATASTEEL', 'tata power': 'TATAPOWER', 'tata consumer': 'TATACONSUM', 'info edge': 'NAUKRI', 'infosys': 'INFY', 'paytm': 'PAYTM', 'nykaa': 'NYKAA', 'swiggy': 'SWIGGY', 'indigo': 'INDIGO', 'vedanta': 'VEDL', 'adani green': 'ADANIGREEN', 'adani power': 'ADANIPOWER', 'adani ports': 'ADANIPORTS', 'adani enterprises': 'ADANIENT', 'tata motors': 'TMPV', 'tata motor': 'TMPV', 'icici': 'ICICIBANK', 'kotak': 'KOTAKBANK', 'bob': 'BANKBARODA', 'pnb': 'PNB', 'hal': 'HAL', 'bel': 'BEL', 'bhel': 'BHEL', 'ntpc': 'NTPC', 'irctc': 'IRCTC', 'lic': 'LICI', 'dmart': 'DMART', 'd mart': 'DMART', 'avenue supermarts': 'DMART', 'policybazaar': 'POLICYBZR', 'divis': 'DIVISLAB', "divi's": 'DIVISLAB', 'tata elxsi': 'TATAELXSI', 'tcs': 'TCS', 'hdfc life': 'HDFCLIFE', 'hdfc amc': 'HDFCAMC', 'sbi life': 'SBILIFE', 'sbi card': 'SBICARD', 'ultratech cement': 'ULTRACEMCO', 'wipro': 'WIPRO', 'hcl': 'HCLTECH', 'hcl tech': 'HCLTECH', 'ltimindtree': 'LTIM', 'nestle': 'NESTLEIND', 'britannia': 'BRITANNIA', 'titan': 'TITAN', 'cipla': 'CIPLA', 'grasim': 'GRASIM', 'hindalco': 'HINDALCO', 'jsw steel': 'JSWSTEEL', 'coal india': 'COALINDIA', 'bharat electronics': 'BEL', 'hindustan aeronautics': 'HAL', 'trent': 'TRENT', 'bajaj holdings': 'BAJAJHLDNG' };
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
  // momentum rank vs the rest of the NIFTY 500 (others at their last close)
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
  mom: d => [`**TrendLens momentum**`, d.rank ? `Risk-adjusted 12-month momentum rank is about #${d.rank} of ${d.rankOf} (other stocks at their last close). A stock in the top 20 becomes a candidate; the CEO agent posts it as an idea only after the committee review, and closes it when an exit signal appears.` : 'Not enough history to rank this stock.',
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
  const advice = /\b(buy|sell|hold|invest|accumulate|should i|worth it)\b/i.test(q);
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

// =====================================================================================================
// Genie v2 "brain" (free, no AI): understands several kinds of question, keeps the conversation,
// and answers only from the site's own data plus the live price.
//   stock      price, outlook, averages, fundamentals, momentum rank, news, "why is it moving", idea status
//   compare    two to four stocks side by side ("TCS vs Infosys", "compare HDFC Bank and ICICI Bank")
//   screen     filters over the NIFTY 500 ("pharma stocks with P/E below 30 and ROE above 15", "top momentum")
//   market     NIFTY, Bank NIFTY, Sensex, India VIX, breadth
//   ideas      live / closed ideas, track record, why the CEO posted an idea
//   glossary   "what is RSI", "explain P/E"
//   follow-ups "and Infosys?", "what about its P/E", "why?" reuse the last stock and topic
// =====================================================================================================

const HELP = 'I can help with any NIFTY 500 stock and with the TrendLens ideas. Try:\n- "Reliance price and outlook"\n- "TCS vs Infosys"\n- "Pharma stocks with P/E below 30 and ROE above 15"\n- "Top 10 momentum stocks"\n- "Why is HDFC Bank down today?"\n- "How is the market today?"\n- "Show live ideas" or "Why did the CEO post Gland?"\n- "What is RSI?"\nEvery stock answer starts from the live price (or the last close when the market is shut).';

// ---------- glossary ----------
const GLOSS = [
  [/\brsi\b|relative strength index/, 'RSI (Relative Strength Index, 14 days)', 'Measures how strong recent gains are against recent losses, on a 0–100 scale. Above 70 is called overbought (stretched after a run-up), below 30 oversold (heavily sold). In strong uptrends RSI can stay at 60–75 for weeks, so it is a warning light, not a signal on its own.'],
  [/\bmacd\b/, 'MACD', 'The gap between the 12-day and 26-day exponential averages, compared with its own 9-day average (the signal line). MACD above the signal line means short-term momentum is rising; below means it is fading.'],
  [/\bp\/?e\b|price to earnings|pe ratio/, 'P/E (price to earnings)', 'Share price divided by earnings per share over the last 12 months. A P/E of 25x means investors pay ₹25 for every ₹1 of yearly profit. Higher usually means more growth is expected, or that the stock is expensive. Compare it with peers in the same industry, not across sectors.'],
  [/\bp\/?b\b|price to book/, 'P/B (price to book)', 'Share price divided by book value (net assets) per share. Most useful for banks and financial companies, where assets are mostly loans and investments.'],
  [/\broe\b|return on equity/, 'ROE (return on equity)', 'Yearly profit as a percentage of shareholders\' equity. 15% or more is generally considered good; consistently high ROE with low debt is a sign of a quality business.'],
  [/\broce\b/, 'ROCE', 'Return on capital employed: operating profit as a percentage of all capital used (equity plus debt). TrendLens does not have ROCE in its data yet; it shows ROE and ROA instead.'],
  [/debt.?to.?equity|\bd\/e\b/, 'Debt to equity', 'Total borrowings divided by shareholders\' equity. Below 0.5 is low; above 2 is high leverage (not meaningful for banks and NBFCs, whose business is borrowing and lending).'],
  [/\beps\b|earnings per share/, 'EPS (earnings per share)', 'Net profit over the last 12 months divided by the number of shares.'],
  [/dividend yield/, 'Dividend yield', 'Yearly dividend per share as a percentage of the current price.'],
  [/market cap|mcap/, 'Market capitalisation', 'Share price × number of shares: what the market values the whole company at. Roughly, large caps are the top 100 companies, mid caps the next 150, small caps the rest.'],
  [/\bbeta\b/, 'Beta', 'How much the stock tends to move when the market moves. Beta 1.2 means about 1.2% for a 1% market move; below 1 means calmer than the market.'],
  [/moving average|\bsma\b|\bdma\b|200.?day|50.?day|golden cross|death cross/, 'Moving averages', 'The average closing price over the last 20, 50 or 200 sessions. Price above a rising 200-day average is the classic sign of a long-term uptrend; the 50-day crossing above the 200-day is called a golden cross, below it a death cross.'],
  [/support|resistance/, 'Support and resistance', 'Price levels where a stock has tended to stop falling (support) or stop rising (resistance): recent lows and highs, key moving averages and the 52-week range. They are zones, not exact prices.'],
  [/52.?week/, '52-week high and low', 'The highest and lowest closing price over the last year. Stocks near their 52-week high are often in strong uptrends; momentum strategies favour them.'],
  [/momentum rank|momentum/, 'TrendLens momentum rank', 'Each NIFTY 500 stock\'s 12-month return divided by its 12-month volatility, ranked from #1 (strongest). The back-tested rule: a stock becomes a candidate in the top 20 and its exit is flagged when it falls below #50.'],
  [/stop.?loss/, 'Stop-loss level', 'The price at which an idea\'s view is considered wrong. TrendLens sets it from the stock\'s typical daily range (ATR), and a gap below it counts at the open price.'],
  [/\batr\b|average true range/, 'ATR (average true range)', 'The average size of a stock\'s daily move over 14 days, including gaps. TrendLens uses it to size the entry range, stop-loss and target.'],
  [/conviction|\bceo\b|committee|judge/, 'How an idea is decided', 'Analysts (technical, fundamentals, news, economy) report, a bull and a bear researcher argue, a risk officer checks size and limits, a coded judge scores it, and the CEO agent makes the final call with a written memo and a conviction level (High, Medium or Low).'],
  [/\bvix\b/, 'India VIX', 'The market\'s expected volatility for the next 30 days, from NIFTY option prices. Below 15 is calm; above 22 signals nervous markets.'],
  [/asm|gsm|surveillance/, 'ASM / GSM', 'NSE surveillance lists for stocks with unusual price moves or weak fundamentals. Higher margins and trading limits apply; TrendLens posts no new ideas in them.'],
];
function glossary(q) {
  const l = q.toLowerCase();
  return GLOSS.find(g => g[0].test(l)) || null;
}

// ---------- finding one or more stocks ----------
const reEsc = x => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
function findStocks(q, universe, F) {
  const set = new Set(universe), low = q.toLowerCase(), found = [], used = [];
  const free = (a, b) => !used.some(([x, y]) => a < y && b > x);
  for (const [a, s] of Object.entries(ALIAS).sort((x, y) => y[0].length - x[0].length)) {
    if (!set.has(s)) continue;
    const re = new RegExp('(^|[^a-z0-9])(' + reEsc(a) + ')($|[^a-z0-9])', 'g'); let m;
    while ((m = re.exec(low))) { const st = m.index + m[1].length, en = st + a.length; if (free(st, en)) { used.push([st, en]); found.push({ sym: s, pos: st }); } }
  }
  const wre = /[A-Za-z0-9&-]+/g; let m;
  while ((m = wre.exec(q))) { const w = m[0], u = w.toUpperCase(); if (set.has(u) && (!WORDLIKE.has(u) || w === u) && free(m.index, m.index + w.length)) { used.push([m.index, m.index + w.length]); found.push({ sym: u, pos: m.index }); } }
  // company names, one segment at a time ("TCS vs Infosys", "HDFC Bank and ICICI Bank")
  let masked = low.split(''); for (const [a, b] of used) for (let i = a; i < b; i++) masked[i] = ' '; masked = masked.join('');
  const segs = []; let off = 0;
  const parts = masked.split(/\s+(?:vs\.?|versus|and|or|with|against|compared to)\s+|[,;]/);
  for (const p of parts) { const at = masked.indexOf(p, off); segs.push([p, at]); off = at + p.length; }
  let amb = null; const alts = [];
  if (F && F.stocks) for (const [seg, at] of segs) {
    if (!seg.trim()) continue;
    const h = nameMatch(seg, universe, F);
    if (h && h.sym && !found.some(f => f.sym === h.sym)) { found.push({ sym: h.sym, pos: at }); if (h.alts) alts.push(...h.alts); }
    else if (h && h.ambiguous && !amb) amb = h.ambiguous;
  }
  // a whole company name that contains "and" ("Bajaj Holdings and Investment") is caught on the full text
  if (!found.length && F && F.stocks) { const h = nameMatch(masked, universe, F); if (h && h.sym) { found.push({ sym: h.sym, pos: 0 }); if (h.alts) alts.push(...h.alts); } else if (h && h.ambiguous) amb = amb || h.ambiguous; }
  found.sort((a, b) => a.pos - b.pos);
  const syms = [...new Set(found.map(f => f.sym))];
  return { syms, ambiguous: syms.length ? null : amb, alts };
}
function nameMatch(q, universe, F) {
  const qt = toks(q).filter(t => !GENERIC.has(t) && !STOP.has(t)), hits = [];
  if (!qt.length) return null;
  for (const s of universe) {
    const nm = F.stocks[s] && F.stocks[s].name; if (!nm) continue;
    const nt = toks(nm).filter(t => !GENERIC.has(t)); if (!nt.length) continue;
    let m = 0; for (const t of nt) if (qt.some(w => w === t || (t.length >= 6 && w.length >= 5 && lev(w, t) <= 1))) m++;
    if (m) hits.push({ sym: s, ratio: m / nt.length, m });
  }
  hits.sort((a, b) => b.ratio - a.ratio || b.m - a.m);
  if (!hits.length) return null;
  const best = hits[0], tied = hits.filter(h => h.ratio === best.ratio && h.m === best.m);
  if (best.ratio < 1) {
    const nt = toks(F.stocks[best.sym].name).filter(t => !GENERIC.has(t)), extra = qt.filter(w => !nt.some(t => w === t || (t.length >= 6 && w.length >= 5 && lev(w, t) <= 1)));
    if (best.ratio >= 0.5 && tied.length === 1 && !extra.length) return { sym: best.sym };   // "hdfc", "divis": only name words were typed
    return extra.length ? null : { ambiguous: hits.slice(0, 5).map(h => h.sym) };
  }
  if (tied.length > 1) { const order = tied.map(h => universe.indexOf(h.sym)).sort((a, b) => a - b); return { sym: universe[order[0]], alts: tied.map(h => h.sym).filter(s => s !== universe[order[0]]) }; }   // most-traded first, alternatives offered
  return { sym: best.sym };
}
// words that are never part of a company name in a question
const STOP = new Set('what whats is are the a an of for to in on at about its it this that how why when which who show me tell give list find stock stocks share shares price prices today now current live latest outlook view trend technical technicals fundamentals fundamental analysis averages average moving support resistance news headlines rank momentum idea ideas pe ratio roe debt equity growth dividend yield market cap cap sector high low week year month returns return chart vs versus compare comparison between better than against doing performing up down fall falling rise rising rally crash jump gain gaining drop dropping please can you could would should i my me we our your do does did any all top best worst cheapest highest lowest with having where above below under over less more than greater least near rsi macd sma dma ema bullish bearish oversold overbought large mid small big buy sell hold target stop loss nifty sensex bank'.split(' '));

// ---------- per-stock metrics over the whole universe (last close; cached per history file) ----------
let ROWS = null;
function macdHistOf(c) { if (c.length <= 35) return null; const e12 = emaSeries(c, 12), e26 = emaSeries(c, 26), m = e12.map((v, i) => v - e26[i]), s = emaSeries(m, 9); return m[m.length - 1] - s[s.length - 1]; }
function rows(H, F) {
  const key = H.dates[H.dates.length - 1] + ':' + H.universe.length + ':' + (F && F.asof);
  if (ROWS && ROWS.key === key) return ROWS.list;
  const sc = {}, list = [];
  for (const s of H.universe) {
    const c = ffill(H.closes[s] || []); if (c.length < 30) continue;
    const p = c[c.length - 1], f = (F && F.stocks && F.stocks[s]) || {};
    const r = { sym: s, name: f.name || s, sector: f.sector || '', industry: f.industry || '', price: p, chg: c.length > 1 ? (p / c[c.length - 2] - 1) * 100 : null,
      sma50: sma(c, 50), sma200: sma(c, 200), rsi: rsi14(c), macdHist: macdHistOf(c),
      hi52: Math.max(...c.slice(-252)), lo52: Math.min(...c.slice(-252)),
      r1m: c.length > 21 ? (p / c[c.length - 22] - 1) * 100 : null, r3m: c.length > 63 ? (p / c[c.length - 64] - 1) * 100 : null,
      r6m: c.length > 126 ? (p / c[c.length - 127] - 1) * 100 : null, r12m: c.length > 252 ? (p / c[c.length - 253] - 1) * 100 : null,
      pe: f.eps_ttm > 0 ? p / f.eps_ttm : null, pb: f.book_value > 0 ? p / f.book_value : null, roe: f.roe_pct, de: f.de_ratio,
      dy: f.div_rate != null ? f.div_rate / p * 100 : null, mcap: f.shares ? f.shares * p : null, revg: f.rev_growth_pct, eg: f.earn_growth_pct,
      nm: f.net_margin_pct, beta: f.beta, loss: f.eps_ttm != null && f.eps_ttm <= 0 };
    let k = 0; for (const t of [r.sma50 && p > r.sma50, r.sma200 && p > r.sma200, r.sma50 && r.sma200 && r.sma50 > r.sma200, r.macdHist != null && r.macdHist > 0]) k += t ? 1 : -1;
    r.tscore = k; r.outlook = k >= 3 ? 'Bullish' : k <= -3 ? 'Bearish' : k > 0 ? 'Mildly bullish' : k < 0 ? 'Mildly bearish' : 'Neutral';
    const v = momScore(c, p, 252); if (v != null) sc[s] = v;
    list.push(r);
  }
  const order = Object.keys(sc).sort((a, b) => sc[b] - sc[a]); const pos = {}; order.forEach((s, i) => pos[s] = i + 1);
  for (const r of list) r.rank = pos[r.sym] || null;
  ROWS = { key, list }; return list;
}

// ---------- screener ----------
const SECTORS = [
  [/\bpharma|drug|healthcare|hospital|diagnostic/, r => /Healthcare/.test(r.sector), 'healthcare and pharma'],
  [/\bprivate banks?|\bpsu banks?|\bpublic sector banks?|\bbanks?\b|banking/, r => /^Banks/.test(r.industry), 'banks'],
  [/\bnbfc|finance compan|lender|credit/, r => /Credit Services|Mortgage Finance/.test(r.industry), 'NBFCs and lenders'],
  [/\binsurance|insurer/, r => /Insurance/.test(r.industry), 'insurance'],
  [/\bfinancials?|financial services/, r => /Financial/.test(r.sector), 'financial services'],
  [/\b(it|tech|technology|software)\b/, r => /Technology/.test(r.sector), 'technology'],
  [/\bautos?\b|automobile|auto ancillar|car maker/, r => /^Auto/.test(r.industry), 'auto and auto parts'],
  [/\bfmcg|consumer staples|consumer defensive/, r => /Consumer Defensive/.test(r.sector), 'FMCG and consumer staples'],
  [/\bcement|building material/, r => /Building Materials/.test(r.industry), 'cement and building materials'],
  [/\bmetals?\b|steel|alumin|mining/, r => /Steel|Aluminum|Metals|Mining|Copper/.test(r.industry), 'metals and mining'],
  [/\bchemical/, r => /Chemical/.test(r.industry), 'chemicals'],
  [/\bpower|utilit|electricity/, r => /Utilities/.test(r.sector), 'power and utilities'],
  [/\boil|gas|energy|refin/, r => /Energy/.test(r.sector) || /Gas/.test(r.industry), 'oil, gas and energy'],
  [/\brealty|real estate|property|housing/, r => /Real Estate/.test(r.sector), 'real estate'],
  [/\btelecom|media|communication/, r => /Communication/.test(r.sector), 'telecom and media'],
  [/\bdefen[cs]e|aerospace/, r => /Aerospace/.test(r.industry), 'defence and aerospace'],
  [/\bcapital goods|industrials?|engineering|infra|construction|machinery/, r => /Industrials/.test(r.sector), 'industrials and capital goods'],
  [/\bconsumer discretionary|consumer cyclical|retail|textile|hotel|travel/, r => /Consumer Cyclical/.test(r.sector), 'consumer discretionary'],
];
const METRICS = [   // [pattern, field, label, unit]
  [/forward p\/?e/, null], [/\bp\/?e\b|pe ratio|price.to.earnings/, 'pe', 'P/E', 'x'], [/\bp\/?b\b|price.to.book/, 'pb', 'P/B', 'x'],
  [/\broe\b|return on equity/, 'roe', 'ROE', '%'], [/debt.?to.?equity|\bd\/e\b|\bdebt\b/, 'de', 'Debt/equity', ''],
  [/dividend( yield)?/, 'dy', 'Dividend yield', '%'], [/revenue growth|sales growth|top.?line/, 'revg', 'Revenue growth', '%'],
  [/earnings growth|profit growth|eps growth|bottom.?line/, 'eg', 'Earnings growth', '%'], [/net margin|profit margin|margin/, 'nm', 'Net margin', '%'],
  [/\brsi\b/, 'rsi', 'RSI', ''], [/\bbeta\b/, 'beta', 'Beta', ''], [/market cap|mcap/, 'mcap', 'Market cap', 'cr'],
  [/1.?m(onth)? return|one month return|last month/, 'r1m', '1-month return', '%'], [/3.?m(onth)? return|three month|quarter return/, 'r3m', '3-month return', '%'],
  [/6.?m(onth)? return|six month/, 'r6m', '6-month return', '%'], [/1.?y(ea)?r? return|12.?m(onth)? return|one year|annual return|yearly return/, 'r12m', '12-month return', '%'],
  [/\bprice\b|share price|trading (at|below|above)/, 'price', 'Price', '₹'], [/\brupees?\b|\brs\b/, 'price', 'Price', '₹'],
];
const CMP = '(below|under|less than|lower than|<=?|at most|max(?:imum)?|not more than|above|over|more than|greater than|higher than|>=?|at least|min(?:imum)?|between)';
function parseNum(n, unit, field) {
  let v = parseFloat(String(n).replace(/,/g, ''));
  if (field === 'mcap') { if (/lakh crore|lk ?cr|lakh cr|trillion/i.test(unit)) v *= 1e12; else if (/crore|cr\b/i.test(unit) || true) v *= 1e7; }
  return v;
}
function screenFilters(q) {
  const l = q.toLowerCase(), F = [], desc = [];
  for (const [re, field, label] of METRICS) {
    if (!field) continue;
    const src = re.source.replace(/\((?!\?)/g, '(?:');
    const r1 = new RegExp('(?:' + src + ')[^0-9a-z]{0,3}(?:ratio|of|is|should be|\\s)*' + CMP + '\\s*₹?\\s*(-?[\\d.,]+)\\s*(%|x|lakh crore|crore|cr|lk ?cr)?(?:\\s*(?:and|to|-)\\s*₹?\\s*(-?[\\d.,]+)\\s*(%|x|lakh crore|crore|cr)?)?', 'g');
    const r2 = new RegExp(CMP + '\\s*₹?\\s*(-?[\\d.,]+)\\s*(%|x|lakh crore|crore|cr)?\\s*(?:' + src + ')', 'g');
    let m;
    while ((m = r1.exec(l))) { add(field, label, m[1], m[2], m[3] || '', m[4], m[5] || m[3] || ''); }
    while ((m = r2.exec(l))) { add(field, label, m[1], m[2], m[3] || ''); }
  }
  function add(field, label, cmp, a, ua, b, ub) {
    const lo = /below|under|less|lower|<|most|max|not more/.test(cmp), bt = cmp === 'between';
    const va = parseNum(a, ua, field), vb = b != null ? parseNum(b, ub, field) : null;
    if (isNaN(va) || F.some(f => f.field === field)) return;
    if (bt && vb != null) { F.push({ field, test: v => v != null && v >= Math.min(va, vb) && v <= Math.max(va, vb) }); desc.push(`${label} between ${a} and ${b}`); }
    const u = ua ? (/^[%x]$/.test(ua) ? ua : ' ' + ua) : '';
    if (lo) { F.push({ field, test: v => v != null && v <= va }); desc.push(`${label} ≤ ${a}${u}`); }
    else { F.push({ field, test: v => v != null && v >= va }); desc.push(`${label} ≥ ${a}${u}`); }
  }
  const kw = (re, test, d) => { if (re.test(l)) { F.push({ test, row: true }); desc.push(d); } };
  kw(/debt.?free|zero debt|no debt/, r => r.de != null && r.de < 0.1, 'debt-free (D/E below 0.1)');
  kw(/near (the |its |their )?52.?week high|at (a |their )?52.?week high|new high/, r => r.price >= 0.95 * r.hi52, 'within 5% of the 52-week high');
  kw(/near (the |its |their )?52.?week low|at (a |their )?52.?week low|new low/, r => r.price <= 1.05 * r.lo52, 'within 5% of the 52-week low');
  kw(/oversold/, r => r.rsi != null && r.rsi < 30, 'RSI below 30 (oversold)');
  kw(/overbought/, r => r.rsi != null && r.rsi > 70, 'RSI above 70 (overbought)');
  kw(/above (the |their |its )?200.?(day|dma|sma|d\b)/, r => r.sma200 && r.price > r.sma200, 'above the 200-day average');
  kw(/below (the |their |its )?200.?(day|dma|sma|d\b)/, r => r.sma200 && r.price < r.sma200, 'below the 200-day average');
  kw(/above (the |their |its )?50.?(day|dma|sma|d\b)/, r => r.sma50 && r.price > r.sma50, 'above the 50-day average');
  kw(/below (the |their |its )?50.?(day|dma|sma|d\b)/, r => r.sma50 && r.price < r.sma50, 'below the 50-day average');
  kw(/golden cross|uptrend|up.?trend/, r => r.sma50 && r.sma200 && r.sma50 > r.sma200 && r.price > r.sma50, 'in an uptrend (price > 50-day > 200-day)');
  kw(/death cross|downtrend|down.?trend/, r => r.sma50 && r.sma200 && r.sma50 < r.sma200 && r.price < r.sma50, 'in a downtrend (price < 50-day < 200-day)');
  if (/\bmildly bullish\b/.test(l)) kw(/./, r => r.outlook === 'Mildly bullish', 'outlook Mildly bullish');
  else kw(/\bbullish\b/, r => r.tscore >= 3, 'outlook Bullish');
  if (/\bmildly bearish\b/.test(l)) kw(/./, r => r.outlook === 'Mildly bearish', 'outlook Mildly bearish');
  else kw(/\bbearish\b/, r => r.tscore <= -3, 'outlook Bearish');
  kw(/profitable|profit.?making|not loss/, r => r.pe != null, 'profitable (positive EPS)');
  kw(/loss.?making|losses\b/, r => r.loss, 'loss-making');
  kw(/large.?caps?|blue.?chips?/, r => r.mcap >= 1e12, 'large cap (market cap ≥ ₹1 lakh crore)');
  kw(/mid.?caps?/, r => r.mcap >= 2.5e11 && r.mcap < 1e12, 'mid cap (₹25,000 crore to ₹1 lakh crore)');
  kw(/small.?caps?/, r => r.mcap != null && r.mcap < 2.5e11, 'small cap (below ₹25,000 crore)');
  kw(/\bhigh dividend|dividend pay/, r => r.dy >= 2, 'dividend yield ≥ 2%');
  kw(/top 20 momentum|momentum leaders?|in the top 20/, r => r.rank && r.rank <= 20, 'momentum rank in the top 20');
  for (const [re, test, d] of SECTORS) if (re.test(l)) { F.push({ test, row: true }); desc.push(d); break; }
  return { F, desc };
}
function screenSort(q) {
  const l = q.toLowerCase();
  const pick = (re, field, dir, label) => re.test(l) ? { field, dir, label } : null;
  return pick(/cheapest|lowest p\/?e|low p\/?e|undervalued|value stocks/, 'pe', 1, 'lowest P/E')
    || pick(/most expensive|highest p\/?e/, 'pe', -1, 'highest P/E')
    || pick(/highest roe|best roe|top roe/, 'roe', -1, 'highest ROE')
    || pick(/highest dividend|best dividend|top dividend/, 'dy', -1, 'highest dividend yield')
    || pick(/\bgainers\b|gained (the )?most|top performers today|(up|rose) the most/, 'chg', -1, 'biggest gain on the last session')
    || pick(/\blosers\b|fell (the )?most|lost (the )?most|(down|dropped) the most/, 'chg', 1, 'biggest fall on the last session')
    || (/best performers|best performing|top performers|(highest|best|top) returns?|outperform/.test(l) ? (f => ({ field: f, dir: -1, label: 'highest ' + { r1m: '1-month', r3m: '3-month', r6m: '6-month', r12m: '12-month' }[f] + ' return' }))(/month|1m/.test(l) && !/12|six|3|three|6/.test(l) ? 'r1m' : /3.?m|three|quarter/.test(l) ? 'r3m' : /6.?m|six/.test(l) ? 'r6m' : 'r12m') : null)
    || pick(/worst performers|worst performing|(lowest|worst) returns?|underperform/, /month|1m/.test(l) ? 'r1m' : 'r12m', 1, /month|1m/.test(l) ? 'lowest 1-month return' : 'lowest 12-month return')
    || pick(/largest|biggest compan|highest market cap/, 'mcap', -1, 'largest market cap')
    || pick(/fastest growing|highest (revenue|sales) growth/, 'revg', -1, 'highest revenue growth')
    || pick(/highest (earnings|profit) growth/, 'eg', -1, 'highest earnings growth')
    || pick(/most oversold|lowest rsi/, 'rsi', 1, 'lowest RSI')
    || pick(/most overbought|highest rsi/, 'rsi', -1, 'highest RSI')
    || pick(/least volatile|lowest beta|defensive/, 'beta', 1, 'lowest beta')
    || pick(/momentum|strongest|trending|rank/, 'rank', 1, 'momentum rank');
}
const isScreenQ = q => /\b(which|list|show|find|screen|filter|scan|top \d+|top ten|best|cheapest|highest|lowest|gainers|losers|leaders|performers|stocks (with|having|that|where|in|near|above|below|under)|any stocks?|companies (with|having|that))\b/i.test(q) || /\bstocks\b/i.test(q);
function screen(q, H, F) {
  const all = rows(H, F), { F: flt, desc } = screenFilters(q); let sort = screenSort(q);
  const nm = q.match(/\b(?:top|best|first|show)\s+(\d{1,2})\b/i) || q.match(/\b(\d{1,2})\s+(?:stocks|companies|names)\b/i);
  const n = Math.min(Math.max(nm ? parseInt(nm[1], 10) : 10, 1), 25);
  if (!flt.length && !sort) return null;
  if (!sort) sort = { field: 'rank', dir: 1, label: 'momentum rank' };
  let L = all.filter(r => flt.every(f => f.row ? f.test(r) : f.test(r[f.field])));
  const total = L.length;
  L = L.filter(r => r[sort.field] != null).sort((a, b) => (a[sort.field] - b[sort.field]) * sort.dir).slice(0, n);
  const asof = H.dates[H.dates.length - 1];
  const head = desc.length ? `**${total} of ${all.length} NIFTY 500 stocks match**: ${desc.join(', ')}. Sorted by ${sort.label}; closing prices of ${asof}.` : `**Top ${Math.min(n, L.length)} NIFTY 500 stocks by ${sort.label}** (closing prices of ${asof}).`;
  if (!L.length) return { text: head + '\nNo stock matches all of these together. Try loosening one condition.', syms: [] };
  const line = r => `- **${r.sym}** ${inr(r.price, r.price < 100 ? 2 : 0)} · ${r.outlook}` + (r.rank ? ` · rank #${r.rank}` : '') + (r.pe != null ? ` · P/E ${r.pe.toFixed(1)}x` : r.loss ? ' · loss-making' : '') + (r.roe != null ? ` · ROE ${r.roe.toFixed(0)}%` : '')
    + (['r1m', 'r3m', 'r6m', 'r12m', 'chg', 'dy', 'revg', 'eg', 'rsi', 'beta', 'mcap', 'de', 'pb', 'nm'].includes(sort.field) ? ` · ${sort.label.replace(/^(highest|lowest|biggest|largest) /, '').replace(/ on the last session/, '')}: ${sort.field === 'mcap' ? cr(r.mcap) : sort.field === 'rsi' || sort.field === 'beta' || sort.field === 'de' || sort.field === 'pb' ? num(r[sort.field], sort.field === 'rsi' ? 0 : 2) : sort.field === 'dy' || sort.field === 'nm' ? pl(r[sort.field]) : pc(r[sort.field])}` : '')
    + (r.sector ? ` · ${r.industry || r.sector}` : '');
  return { text: [head, ...L.map(line), total > L.length ? `…and ${total - L.length} more. Ask for "top 25" or add a condition to narrow it.` : '', 'Ask about any of them by name for the full picture.'].filter(Boolean).join('\n'), syms: L.map(r => r.sym) };
}

// ---------- market ----------
async function market(H, F) {
  const idx = [['NIFTY 50', '^NSEI'], ['NIFTY BANK', '^NSEBANK'], ['SENSEX', '^BSESN'], ['India VIX', '^INDIAVIX']];
  const qs = await Promise.all(idx.map(([, y]) => liveQuote(y, true)));
  const lines = idx.map(([n], i) => { const q = qs[i]; return q ? `- **${n}** ${Number(q.p).toLocaleString('en-IN', { maximumFractionDigits: 2 })} (${pc((q.p / q.pc - 1) * 100, 2)})` : `- **${n}** not available`; });
  const all = rows(H, F), n = all.length;
  const up = all.filter(r => r.chg > 0).length, a50 = all.filter(r => r.sma50 && r.price > r.sma50).length, a200 = all.filter(r => r.sma200 && r.price > r.sma200).length, nh = all.filter(r => r.price >= 0.98 * r.hi52).length;
  const vix = qs[3] && qs[3].p;
  return [`**Market ${marketHours() ? 'now' : 'at the last close'}**`, ...lines,
    `**Breadth (NIFTY 500, close of ${H.dates[H.dates.length - 1]})**: ${up} of ${n} rose on the session; ${Math.round(a50 / n * 100)}% are above their 50-day average and ${Math.round(a200 / n * 100)}% above their 200-day; ${nh} are within 2% of a 52-week high.`,
    vix ? `India VIX at ${vix.toFixed(1)} means ${vix < 15 ? 'calm markets' : vix > 22 ? 'nervous markets (expect big swings)' : 'normal volatility'}.` : ''].filter(Boolean).join('\n');
}

// ---------- ideas ----------
function ideasAnswer(q, P, sym) {
  if (!P) return 'The ideas file is not available right now.';
  const live = P.live || [], closed = P.closed || [], l = q.toLowerCase();
  if (sym) {
    const L = live.find(e => e.sym === sym), C = closed.find(e => e.sym === sym);
    if (!L && !C) return `${sym} is not a TrendLens idea right now and has no closed idea on record.`;
    const e = L || C, out = [`**${sym}: ${L ? 'live idea' : 'closed idea'}** (${e.term} term, posted ${e.posted || e.entry_date}${e.approved_by ? ' by the ' + e.approved_by : ''})`,
      `Entry range ${inr(e.entry_lo)}–${inr(e.entry_hi)} · stop-loss ${inr(e.stop)} · target ${inr(e.target)} · ${e.days} trading days.`];
    if (e.ceo) out.push(`**CEO memo:** ${e.ceo.memo}`);
    if (e.judge) out.push(`Judge score ${e.judge.score}: ` + e.judge.rules.filter(r => r.points > 0).map(r => r.rule).slice(0, 5).join('; ') + '.');
    if (e.review) out.push(`Bull case: ${(e.review.bull || []).join('; ') || '—'}. Bear case: ${(e.review.bear || []).join('; ') || 'none raised'}.`);
    if (e.committee) out.push(`Committee: ${e.committee.agreement}; economy ${e.committee.macro}.`);
    if (L && L.exit_signal) out.push(`Exit signal: ${L.exit_signal.reason} at ${inr(L.exit_signal.price)}; the CEO closes it at the close.`);
    if (C) out.push(`Closed ${C.exit_date} at ${inr(C.exit_price)}: ${C.reason} (${pc(C.ret_pct)}).${C.ceo_exit ? ' ' + C.ceo_exit : ''}`);
    return out.join('\n');
  }
  if (/closed|track record|performance|win rate|result|how (good|well)|hit rate|accuracy/.test(l)) {
    if (!closed.length) return `No closed ideas yet. ${live.length} idea${live.length === 1 ? ' is' : 's are'} live. Results build up as ideas reach their target, stop-loss or time limit.`;
    const w = closed.filter(c => c.ret_pct > 0), avg = closed.reduce((s, c) => s + c.ret_pct, 0) / closed.length;
    return [`**Track record: ${closed.length} closed ideas**`, `Profitable ${w.length} (${Math.round(w.length / closed.length * 100)}%) · average result ${pc(avg)}.`,
      ...closed.slice(0, 10).map(c => `- **${c.sym}** ${pc(c.ret_pct)} · ${c.reason} · ${c.entry_date} → ${c.exit_date}`)].join('\n');
  }
  if (!live.length) return 'No live ideas right now. The CEO reviews candidates every 5 minutes in market hours and after the close.';
  return [`**${live.length} live idea${live.length === 1 ? '' : 's'}**`, ...live.map(e => {
    const chg = e.price_now && e.entry_price ? (e.price_now / e.entry_price - 1) * 100 : null;
    return `- **${e.sym}** posted ${e.posted || e.entry_date} at ${inr(e.entry_price)}${chg != null ? ` (${pc(chg)} since)` : ''} · stop ${inr(e.stop, 0)} · target ${inr(e.target, 0)}${e.ceo ? ` · ${e.ceo.conviction} conviction` : ''}${e.exit_signal ? ' · ⚠ exit signal: ' + e.exit_signal.reason : ''}`;
  }), 'Ask "why did the CEO post <stock>?" for the full reasoning.'].join('\n');
}

// ---------- news (Google News RSS, filtered to headlines that name the company) ----------
const NEG_W = /\b(fraud|probe|raids?|sebi (order|bans?|notice)|penalt(y|ies)|fine[ds]?|downgrade[sd]?|default(s|ed)?|resign(s|ed|ation)|loss widens|plunges?|crash(es)?|slumps?|tumbles?|insolvency|lawsuit|show.?cause|ed (raid|summons|probe))\b/i;
const POS_W = /\b(order win|bags? (an? )?order|wins? (an? )?(order|contract)|upgrade[sd]?|record (profit|revenue|high)|profit (jumps|rises|surges)|surges?|soars?|rall(y|ies)|bonus|buyback|dividend|beats? estimates|acquires?|expansion|approval)\b/i;
const QUOTE_PAGE = /share price|stock price|price today|\bquote\b|price & chart|stock \(\w+\)|returns chart|stock forecast|price target|technical analysis by|\btgt\b|\bsl \d|stock of the day|- (facebook|instagram|youtube|x|twitter|telegram|threads|reddit)$/i;
async function news(sym, name) {
  const words = toks(name).filter(t => !GENERIC.has(t) && t.length > 1);
  const key = (words.slice(0, 2).join(' ') || sym).trim();
  try {
    const r = await fetch('https://news.google.com/rss/search?q=' + encodeURIComponent(`"${key}" when:7d`) + '&hl=en-IN&gl=IN&ceid=IN:en', { signal: AbortSignal.timeout(5000), headers: { 'User-Agent': 'Mozilla/5.0 TrendLens' } });
    if (!r.ok) return null;
    const x = await r.text(), items = [];
    const re = /<item>[\s\S]*?<title>([\s\S]*?)<\/title>[\s\S]*?<pubDate>([\s\S]*?)<\/pubDate>[\s\S]*?<\/item>/g; let m;
    const dec = t => t.replace(/<!\[CDATA\[|\]\]>/g, '').replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>');
    const kn = key.replace(/[^a-z0-9]/gi, '').toLowerCase(), sn = sym.toLowerCase();
    while ((m = re.exec(x)) && items.length < 6) {
      const t = dec(m[1]).trim(), n = t.replace(/[^a-z0-9]/gi, '').toLowerCase();
      if ((n.includes(kn) || n.includes(sn)) && !QUOTE_PAGE.test(t)) items.push({ t, d: new Date(m[2]).toISOString().slice(0, 10), neg: NEG_W.test(t), pos: POS_W.test(t) });
    }
    return items;
  } catch { return null; }
}

// ---------- compare ----------
function compare(ds) {
  const col = (lab, f) => `- **${lab}**: ` + ds.map(d => `${d.sym} ${f(d)}`).join(' · ');
  const out = [`**${ds.map(d => d.sym).join(' vs ')}** (${ds[0].live ? 'live prices' : 'last close'})`,
    col('Price', d => `${inr(d.price, 0)} (${pc(d.dayChg, 2)})`),
    col('Outlook', d => `${d.outlook} (${d.score > 0 ? '+' : ''}${d.score})`),
    col('RSI', d => num(d.rsi, 0)),
    col('Momentum rank', d => d.rank ? '#' + d.rank : 'n/a'),
    col('Return 3M / 12M', d => `${pc(d.r3m, 0)} / ${pc(d.r12m, 0)}`),
    col('From 52-week high', d => pc((d.price / d.hi52 - 1) * 100)),
    col('P/E', d => d.f ? peTxt(d.f) : 'n/a'),
    col('P/B', d => d.f && d.f.pb ? d.f.pb.toFixed(1) + 'x' : 'n/a'),
    col('ROE', d => d.f ? pl(d.f.roe, 0) : 'n/a'),
    col('Debt/equity', d => d.f ? num(d.f.de, 2) : 'n/a'),
    col('Revenue / earnings growth', d => d.f ? `${pc(d.f.revg, 0)} / ${pc(d.f.eg, 0)}` : 'n/a'),
    col('Market cap', d => d.f ? cr(d.f.mcap) : 'n/a')];
  // plain-words summary of who leads on what (facts, not a recommendation)
  const lead = (lab, f, hi = true) => { const v = ds.map(f); if (v.some(x => x == null || typeof x !== 'number' || isNaN(x))) return null; const i = v.indexOf(hi ? Math.max(...v) : Math.min(...v)); return `${lab}: ${ds[i].sym}`; };
  const L = [lead('stronger trend score', d => d.score), lead('better momentum rank', d => d.rank, false), lead('lower P/E', d => d.f && typeof d.f.pe === 'number' ? d.f.pe : null, false), lead('higher ROE', d => d.f && d.f.roe), lead('faster earnings growth', d => d.f && d.f.eg)].filter(Boolean);
  if (L.length) out.push('**Who leads on what:** ' + L.join('; ') + '.');
  if (ds.some(d => d.f && d.f.sector) && new Set(ds.map(d => d.f && d.f.sector)).size > 1) out.push('They are in different sectors, so compare valuations with care.');
  return out.join('\n');
}

// ---------- stock topics ----------
function routeStock(q) {
  const l = q.toLowerCase(), t = [];
  if (/^\s*why\s*\??\s*$/.test(l)) return ['why'];
  if (/\b(why|what happened|reason)\b/.test(l) && /\b(up|down|fall|fell|falling|rise|rising|rose|rally|drop|dropped|crash|jump|jumped|gain|gaining|moving|move|surge|slump|red|green)\b/.test(l)) t.push('why');
  if (/\b(news|headline|announcement|update|latest on|happening)\b/.test(l) && !t.includes('why')) t.push('news');
  if (/\b(idea|posted|ceo|committee|judge|conviction|why did (you|the ceo)|why was it|target|stop.?loss|entry (range|price|level))\b/.test(l)) t.push('idea');
  if (/\b(pe|p\/e|eps|market ?cap|valuation|valued|roe|roce|roa|debt|growth|dividends?|book value|p\/b|pb|margins?|fundamentals?|financials?|expensive|cheap|overvalued|undervalued|earnings|profits?|revenues?|sales|beta|sector|industry|business|ratios?)\b/.test(l)) t.push('fund');
  if (/\b(rsi|macd|trends?|bullish|bearish|outlook|support|resistance|signals?|technicals?|overbought|oversold|charts?|analysis|view|strong|weak|breakout|indicators?)\b/.test(l)) t.push('tech');
  if (/\b(average|averages|sma|dma|ema|moving|52|week|high|low|return|returns|ytd|performance|performed)\b/.test(l)) t.push('avg');
  if (/\b(rank|top 20|trendlens|screener|momentum)\b/.test(l) && !t.includes('idea')) t.push('mom');
  if (/\b(price|quote|trading at|ltp|cmp|today|how much|volume|day range|now)\b/.test(l) && !t.length) t.push('price');
  return t.length ? [...new Set(t)] : ['overview'];
}

const isFollowUp = q => q.trim().split(/\s+/).length <= 5 && /^(and|what about|how about|same for|also|now|then|ok(ay)?,?)\b|\?$/i.test(q.trim());

// ---------- handler ----------
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
  const out = await answer(question, b, H, F, P);
  return res.status(200).json({ disclaimer: DISCLAIMER, ...out });
};

async function answer(question, b, H, F, P) {
  const l = question.toLowerCase();
  const last = b.last && H.universe.includes(String(b.last)) ? String(b.last) : null;
  const lastTopics = Array.isArray(b.lastTopics) ? b.lastTopics.filter(t => typeof t === 'string' && /^(price|tech|avg|fund|mom|news|why|idea|overview)$/.test(t)) : null;
  let { syms, ambiguous, alts } = findStocks(question, H.universe, F);
  if (syms.length === 1 && last && last !== syms[0] && /\b(compare|vs\.?|versus|against|it with|with it|than it)\b/i.test(l)) syms = [last, syms[0]];   // "compare it with TCS"
  const advice = /\b(buy|sell|hold|accumulate|should i (buy|sell|invest|hold|exit|enter)|worth (buying|investing)|multibagger|tip|tips)\b/i.test(question);
  const pre = advice ? "TrendLens doesn't give buy, sell or hold calls. Here are the facts instead:\n" : '';

  // 1) greetings / help / out of scope
  if (!syms.length && /^(hi|hello|hey|help|namaste|good (morning|evening|afternoon))\b|what can you (do|answer)|how (do|can) i use/i.test(l)) return { text: HELP, mode: 'quick', kind: 'help' };
  // 2) glossary ("what is RSI") when no stock is the subject, or when the question is only about a term
  const g = glossary(question);
  if (g && !syms.length && /\b(what('s| is| are| does)|meaning|explain|define|stand for|how (is|does) .* (work|calculated))\b/i.test(l))
    return { text: `**${g[1]}**\n${g[2]}`, mode: 'quick', kind: 'glossary' };
  // 3) ideas
  if (/\b(ideas?|posted|live ideas|closed ideas|track record|win rate|hit rate|your (picks|calls)|ceo|portfolio)\b/.test(l) && (!syms.length || /\b(idea|post|ceo|committee|conviction)\b/.test(l)))
    return { text: pre + ideasAnswer(question, P, syms[0]), mode: 'quick', kind: 'ideas', sym: syms[0] || null };
  // 4) compare
  if (syms.length >= 2) {
    const take = syms.slice(0, 4), qs = await Promise.all(take.map(s => liveQuote(s)));
    const ds = take.map((s, i) => build(s, qs[i], H, F, P));
    return { text: pre + compare(ds), mode: 'quick', kind: 'compare', syms: take, sym: take[0] };
  }
  // 5) market
  const sf = screenFilters(question);
  if (!syms.length && /\b(market|nifty|sensex|bank ?nifty|vix|breadth|dalal)\b/.test(l.replace(/market ?cap|nifty ?500/g, '')) && !/\b(stocks?|compan(y|ies)|shares)\b/.test(l) && !screenSort(question))
    return { text: await market(H, F), mode: 'quick', kind: 'market' };
  // 6) screener
  if (!syms.length && (isScreenQ(question) || sf.F.length)) {
    const s = screen(question, H, F);
    if (s) return { text: pre + s.text, mode: 'quick', kind: 'screen', candidates: s.syms.slice(0, 6) };
  }
  // 7) one stock (named, or the last one for a follow-up)
  let sym = syms[0] || null, follow = false;
  if (!sym && ambiguous) return { text: 'Which one do you mean? ' + ambiguous.map(s => `${(F && F.stocks && F.stocks[s] && F.stocks[s].name) || s} (${s})`).join(', ') + '.', candidates: ambiguous, mode: 'quick', kind: 'ambiguous' };
  if (!sym && last) { sym = last; follow = true; }
  if (!sym) {
    if (g) return { text: `**${g[1]}**\n${g[2]}`, mode: 'quick', kind: 'glossary' };
    if (!/\b(stock|share|price|market|nifty|invest|pe|roe|rsi|company|ltd|limited|idea)\w*/i.test(l)) return { text: 'I only answer questions about NSE-listed stocks, the market and the TrendLens ideas. Try a company name, "TCS vs Infosys" or "top 10 momentum stocks".', mode: 'quick', kind: 'out_of_scope' };
    return { text: "I couldn't tell which stock you mean. I cover the NIFTY 500: try the company name or NSE symbol (\"TCS\", \"Reliance\"), compare two (\"TCS vs Infosys\"), or screen (\"banks with P/E below 15\"). Type \"help\" for more.", mode: 'quick', kind: 'unknown' };
  }
  let topics = routeStock(question);
  if (topics[0] === 'overview' && lastTopics && lastTopics.length && (follow || isFollowUp(question))) topics = lastTopics;
  const q = await liveQuote(sym);
  const d = build(sym, q, H, F, P);
  const parts = [];
  if (alts && alts.length && syms.length === 1) parts.push(`(Showing ${sym}, the most-traded match. Also matches: ${alts.slice(0, 3).join(', ')}.)`);
  if (pre) parts.push(pre.trim());
  if (topics.includes('overview') || advice) {
    parts.push(...T.price(d).slice(0, 1), ...T.tech(d), ...T.avg(d).slice(1, 3), ...T.fund(d).slice(0, 3), ...T.mom(d).slice(1, 3));
  } else {
    if (!topics.includes('price')) parts.push(head(d));
    for (const k of topics) {
      if (T[k]) parts.push(...T[k](d));
      else if (k === 'idea') {
        parts.push(ideasAnswer(question, P, sym));
        const isIdea = P && [...(P.live || []), ...(P.closed || [])].some(e => e.sym === sym);
        if (!isIdea && /target|stop|entry/.test(l)) parts.push('For reference, the chart levels:', T.tech(d).slice(-1)[0]);
      }
      else if (k === 'news' || k === 'why') {
        const items = await news(sym, d.name);
        if (k === 'why') {
          const nq = await liveQuote('^NSEI', true), nchg = nq ? (nq.p / nq.pc - 1) * 100 : null;
          const rel = d.dayChg != null && nchg != null ? d.dayChg - nchg : null;
          if (d.dayChg != null && (/\b(down|fall|fell|falling|drop|dropped|crash|slump|red)\b/.test(l) && d.dayChg > 0 || /\b(up|rise|rising|rose|rally|jump|jumped|gain|surge|green)\b/.test(l) && d.dayChg < 0)) parts.push(`Note: ${d.sym} is actually ${d.dayChg > 0 ? 'up' : 'down'} ${pc(Math.abs(d.dayChg), 2).replace('+', '')} ${d.live ? 'today' : 'on the last session'}.`);
          parts.push(`**Today's move:** ${pc(d.dayChg, 2)} vs NIFTY ${pc(nchg, 2)}${rel != null ? ` (${Math.abs(rel) < 0.5 ? 'moving with the market' : rel > 0 ? 'outperforming the market by ' + rel.toFixed(1) + ' points' : 'underperforming the market by ' + Math.abs(rel).toFixed(1) + ' points'})` : ''}.`
            + (d.volume && d.avgVol ? ` Volume is ${(d.volume / d.avgVol).toFixed(1)}x its usual level.` : ''));
          const sect = d.f && d.f.industry ? rows(H, F).filter(r => r.industry === d.f.industry && r.sym !== sym) : [];
          if (sect.length >= 2) { const avg = sect.reduce((s, r) => s + (r.chg || 0), 0) / sect.length; parts.push(`Peers in ${d.f.industry} moved ${pc(avg, 2)} on average on the last session.`); }
        }
        if (items == null) parts.push('News could not be fetched just now.');
        else if (!items.length) parts.push(`No headlines naming ${d.name} in the last 7 days${k === 'why' ? ', so the move looks market- or sector-driven rather than company news' : ''}.`);
        else parts.push(`**Recent headlines (last 7 days):**`, ...items.slice(0, 5).map(i => `- ${i.t} (${i.d})${i.neg ? ' ⚠' : i.pos ? ' ▲' : ''}`), items.some(i => i.neg) ? '⚠ marks a headline with a negative keyword (penalty, probe, downgrade…). Read the source before drawing conclusions.' : '');
      }
    }
  }
  let text = parts.filter(Boolean).join('\n'), mode = 'quick';
  if (process.env.ANTHROPIC_API_KEY && !advice && !(await limited('g:ai:site', DAILY_LIMIT, 86400))) {
    try {
      const t = await claude(question, b.history, JSON.stringify({ ...d, checks: d.checks.map(c => c.t) }) + '\n\nTRENDLENS VERDICT (quick answer):\n' + text);
      if (t && !/\b(buy|sell|hold)\b/i.test(t)) { text = t; mode = 'smart'; }
    } catch (e) { console.error(e.message); }
  }
  return { text, mode, kind: 'stock', candidates: alts && alts.length ? alts.slice(0, 3) : undefined, sym: d.sym, name: d.name, price: d.price, priceLabel: d.priceLabel, live: d.live, outlook: d.outlook, topics, followUp: follow };
}
module.exports._test = { findStocks, build, routeStock, screen, screenFilters, ideasAnswer, compare, glossary, answer, rows };
