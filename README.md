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
