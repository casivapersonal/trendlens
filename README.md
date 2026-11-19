# TrendLens

Momentum Ideas on real NSE prices. A static web app (index.html + picks.json) plus a
Python screener that GitHub Actions runs on a schedule. No server and no Claude needed.

## How it works
- `pipeline/update_picks.py` downloads ~2 years of daily prices (Yahoo Finance) for the
  stocks in `pipeline/syms.txt`, ranks the 300 most-traded by 12-month momentum
  (skipping the latest month), and replays the rules:
  - ENTRY alert: stock enters the top 20  -> Live ideas
  - EXIT alert: rank falls below #50, or it leaves the top-300 -> Closed ideas (with reason and result)
- `.github/workflows/refresh.yml` runs it 4 times a trading day (09:25, 11:25, 13:25 and after the
  close at 15:45 IST), then commits `picks.json`. Vercel redeploys on every commit.
- `api/quotes.js` (Vercel serverless function) serves live prices; the page polls it every
  60 seconds in market hours to update prices, P&L and intraday alerts (sharp moves,
  new 20-day lows, breaks below the 50-day average). Browser notifications are optional.
- During market hours the day's alerts are marked "Provisional"; the after-close run confirms them.

## One-time setup (about 15 minutes)
1. Create a free GitHub account and a new **private** repository, e.g. `trendlens`.
2. Upload all files from this folder (drag-and-drop in the GitHub web page works;
   include the hidden `.github` folder).
3. In the repo: Settings -> Actions -> General -> Workflow permissions -> "Read and write" -> Save.
4. Actions tab -> "Refresh momentum ideas" -> "Run workflow" once to test (takes ~5 minutes).
5. In Vercel: Add New -> Project -> Import the GitHub repo -> Framework "Other" -> Deploy.
   Add a domain if you like (e.g. a subdomain of kycb.in).

## Push notifications (optional, about 10 minutes)
Entry and exit alerts reach your phone or desktop even when the site is closed.
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
