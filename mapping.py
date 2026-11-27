"""Ticker mapping for Live Signals (L1).

India  BSE RSS items carry a numeric scrip code; Yahoo accepts it directly as "<code>.BO", so the
       mapping is exact and needs no reference list. Codes 500000–599999 are equities; ETFs, mutual
       funds and debt are tagged non-equity and kept out of scoring.
US     EDGAR entries carry a CIK → SEC company_tickers.json. Press releases carry an exchange tag
       ("Nasdaq: IQMX") or an issuer name → Nasdaq Trader symbol lists (free, no key).
News   Exchange tags, cashtags, then a conservative exact-name match. Unsure → no ticker; a wrong
       ticker is worse than none.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
REF_MAX_AGE_S = 7 * 86400

# ── name normalising ─────────────────────────────────────────────────────────
_SUFFIX = re.compile(
    r"\b(limited|ltd|inc|incorporated|corp|corporation|co|company|plc|llc|lp|l\.p|n\.v|s\.a|ag|sa|nv|"
    r"holdings?|group|the|common stock|ordinary shares|class [a-c]|american depositary shares|ads|"
    r"depositary shares|units?|warrants?|rights?|shares?|of beneficial interest|\(the\))\b\.?", re.I)
_NONALNUM = re.compile(r"[^a-z0-9& ]+")
# Single-word names that are also ordinary words: never matched on their own.
_COMMON = set("""target general international american national first united global energy capital
financial bank power steel motors industries technologies systems solutions services trust fund india
indian reliance best gold silver oil gas future life health care data digital media green new"""
              .split())


def norm_name(s: str) -> str:
    s = s.lower().replace("’", "'")
    s = re.sub(r"-\$|\$$", "", s)          # BSE marks some names "-$"
    s = re.sub(r"\s+-\s+.*$", "", s)       # Nasdaq "Foo Inc. - Common Stock"
    s = _SUFFIX.sub(" ", s)
    s = _NONALNUM.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# ── India ───────────────────────────────────────────────────────────────────
BSE_TITLE = re.compile(r"^(?P<name>.*?)\s*\((?P<code>\d{6})\)\s*$")
_IN_NONEQ = re.compile(r"\b(etf|exchange traded|mutual fund|mf|nav|fund|fund of fund|liquid|gilt|bees|reit|invit|"
                       r"scheme|dividend (?:payout|reinvest\w*)|growth option|issue price)\b", re.I)
STATIC = Path(__file__).resolve().parent / "ref_static"
# All-caps words in Indian headlines that are not company symbols.
_IN_CAPS_STOP = set("""NSE BSE IPO SEBI RBI FII FIIS DII DIIS FPI GDP CPI GST CEO CFO MD USA US UK EU IT AI
EV PSU PSB SME FMCG OMC MCX NCDEX LIC MSCI FTSE ETF NAV QIP OFS AGM EGM EPS PAT EBITDA YOY QOQ FY Q1 Q2 Q3 Q4
BUY SELL HOLD NIFTY SENSEX INR USD RS DMA RSI VWAP LIVE NEWS TODAY BIG TOP NEW""".split())


def map_bse(title: str, scripcode: str | None, ref: "RefData | None" = None):
    m = BSE_TITLE.match(title or "")
    code = (scripcode or (m.group("code") if m else "") or "").strip()
    name = (m.group("name") if m else title or "").strip().rstrip("-$ ").strip()
    if not code.isdigit():
        return dict(map_status="unmapped", name=name)
    kind = "equity" if 500000 <= int(code) <= 599999 and not _IN_NONEQ.search(name) else "non_equity"
    # One ticker per company everywhere: the NSE symbol when the company is also on NSE (yfinance
    # data is better there), else the BSE code.
    nse = ref.in_nse_names.get(norm_name(name)) if (ref and kind == "equity") else None
    return dict(map_status="mapped", ticker=nse or f"{code}.BO", bse_code=code, name=name, security=kind,
                map_method="bse_scripcode+nse_name" if nse else "bse_scripcode")


# ── reference lists (cached in the store, refreshed weekly) ─────────────────
class RefData:
    def __init__(self, store: Path, sec_session: requests.Session | None, log=print):
        self.dir = store / "ref"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sec = sec_session
        self.log = log
        self.cik_to_ticker: dict[str, dict] = {}
        self.us_symbols: dict[str, str] = {}      # ticker -> name
        self.us_names: dict[str, str] = {}        # normalised name -> ticker
        self.in_names: dict[str, str] = {}        # normalised name -> ticker (from BSE history)
        self.us_single_ok: set[str] = set()       # one-word US names safe in headlines (top 500 by size)
        self.in_nse_names: dict[str, str] = {}    # normalised NSE name -> SYMBOL.NS (static list)
        self.in_symbols: set[str] = set()
        self._load()

    def _fresh(self, path: Path):
        return path.exists() and time.time() - path.stat().st_mtime < REF_MAX_AGE_S

    def _load(self):
        self._load_sec()
        self._load_nasdaqtrader()
        self._load_in_static()
        p = self.dir / "in_names.json"
        if p.exists():
            self.in_names = json.loads(p.read_text())
        for k, t in self.in_nse_names.items():
            if _distinctive(k):
                self.in_names.setdefault(k, t)

    def _load_in_static(self):
        """NSE's equity list, saved once from a Mac (NSE blocks GitHub). Static names only, no live
        NSE data; a company listed after the snapshot falls back to its BSE code."""
        p = STATIC / "in_equity_nse.csv"
        if not p.exists():
            return
        import csv
        for row in csv.DictReader(p.open(encoding="utf-8", errors="ignore")):
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            sym, name = row.get("SYMBOL"), row.get("NAME OF COMPANY")
            if sym and name:
                self.in_nse_names.setdefault(norm_name(name), f"{sym}.NS")
                self.in_symbols.add(sym)

    def _load_sec(self):
        p = self.dir / "sec_company_tickers.json"
        if not self._fresh(p) and self.sec is not None:
            try:
                r = self.sec.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
                if r.ok and r.text.lstrip().startswith("{"):
                    p.write_text(r.text)
                else:
                    self.log(f"ref: SEC company_tickers http {r.status_code}; using cached copy if any")
            except requests.RequestException as e:
                self.log(f"ref: SEC company_tickers {type(e).__name__}")
        if p.exists():
            rows = json.loads(p.read_text())
            for i, row in enumerate(rows.values()):
                cik = str(row["cik_str"]).zfill(10)
                if i < 500:  # SEC orders this file roughly by company size
                    k = norm_name(row["title"])
                    if len(k.split()) == 1:
                        self.us_single_ok.add(k)
                # first ticker per CIK is the primary listing
                self.cik_to_ticker.setdefault(cik, {"ticker": row["ticker"], "name": row["title"]})

    def _load_nasdaqtrader(self):
        p = self.dir / "us_symbols.json"
        if not self._fresh(p):
            syms = {}
            for url, sym_col, name_col, etf_col in [
                ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", 0, 1, 6),
                ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", 0, 1, 4)]:
                try:
                    r = requests.get(url, headers={"User-Agent": UA_BROWSER}, timeout=30)
                    r.raise_for_status()
                except requests.RequestException as e:
                    self.log(f"ref: {url.rsplit('/', 1)[-1]} {type(e).__name__}")
                    continue
                for line in r.text.splitlines()[1:]:
                    f = line.split("|")
                    if len(f) <= etf_col or line.startswith("File Creation"):
                        continue
                    if f[etf_col] == "Y" or "test" in f[name_col].lower():
                        continue
                    syms[f[sym_col].replace(".", "-")] = f[name_col]
            if syms:
                p.write_text(json.dumps(syms))
        if p.exists():
            self.us_symbols = json.loads(p.read_text())
        # Name index: only common stock, and only names that stay distinctive after normalising.
        for t, n in self.us_symbols.items():
            if re.search(r"warrant|right|unit|preferred|notes?\b|debenture|depositary", n, re.I):
                continue
            k = norm_name(n)
            if _distinctive(k):
                self.us_names.setdefault(k, t)
        for row in self.cik_to_ticker.values():
            k = norm_name(row["name"])
            if _distinctive(k):
                self.us_names.setdefault(k, row["ticker"])

    def remember_in_name(self, name: str, ticker: str):
        k = norm_name(name)
        if _distinctive(k):
            self.in_names[k] = ticker

    def save(self):
        (self.dir / "in_names.json").write_text(json.dumps(self.in_names, sort_keys=True))


def _distinctive(k: str) -> bool:
    toks = k.split()
    if not toks:
        return False
    if len(toks) == 1:
        return len(toks[0]) >= 5 and toks[0] not in _COMMON
    return len(k) >= 6


# ── US filings / releases ───────────────────────────────────────────────────
EXCH_TAG = re.compile(
    r"\(\s*(?P<ex>NYSE American|NYSE Arca|NYSE MKT|NYSE|NASDAQ(?:\s*(?:GS|GM|CM))?|Nasdaq(?:\s*(?:GS|GM|CM))?|"
    r"Cboe(?: BZX)?|OTCQX|OTCQB|OTC|NSE|BSE)\s*:\s*(?P<t>[A-Za-z][A-Za-z.\-]{0,6})"
    r"(?:\s*[;,/]\s*[A-Z]+\s*:\s*[A-Za-z.\-]+)*\s*\)")
CASHTAG = re.compile(r"(?<![A-Za-z0-9$])\$([A-Z]{1,5})(?![A-Za-z])")


def map_cik(ref: RefData, cik: str, name: str):
    row = ref.cik_to_ticker.get(cik.zfill(10))
    if row:
        return dict(map_status="mapped", ticker=row["ticker"], name=row["name"], cik=cik,
                    security="equity", map_method="sec_cik")
    # Many filers (funds, private issuers, individuals) have a CIK but no listed stock.
    return dict(map_status="unlisted", name=name, cik=cik)


def tickers_from_text(ref: RefData, text: str, market_hint: str | None = None):
    """Exchange tags first ("NYSE: X", "NSE: Y"), then cashtags. Returns [(ticker, market, method)]."""
    out = []
    for m in EXCH_TAG.finditer(text or ""):
        ex, t = m.group("ex").upper(), m.group("t").upper().replace(".", "-")
        if ex in ("NSE",):
            out.append((f"{t}.NS", "IN", "exchange_tag"))
        elif ex in ("BSE",):
            out.append((f"{t}.BO", "IN", "exchange_tag"))
        elif t in ref.us_symbols or not ref.us_symbols:
            out.append((t, "US", "exchange_tag"))
    for t in CASHTAG.findall(text or ""):
        if t in ref.us_symbols:
            out.append((t, "US", "cashtag"))
    seen, uniq = set(), []
    for row in out:
        if row[0] not in seen:
            seen.add(row[0])
            uniq.append(row)
    return uniq


def ticker_from_name(ref: RefData, name: str, market: str):
    k = norm_name(name or "")
    if not k:
        return None
    idx = ref.in_names if market == "IN" else ref.us_names
    return idx.get(k)


_NAME_SPAN = re.compile(r"[A-Z][A-Za-z&.'\-]+(?:\s+(?:[A-Z][A-Za-z&.'\-]+|&|of|and))*")


_US_CAPS_STOP = set("USA US AI IPO ETF EPS CEO CFO GDP CPI FED SEC FDA EV IT UK EU ALL NEW BIG TOP BUY".split())
_PAREN_TICK = re.compile(r"\(([A-Z]{1,5})\)")
_TICK_STOCK = re.compile(r"\b([A-Z]{2,5})\s+(?:stock|shares)\b", re.I)
_CAPS = re.compile(r"(?<![A-Za-z&])([A-Z][A-Z&]{2,11})(?![A-Za-z])")


def tickers_in_headline(ref: RefData, headline: str, market: str, limit=3):
    """Conservative: explicit ticker forms first, then an exact normalised match of a capitalised
    span against the name index."""
    idx = ref.in_names if market == "IN" else ref.us_names
    found = []
    h = headline or ""
    if market == "US":
        for t in _PAREN_TICK.findall(h) + [m.group(1) for m in _TICK_STOCK.finditer(h) if m.group(1).isupper()]:
            if t in ref.us_symbols and t not in _US_CAPS_STOP and t not in found:
                found.append(t)
    else:
        for t in _CAPS.findall(h):
            if t in ref.in_symbols and t not in _IN_CAPS_STOP and f"{t}.NS" not in found:
                found.append(f"{t}.NS")
    for span in _NAME_SPAN.findall(headline or ""):
        words = span.split()
        # try longest sub-spans first so "Tata Motors" wins over "Tata"
        for n in range(min(len(words), 6), 0, -1):
            hit = None
            for i in range(0, len(words) - n + 1):
                k = norm_name(" ".join(words[i:i + n]))
                if _distinctive(k) and k in idx and (
                        market == "IN" or len(k.split()) > 1 or k in ref.us_single_ok):
                    hit = idx[k]
                    break
            if hit:
                if hit not in found:
                    found.append(hit)
                break
        if len(found) >= limit:
            break
    return found
