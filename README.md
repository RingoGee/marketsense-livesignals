# Live Signals scanner (MarketSense)

Zero-cost news and filings scanner for Indian and US stocks. Plan: `../PLAN-live-signals.md`.
Analysis, not advice.

## L0 — source spike (current)
Checks whether each source in `sources.py` can be read, and how fresh its newest item is.

```bash
pip install -r requirements.txt
python spike.py                 # all sources → report/spike-<runner>-<time>.md/.json
python spike.py --only nse,bse  # a subset
```

Optional env vars (never committed; on GitHub they are repo secrets):
- `SEC_UA_EMAIL`: contact address SEC requires in the User-Agent. EDGAR returns 403 without it.
- `FINNHUB_KEY`: Finnhub free-tier key.

On GitHub, `.github/workflows/spike.yml` runs the same test from GitHub's servers (manual
"Run workflow" button, plus twice on weekdays). The report appears in the run summary.

Files: `sources.py` (source registry), `spike.py` (tester), `rss.py` (stdlib RSS/Atom parser,
copied from MarketSense with extra Indian date formats).

## L1 scanner
`scan.py` polls the sources, maps items to tickers, dedups and writes JSON to a store folder.
`scan.yml` runs it on a schedule and publishes the store to the `gh-pages` branch.

```bash
python scan.py --store store --all     # local run, every source
```
Outputs: `store/data/events.json`, `data/archive/*.jsonl`, `data/health.json`, `data/stats.json`.
Secrets (`SEC_UA_EMAIL`, `FINNHUB_KEY`) are read from the environment only and never written.
See PLAN-live-signals.md §16.

## L2 classification and News score
Each new event gets a `cls` (`classify.py`): type, direction, size, confidence and the rule that
fired. Rules handle almost everything; items they can't place go to Gemini's free tier in one
batched call per run, only if the `GEMINI_KEY` secret exists (model: repo variable `GEMINI_MODEL`,
default `gemini-flash-lite-latest`). `nscore.py` turns the last 90 days of scored events into a
News score per stock for swing, positional and long-term horizons.

New outputs: `data/nscore.json` (N per ticker with its top three events), `state/signals.json`
(90-day ledger), `state/gemini.json` (calls used today), and a `classification` block in
`stats.json`. Hand-check: `report/l2-handcheck-2026-11-27.md`. See PLAN-live-signals.md §17.

## Triggering runs (external cron)
GitHub's own cron starts only ~5% of frequent schedules on this account, often hours late (PLAN
§18). Runs are started instead by a free external cron calling GitHub's `workflow_dispatch` API
every 10 minutes. The cron lines in `scan.yml` stay as a backup.

**1. Token (GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token)**
- Name `livesignals-cron`, expiry 1 year (set a reminder to renew)
- Repository access: *Only select repositories* → `marketsense-livesignals`
- Repository permissions: **Actions → Read and write**. Nothing else (Metadata read-only is automatic)
- If the token ever leaks, all it can do is start scans on this repo. Revoke it on the same page.

**2. Job (cron-job.org, free account → Create cronjob)**
- URL: `https://api.github.com/repos/RingoGee/marketsense-livesignals/actions/workflows/scan.yml/dispatches`
- Schedule: every 10 minutes, all days
- Advanced → Request method `POST`; body `{"ref":"main"}`; headers:
  `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
  `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
- Save, then *Test run*: a `204` response means a scan has started (visible under Actions).

**Check it works:** `data/stats.json` → `runs` on gh-pages shows runs in the last 24h, what
started them (`workflow_dispatch` = this cron), and the largest gap in minutes. Expect ~144 runs
and gaps of ~10 minutes.
