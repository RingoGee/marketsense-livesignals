"""L0 source spike: can each Live Signals source be read from this machine, and how fresh is it?

Usage:
    python spike.py                  # test all sources, write report/
    python spike.py --only bse,nse   # names containing any of these substrings

Outputs report/spike-<runner>-<UTC timestamp>.json and .md, and prints a table.
Env (all optional): SEC_UA_EMAIL (contact for SEC's User-Agent rule), FINNHUB_KEY, SPIKE_RUNNER (label).
No secrets are written to the report.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from rss import parse_feed
from sources import SOURCES

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TIMEOUT = 20
HERE = Path(__file__).resolve().parent


# ── sessions ────────────────────────────────────────────────────────────────
def make_sessions():
    base = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}

    plain = requests.Session()
    plain.headers.update(base)

    bse = requests.Session()
    bse.headers.update({**base, "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com",
                        "Accept": "application/json, text/plain, */*"})

    nse = requests.Session()
    nse.headers.update({**base, "Referer": "https://www.nseindia.com/", "Accept": "*/*"})
    nse_warm = None
    try:
        r = nse.get("https://www.nseindia.com/", timeout=TIMEOUT)
        nse_warm = r.status_code
    except requests.RequestException as e:
        nse_warm = f"error: {type(e).__name__}"

    sec = requests.Session()
    contact = os.environ.get("SEC_UA_EMAIL", "").strip()
    sec.headers.update({"User-Agent": f"MarketSense-LiveSignals research {contact}".strip(),
                        "Accept-Encoding": "gzip, deflate"})

    return {"plain": plain, "bse": bse, "nse": nse, "sec": sec}, {"nse_warmup": nse_warm,
                                                                  "sec_contact_set": bool(contact)}


# ── date helpers ────────────────────────────────────────────────────────────
DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%d %b %Y %H:%M:%S", "%d %b %Y", "%d-%B-%Y",
]
DATE_RE = re.compile(r"\d{1,4}[-/ ](?:\d{1,2}|[A-Za-z]{3,9})[-/ ]\d{2,4}(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?")


def parse_any_date(s, tz):
    s = s.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def newest_in(obj, tz, limit=5000):
    """Walk JSON/str values, return the newest datetime that is not in the future."""
    now = datetime.now(UTC) + timedelta(hours=1)
    best, seen = None, 0
    stack = [obj]
    while stack and seen < limit:
        o = stack.pop()
        seen += 1
        if isinstance(o, dict):
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
        elif isinstance(o, str) and 8 <= len(o) <= 40:
            for m in DATE_RE.findall(o):
                d = parse_any_date(m, tz)
                if d and d <= now and (best is None or d > best):
                    best = d
    return best


def count_records(obj):
    if isinstance(obj, list):
        return len(obj)
    if isinstance(obj, dict):
        for key in ("Table", "data", "Data", "result", "BLOCK_DEALS_DATA", "BULK_DEALS_DATA"):
            v = obj.get(key)
            if isinstance(v, list):
                return len(v)
        lists = [v for v in obj.values() if isinstance(v, list)]
        if lists:
            return sum(len(v) for v in lists)
        return len(obj)
    return 0


# ── one source ──────────────────────────────────────────────────────────────
def fill_url(src):
    day = datetime.now(IST)
    if src.get("prev_day"):
        day -= timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
    url = src["url"].format(ymd=day.strftime("%Y%m%d"), dmy=day.strftime("%d%m%Y"),
                            dmy_=day.strftime("%d-%m-%Y"),
                            FINNHUB_KEY=os.environ.get("FINNHUB_KEY", ""))
    return url


def classify_http(code, body_head):
    if code in (401, 403, 429) or "access denied" in body_head.lower() or "captcha" in body_head.lower():
        return "blocked"
    if code >= 400:
        return "error"
    return "ok"


def test_yf(src):
    import yfinance as yf
    tickers = src["url"].split()
    t0 = time.time()
    df = yf.download(tickers, period="5d", interval="1d", progress=False, threads=True, auto_adjust=False)
    latency = round(time.time() - t0, 2)
    closes = df["Close"] if "Close" in df else df
    got = [t for t in tickers if t in closes and closes[t].dropna().shape[0] > 0]
    newest = closes.dropna(how="all").index.max() if not closes.empty else None
    newest = newest.to_pydatetime().replace(tzinfo=UTC) if newest is not None else None
    missing = [t for t in tickers if t not in got]
    return dict(status="ok" if got else "empty", http=None, latency_s=latency, items=len(got),
                newest=newest, note=("missing: " + ", ".join(missing)) if missing else "")


def test_source(src, sessions):
    if src.get("needs") and not os.environ.get(src["needs"]):
        return dict(status="skipped", note=f"needs {src['needs']}")
    if src["kind"] == "yf":
        try:
            return test_yf(src)
        except Exception as e:  # noqa: BLE001
            return dict(status="error", note=f"{type(e).__name__}: {e}"[:200])

    sess = sessions.get(src.get("session", "plain"), sessions["plain"])
    url = fill_url(src)
    t0 = time.time()
    try:
        r = sess.get(url, timeout=TIMEOUT)
    except requests.RequestException as e:
        return dict(status="error", latency_s=round(time.time() - t0, 2), note=type(e).__name__)
    latency = round(time.time() - t0, 2)
    head = r.text[:600] if r.text else ""
    status = classify_http(r.status_code, head)
    out = dict(status=status, http=r.status_code, latency_s=latency, bytes=len(r.content), items=0,
               newest=None, note="")
    if status != "ok":
        out["note"] = re.sub(r"\s+", " ", head[:120])
        return out

    tz = IST if src.get("tz") == "IST" else UTC
    kind = src["kind"]
    try:
        if kind == "rss":
            entries = parse_feed(r.content, naive_tz=tz)
            out["items"] = len(entries)
            dates = [e["published"] for e in entries if e.get("published")]
            out["newest"] = max(dates) if dates else None
            if not entries:
                out["status"] = "empty"
                out["note"] = "no RSS/Atom items parsed: " + re.sub(r"\s+", " ", head[:100])
        elif kind == "json":
            data = r.json()
            out["items"] = count_records(data)
            out["newest"] = newest_in(data, tz)
            if out["items"] == 0:
                out["status"] = "empty"
        elif kind == "csv":
            rows = list(csv.reader(io.StringIO(r.text)))
            out["items"] = max(len(rows) - 1, 0)
            out["newest"] = newest_in(rows[:3] + rows[-3:], tz) if rows else None
            if out["items"] == 0:
                out["status"] = "empty"
        elif kind == "text":
            lines = [ln for ln in r.text.splitlines() if ln.strip()]
            out["items"] = len(lines)
            m = re.search(r"/(\d{14})\.", r.text)
            if m:
                out["newest"] = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        elif kind == "html":
            text = r.text
            out["items"] = len(re.findall(r"<a\s", text, flags=re.I))
            out["newest"] = newest_in(re.findall(DATE_RE, text)[:400], tz)
            out["note"] = "reachable; needs a page-specific parser"
            if len(text) < 2000:
                out["status"] = "empty"
                out["note"] = "very small page — likely JS-rendered"
    except (ValueError, json.JSONDecodeError) as e:
        out["status"] = "error"
        out["note"] = f"parse failed ({type(e).__name__}): " + re.sub(r"\s+", " ", head[:100])
    return out


# ── report ──────────────────────────────────────────────────────────────────
def age_minutes(d):
    if not d:
        return None
    return round((datetime.now(UTC) - d.astimezone(UTC)).total_seconds() / 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    filters = [f.strip() for f in args.only.split(",") if f.strip()]
    chosen = [s for s in SOURCES if not filters or any(f in s["name"] for f in filters)]

    runner = os.environ.get("SPIKE_RUNNER") or ("github-actions" if os.environ.get("GITHUB_ACTIONS")
                                                else f"local-{platform.node().split('.')[0]}")
    sessions, meta = make_sessions()
    started = datetime.now(UTC)
    results = []
    for src in chosen:
        res = test_source(src, sessions)
        res = {"name": src["name"], "group": src["group"], "market": src["market"], "kind": src["kind"], **res}
        res["newest_age_min"] = age_minutes(res.get("newest"))
        res["newest"] = res["newest"].astimezone(UTC).isoformat() if res.get("newest") else None
        results.append(res)
        print(f"{res['status']:8} {res['name']:24} http={res.get('http')!s:4} items={res.get('items', 0)!s:5} "
              f"age_min={res['newest_age_min']!s:6} {res.get('latency_s', '')!s:6}s {res.get('note', '')[:70]}")
        time.sleep(0.4)  # be polite; NSE in particular rate-limits bursts

    summary = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    report = {"runner": runner, "started_utc": started.isoformat(), "meta": meta, "summary": summary,
              "results": results}

    outdir = HERE / "report"
    outdir.mkdir(exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%MZ")
    (outdir / f"spike-{runner}-{stamp}.json").write_text(json.dumps(report, indent=2))

    md = [f"# L0 source spike — {runner} — {started.astimezone(IST):%Y-%m-%d %H:%M IST}", "",
          f"Summary: {summary} · NSE warm-up: {meta['nse_warmup']} · SEC contact set: {meta['sec_contact_set']}", "",
          "| Status | Source | Group | Mkt | HTTP | Items | Newest item age (min) | Latency (s) | Note |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        note = (r.get("note") or "").replace("|", "/")[:90]
        md.append(f"| {r['status']} | {r['name']} | {r['group']} | {r['market']} | {r.get('http') or ''} | "
                  f"{r.get('items', '')} | {r['newest_age_min'] if r['newest_age_min'] is not None else ''} | "
                  f"{r.get('latency_s', '')} | {note} |")
    (outdir / f"spike-{runner}-{stamp}.md").write_text("\n".join(md) + "\n")
    print(f"\nSummary: {summary}  → report/spike-{runner}-{stamp}.md")


if __name__ == "__main__":
    main()
