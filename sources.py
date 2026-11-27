"""Registry of every source the Live Signals scanner may poll.

Each entry is tested by spike.py (L0). Fields:
  name     unique id
  group    which plan tier it belongs to (see PLAN-live-signals.md §4 / §4b)
  market   IN / US / GLOBAL
  kind     rss | json | csv | html | text | yf
  url      endpoint (date placeholders: {ymd} {dmy} {dmy_} filled at run time)
  tz       timezone of naive timestamps in the payload (IST or UTC)
  session  "nse" → warm NSE cookies first; "bse" → BSE referer headers
  needs    env var that must be set for the source to run (else "skipped: needs X")
  disabled reason string; the source stays listed for the record but is never polled
"""

SOURCES = [
    # ── India: exchange filings (core, every scan) ──────────────────────────
    # BSE's JSON API (api.bseindia.com) returns 403 Access Denied even from a home connection,
    # so the public RSS is the BSE source. It carries scripcode + IST timestamps; board-meeting
    # notices are filtered out of this same feed by title/description.
    dict(name="bse_announcements_rss", group="core", market="IN", kind="rss", tz="IST",
         url="https://www.bseindia.com/data/xml/announcements.xml"),
    dict(name="bse_notices_rss", group="core", market="IN", kind="rss", tz="IST",
         url="https://www.bseindia.com/data/xml/notices.xml"),
    dict(name="bse_api_announcements", group="core", market="IN", kind="json", session="bse", tz="IST",
         disabled="403 Access Denied everywhere; BSE RSS replaces it",
         url="https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1"
             "&strPrevDate={ymd}&strScrip=&strSearch=P&strToDate={ymd}&strType=C&subcategory=-1"),
    dict(name="nse_announcements", group="core", market="IN", kind="json", session="nse", disabled="NSE blocks GitHub runners (403); owner chose to skip NSE, 27 Nov 2026", tz="IST",
         url="https://www.nseindia.com/api/corporate-announcements?index=equities"),
    dict(name="nse_board_meetings", group="add3", market="IN", kind="json", session="nse", disabled="NSE blocks GitHub runners (403); owner chose to skip NSE, 27 Nov 2026", tz="IST",
         url="https://www.nseindia.com/api/corporate-board-meetings?index=equities"),
    dict(name="nse_insider_pit", group="core", market="IN", kind="json", session="nse", disabled="NSE blocks GitHub runners (403); owner chose to skip NSE, 27 Nov 2026", tz="IST",
         url="https://www.nseindia.com/api/corporates-pit?index=equities"),
    dict(name="nse_large_deals", group="core", market="IN", kind="json", session="nse", disabled="NSE blocks GitHub runners (403); owner chose to skip NSE, 27 Nov 2026", tz="IST",
         url="https://www.nseindia.com/api/snapshot-capital-market-largedeal"),

    # ── India: flows and F&O positioning (addition 4, daily) ────────────────
    dict(name="nse_fii_dii", group="add4", market="IN", kind="json", session="nse", disabled="NSE blocks GitHub runners (403); owner chose to skip NSE, 27 Nov 2026", tz="IST",
         url="https://www.nseindia.com/api/fiidiiTradeReact"),
    dict(name="nse_fo_ban", group="add4", market="IN", kind="csv", session="nse", disabled="NSE blocks GitHub runners (403); owner chose to skip NSE, 27 Nov 2026", tz="IST",
         url="https://nsearchives.nseindia.com/content/fo/fo_secban.csv"),
    dict(name="nse_participant_oi", group="add4", market="IN", kind="csv", session="nse", disabled="NSE blocks GitHub runners (403); owner chose to skip NSE, 27 Nov 2026", tz="IST",
         url="https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{dmy}.csv", prev_day=True),
    dict(name="nsdl_fpi_daily", group="add4", market="IN", kind="html", tz="IST",
         url="https://www.fpi.nsdl.co.in/web/Reports/Latest.aspx"),

    # ── India: regulators and policy (hourly) ───────────────────────────────
    dict(name="sebi_rss", group="policy", market="IN", kind="rss", tz="IST", url="https://www.sebi.gov.in/sebirss.xml"),
    dict(name="rbi_press_rss", group="policy", market="IN", kind="rss", tz="IST",
         url="https://www.rbi.org.in/pressreleases_rss.xml"),
    dict(name="pib_rss", group="policy", market="IN", kind="rss", tz="IST",
         url="https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"),  # serves Hindi whatever Lang says; classifier drops it

    # ── India: rating agencies (addition 2) ─────────────────────────────────
    # The rationale page loads its list from this JSON (found 27 Nov 2026); 50 rows ≈ 1.5 days of actions.
    dict(name="crisil_ratings", group="add2", market="IN", kind="json", tz="IST",
         url="https://www.crisilratings.com/bin/crisil/listing/rating_sme_listing_service.json?cmd=RR&start=0&limit=50"),
    # ICRA (reCAPTCHA), CARE and India Ratings (script-loaded lists) have no open list; listed
    # companies must file rating actions on BSE within 24h, so those reach us via BSE instead.
    dict(name="icra_ratings", group="add2", market="IN", kind="html", disabled="reCAPTCHA; covered by BSE filings",
         url="https://www.icra.in/Rating/RatingList"),
    dict(name="care_ratings", group="add2", market="IN", kind="html", disabled="script-loaded list; covered by BSE filings",
         url="https://www.careratings.com/press-release"),
    dict(name="indiaratings", group="add2", market="IN", kind="html", disabled="script-loaded list; covered by BSE filings",
         url="https://www.indiaratings.co.in/pressReleases"),

    # ── India: news (corroboration) ─────────────────────────────────────────
    dict(name="et_markets_rss", group="news", market="IN", kind="rss",
         url="https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    # Moneycontrol RSS dropped: its newest item was 23 Apr 2024 when tested on 26 Nov 2026 (feed is dead).
    dict(name="livemint_markets_rss", group="news", market="IN", kind="rss",
         url="https://www.livemint.com/rss/markets"),
    dict(name="business_standard_rss", group="news", market="IN", kind="rss",
         url="https://www.business-standard.com/rss/markets-106.rss"),
    dict(name="google_news_in", group="news", market="IN", kind="rss",
         url="https://news.google.com/rss/search?q=NSE+OR+BSE+shares&hl=en-IN&gl=IN&ceid=IN:en"),

    # ── US: filings (core, every scan) ──────────────────────────────────────
    dict(name="edgar_8k", group="core", market="US", kind="rss", session="sec",
         url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&start=0&count=40&output=atom"),
    dict(name="edgar_form4", group="core", market="US", kind="rss", session="sec",
         url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb=&owner=include&start=0&count=40&output=atom"),
    dict(name="finnhub_news", group="core", market="US", kind="json", needs="FINNHUB_KEY",
         url="https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"),

    # ── US: company press releases (addition 1) ─────────────────────────────
    dict(name="globenewswire_rss", group="add1", market="US", kind="rss",
         disabled="bot-blocked (403) from both India and GitHub US runners, 26 Nov 2026",
         url="https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    dict(name="prnewswire_rss", group="add1", market="US", kind="rss",
         url="https://www.prnewswire.com/rss/news-releases-list.rss"),
    dict(name="businesswire_rss", group="add1", market="US", kind="rss",
         url="https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA=="),

    # ── US: macro and policy (hourly) ───────────────────────────────────────
    dict(name="fed_press_rss", group="policy", market="US", kind="rss",
         url="https://www.federalreserve.gov/feeds/press_all.xml"),
    dict(name="bls_rss", group="policy", market="US", kind="rss", url="https://www.bls.gov/feed/bls_latest.rss"),
    dict(name="bea_rss", group="policy", market="US", kind="rss", url="https://apps.bea.gov/rss/rss.xml"),

    # ── US: news (corroboration) ────────────────────────────────────────────
    dict(name="yahoo_ticker_rss", group="news", market="US", kind="rss",
         disabled="rate-limited (429) everywhere; Google News + yfinance cover it",
         url="https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL,MSFT,NVDA&region=US&lang=en-US"),
    dict(name="google_news_us", group="news", market="US", kind="rss",
         url="https://news.google.com/rss/search?q=stock+earnings+OR+guidance&hl=en-US&gl=US&ceid=US:en"),

    # ── Nightly: universe lists, prices, macro ──────────────────────────────
    dict(name="nse_equity_list", group="nightly", market="IN", kind="csv", session="nse", disabled="NSE blocks GitHub runners (403); owner chose to skip NSE, 27 Nov 2026",
         url="https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"),
    dict(name="sec_company_tickers", group="nightly", market="US", kind="json", session="sec",
         url="https://www.sec.gov/files/company_tickers.json"),
    dict(name="yf_prices", group="nightly", market="GLOBAL", kind="yf",
         url="RELIANCE.NS TCS.NS AAPL MSFT ^INDIAVIX CL=F GC=F DX-Y.NYB ^TNX INR=X"),

    # ── Later phase (L4b) — reachability only in L0 ─────────────────────────
    dict(name="gdelt_lastupdate", group="L4b", market="GLOBAL", kind="text",
         url="http://data.gdeltproject.org/gdeltv2/lastupdate.txt"),
    dict(name="us_defense_contracts", group="L4b", market="US", kind="rss",
         url="https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max=10"),
]
