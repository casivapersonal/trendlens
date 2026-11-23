# TrendLens trading arena (NSE, paper only)
One system built from three sources, tuned for Indian stocks (Nifty 500 default):
- Breaking Money Trading Agent (MIT, kept intact in `arena/`): paper engine, Indian charges, safety guard, Yahoo/Upstox feeds, Claude or rules brain, replay, report, GUI.
- Day Trader skill (MIT) ideas, rebuilt for NSE: universe scanner with daily setups, risk-based sizing, trades-a-day limit, Time Machine.
- TrendLens: Nifty universe + ASM/GSM exclusion, risk-adjusted momentum rank, fundamentals file, plus the committee below.

How a day runs
1. Morning: `scanner` scans every liquid stock in the universe (20-day breakout on volume, bull flag, oversold bounce, momentum rank) and keeps a pool of 40.
2. Every decision cycle (15 min): the pool is re-ranked on live strength and the top 15 go to the brain (Yahoo cannot serve 500 candle series every 15 minutes).
3. Committee memo per stock goes to the brain: technical, fundamentals (P/E on the live price), news (Google News headlines), macro (India VIX, USD/INR, Brent, S&P 500, US 10-year, Nifty), and a risk officer.
4. Claude (or the rules brain) decides. The engine sizes the order so the loss at the stop stays within `max_loss_per_trade` (charges included), refuses after `max_trades_per_day`, and fills with spread, slippage and Indian charges.
5. Optional Time Machine gate: refuses a BUY when past sessions of the same trade lost money after charges.

Evidence rule (from our own back-test): a technical bull/bear judge did not improve the rank rule, so nothing auto-vetoes a trade except the risk officer.
Fundamentals, news and macro views are evidence for the decision-maker and are labelled "not back-tested".

Commands (run inside trading/):  pip install -r requirements.txt
  python -m unittest discover tests      python -m arena demo      python -m arena check
  python -m arena scan                   python -m arena committee SYM      python -m arena timemachine SYM
  python -m arena run --new              python -m arena replay --days 10    python -m arena compete
Settings: config.toml ([universe], [risk], [timemachine], [committee]). Needs web/fundamentals.json from the TrendLens daily workflow for the fundamentals analyst.

Limits: Yahoo data is unofficial and can lag; news is keyword flags, not sentiment; the setups and charges are untested on real data;
paper results are not real fills. Educational simulation. Not investment advice. Not a SEBI-registered adviser.

