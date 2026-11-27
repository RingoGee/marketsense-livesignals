"""L1 scanner: poll sources → map to tickers → dedup → write JSON to the store.

Usage:
    python scan.py --store store            # one scan; respects each source's cadence
    python scan.py --store store --all      # poll every source now, ignoring cadence

The store is a plain folder (the gh-pages branch in CI):
    data/events.json             last 48h of non-noise events, newest first (dashboard reads this)
    data/archive/YYYY-MM-DD.jsonl every event, by IST publish date (kept 30 days)
    data/health.json             last poll per source: status, items, new, newest age
    data/stats.json              24h mapping + classification stats
    data/nscore.json             L2 News score per ticker (swing / positional / long-term)
    data/combined.json           L4 combined score + label per liquid ticker, per horizon
    data/alerts.json             L4 market-wide alerts (holdings/watchlist alerts are never published)
    state/seen.json              dedup keys, kept 8 days
    state/signals.json           90-day ledger of scored events (feeds nscore.json)
    state/gemini.json            Gemini calls used today (free-tier cap)
    state/runs.json              3 days of run times and triggers (stats.json → runs)
    ref/                         cached ticker reference lists

L2: every new event gets a `cls` (classify.py); N is recomputed each run (nscore.py).
Secrets come from env only and are never written.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import classify as cl
import combine
import mapping as mp
import nscore
from rss import parse_feed
from sources import SOURCES

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
TIMEOUT = 20
MAX_AGE = timedelta(hours=72)        # ignore items older than this on ingest
EVENTS_WINDOW = timedelta(hours=48)
SEEN_KEEP = timedelta(days=8)
ARCHIVE_DAYS = 30

# What L1 scans, how (handler) and how often (minutes; 0 = every run).
PLAN = {
    "bse_announcements_rss": ("bse", 0),
    "bse_notices_rss": ("policy", 60),
    "edgar_8k": ("edgar", 0),
    "edgar_form4": ("edgar", 0),
    "prnewswire_rss": ("release", 0),
    "businesswire_rss": ("release", 0),
    "finnhub_news": ("finnhub", 0),
    "et_markets_rss": ("news", 0),
    "livemint_markets_rss": ("news", 0),
    "business_standard_rss": ("news", 0),
    "google_news_in": ("news", 30),
    "google_news_us": ("news", 30),
    "sebi_rss": ("policy", 60),
    "rbi_press_rss": ("policy", 60),
    "pib_rss": ("policy", 60),
    "fed_press_rss": ("policy", 60),
    "bea_rss": ("policy", 60),
    "crisil_ratings": ("crisil", 60),
}
# EDGAR "current" feeds: ask for the most entries allowed per call so a busy 10 minutes isn't cut off.
URL_OVERRIDE = {"edgar_8k": ("count=40", "count=100"), "edgar_form4": ("count=40", "count=100")}

# Feeds where a CDN edge can keep serving a stale copy. On 27 Nov, GitHub's US runners got the
# empty post-midnight BSE file 11 hours later, while an Indian connection got the live one.
CACHE_BUST = {"bse_announcements_rss", "bse_notices_rss"}

# BSE filings that never move a price; archived but kept off the dashboard.
NOISE = re.compile(
    r"newspaper publication|trading window|loss of share certificate|duplicate share|"
    r"\bnav\b|net asset value|compliance certificate|reg(?:ulation)?\.?\s*74\s*\(5\)|"
    r"certificate under reg|copy of newspaper|book closure|record date for interest|"
    r"intimation of (?:cut[- ]off|record) date|change in (?:registrar|rta)|"
    r"reconciliation of share capital|statement of investor complaints|closure of trading window|"
    r"structured digital database", re.I)
# Classifier rules whose items are pure noise (page templates, law-firm ads, auctions, Hindi PIB copies).
NOISE_RULES = {"tpl_liveprice", "law_firm", "policy_noise", "non_english"}
# Kept as signal on purpose: Reg 29(1)/29(2) SAST disclosures (large holders crossing 2%/5%).
STOP = set("the a an and or of to in on for with at by from is are as its it this that after over amid "
           "shares stock stocks share price market markets says said today news update live".split())


def log(*a):
    print(*a, flush=True)


# ── sessions ────────────────────────────────────────────────────────────────
def sessions():
    base = {"User-Agent": mp.UA_BROWSER, "Accept-Language": "en-US,en;q=0.9"}
    plain = requests.Session()
    plain.headers.update({**base, "Cache-Control": "no-cache", "Pragma": "no-cache"})
    contact = os.environ.get("SEC_UA_EMAIL", "").strip()
    sec = None
    if contact:
        sec = requests.Session()
        sec.headers.update({"User-Agent": f"MarketSense-LiveSignals research {contact}",
                            "Accept-Encoding": "gzip, deflate"})
    return plain, sec


# ── helpers ─────────────────────────────────────────────────────────────────
def eid(*parts):
    return hashlib.sha1("|".join(p or "" for p in parts).encode()).hexdigest()[:16]


def clean(s, n=400):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def iso(d):
    return d.astimezone(UTC).isoformat(timespec="seconds") if d else None


def parse_iso(s):
    return datetime.fromisoformat(s) if s else None


def tokens(title):
    return {w for w in re.findall(r"[a-z0-9]{3,}", title.lower()) if w not in STOP}


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(p)


def base_event(src, title, link, published, summary, kind, guid=""):
    return dict(id=eid(src["name"], guid or link or title), source=src["name"], kind=kind,
                market=src["market"], published=iso(published), title=clean(title, 300),
                summary=clean(summary), link=link, tickers=[], map_status="none")


def tag(t, market, method, name="", security="equity"):
    return dict(ticker=t, market=market, method=method, name=name, security=security)


# ── handlers: one feed's raw payload → list of events ───────────────────────
def h_bse(src, r, ref):
    out = []
    for e in parse_feed(r.content, naive_tz=IST):
        ex = e.get("extra", {})
        # One PDF can cover hundreds of filings (e.g. an AMC's NAV notice for all its ETFs),
        # so the scrip code is part of the id.
        ev = base_event(src, e["title"], e["link"], e["published"], e["summary"], "filing",
                        f"{ex.get('scripcode', '')}|{e['link']}|{e['summary'][:80]}")
        m = mp.map_bse(e["title"], ex.get("scripcode"), ref)
        ev["map_status"] = m["map_status"]
        ev["category"] = clean(e["summary"], 160)
        if m["map_status"] == "mapped":
            ev["tickers"] = [tag(m["ticker"], "IN", m["map_method"], m["name"], m["security"])]
            if m["security"] == "equity":
                ref.remember_in_name(m["name"], m["ticker"])
            else:
                ev["noise"] = True
        if NOISE.search(e["summary"] or ""):
            ev["noise"] = True
        out.append(ev)
    return out


EDGAR_TITLE = re.compile(r"^(?P<form>[^ ]+) - (?P<name>.*?) \((?P<cik>\d{6,10})\) \((?P<role>[^)]+)\)")
EDGAR_ITEMS = re.compile(r"Item (\d+\.\d+)")
EDGAR_ACC = re.compile(r"AccNo:</b>\s*([\d-]+)|(\d{10}-\d{2}-\d{6})")


def h_edgar(src, r, ref):
    out, by_acc = [], {}
    for e in parse_feed(r.content):
        m = EDGAR_TITLE.match(e["title"] or "")
        if not m:
            continue
        acc = EDGAR_ACC.search(e["summary"] or "") or EDGAR_ACC.search(e["link"] or "")
        acc = (acc.group(1) or acc.group(2)) if acc else e["link"]
        role = m.group("role")
        if src["name"] == "edgar_form4":
            # Form 4 appears once per party; the issuer entry names the company.
            if role != "Issuer":
                by_acc.setdefault(acc, {})["reporter"] = m.group("name")
                continue
        elif role not in ("Filer", "Subject"):
            continue
        ev = base_event(src, e["title"], e["link"], e["published"], e["summary"], "filing", acc)
        ev["form"] = m.group("form")
        ev["items"] = sorted(set(EDGAR_ITEMS.findall(e["summary"] or "")))
        mm = mp.map_cik(ref, m.group("cik"), m.group("name"))
        ev["map_status"] = mm["map_status"]
        if mm["map_status"] == "mapped":
            ev["tickers"] = [tag(mm["ticker"], "US", mm["map_method"], mm["name"])]
        if not ref.cik_to_ticker:
            ev["map_status"] = "unmapped"  # reference list missing: count honestly
        by_acc.setdefault(acc, {})["event"] = ev
    for acc, d in by_acc.items():
        if "event" in d:
            if d.get("reporter"):
                d["event"]["reporter"] = d["reporter"]
            out.append(d["event"])
    return out


def h_release(src, r, ref):
    out = []
    for e in parse_feed(r.content):
        if src["name"] == "businesswire_rss" and "/en/" not in (e["link"] or ""):
            continue  # same release in other languages
        ex = e.get("extra", {})
        ev = base_event(src, e["title"], e["link"], e["published"], e["summary"], "release", e.get("guid"))
        text = f"{e['title']} {e['summary']}"
        hits = mp.tickers_from_text(ref, text)
        issuer = ex.get("contributor", "")
        if not hits and issuer:
            t = mp.ticker_from_name(ref, issuer, "US")
            if t:
                hits = [(t, "US", "issuer_name")]
        ev["issuer"] = issuer
        ev["tickers"] = [tag(t, mk, how) for t, mk, how in hits]
        ev["map_status"] = "mapped" if hits else "none"
        out.append(ev)
    return out


GN_SUFFIX = re.compile(r"\s+-\s+[^-]{2,60}$")


def h_news(src, r, ref):
    out = []
    for e in parse_feed(r.content, naive_tz=IST if src["market"] == "IN" else UTC):
        title = e["title"] or ""
        if src["name"].startswith("google_news"):
            title = GN_SUFFIX.sub("", title)
        ev = base_event(src, title, e["link"], e["published"], e["summary"], "news", e.get("guid"))
        add_news_tickers(ev, ref, f"{title} {clean(e['summary'], 300)}", title)
        out.append(ev)
    return out


def add_news_tickers(ev, ref, text, headline):
    hits = mp.tickers_from_text(ref, text)
    tick = [tag(t, mk, how) for t, mk, how in hits]
    if not tick:
        tick = [tag(t, ev["market"], "name_match") for t in mp.tickers_in_headline(ref, headline, ev["market"])]
    ev["tickers"] = tick
    ev["map_status"] = "mapped" if tick else "none"


def h_finnhub(src, r, ref):
    out = []
    for a in r.json() if isinstance(r.json(), list) else []:
        pub = datetime.fromtimestamp(a.get("datetime", 0), UTC) if a.get("datetime") else None
        ev = base_event(src, a.get("headline", ""), a.get("url", ""), pub, a.get("summary", ""), "news",
                        str(a.get("id", "")))
        rel = [t.strip() for t in (a.get("related") or "").split(",") if t.strip()]
        if rel:
            ev["tickers"] = [tag(t, "US", "finnhub_related") for t in rel[:5]]
            ev["map_status"] = "mapped"
        else:
            add_news_tickers(ev, ref, f"{a.get('headline', '')} {a.get('summary', '')}", a.get("headline", ""))
        out.append(ev)
    return out


def h_policy(src, r, ref):
    out = []
    for e in parse_feed(r.content, naive_tz=IST if src["market"] == "IN" else UTC):
        ev = base_event(src, e["title"], e["link"], e["published"], e["summary"], "policy", e.get("guid"))
        code = (e.get("extra") or {}).get("scripcode")
        if code:
            m = mp.map_bse(e["title"], code, ref)
            if m["map_status"] == "mapped":
                ev["tickers"] = [tag(m["ticker"], "IN", m["map_method"], m["name"], m["security"])]
                ev["map_status"] = "mapped"
        out.append(ev)
    return out


CRISIL_DOCS = "https://www.crisilratings.com/mnt/winshare/Ratings/RatingList/RatingDocs/"


def h_crisil(src, r, ref):
    """CRISIL rating actions (JSON behind its rating-rationale page). Most rated firms are unlisted."""
    out = []
    now = datetime.now(UTC)
    for d in (r.json() or {}).get("docs", []):
        try:
            day = datetime.strptime(d.get("transDate") or d.get("ratingDate"), "%b %d, %Y").replace(tzinfo=IST)
        except (TypeError, ValueError):
            day = None
        # Date only: today's actions get first-seen time, older ones noon IST of their day.
        pub = now if day and day.date() == now.astimezone(IST).date() else (day + timedelta(hours=12) if day else None)
        link = CRISIL_DOCS + d["ratingFileName"] if d.get("ratingFileName") else src["url"]
        ev = base_event(src, d.get("heading", ""), link, pub, "", "rating", d.get("prId") or d.get("ratingFileName"))
        name = d.get("companyName", "")
        t = mp.ticker_from_name(ref, name, "IN")
        ev["issuer"] = name
        if t:
            ev["tickers"] = [tag(t, "IN", "crisil_name", name)]
            ev["map_status"] = "mapped"
        else:
            ev["map_status"] = "unlisted"  # private issuers are the norm here, not a miss
        out.append(ev)
    return out


HANDLERS = dict(crisil=h_crisil, bse=h_bse, edgar=h_edgar, release=h_release, news=h_news, finnhub=h_finnhub, policy=h_policy)


# ── clustering: same story from several sources gets one cluster id ─────────
def assign_clusters(new_events, recent):
    pool = [(e, tokens(e["title"]), {t["ticker"] for t in e["tickers"]})
            for e in recent if e["kind"] in ("news", "release")]
    for ev in new_events:
        ev["cluster"] = ev["id"]
        if ev["kind"] not in ("news", "release"):
            continue
        tk, tt = tokens(ev["title"]), {t["ticker"] for t in ev["tickers"]}
        if len(tk) < 4:
            pool.append((ev, tk, tt))
            continue
        for other, otk, ott in pool:
            if other["source"] == ev["source"] or not otk:
                continue
            jac = len(tk & otk) / len(tk | otk)
            if jac >= 0.6 or (jac >= 0.4 and tt and tt & ott):
                ev["cluster"] = other.get("cluster", other["id"])
                break
        pool.append((ev, tk, tt))


# ── main ────────────────────────────────────────────────────────────────────
def fill_url(src):
    url = src["url"].format(FINNHUB_KEY=os.environ.get("FINNHUB_KEY", ""), ymd="", dmy="", dmy_="")
    if src["name"] in URL_OVERRIDE:
        url = url.replace(*URL_OVERRIDE[src["name"]])
    if src["name"] in CACHE_BUST:
        url += ("&" if "?" in url else "?") + f"_={int(time.time())}"
    return url


def poll(src, plain, sec):
    sess = sec if src.get("session") == "sec" else plain
    t0 = time.time()
    r = sess.get(fill_url(src), timeout=TIMEOUT)
    return r, round(time.time() - t0, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="store")
    ap.add_argument("--all", action="store_true", help="ignore per-source cadence")
    ap.add_argument("--only", default="", help="comma list of source-name substrings")
    args = ap.parse_args()

    store = Path(args.store)
    now = datetime.now(UTC)
    plain, sec = sessions()
    ref = mp.RefData(store, sec, log=log)
    log(f"ref: {len(ref.cik_to_ticker)} SEC CIKs, {len(ref.us_symbols)} US symbols, "
        f"{len(ref.in_names)} India names")

    health = load_json(store / "data/health.json", {})
    seen = load_json(store / "state/seen.json", {})
    events = load_json(store / "data/events.json", [])
    for e in events:  # events stored before L2 have no classification yet
        e.setdefault("cls", cl.classify(e))
    by_name = {s["name"]: s for s in SOURCES}
    only = [f for f in args.only.split(",") if f]

    fresh = []
    for name, (handler, every) in PLAN.items():
        src = by_name.get(name)
        if not src or src.get("disabled") or (only and not any(f in name for f in only)):
            continue
        h = health.setdefault(name, {})
        if src.get("needs") and not os.environ.get(src["needs"]):
            h.update(status="skipped", note=f"needs {src['needs']}")
            continue
        if src.get("session") == "sec" and sec is None:
            h.update(status="skipped", note="needs SEC_UA_EMAIL")
            continue
        last = parse_iso(h.get("last_poll"))
        if not args.all and every and last and now - last < timedelta(minutes=every - 2):
            continue
        try:
            r, lat = poll(src, plain, sec)
        except requests.RequestException as e:
            h.update(status="error", last_poll=iso(now), note=type(e).__name__)
            log(f"error   {name:24} {type(e).__name__}")
            continue
        h.update(last_poll=iso(now), http=r.status_code, latency_s=lat)
        if r.status_code >= 400:
            h.update(status="blocked" if r.status_code in (401, 403, 429) else "error",
                     note=re.sub(r"\s+", " ", r.text[:100]))
            log(f"{h['status']:7} {name:24} http={r.status_code}")
            continue
        try:
            items = HANDLERS[handler](src, r, ref)
        except Exception as e:  # noqa: BLE001 — one bad feed must never stop the run
            h.update(status="error", note=f"parse {type(e).__name__}: {e}"[:160])
            log(f"error   {name:24} parse {type(e).__name__}: {e}")
            continue
        new = 0
        newest = None
        for ev in items:
            pub = parse_iso(ev["published"]) or now
            newest = max(newest, pub) if newest else pub
            if ev["id"] in seen or now - pub > MAX_AGE:
                continue
            if pub > now + timedelta(minutes=10):
                ev["published"] = iso(now)  # clock-skewed feed; don't let it sit on top forever
            if not ev["published"]:
                ev["published"] = iso(now)  # undated item: use first-seen time
            ev["fetched"] = iso(now)
            seen[ev["id"]] = ev["published"] or iso(now)
            fresh.append(ev)
            new += 1
        note = "" if items else "0 items; body starts: " + re.sub(r"\s+", " ", r.text[:120])
        h.update(status="ok" if items else "empty", items=len(items), new=new, newest=iso(newest),
                 bytes=len(r.content), note=note)
        log(f"{h['status']:7} {name:24} items={len(items):5} new={new:5} {lat}s {len(r.content)}B {note}")
        time.sleep(0.3)

    recent = [e for e in events if now - parse_iso(e["published"]) < timedelta(hours=36)]
    assign_clusters(fresh, recent)

    # L2: classify (rules, then Gemini for what the rules leave unclear), mark pure noise
    for ev in fresh:
        ev["cls"] = cl.classify(ev)
        if ev["cls"]["rule"] in NOISE_RULES:
            ev["noise"] = True
    gstate = load_json(store / "state/gemini.json", {})
    gnote = cl.gemini_classify(fresh, gstate, log)
    write_json(store / "state/gemini.json", gstate)

    # archive every new event under its IST publish date
    by_day = {}
    for ev in fresh:
        day = parse_iso(ev["published"]).astimezone(IST).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(ev)
    arch = store / "data/archive"
    arch.mkdir(parents=True, exist_ok=True)
    for day, evs in by_day.items():
        with open(arch / f"{day}.jsonl", "a", encoding="utf-8") as f:
            for ev in evs:
                f.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")
    cutoff_day = (now.astimezone(IST) - timedelta(days=ARCHIVE_DAYS)).strftime("%Y-%m-%d")
    for p in arch.glob("*.jsonl"):
        if p.stem < cutoff_day:
            p.unlink()

    keep = [e for e in events + [e for e in fresh if not e.get("noise")]
            if now - parse_iso(e["published"]) < EVENTS_WINDOW]
    keep.sort(key=lambda e: e["published"], reverse=True)
    write_json(store / "data/events.json", keep)

    # L2: News score. First L2 run seeds the ledger from the archive.
    ledger_p = store / "state/signals.json"
    ledger = load_json(ledger_p, None)
    if ledger is None:
        ledger, back = [], archive_events(arch)
        for ev in back:
            ev.setdefault("cls", cl.classify(ev))
        log(f"ledger: seeding from {len(back)} archived events")
        ledger = nscore.update_ledger(ledger, back, now)
    ledger = nscore.update_ledger(ledger, fresh, now)
    write_json(ledger_p, ledger)
    ns = nscore.compute(ledger, now)
    write_json(store / "data/nscore.json", ns)

    # L4: combined score, labels, alert gate, Telegram (never fails the run)
    try:
        l4 = combine.run(store, ns, fresh, now, log)
    except Exception as e:  # noqa: BLE001
        l4 = dict(note=f"error: {type(e).__name__}: {e}"[:200])
        log(f"L4 error: {l4['note']}")

    seen = {k: v for k, v in seen.items() if v and now - parse_iso(v) < SEEN_KEEP}
    write_json(store / "state/seen.json", seen)
    write_json(store / "data/health.json", health)
    ref.save()

    stats = mapping_stats(arch, now)
    stats.update(run_utc=iso(now), new_this_run=len(fresh), events_48h=len(keep),
                 classification=class_stats(arch, now), gemini=dict(gstate, note=gnote),
                 nscore_tickers=len(ns["tickers"]), ledger_rows=len(ledger),
                 runs=run_log(store, now), l4=l4)
    write_json(store / "data/stats.json", stats)
    if nightly_due(store, now):
        log("nightly: due, requesting a run")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("nightly_due=true\n")
    c24 = stats["classification"]
    log(f"classified 24h: {c24['classified_pct']}% of {c24['mapped_signal']} mapped items; "
        f"N scores for {len(ns['tickers'])} tickers; gemini {gnote}")
    log(f"\nnew={len(fresh)} events_48h={len(keep)} "
        f"filings_unmapped_24h={stats['filings']['unmapped_pct']}% "
        f"(n={stats['filings']['total']})  news_with_ticker={stats['news']['mapped_pct']}%")
    return 0


NIGHTLY_AT_IST = (3, 30)        # after the US close; both markets' previous day is complete
NIGHTLY_RETRY = timedelta(hours=3)


def nightly_due(store: Path, now):
    """True once per IST day after 03:30, unless a request is still fresh. GitHub's own cron for
    nightly.yml is unreliable on this account (PLAN §18), so the scan job starts it."""
    ist = now.astimezone(IST)
    if (ist.hour, ist.minute) < NIGHTLY_AT_IST:
        return False
    today = ist.strftime("%Y-%m-%d")
    if load_json(store / "state/nightly.json", {}).get("ist_date") == today:
        return False
    req = load_json(store / "state/nightly_request.json", {})
    if req.get("at") and now - parse_iso(req["at"]) < NIGHTLY_RETRY:
        return False
    write_json(store / "state/nightly_request.json", dict(at=iso(now), for_ist_date=today))
    return True


def run_log(store: Path, now):
    """Record what started this run; report 24h delivery so a failing trigger shows up.
    `workflow_dispatch` = the external cron (or a manual click), `schedule` = GitHub's own cron."""
    p = store / "state/runs.json"
    runs = load_json(p, [])
    runs.append(dict(t=iso(now), ev=os.environ.get("GITHUB_EVENT_NAME", "local")))
    runs = [r for r in runs if now - parse_iso(r["t"]) < timedelta(days=3)]
    write_json(p, runs)
    day = sorted(parse_iso(r["t"]) for r in runs if now - parse_iso(r["t"]) < timedelta(hours=24))
    gaps = [round((b - a).total_seconds() / 60, 1) for a, b in zip(day, day[1:])]
    by_ev = {}
    for r in runs:
        if now - parse_iso(r["t"]) < timedelta(hours=24):
            by_ev[r["ev"]] = by_ev.get(r["ev"], 0) + 1
    return dict(runs_24h=len(day), by_trigger=by_ev, max_gap_min=max(gaps) if gaps else None,
                median_gap_min=sorted(gaps)[len(gaps) // 2] if gaps else None)


def archive_events(arch: Path, days=3):
    out = []
    for p in sorted(arch.glob("*.jsonl"))[-days:]:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def class_stats(arch: Path, now):
    """24h: share of mapped, non-noise items that got a type other than 'unclear', by type and method."""
    since = now - timedelta(hours=24)
    by_type, by_method, n, ok = {}, {}, 0, 0
    for ev in archive_events(arch, 2):
        if parse_iso(ev.get("fetched") or ev["published"]) < since or ev.get("noise") or not ev.get("tickers"):
            continue
        c = ev.get("cls") or cl.classify(ev)
        n += 1
        ok += c["type"] != "unclear"
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        by_method[c["by"]] = by_method.get(c["by"], 0) + 1
    return dict(mapped_signal=n, classified_pct=round(100 * ok / n, 1) if n else 0.0,
                by_method=by_method, by_type=dict(sorted(by_type.items(), key=lambda kv: -kv[1])))


def mapping_stats(arch: Path, now):
    """24h view. Filings (BSE, EDGAR) carry IDs, so unmapped there is a real miss (target < 2%).
    Releases and news may be about private firms or the market, so only a match rate is reported."""
    since = now - timedelta(hours=24)
    groups = {"filings": {}, "releases": {}, "news": {}, "policy": {}, "ratings": {}}
    gmap = {"filing": "filings", "release": "releases", "news": "news", "policy": "policy", "rating": "ratings"}
    per_source = {}
    for p in sorted(arch.glob("*.jsonl"))[-3:]:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if parse_iso(ev.get("fetched") or ev["published"]) < since:
                continue
            g = groups[gmap[ev["kind"]]]
            g[ev["map_status"]] = g.get(ev["map_status"], 0) + 1
            ps = per_source.setdefault(ev["source"], {})
            ps[ev["map_status"]] = ps.get(ev["map_status"], 0) + 1
    out = {}
    for k, g in groups.items():
        total = sum(g.values())
        out[k] = dict(total=total, **g,
                      unmapped_pct=round(100 * g.get("unmapped", 0) / total, 2) if total else 0.0,
                      mapped_pct=round(100 * g.get("mapped", 0) / total, 1) if total else 0.0)
    out["per_source"] = per_source
    return out


if __name__ == "__main__":
    sys.exit(main())
