"""L2 News score (N), per ticker, on −100…+100, for each horizon.

    contribution = dir × mag × credibility × weight × 0.5^(age / half-life)
    N            = 100 × tanh(Σ contributions / SCALE)

- One contribution per (ticker, story). News and releases are grouped by cluster id; filings by
  (ticker, type, IST day), so a filing posted twice counts once. A story seen in several sources
  gets a credibility bonus (+0.1 per extra source, capped at 1.0).
- A headline naming several companies splits its weight between them (max 3).
- Half-lives: swing 2 days, positional 10, long-term 30 (PLAN §5).
The ledger (state/signals.json) keeps 90 days of scored rows so long-term N has history.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HALF_LIFE = {"swing": 2.0, "positional": 10.0, "long": 30.0}
SCALE = 60.0
KEEP = timedelta(days=90)

# Source credibility (PLAN §4b: filings, rating agencies and press releases are high-credibility).
CRED = {
    "bse_announcements_rss": 1.0, "edgar_8k": 1.0, "edgar_form4": 1.0, "crisil_ratings": 1.0,
    "sebi_rss": 1.0, "bse_notices_rss": 0.9,
    "prnewswire_rss": 0.85, "businesswire_rss": 0.85,
    "et_markets_rss": 0.6, "livemint_markets_rss": 0.6, "business_standard_rss": 0.6, "finnhub_news": 0.6,
    "google_news_in": 0.5, "google_news_us": 0.5,
}
GEMINI_FACTOR = 0.8   # model-classified items count for less than rule-classified ones


def _dt(s):
    return datetime.fromisoformat(s)


def ledger_rows(ev):
    """Scored rows for one classified event: one per ticker, only if it moves N."""
    c = ev.get("cls") or {}
    if ev.get("noise") or not c.get("dir") or not c.get("mag") or not ev.get("tickers"):
        return []
    eq = [t for t in ev["tickers"] if t.get("security", "equity") == "equity"][:3]
    if not eq:
        return []
    w = 1.0 / len(eq) if ev["kind"] in ("news", "release") else 1.0
    cred = CRED.get(ev["source"], 0.5) * (GEMINI_FACTOR if c.get("by") == "gemini" else 1.0)
    day = _dt(ev["published"]).astimezone(IST).strftime("%Y-%m-%d")
    rows = []
    for t in eq:
        story = ev.get("cluster") or ev["id"]
        if ev["kind"] not in ("news", "release"):
            story = f"{t['ticker']}|{c['type']}|{day}"
        rows.append(dict(id=ev["id"], t=t["ticker"], m=t.get("market") or ev["market"], nm=t.get("name", ""),
                         ts=ev["published"], src=ev["source"], story=story, type=c["type"], dir=c["dir"],
                         mag=c["mag"], cred=round(cred, 2), w=round(w, 3), sev=bool(c.get("severe")),
                         title=ev["title"][:160], link=ev.get("link", "")))
    return rows


def update_ledger(ledger, events, now):
    have = {(r["id"], r["t"]) for r in ledger}
    for ev in events:
        for r in ledger_rows(ev):
            if (r["id"], r["t"]) not in have:
                ledger.append(r)
                have.add((r["id"], r["t"]))
    return [r for r in ledger if now - _dt(r["ts"]) < KEEP]


def compute(ledger, now):
    stories = {}
    for r in ledger:
        stories.setdefault((r["t"], r["story"]), []).append(r)
    out = {}
    for (tick, _), rows in stories.items():
        best = max(rows, key=lambda r: r["mag"] * r["cred"] * r["w"])
        nsrc = len({r["src"] for r in rows})
        cred = min(1.0, best["cred"] + 0.1 * (nsrc - 1))
        base = best["dir"] * best["mag"] * cred * best["w"]
        age = max(0.0, (now - _dt(min(r["ts"] for r in rows))).total_seconds() / 86400)
        o = out.setdefault(tick, dict(market=best["m"], name="", sums={h: 0.0 for h in HALF_LIFE},
                                      events_30d=0, last=None, severe=None, top=[]))
        o["name"] = o["name"] or best["nm"]
        contrib = {}
        for h, hl in HALF_LIFE.items():
            contrib[h] = base * 0.5 ** (age / hl)
            o["sums"][h] += contrib[h]
        if age <= 30:
            o["events_30d"] += 1
        o["last"] = max(o["last"] or best["ts"], best["ts"])
        if best["sev"] and best["dir"] < 0 and age <= 7:
            o["severe"] = dict(type=best["type"], ts=best["ts"], title=best["title"], link=best["link"])
        o["top"].append(dict(id=best["id"], type=best["type"], dir=best["dir"], ts=best["ts"], src=best["src"],
                             sources=nsrc, title=best["title"], link=best["link"],
                             pts=round(contrib["positional"], 1)))
    result = {}
    for tick, o in out.items():
        n = {h: round(100 * math.tanh(v / SCALE), 1) for h, v in o["sums"].items()}
        if all(abs(v) < 0.5 for v in n.values()):
            continue
        o["top"].sort(key=lambda x: abs(x["pts"]), reverse=True)
        result[tick] = dict(market=o["market"], name=o["name"], n=n, events_30d=o["events_30d"],
                            last=o["last"], severe=o["severe"], top=o["top"][:3])
    return dict(run_utc=now.isoformat(timespec="seconds"),
                params=dict(half_life_days=HALF_LIFE, scale=SCALE, cred=CRED, gemini_factor=GEMINI_FACTOR),
                tickers=dict(sorted(result.items(), key=lambda kv: -abs(kv[1]["n"]["positional"]))))