## Ideas (the TrendLens workflow)
Real-time trading is switched off (`REALTIME_TRADING = False` in `arena/safety.py`; `run` and the GUI's live start raise `RealtimeBlocked`).
Flow: scan -> committee -> coded judge (`arena/judge.py`, no AI) -> pending -> decision-maker approves -> live idea -> decision-maker closes -> closed idea.

    python -m arena ideas scan               # proposals go to trading/ideas/pending.json
    python -m arena ideas pending            # list them with every judge rule that fired
    python -m arena ideas approve <id> --by "Name"   # posts it to web/picks.json (Live ideas tab) and sends a push alert
    python -m arena ideas reject <id>
    python -m arena ideas monitor            # daily bars: updates chart bars, flags exit signals (stop / target / time up / faded momentum rank)
    python -m arena ideas close <id|SYM> --reason "Target hit" [--price 123.4] --by "Name"   # moves it to Closed ideas (no reason = the flagged exit signal)

Exit signals are only flagged; the decision-maker closes. Set `auto_close = true` in `[ideas]` to close at the signal price.
GitHub: `.github/workflows/ideas.yml` runs monitor + scan at 16:15 IST, and can be run by hand with action/target/reason/price/by.
One store: `web/picks.json`, read by the Trade Ideas tab in `web/index.html`. Candidates are the scanner's setups plus the top 20 on TrendLens momentum rank (entry_rank in `[ideas]`); momentum-based (medium term) ideas are flagged when rank falls below exit_rank (50). Judge weights and levels are starting points, not back-tested.

## Review layer (Day Trader skill ideas, Breaking Money plumbing, committee debate)
Every proposal in `trading/ideas/pending.json` now carries, besides the judge's score and rules (`arena/review.py`, `arena/timemachine.py`):
- `review.bull` / `review.bear` / `review.lean`: bull and bear analysts list their case from the same evidence the judge scores. `review.economic`: India VIX, USD/INR, Brent, S&P 500, US 10-year, Nifty (the macro analyst). Fundamentals and news feed both sides.
- `review.time_machine` (trader-timemachine): simulation on the stock's own daily moves plus a replay of the same stop/target on its past daily bars, charges included. Judge: +/- `timemachine_points` when 20+ past samples exist; `timemachine_gate = true` rejects outright. Labelled "not back-tested". Replay starts overlap, so samples are not independent.
- `entry_state` (trader-entry): triggered / waiting above range / waiting below range / invalidated. Refreshed by `ideas monitor`.
- `size` (trader-budget): quantity so the loss at the stop, delivery charges included, fits `[risk].max_loss_per_trade` and `max_position_pct` of the paper account.
- `risk` (trader-risk): PASS/BLOCK on reward-to-risk (`min_rr`), live-idea capacity and a position that fits the loss limit. A BLOCK demotes an APPROVE to watch, and `ideas approve` refuses it unless `--force`.
- Live ideas get `stop_advice` (trader-exit's 1R idea): once price gains one stop distance, move the stop to the entry. Advice only; the stop level is not changed.
Not ported: trader-analyst (earnings facts; `fundamentals.json` is the nearest), trader-pelosi (US Congress filings; no NSE equivalent in the data used), trader-broker (kept as the approval step; nothing is ever sent to a broker).
The website does not yet render `review`, `size`, `risk`, `entry_state` or `stop_advice`; the judge's rules and committee block still show.

## Licenses
Breaking Money: `LICENSE-BreakingMoney`. Day Trader skill (MIT, Jake Schincariol): `LICENSE-DayTrader`. Keep both with this folder.

## Nov 2026 audit: entry and exit rules (20 fixes)

Back-test: `python pipeline/backtest.py --setups` (downloads ~20 years from Yahoo, caches to backtest_cache.pkl, writes web/backtest.json).
It uses `core.momentum_score`, the same function as the live ranking. Known bias: today's NIFTY 500 members only.

| # | Fix | Status |
|---|-----|--------|
| 1 | Time Machine judged against a no-edge (mirrored) baseline | done |
| 2 | Re-entry block after a stop | tested, hurt returns (18.3% vs 20.4% in 2011-19): off, `reentry_block = false` |
| 3 | Separate momentum (20) and setup (4) slots; momentum sector caps in rank order | done; caps back-tested: helped |
| 4 | Each signal counted once (CEO no longer re-vetoes judge points) | done |
| 5 | Fill re-checked: momentum stop from the fill; setups skipped below 2:1 at the fill | done |
| 6 | Breakout = next open, cancelled below the breakout level | done (breakouts later dropped, see 18) |
| 7 | Liquidity floor scaled to the market level; costs; survivorship stated | done |
| 8 | New ideas only at the 16:15 scan on closing prices | done |
| 9 | One shared momentum score; 12-minus-1 tested | done; 12-1 was worse, not adopted |
| 10 | No Time Machine on momentum ideas | done |
| 11 | No "protect the gain" advice on momentum | done |
| 12 | Setups skipped when results fall inside the hold; momentum shows a badge | done |
| 13 | Headline keywords: warning on momentum, veto only on setups | done |
| 14 | NSE ASM / GSM / results calendar refreshed every morning (pipeline/nse_fetch.py) | done; NSE may block cloud servers, old files are then kept |
| 15 | Circuit-locked stop exits at the next tradable price | done |
| 16 | Scorecard after brokerage, STT, exchange, GST, stamp, 0.1% slippage | done |
| 17 | Benchmark NIFTY 500 (price index; NIFTY 50 kept as a second line) | done |
| 18 | Flag 3-10 sessions < 1.5x ATR; volume vs 50-day average | done; back-test (one signal per stock per 15 sessions, live filters, after costs): flag +0.07R (95% range +0.02 to +0.13, 2,588 trades), breakout -0.08R (-0.10 to -0.06); breakouts no longer posted |
| 19 | 6x ATR momentum stop tested | about neutral (21.2% vs 22.5% with caps; 45.4% vs 45.7%); kept as crash protection |
| 20 | No RSI penalty on strength setups | done |

Momentum back-test with the live rules (stop + caps), after costs: 2011-19 21.2% a year (max fall -28.1%), 2020-26 45.4% (-37.9%);
NIFTY 500 price index 7.9% and 13.8%.

## Intraday segment: exhaustion fade (Nov 2026)
Code: `trading/arena/intraday_ideas.py`, `python -m arena intraday prep|run`, settings in `config.toml [intraday]`, tests in `trading/tests/test_intraday_segment.py`.
- **Rule.** F&O stocks, price >= Rs 20, 20-day traded value >= Rs 5 Cr. Setup at the close: > 25% above the 50-day average, or up > 20% in 5 sessions, or a 20-day high on 1.5x the 50-day volume. Trigger: the 9:15 open is 1.5-8% above the previous close. Short at the open of the 9:20 candle; stop = entry + 1 x daily ATR(14); target = entry - 2 x ATR; whatever is open is squared off at 15:15. At most 3 a day, biggest 5-day run-up first.
- **Evidence.** 5-minute data 2014-2026 (old F&O archive for 2014-21, Hugging Face Upstox data for 2022-26), Indian intraday charges + 0.10% slippage each side: +0.61% a trade in 2014-21 (367 trades), +0.34% in 2022-26 (714 trades), profitable in 4 of 5 years in both. Exit test: no other exit (fixed % stops, gap-fill targets, early exits, trailing) beat 1 ATR / 2R / 15:15 in both periods. Late-entry test: +0.34% at 9:20, +0.16% at 9:30, +0.04% at 10:00, so the setups are published the evening before. The fixed 1.5% / 3% defaults in `[risk]` belong only to the switched-off real-time arena engine.
- **Flow.** 16:15 IST "Trade ideas" run: `intraday prep` closes anything still open, refreshes the F&O list (NSE, else Upstox), builds conditional setups for the next session (judge: results due up to the next session, NSE surveillance, serious news, stop > 8%), writes daily bars for the cards. 09:20 onwards (`intraday.yml`): `intraday run` waits until 10:00 for the 9:15 and 9:20 candles, the CEO posts the triggered ones with the 9:20 open as entry, then stop / target / 15:15 exits every 5 minutes (until 16:00 so a late run still books the 15:15 candle). No price data = no result counted.
- **Website.** Intraday ideas use the same idea cards, About-trade pop-up, chart and PDF as the other ideas, in their own section of Live and Closed ideas and under the "Intraday" filter; the Scorecard has an Intraday filter (shorts, after costs).
