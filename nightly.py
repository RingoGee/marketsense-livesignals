"""L3 nightly: whole-market Fundamental (F) and Momentum (M) scores, plus India FPI flows.

Usage:
    python nightly.py --store store --out out      # reads caches from store, writes results to out/

Reads (from the store, if present): ref/fundamentals.json (per-stock cache), data/flows.json (history).
Writes to --out, with the same layout as the store, so the publish step just copies it over:
    data/scores.json              liquid stocks: F, M and their parts, liquidity, sector
    data/snapshots/YYYY-MM-DD.json compact daily copy (F, M, price) for the paper-trade ledger (L6)
    data/flows.json               NSDL FPI daily net flows (₹ Cr) + 5-day mood score
    ref/fundamentals.json         refreshed cache

Sources (all free, no key):
- Yahoo screener API (yfinance.screen), paged by region × sector: universe, price, volume, trend
  fields, P/E, P/B, EPS trailing/forward, market cap. ~40s for both markets.
- yfinance .info for liquid stocks only, cached and refreshed after 6 days, under a time budget:
  ROE, operating margin, debt/equity, revenue and earnings growth, free cash flow.
- NSDL FPI "Latest" page.

Scores are ranks, not thresholds: each metric is a percentile within (market, sector) when the
group has ≥ 10 liquid stocks, else within the market. A missing metric is skipped, never zero.
Analysis, not advice.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf
from yfinance import EquityQuery as Q

import mapping as mp

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
SECTORS = ["Technology", "Financial Services", "Healthcare", "Consumer Cyclical", "Industrials",
           "Communication Services", "Consumer Defensive", "Energy", "Basic Materials", "Real Estate", "Utilities"]
# Liquidity floor (PLAN §7): 3-month average daily traded value.
FLOOR = {"IN": 5e7, "US": 5e6}            # ₹5 Cr, $5M
MIN_MCAP = {"in": 2e8, "us": 2e7}          # screener pre-filter, well below anything liquid
US_EXCH = {"NYQ", "NMS", "NCM", "NGM", "ASE", "PCX", "BTS", "NAS", "NYS"}   # no OTC (PNK, OQX…)
IN_EXCH = {"NSI", "BSE"}
FUND_MAX_AGE = timedelta(days=6)
FUND_BUDGET_S = 900                        # stop refreshing after 15 min; the rest waits a night
FUND_PER_NIGHT = 750                       # Yahoo rate-limits one IP after ~1,300 quick lookups (27 Nov)
FUND_WORKERS = 3
GROUP_MIN = 10
SNAP_KEEP_DAYS = 400
FLOWS_KEEP_DAYS = 120


def log(*a):
    print(*a, flush=True)


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ── 1. universe from the Yahoo screener ─────────────────────────────────────
def screen_region(region):
    rows = []
    for sector in SECTORS:
        off = 0
        while True:
            q = Q("and", [Q("eq", ["region", region]), Q("eq", ["sector", sector]),
                          Q("gt", ["intradaymarketcap", MIN_MCAP[region]])])
            for attempt in range(3):
                try:
                    r = yf.screen(q, size=250, offset=off, sortField="intradaymarketcap", sortAsc=False)
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"screen {region}/{sector}/{off}: {type(e).__name__}; retry")
                    time.sleep(3 * (attempt + 1))
            else:
                break
            qs = r.get("quotes", []) or []
            for x in qs:
                x["_sector"] = sector
            rows += qs
            off += 250
            if not qs or off >= (r.get("total") or 0):
                break
    return rows


def build_universe():
    """Liquid equities, one listing per company. India prefers the NSE listing over the BSE copy."""
    out = {}
    for region, market in (("in", "IN"), ("us", "US")):
        rows = screen_region(region)
        log(f"screener {market}: {len(rows)} listings")
        if market == "IN":
            nse_names = {mp.norm_name(x.get("longName") or x.get("shortName") or "")
                         for x in rows if x.get("exchange") == "NSI"}
        for x in rows:
            ex, sym = x.get("exchange"), x.get("symbol") or ""
            if x.get("quoteType") not in (None, "EQUITY"):
                continue
            if market == "US" and ex not in US_EXCH:
                continue
            if market == "IN":
                if ex not in IN_EXCH:
                    continue
                if ex == "BSE" and mp.norm_name(x.get("longName") or x.get("shortName") or "") in nse_names:
                    continue  # same company, NSE listing kept (one ticker per company, as in L1)
            price = num(x.get("regularMarketPrice"))
            adv = num(x.get("averageDailyVolume3Month"))
            tv = price * adv if price and adv else None
            if not tv or tv < FLOOR[market]:
                continue
            key = sym.replace("-", ".") if market == "US" else sym   # L1 uses BRK.B, Yahoo BRK-B
            out[key] = dict(yf=sym, market=market, sector=x["_sector"], exch=ex,
                            name=x.get("shortName") or x.get("longName") or sym, currency=x.get("currency"),
                            price=price, tv=round(tv), mcap=num(x.get("marketCap")),
                            pe=num(x.get("trailingPE")), pb=num(x.get("priceToBook")),
                            eps_ttm=num(x.get("epsTrailingTwelveMonths")), eps_fwd=num(x.get("epsForward")),
                            d50=num(x.get("fiftyDayAverageChangePercent")),
                            d200=num(x.get("twoHundredDayAverageChangePercent")),
                            r52=num(x.get("fiftyTwoWeekChangePercent")),
                            hi52=num(x.get("fiftyTwoWeekHigh")), lo52=num(x.get("fiftyTwoWeekLow")),
                            v10=num(x.get("averageDailyVolume10Day")), v3m=adv,
                            div=num(x.get("dividendYield")), quote_time=x.get("regularMarketTime"))
    return out


# ── 2. fundamentals cache (yfinance .info) ──────────────────────────────────
INFO_FIELDS = dict(roe="returnOnEquity", opm="operatingMargins", de="debtToEquity", revg="revenueGrowth",
                   epsg="earningsGrowth", fcf="freeCashflow", ind="industry")


def refresh_fundamentals(uni, cache, now):
    def stale(t):
        c = cache.get(t)
        return not c or now - datetime.fromisoformat(c["at"]) > FUND_MAX_AGE
    todo = sorted((t for t in uni if stale(t)), key=lambda t: cache.get(t, {}).get("at", ""))  # oldest first
    log(f"fundamentals: {len(uni) - len(todo)} cached, {len(todo)} to refresh (budget {FUND_BUDGET_S}s)")
    t0, done, errors = time.time(), 0, 0

    def one(t):
        info = yf.Ticker(uni[t]["yf"]).info or {}
        return t, {k: (info.get(v) if k == "ind" else num(info.get(v))) for k, v in INFO_FIELDS.items()}

    todo_now = todo[:FUND_PER_NIGHT]
    throttled = False
    for i in range(0, len(todo_now), 30):
        if time.time() - t0 > FUND_BUDGET_S or throttled:
            break
        batch_err = 0
        with ThreadPoolExecutor(FUND_WORKERS) as ex:
            futs = [ex.submit(one, t) for t in todo_now[i:i + 30]]
            for f in as_completed(futs):
                try:
                    t, d = f.result()
                except Exception:  # noqa: BLE001 — rate limit or bad symbol; retried another night
                    errors += 1
                    batch_err += 1
                    continue
                d["at"] = now.isoformat(timespec="seconds")
                cache[t] = d
                done += 1
        if batch_err >= 9:          # ≥ 30% of a batch failing = throttled; stop for tonight
            throttled = True
        time.sleep(1.0)
    log(f"fundamentals: refreshed {done} in {time.time() - t0:.0f}s, {errors} errors"
        + (" (throttled)" if throttled else ""))
    return dict(refreshed=done, pending=len(todo) - done, errors=errors, throttled=throttled)


# ── 3. scoring ──────────────────────────────────────────────────────────────
def pct_ranks(items, key, groups):
    """Percentile rank (0..1) of key within each group; items lacking the key are skipped."""
    out = {}
    for pool, assign in groups.values():
        assign = set(assign)
        vals = sorted((v, t) for t in pool if (v := key(items[t])) is not None)
        n = len(vals)
        if n < 3:
            continue
        i = 0
        while i < n:                       # average rank for ties
            j = i
            while j + 1 < n and vals[j + 1][0] == vals[i][0]:
                j += 1
            r = (i + j) / 2 / (n - 1)
            for k in range(i, j + 1):
                if vals[k][1] in assign:
                    out[vals[k][1]] = r
            i = j + 1
    return out


def grouping(uni):
    """Groups as (rank-against pool, stocks to assign). (market, sector) when it has ≥ GROUP_MIN
    members; stocks in smaller sectors are ranked against their whole market."""
    by_ms, by_m = {}, {}
    for t, u in uni.items():
        by_ms.setdefault((u["market"], u["sector"]), []).append(t)
        by_m.setdefault(u["market"], []).append(t)
    groups = {k: (v, v) for k, v in by_ms.items() if len(v) >= GROUP_MIN}
    for m, members in by_m.items():
        rest = [t for k, v in by_ms.items() if k[0] == m and len(v) < GROUP_MIN for t in v]
        if rest:
            groups[(m, "_market")] = (members, rest)
    return groups, {m: (v, v) for m, v in by_m.items()}


def inv(v):
    return 1.0 / v if v and v > 0 else None


def f_metrics(u, f):
    """Higher is better for every metric. Banks/insurers skip debt and operating margin (not meaningful)."""
    fin = u["sector"] == "Financial Services"
    eps = u.get("eps_ttm")
    earn_yield = (eps / u["price"]) if (eps is not None and u.get("price")) else None   # negative EPS ranks low
    fcf_yield = (f["fcf"] / u["mcap"]) if (f.get("fcf") is not None and u.get("mcap")) else None
    # Growth from a loss isn't meaningful (−5 → −1 reads as +80%), so only from positive earnings.
    eps_fwd_g = ((u["eps_fwd"] - eps) / eps) if (u.get("eps_fwd") is not None and eps and eps > 0) else None
    return {
        "value": {"earn_yield": earn_yield, "book_yield": inv(u.get("pb")), "fcf_yield": None if fin else fcf_yield},
        "quality": {"roe": f.get("roe"), "opm": None if fin else f.get("opm"),
                    "low_debt": None if (fin or f.get("de") is None) else -f["de"]},
        "growth": {"revg": f.get("revg"), "epsg": f.get("epsg"), "eps_fwd_g": eps_fwd_g},
    }


def m_metrics(u):
    pos = None
    if u.get("hi52") and u.get("lo52") is not None and u["hi52"] > u["lo52"] and u.get("price"):
        pos = (u["price"] - u["lo52"]) / (u["hi52"] - u["lo52"])
    return {"r52": u.get("r52"), "d200": u.get("d200"), "d50": u.get("d50"), "pos52": pos}


M_WEIGHTS = {"r52": 0.35, "d200": 0.25, "d50": 0.25, "pos52": 0.15}


def score(uni, cache):
    groups, by_market = grouping(uni)
    fm = {t: f_metrics(u, cache.get(t, {})) for t, u in uni.items()}
    mm = {t: m_metrics(u) for t, u in uni.items()}
    f_ranks = {}
    for grp, mets in (("value", ("earn_yield", "book_yield", "fcf_yield")), ("quality", ("roe", "opm", "low_debt")),
                      ("growth", ("revg", "epsg", "eps_fwd_g"))):
        for m in mets:
            f_ranks[m] = pct_ranks(fm, lambda x, g=grp, m=m: x[g][m], groups)
    m_ranks = {m: pct_ranks(mm, lambda x, m=m: x[m], by_market) for m in M_WEIGHTS}   # momentum vs whole market

    out = {}
    for t, u in uni.items():
        parts, used = {}, 0
        for grp, mets in fm[t].items():
            rs = [f_ranks[m][t] for m in mets if t in f_ranks[m]]
            used += len(rs)
            parts[grp] = round(sum(rs) / len(rs), 3) if rs else None
        avail = [v for v in parts.values() if v is not None]
        F = round((sum(avail) / len(avail) - 0.5) * 200, 1) if len(avail) >= 2 else None
        wsum = sum(w for m, w in M_WEIGHTS.items() if t in m_ranks[m])
        M = None
        if wsum >= 0.6:
            M = round((sum(w * m_ranks[m][t] for m, w in M_WEIGHTS.items() if t in m_ranks[m]) / wsum - 0.5) * 200, 1)
        vr = round(u["v10"] / u["v3m"], 2) if u.get("v10") and u.get("v3m") else None
        f_at = cache.get(t, {}).get("at")
        out[t] = dict(name=u["name"], market=u["market"], sector=u["sector"], industry=cache.get(t, {}).get("ind"),
                      price=u["price"], currency=u["currency"], tv=u["tv"], mcap=u["mcap"],
                      F=F, F_parts=parts, F_metrics=used, F_partial=parts["quality"] is None, F_asof=f_at[:10] if f_at else None,
                      M=M, M_parts={k: (round(v, 4) if v is not None else None) for k, v in mm[t].items()},
                      vol_ratio=vr, pe=u["pe"], pb=u["pb"], div=u["div"])
    return out


# ── 4. NSDL FPI flows ───────────────────────────────────────────────────────
NSDL = "https://www.fpi.nsdl.co.in/web/Reports/Latest.aspx"


def fpi_flows(history, now):
    try:
        r = requests.get(NSDL, headers={"User-Agent": mp.UA_BROWSER}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        return history, f"error: {type(e).__name__}"
    txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " | ", r.text)))
    d = re.search(r"Daily Trends in FPI Investments on (\d{2}-[A-Za-z]{3}-\d{4})", txt)
    eq = re.search(r"\| Equity \|.*?Sub-total \| \| ([\d.,()]+) \| \| ([\d.,()]+) \| \| ([\d.,()-]+) \|", txt)
    if not d or not eq:
        return history, "error: page format changed"

    def val(s):
        s = s.replace(",", "")
        return -float(s.strip("()")) if s.startswith("(") else float(s)
    day = datetime.strptime(d.group(1), "%d-%b-%Y").strftime("%Y-%m-%d")
    rows = {h["date"]: h for h in history.get("daily", [])}
    rows[day] = dict(date=day, equity_buy=val(eq.group(1)), equity_sell=val(eq.group(2)), equity_net=val(eq.group(3)))
    daily = sorted(rows.values(), key=lambda h: h["date"])
    cutoff = (now - timedelta(days=FLOWS_KEEP_DAYS)).strftime("%Y-%m-%d")
    daily = [h for h in daily if h["date"] >= cutoff]
    last5 = daily[-5:]
    net5 = sum(h["equity_net"] for h in last5)
    # Mood for swing weighting only (PLAN §4b): ±₹20,000 Cr over 5 days ≈ ±76.
    mood = round(100 * math.tanh(net5 / 20000), 1)
    return dict(asof=day, net_5d_cr=round(net5, 2), days_in_5d=len(last5), mood=mood, daily=daily), "ok"


# ── main ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="store", help="read caches from here")
    ap.add_argument("--out", default="out", help="write results here")
    ap.add_argument("--fund-budget", type=int, default=FUND_BUDGET_S)
    args = ap.parse_args()
    store, out = Path(args.store), Path(args.out)
    now = datetime.now(UTC)
    globals()["FUND_BUDGET_S"] = args.fund_budget

    t0 = time.time()
    uni = build_universe()
    nin = sum(1 for u in uni.values() if u["market"] == "IN")
    log(f"liquid universe: {len(uni)} ({nin} IN, {len(uni) - nin} US) in {time.time() - t0:.0f}s")
    if len(uni) < 500:
        log("universe too small; screener likely failed. Not publishing.")
        return 1

    cache = load_json(store / "ref/fundamentals.json", {})
    fstat = refresh_fundamentals(uni, cache, now)
    cache = {t: c for t, c in cache.items()
             if t in uni or now - datetime.fromisoformat(c["at"]) < timedelta(days=30)}
    write_json(out / "ref/fundamentals.json", cache)

    scores = score(uni, cache)
    nF = sum(1 for s in scores.values() if s["F"] is not None)
    nM = sum(1 for s in scores.values() if s["M"] is not None)
    day = now.astimezone(IST).strftime("%Y-%m-%d")
    meta = dict(run_utc=now.isoformat(timespec="seconds"), ist_date=day, liquid=len(uni), liquid_in=nin,
                liquid_us=len(uni) - nin, with_F=nF, with_M=nM, fundamentals=fstat,
                floor=FLOOR, group_min=GROUP_MIN, m_weights=M_WEIGHTS, seconds=round(time.time() - t0))
    write_json(out / "data/scores.json", dict(meta=meta, tickers=scores))
    write_json(out / f"data/snapshots/{day}.json",
               {t: [s["F"], s["M"], s["price"]] for t, s in scores.items()})
    log(f"scored: F for {nF}, M for {nM} of {len(uni)} liquid stocks")

    flows, note = fpi_flows(load_json(store / "data/flows.json", {}), now)
    if note == "ok":
        write_json(out / "data/flows.json", flows)
        log(f"FPI flows: {flows['asof']} net ₹{flows['daily'][-1]['equity_net']:,.0f} Cr; "
            f"5-day ₹{flows['net_5d_cr']:,.0f} Cr → mood {flows['mood']}")
    else:
        log(f"FPI flows: {note}")
    write_json(out / "state/nightly.json", dict(meta, flows=note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
