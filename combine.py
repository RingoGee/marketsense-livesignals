"""L4: combined score, labels, alert gate, Telegram.

Runs inside scan.py every 10 minutes, after the News score.

    score = F·w1 + N·w2 + M·w3          (PLAN §5; weights by horizon)
    label: ≥ +40 Accumulate · −15…+40 Hold · −40…−15 Trim · ≤ −40 Exit

Only liquid stocks (data/scores.json from the nightly job) get a combined score and a label.
A missing F or M is dropped and the remaining weights are rescaled; the row says which parts it used.
India swing scores take a small nudge from the FPI 5-day mood (PLAN §4b: flows only weight, never alert).

Alert gate (PLAN §7). Alert only if:
  1. a holding or watchlist stock's positional score moved ≥ 20 points since the day's baseline,
     or its label changed
  2. a severe event (regulator, fraud, auditor exit, default…) hits a holding
  3. a stock not alerted in the last 7 days crosses ±60 positional, on a high-credibility source
     (filing, rating agency, press release) seen in the last 24h. All scored stocks are liquid.
At most one alert per ticker per 6 hours and five per IST day; severe first, then holdings, then
market. Format: TICKER · Hold → Accumulate (+27) · why · link · analysis, not advice.

Privacy. HOLDINGS and WATCHLIST come from repo secrets and are never written to the store,
which is public. Rate-limit records are keyed by an HMAC of the ticker (key: TG_TOKEN), so the
published state doesn't reveal what you hold. Only market-wide alerts (rule 3) are listed in
data/alerts.json. Without TG_TOKEN/TG_CHAT_ID, alerts are logged as a dry run and not sent.
Analysis, not advice.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

IST = timezone(timedelta(hours=5, minutes=30))
WEIGHTS = {"swing": {"F": 0.25, "N": 0.45, "M": 0.30},
           "positional": {"F": 0.45, "N": 0.35, "M": 0.20},
           "long": {"F": 0.70, "N": 0.20, "M": 0.10}}
DEFAULT_H = "positional"
MOOD_WEIGHT = 0.10            # India swing: + 0.1 × FPI mood (mood is −100…+100)
MOVE_PTS = 20
CROSS = 60
PER_TICKER = timedelta(hours=6)
NEW_NAME_DAYS = timedelta(days=7)
DAILY_MAX = 5
HIGH_CRED = 0.85              # filings, SEBI, CRISIL, press releases (nscore.CRED)
FRESH = timedelta(hours=24)
DISCLAIMER = "Analysis, not advice."


def label(x):
    if x is None:
        return None
    if x >= 40:
        return "Accumulate"
    if x >= -15:
        return "Hold"
    if x > -40:
        return "Trim"
    return "Exit"


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def tickers_from_env(name, known):
    """'HDFCBANK, aapl, TCS.NS' → tickers as the store spells them (bare Indian symbols get .NS)."""
    out = set()
    for raw in os.environ.get(name, "").replace(";", ",").replace("\n", ",").split(","):
        t = raw.strip().upper()
        if not t:
            continue
        if t not in known and f"{t}.NS" in known:
            t = f"{t}.NS"
        out.add(t)
    return out


# ── combined score ──────────────────────────────────────────────────────────
def combine(scores, ns, flows):
    mood = (flows or {}).get("mood") or 0.0
    out = {}
    for t, s in scores.items():
        n = (ns.get(t) or {}).get("n") or {"swing": 0.0, "positional": 0.0, "long": 0.0}
        row = dict(name=s["name"], market=s["market"], sector=s["sector"], F=s.get("F"), M=s.get("M"),
                   N=n, F_partial=s.get("F_partial"), tv=s.get("tv"), price=s.get("price"))
        sc, used = {}, None
        for h, w in WEIGHTS.items():
            parts = {"F": s.get("F"), "N": n[h], "M": s.get("M")}
            have = {k: v for k, v in parts.items() if v is not None}
            wsum = sum(w[k] for k in have)
            if wsum < 0.5:
                sc[h] = None
                continue
            x = sum(w[k] * v for k, v in have.items()) / wsum
            if h == "swing" and s["market"] == "IN":
                x += MOOD_WEIGHT * mood
            sc[h] = round(max(-100.0, min(100.0, x)), 1)
            used = "".join(sorted(have))
        row.update(score=sc, label={h: label(v) for h, v in sc.items()}, parts=used)
        out[t] = row
    return out


# ── alert gate ──────────────────────────────────────────────────────────────
def _hkey(t):
    key = os.environ.get("TG_TOKEN", "") or "dry-run"
    return hmac.new(key.encode(), t.encode(), hashlib.sha256).hexdigest()[:16]


def _baseline(store, combined, now):
    """Scores at the first run of the IST day. Moves and label changes are measured against it."""
    p = store / "state/combined_base.json"
    base = _load(p, {})
    today = now.astimezone(IST).strftime("%Y-%m-%d")
    if base.get("date") != today:
        base = dict(date=today, at=now.isoformat(timespec="seconds"),
                    s={t: [r["score"][DEFAULT_H], r["label"][DEFAULT_H]] for t, r in combined.items()})
        _write(p, base)
    return base


def _why(t, ns_row):
    top = (ns_row or {}).get("top") or []
    if top:
        e = top[0]
        return e["title"][:140], e.get("link") or ""
    return "Score moved on fundamentals/momentum (no fresh news)", ""


def candidates(combined, ns, base, holdings, watch, fresh_ids, now):
    """Alert candidates with a priority (lower first). fresh_ids: event ids first seen this run."""
    cands = []
    for t, r in combined.items():
        cur, lab = r["score"][DEFAULT_H], r["label"][DEFAULT_H]
        if cur is None:
            continue
        b_score, b_label = (base["s"].get(t) or [None, None])
        nrow = ns.get(t) or {}
        mine = t in holdings or t in watch
        if mine:
            sev = nrow.get("severe")
            if t in holdings and sev and any(e["id"] in fresh_ids for e in nrow.get("top", [])):
                cands.append((0, t, "severe", b_label, lab, cur, b_score))
                continue
            if b_score is not None and (abs(cur - b_score) >= MOVE_PTS or (b_label and lab != b_label)):
                cands.append((1, t, "move", b_label, lab, cur, b_score))
                continue
        if abs(cur) >= CROSS and (b_score is None or abs(b_score) < CROSS):
            top = nrow.get("top") or []
            ok = any(e.get("src") and _cred(e["src"]) >= HIGH_CRED
                     and now - datetime.fromisoformat(e["ts"]) < FRESH for e in top)
            if ok:
                cands.append((2, t, "cross", b_label, lab, cur, b_score))
    cands.sort(key=lambda c: (c[0], -abs(c[5])))
    return cands


def _cred(src):
    from nscore import CRED
    return CRED.get(src, 0.5)


def fmt(t, r, kind, b_label, lab, cur, b_score, why, link):
    delta = f" ({cur - b_score:+.0f})" if b_score is not None else ""
    head = f"{t} · {b_label} → {lab}{delta}" if b_label and b_label != lab else f"{t} · {lab} {cur:+.0f}{delta}"
    tag = {"severe": "⚠️ Severe event · ", "move": "", "cross": "New name · "}[kind]
    parts = [f"{tag}{head}", f"{r['name']} · F {r['F'] if r['F'] is not None else '–'} · "
             f"N {r['N'][DEFAULT_H]:+.0f} · M {r['M'] if r['M'] is not None else '–'}", why]
    if link.startswith("https://"):
        parts.append(link)
    parts.append(DISCLAIMER)
    return "\n".join(parts)


def send_telegram(text):
    tok, chat = os.environ.get("TG_TOKEN", "").strip(), os.environ.get("TG_CHAT_ID", "").strip()
    if not tok or not chat:
        return "dry-run"
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20,
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
    except requests.RequestException as e:
        return f"error: {type(e).__name__}"
    return "sent" if r.status_code == 200 else f"error: http {r.status_code}"


def run(store: Path, ns_doc, fresh, now, log):
    scores = (_load(store / "data/scores.json", {}) or {}).get("tickers") or {}
    if not scores:
        log("L4: no nightly scores yet; skipped")
        return dict(note="no scores")
    ns = ns_doc.get("tickers", {})
    combined = combine(scores, ns, _load(store / "data/flows.json", {}))
    labels = {}
    for r in combined.values():
        lab = r["label"][DEFAULT_H]
        labels[lab] = labels.get(lab, 0) + 1
    _write(store / "data/combined.json", dict(run_utc=now.isoformat(timespec="seconds"), horizon_default=DEFAULT_H,
                                              weights=WEIGHTS, labels=labels, tickers=combined))
    base = _baseline(store, combined, now)

    holdings = tickers_from_env("HOLDINGS", combined)
    watch = tickers_from_env("WATCHLIST", combined) - holdings
    st = _load(store / "state/alerts.json", {})
    today = now.astimezone(IST).strftime("%Y-%m-%d")
    if st.get("day") != today:
        st = dict(day=today, sent_today=0, last=st.get("last", {}), hello=st.get("hello"))
    last = {k: v for k, v in st.get("last", {}).items()
            if now - datetime.fromisoformat(v) < NEW_NAME_DAYS}
    if os.environ.get("TG_TOKEN") and os.environ.get("TG_CHAT_ID") and not st.get("hello"):
        hs = send_telegram("Live Signals connected. You'll get at most 5 alerts a day: big score moves or "
                           "label changes on your holdings/watchlist, severe events on holdings, and new names "
                           "crossing ±60 on a filing or rating. " + DISCLAIMER)
        log(f"L4: telegram hello {hs}")
        if hs == "sent":
            st["hello"] = now.isoformat(timespec="seconds")
    fresh_ids = {e["id"] for e in fresh}
    pub = _load(store / "data/alerts.json", [])
    out = []
    for prio, t, kind, b_label, lab, cur, b_score in candidates(combined, ns, base, holdings, watch, fresh_ids, now):
        if st["sent_today"] >= DAILY_MAX:
            break
        k = _hkey(t)
        if k in last:
            age = now - datetime.fromisoformat(last[k])
            if age < PER_TICKER or (kind == "cross" and age < NEW_NAME_DAYS):
                continue
        why, link = _why(t, ns.get(t))
        text = fmt(t, combined[t], kind, b_label, lab, cur, b_score, why, link)
        status = send_telegram(text)
        if status.startswith("error"):
            log(f"L4 alert {kind}: telegram {status}")
            continue
        last[k] = now.isoformat(timespec="seconds")
        st["sent_today"] += 1
        out.append(kind)
        if kind == "cross":   # market-wide, safe to publish; holdings alerts never are
            pub.append(dict(at=now.isoformat(timespec="seconds"), ticker=t, label=lab, score=cur,
                            why=why, link=link, status=status))
        # Actions logs are public: never print which holding or watchlist stock alerted.
        log(f"L4 alert ({status}) {kind}: " + (text.splitlines()[0] if kind == "cross" else "[private]"))
    st["last"] = last
    _write(store / "state/alerts.json", st)
    _write(store / "data/alerts.json", [a for a in pub if now - datetime.fromisoformat(a["at"]) < timedelta(days=30)])
    note = dict(scored=len(combined), labels=labels, alerts_this_run=len(out), sent_today=st["sent_today"],
                lists="set" if (holdings or watch) else "none",
                telegram="on" if os.environ.get("TG_TOKEN") and os.environ.get("TG_CHAT_ID") else "dry-run")
    log(f"L4: {len(combined)} scored; labels {labels}; alerts {len(out)} this run, "
        f"{st['sent_today']} today ({note['telegram']})")
    return note
