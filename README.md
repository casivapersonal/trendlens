# TrendLens

Momentum Ideas on real NSE prices. A static web app (index.html + picks.json) plus a
Python screener that GitHub Actions runs on a schedule. No server and no Claude needed.

## How it works
- `pipeline/update_picks.py` downloads ~2 years of daily prices (Yahoo Finance) for the stocks in
  `pipeline/syms.txt`, ranks the 300 most-traded by 12-month risk-adjusted momentum (skipping the latest
  month) and refreshes `history.json`. It **never opens or closes an idea**: it updates price/rank on live
  ideas and flags momentum ideas whose rank has fallen below #50.
- Ideas are **approval-based**. `python -m arena ideas scan` (run by `.github/workflows/ideas.yml` at 16:15 IST)
  proposes candidates: daily setups plus the top 20 by momentum rank. A coded judge (no AI) scores each, and a
  decision-maker approves (`ideas approve`) or rejects it. Approved ideas go live in `picks.json` with an entry
  range, stop-loss, target and time frame, and stay live until the decision-maker closes them (`ideas close`).
  The monitor only flags exit signals (stop, target, time up, faded rank). See `trading/TRENDLENS.md`.
- `.github/workflows/refresh.yml` runs the data job (08:45 and 15:45 IST), then commits `picks.json`.
- `api/quotes.js` serves live prices; the page polls it every 60 seconds in market hours. The 10-minute
  live job (`warnings.yml`) pushes price warnings on approved live ideas. Browser notifications are optional.

## One-time setup (about 15 minutes)
1. Create a free GitHub account and a new **private** repository, e.g. `trendlens`.
2. Upload all files from this folder (drag-and-drop in the GitHub web page works;
   include the hidden `.github` folder).
3. In the repo: Settings -> Actions -> General -> Workflow permissions -> "Read and write" -> Save.
4. Actions tab -> "Refresh momentum ideas" -> "Run workflow" once to test (takes ~5 minutes).
5. In Vercel: Add New -> Project -> Import the GitHub repo -> Framework "Other" -> Deploy.
   Add a domain if you like (e.g. a subdomain of kycb.in).

## Push notifications (optional, about 10 minutes)
Alerts for newly posted ideas, closed ideas and price warnings reach your phone or desktop even when the site is closed.
1. On your computer: `pip install cryptography` then `python pipeline/make_vapid_keys.py`.
   Keep the printed keys private.
2. Upstash: create a free Redis database (you already use Upstash for KYCB). Copy its
   REST URL and REST token.
3. Vercel -> Project -> Settings -> Environment Variables: add `VAPID_PUBLIC_KEY`,
   `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`. Redeploy.
4. GitHub repo -> Settings -> Secrets and variables -> Actions: add `VAPID_PRIVATE_KEY`,
   `VAPID_SUBJECT` (e.g. mailto:you@yourdomain), `UPSTASH_REDIS_REST_URL`,
   `UPSTASH_REDIS_REST_TOKEN`, `SITE_URL` (your site address).
5. Open the site and tap "Turn on alerts". On iPhone, first add the site to the Home Screen
   (Share -> Add to Home Screen) and open it from there; iOS only allows web push for installed sites.

## Run the screener on your own computer (optional)
    pip install -r pipeline/requirements.txt
    python pipeline/update_picks.py picks.json picks.json

## Limits to know
- Yahoo Finance data is for research use; for a public product, switch the download step
  to a licensed feed (Upstox / Groww / NSE vendor).
- GitHub may delay scheduled runs by a few minutes at busy times.
- Only Indian stocks are live. US stocks, F&O and commodities are added once each passes its own back-test.
