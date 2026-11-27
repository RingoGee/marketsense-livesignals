"""L2 classification: event → type, direction, magnitude, confidence.

Rules first; Gemini (free tier, optional) only for mapped items the rules can't place.
Each event gets a `cls` dict:
    type   event type (see TYPES)
    dir    +1 bullish, -1 bearish, 0 informational
    mag    size in score points before credibility and decay (0 = doesn't move N)
    conf   0..1, how sure the classifier is
    by     "rule" | "gemini" | "none"
    rule   id of the rule that fired (for the hand-check)
    severe True for regulator action, fraud, auditor exit, default, restatement, delisting

Directions are defaults from PLAN-live-signals.md §6. Analysis, not advice.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import requests

# type → (default dir, default mag, severe). Rules may override dir/mag.
TYPES = {
    "fraud_probe":      (-1, 55, True),
    "regulator_action": (-1, 25, False),
    "default":          (-1, 60, True),
    "insolvency":       (-1, 10, False),
    "auditor_exit":     (-1, 45, True),
    "restatement":      (-1, 55, True),
    "delisting":        (-1, 50, True),
    "rating_up":        (+1, 25, False),
    "rating_down":      (-1, 35, True),
    "rating_outlook":   (0, 15, False),
    "rating_action":    (0, 0, False),
    "results":          (0, 25, False),
    "guidance":         (0, 35, False),
    "analyst":          (0, 15, False),
    "order_win":        (+1, 20, False),
    "partnership":      (+1, 10, False),
    "mna":              (+1, 15, False),
    "divestment":       (0, 5, False),
    "buyback":          (+1, 25, False),
    "dividend":         (+1, 5, False),
    "bonus_split":      (+1, 12, False),
    "fundraise":        (-1, 5, False),
    "debt_issue":       (0, 0, False),
    "pledge":           (0, 20, False),
    "stake_change":     (0, 5, False),
    "insider_trade":    (0, 15, False),
    "block_deal":       (0, 10, False),
    "kmp_exit":         (-1, 20, False),
    "director_exit":    (-1, 8, False),
    "officer_exit":     (-1, 3, False),
    "kmp_change":       (0, 0, False),
    "fda":              (0, 20, False),
    "product":          (+1, 5, False),
    "listing":          (0, 5, False),
    "exchange_query":   (0, 0, False),
    "board_meeting":    (0, 0, False),
    "routine":          (0, 0, False),
    "price_move":       (0, 5, False),
    "roundup":          (0, 0, False),
    "law_firm_ad":      (0, 0, False),
    "policy":           (0, 0, False),
    "unclear":          (0, 0, False),
}

UP = r"(?:shin(?:e|es|ing)|up to \d|jump|surge|soar|rall(?:y|ies)|zoom|spurt|gain|rise|rising|climb|rebound|skyrocket|jumps|surges|soars|gains|rises|climbs|rebounds|up\s+\d)"
DOWN = r"(?:fall|slump|plunge|tank|crash|drop|slide|decline|sink|tumble|falls|slumps|plunges|tanks|crashes|drops|slides|declines|sinks|tumbles|down\s+\d|\d+-(?:year|week) low|52-week low)"

# (rule id, type, pattern, dir override or None, mag override or None, scope)
# scope: "any", "filing" (exchange/SEC text), "news" (headlines). First match wins.
R = []


def rule(rid, typ, pat, d=None, mag=None, scope="any"):
    R.append((rid, typ, re.compile(pat, re.I), d, mag, scope))


# ── things that are not events ──────────────────────────────────────────────
rule("tpl_liveprice", "roundup", r"share price (?:live updates|highlights)|stock/share price ,? nse/bse forecast|live quotes$", scope="news")
rule("law_firm", "law_firm_ad", r"(?:investors? (?:have|has) (?:the )?opportunity to lead|securities (?:fraud|class action) lawsuit|sued for securities law|"
     r"class action (?:filed|lawsuit)|law (?:firm|group|offices?)\b.*(?:investors?|shareholders?)|investigation (?:of|on behalf of) .* investors|"
     r"reminds? .* (?:investors|shareholders) .* deadline|lead plaintiff)", scope="news")
rule("roundup", "roundup", r"stocks? to (?:watch|buy|sell|track)|stock (?:recommendations?|picks?|ideas)|recommends? (?:\w+ ){0,2}stocks|\b(?:top|best) \d+ stocks|\d+ stocks (?:that|with|showing|to)|"
     r"among \d+ stocks|top (?:gainers|losers)|gainers (?:and|&) losers|\d+ other stocks|these \d+ stocks|stock movers|stock market (?:live|today|highlights)|market (?:trading guide|wrap|close)|buzzing stocks|"
     r"stocks in (?:focus|news)|sensex.{0,40}nifty|nifty.{0,40}sensex|opening bell|closing bell|pre-market|"
     r"how to check (?:ipo )?allotment|allotment status|gmp\b|grey market premium", scope="news")

rule("exch_query", "exchange_query", r"(?:exchange|bse|nse) (?:has )?sought clarification|clarification (?:sought|on|regarding|/confirmation)|reply to (?:the )?clarification|query raised by (?:the )?(?:stock )?exchange|"
     r"increase in volume|price movement|movement in (?:the )?price|news item appearing|discrepanc")

# ── severe negatives ────────────────────────────────────────────────────────
rule("fraud", "fraud_probe", r"\bfraud\b(?! lawsuit)|forensic audit|money laundering|\bED (?:raid|search|attach)|enforcement directorate|\bCBI\b|"
     r"income[- ]tax (?:search|raid|survey)|search (?:and|&) seizure|siphon|embezzl|ponzi|accounting irregularit|whistle-?blower")
rule("restatement", "restatement", r"non-reliance on previously issued|restate(?:d|ment) (?:of )?(?:financial|result|earnings)")
rule("revised_fin", "regulator_action", r"revised (?:financials?|(?:financial )?results?) (?:after|following|with reference to) .{0,40}(?:query|discrepanc)|discrepanc\w* raised by the exchange", mag=12)
rule("default", "default", r"\bdefault(?:ed|s)? (?:on|in)|delay in (?:payment|servicing|repayment)|missed (?:a |its )?(?:interest|coupon|loan|bond) payment|"
     r"chapter 11|files? for bankruptcy|bankruptcy (?:filing|protection)|going concern|NCLT (?:admits|admitted)|admitted (?:to|into) (?:CIRP|insolvency)|"
     r"initiation of (?:corporate )?insolvency")
rule("insolvency", "insolvency", r"\bCoC\b|committee of creditors|\bCIRP\b|resolution professional|liquidator|\bIBC\b|insolvency")
rule("delisting", "delisting", r"delisting notice|notice of delisting|fail(?:s|ed|ure)? to (?:satisfy|meet|comply with) (?:a |the )?(?:continued )?listing|"
     r"suspension of trading|trading (?:in .* )?suspended|voluntary delisting")
rule("auditor_stat", "auditor_exit", r"resign\w* (?:of|by)? ?(?:the |m/s\.? ?)?(?:\w+ ){0,6}(?:as |from the position of )?(?:the )?statutory auditors?|"
     r"statutory auditors?\W.{0,60}resign|auditor(?:s)? (?:quits?|resigns?)|changes? in (?:registrant'?s )?certifying accountant")
rule("auditor_other", "auditor_exit", r"resign\w* .{0,60}(?:secretarial|internal|cost) auditor|(?:secretarial|internal|cost) auditors?\W.{0,40}resign", mag=10)

# ── regulators ──────────────────────────────────────────────────────────────
rule("sec_enforce", "regulator_action", r"\bSEC (?:charges|sues|fines|settles|probe|investigat)|SEBI (?:bans|bars|order|penal|probe|slaps|impos|attach)|"
     r"(?:debarred|barred) from (?:the )?(?:securities )?market|show[- ]cause notice|adjudication order|settlement order|"
     r"wells notice|department of justice|\bDOJ\b|subpoena|antitrust (?:probe|suit|lawsuit)|competition commission .*(?:penalty|probe|order)|\bCCI (?:penalty|probe|order|raid)", mag=30)
rule("tax_demand", "regulator_action", r"(?:GST|tax|customs|excise|income[- ]tax|demand|penalty)\W.{0,40}(?:order|notice|demand)|"
     r"(?:order|notice|demand)\W.{0,30}(?:GST|tax|customs|excise|penalty|regional|commissioner|assessment)|penalty (?:of|levied|imposed)|"
     r"fine (?:of|imposed)", mag=12)
rule("legal", "regulator_action", r"prosecution|(?:supreme|high) court\b.{0,60}(?:asks|orders|directs|rejects|dismisses|quashes|against)|\bSC (?:asks|orders|directs|rejects|dismisses)|"
     r"(?:sued|lawsuit|suit) (?:by|filed by)|faces? (?:a )?(?:lawsuit|probe|investigation)", mag=10)
rule("regulator_other", "regulator_action", r"warning from (?:SEBI|BSE|NSE)|(?:SEBI|SEC|regulator)s? (?:is |are )?(?:looking into|may (?:review|scrutini[sz]e|probe)|to (?:review|probe|scrutini[sz]e))|SEBI (?:warns|cautions)|administrative warning", mag=12)

# ── credit ratings ──────────────────────────────────────────────────────────
rule("rating_up", "rating_up", r"(?:rating|ratings|outlook)s? (?:\w+ ){0,3}upgraded|upgrade[sd]? (?:of |in )?(?:the )?(?:\w+ ){0,3}(?:credit )?ratings?|credit rating upgrade")
rule("rating_down", "rating_down", r"(?:rating|ratings)s? (?:\w+ ){0,3}downgraded|downgrade[sd]? (?:of |in )?(?:the )?(?:\w+ ){0,3}(?:credit )?ratings?|credit rating downgrade|"
     r"placed (?:on|under) (?:rating )?watch with negative|credit watch negative|rating watch negative|watch negative|watch with negative implications")
rule("rating_neg", "rating_outlook", r"outlook (?:\w+ ){0,3}(?:revised|changed|cut) to negative|negative outlook", d=-1)
rule("rating_pos", "rating_outlook", r"outlook (?:\w+ ){0,3}(?:revised|changed|raised) to positive|positive outlook|watch positive|watch with positive implications|credit watch positive", d=+1, mag=10)
rule("rating_other", "rating_action", r"credit rating|\brating(?:s)? (?:reaffirmed|assigned|migrated|continues|withdrawn)|reaffirm|"
     r"(?:CRISIL|ICRA|CARE|India Ratings|Brickwork|Acuite|Infomerics|Moody'?s|Fitch|S&P Global Ratings)\b.{0,40}rating|"
     r"\b(?:CRISIL|ICRA|CARE Ratings|CareEdge|Acuite|Infomerics|Brickwork)\b")

# ── results and guidance ────────────────────────────────────────────────────
rule("guid_up", "guidance", r"(?:rais|lift|boost|hik|upgrad)\w* (?:its |the |full[- ]year |fy\w* |annual |\d{4} |sales |revenue |profit |earnings )*(?:guidance|outlook|forecast)|guidance (?:raise|hike|upgrade)|"
     r"raised (?:full[- ]year )?guidance|ups (?:its )?(?:guidance|outlook)|(?:strong|higher|upbeat|bullish|robust)\w* (?:\w+ ){0,2}(?:guidance|outlook|forecast)", d=+1)
rule("guid_down", "guidance", r"(?:cut|lower|slash|reduc|trim|downgrad)\w* (?:its |the |full[- ]year |fy\w* |annual |\d{4} )*(?:guidance|outlook|forecast)|"
     r"guts guidance|guidance (?:cut|miss|disappoint)|(?:weak|lower|soft|disappointing|cautious)\w* (?:\w+ ){0,2}(?:guidance|outlook|forecast)|profit warning|warns? (?:on|of) (?:profit|sales|revenue|earnings)", d=-1)
rule("res_beat", "results", r"beat\w* (?:\w+ ){0,3}(?:estimates?|expectations?|forecasts?|consensus|street)|(?:crush|top|exceed|surpass)\w* (?:\w+ ){0,2}(?:estimates?|expectations?)|"
     r"earnings beat|better[- ]than[- ]expected|record (?:profit|revenue|quarter)|"
     r"(?:net |q\d |quarterly |standalone |consolidated )?(?:profit|pat|earnings|revenue|net income|ebitda|sales)\s+(?:\w+ ){0,2}(?:rises?|jumps?|surges?|soars?|climbs?|grows?|up|doubles?|triples?|zooms?|increases?|strengthens?|improves?)\b", d=+1)
rule("res_miss", "results", r"miss\w* (?:\w+ ){0,3}(?:estimates?|expectations?|forecasts?|consensus)|worse[- ]than[- ]expected|"
     r"(?:net |q\d |quarterly |standalone |consolidated )?(?:profit|pat|earnings|revenue|net income|ebitda|sales)\s+(?:\w+ ){0,2}(?:falls?|drops?|declines?|slumps?|plunges?|tanks?|down|halves|dips?|decreases?|shrinks?)\b|"
     r"loss (?:widens|deepens)|swings? to (?:a )?(?:net )?loss|posts? (?:a )?(?:net )?loss", d=-1)
rule("res_plain", "results", r"financial results?|(?:q[1-4]|quarterly|half[- ]year(?:ly)?|annual|audited|unaudited) (?:fy\d+ )?(?:results?|earnings)|"
     r"results? for the (?:quarter|half|year|period)|earnings (?:report|release|call)|results of operations", d=0, mag=0)

# ── analysts (broker views) ─────────────────────────────────────────────────
rule("an_up", "analyst", r"upgrade[sd]? (?:\w+ ){0,4}to \W?(?:buy|outperform|overweight|accumulate|add|strong buy)|"
     r"(?:raises?|hikes?|lifts?) (?:\w+ ){0,3}(?:price )?target|target (?:price )?(?:raised|hiked)|initiat\w* (?:coverage )?(?:\w+ ){0,3}with (?:a )?(?:buy|outperform|overweight)|earnings upgrades|estimates? (?:\w+ ){0,2}(?:raised|rise|increased|upgraded|hiked)|"
     r"highest target price|(?:is|are|turns?) bullish on|top pick", d=+1)
rule("an_down", "analyst", r"downgrade[sd]? (?:\w+ ){0,4}to \W?(?:sell|underperform|underweight|reduce|neutral|hold|equal[- ]weight)|sparks downgrades|"
     r"(?:cuts?|lowers?|slashes?) (?:\w+ ){0,3}(?:price )?target|target (?:price )?(?:cut|lowered|slashed)|initiat\w* (?:coverage )?(?:\w+ ){0,3}with (?:a )?(?:sell|underperform|underweight)|earnings (?:downgrades|cuts)|estimates? (?:\w+ ){0,2}(?:cut|lowered|decreased|reduced|slashed)|"
     r"(?:is|are|turns?) bearish on", d=-1)

rule("an_init", "analyst", r"initiat\w* (?:coverage )?on|price targets?", d=0, mag=0)

# ── promoters, insiders, holders ────────────────────────────────────────────
rule("pledge_rel", "pledge", r"(?:release|revocation|invocation)\w* of (?:the )?(?:\w+ ){0,3}(?:pledge|encumbrance)|pledge (?:released|revoked)", d=+1, mag=10)
rule("pledge_inv", "pledge", r"invocation of pledge|pledge(?:d)? shares (?:invoked|sold)", d=-1, mag=25)
rule("pledge", "pledge", r"\bpledg|encumbrance|regulation 31\b", d=0, mag=0)
rule("promoter_buy", "insider_trade", r"promoters? (?:\w+ ){0,3}(?:buys?|bought|hikes? stake|raises? stake|increases? (?:\w+ )?stake|acquires?|picks? up|creeping acquisition)|"
     r"insiders? (?:are )?(?:buying|bought|buy)|(?:director|ceo|cfo|chairman) (?:\w+ ){0,3}(?:buys|bought) (?:\w+ ){0,3}shares", d=+1)
rule("promoter_sell", "insider_trade", r"promoters? (?:\w+ ){0,3}(?:sells?|sold|offloads?|trims? stake|cuts? stake|pares? stake|reduces? (?:\w+ )?stake|stake sale|dilutes?)|"
     r"insiders? (?:are )?(?:selling|sold|sell)|(?:director|ceo|cfo|chairman) (?:\w+ ){0,3}(?:sells|sold) (?:\w+ ){0,3}shares", d=-1)
rule("block_sell", "block_deal", r"(?:block|bulk) deals?.{0,80}(?:sold|sells?|offload|exit|down \d|falls?)|(?:sold|sells?|offloads?).{0,80}(?:block|bulk) deals?", d=-1)
rule("block_buy", "block_deal", r"(?:block|bulk) deals?.{0,80}(?:bought|buys?|picks? up|acquires?)|(?:bought|buys?|picks? up|acquires?).{0,80}(?:block|bulk) deals?", d=+1)
rule("block", "block_deal", r"(?:block|bulk) deals?", d=0, mag=0)
rule("sast", "stake_change", r"regulation 29\s*\(?[12]|substantial acquisition of shares|regulation 10\s*\(|SAST\b", d=0, mag=0)
rule("pit", "insider_trade", r"regulation 7\s*\(\s*[23]|prohibition of insider trading|\bPIT\b|form 4\b|statement of changes in beneficial ownership", d=0, mag=0)
rule("sch13d", "stake_change", r"SC 13D|SC 13G|schedule 13[DG]|beneficial ownership report", d=0, mag=0)

# ── corporate actions ───────────────────────────────────────────────────────
rule("buyback", "buyback", r"buy[- ]?back|share repurchase|repurchase (?:program|plan|authori)|tender offer for (?:its )?(?:own )?shares")
rule("bonus", "bonus_split", r"bonus (?:issue|shares?|eligibility)|ex-bonus|record date for bonus|stock split|sub-?division of (?:equity )?shares|split of (?:equity )?shares|\d+-for-\d+ (?:stock )?split")
rule("div_special", "dividend", r"special dividend", mag=15)
rule("dividend", "dividend", r"(?:interim|final|quarterly)? ?dividend (?:declared|of|increase|hike|raise)|declares? (?:\w+ ){0,2}dividend|raises? (?:\w+ ){0,2}dividend|(?:increases?|boosts?) (?:quarterly )?dividend")
rule("div_cut", "dividend", r"(?:cuts?|suspends?|eliminates?|omits?) (?:\w+ ){0,2}dividend", d=-1, mag=20)

# ── deals, orders, partnerships ─────────────────────────────────────────────
rule("divest", "divestment", r"sale of (?:its )?(?:entire )?(?:equity|stake|shareholding|business|division|undertaking)|divest|"
     r"(?:sells?|offloads?) (?:\w+ ){0,3}(?:stake|unit|business|division) (?:in|to)|slump sale")
rule("mna_done", "mna", r"completion of (?:the )?acquisition|completes? (?:the )?acquisition|acquisition (?:completed|closed)|completion of acquisition|"
     r"merger (?:completed|effective)", mag=10)
rule("mna", "mna", r"\bacqui(?:re|res|red|sition|ring)\b|to buy (?:\w+ ){0,4}(?:stake|company|firm|business)|\bmerger\b|amalgamation|demerger|scheme of arrangement|takeover|"
     r"open offer|definitive agreement to (?:acquire|merge|combine)|agrees? to (?:buy|acquire|merge)|\bbuys? (?:\w+ ){0,3}(?:land|stake|company|firm|plant)")
rule("order", "order_win", r"(?:bags?|bagging|wins?|winning|wons?|secures?|securing|receives?|receiving|receipt of|bagged|won|secured|received|grabs?|lands (?:a|an|the)|clinch\w*) (?:\S+ ){0,6}(?:orders?|contracts?|work orders?|letters? of (?:award|intent)|LoA|LOI|mandates?|projects?|deals?)\b|"
     r"(?:orders?|contracts?) (?:\w+ ){0,3}worth|order (?:inflow|win|book (?:rises|jumps))|work order|letter of award|\bL-?1 bidder|lowest bidder|\bLoA\b|awarded (?:a |the )?(?:\w+ ){0,4}(?:contract|project|order)|"
     r"(?:contract|order) (?:from|by) (?:the )?(?:\w+ ){0,4}(?:ministry|government|railway|ntpc|nhai|defen[cs]e|army|navy|air force|department|corporation|authority|power|board)")
rule("partner", "partnership", r"\b(?:MoU|memorandum of understanding|joint venture|\bJV\b|tie[- ]?ups?|partnership|partners with|collaborat\w*|strategic alliance|\binks?\b|inking|(?:seals?|signs?|strikes?|inks?) (?:a |an )?(?:\S+ ){0,3}deal with|"
     r"(?:enters?|entering|entered) into (?:an? )?(?:\S+ ){0,4}agreement|signs? (?:an? )?(?:\S+ ){0,3}agreement|execution of (?:an? )?(?:\w+ ){0,3}agreement)")
rule("material_agreement", "partnership", r"entry into a material definitive agreement", mag=8)

# ── management changes ──────────────────────────────────────────────────────
KMP = r"(?:chief executive|ceo|chief financial|cfo|managing director|\bmd\b|whole[- ]?time director|executive director|chairman|chairperson|president|chief operating|coo)"
rule("kmp_exit", "kmp_exit", r"(?:resign\w*|steps? down|step(?:ping)? down|quits?|exit|departure|cessation|ouster|fired|terminat\w*)\b.{0,80}" + KMP + r"|" + KMP +
     r"\b.{0,60}(?:resign\w*|steps? down|quits?|to exit|departs?)")
rule("dir_exit", "director_exit", r"(?:resign\w*|cessation|steps? down)\b.{0,80}\bdirectors?\b|\bdirectors?\b.{0,40}\bresign\w*")
rule("officer_exit", "officer_exit", r"resign\w*.{0,80}(?:company secretary|compliance officer|senior management|\bKMP\b|key managerial|chief \w+ officer)|"
     r"(?:company secretary|compliance officer|senior management personnel|chief \w+ officer)\b.{0,140}resign")
rule("kmp_8k", "kmp_change", r"departure of directors or certain officers|election of directors; appointment of certain officers")
rule("appoint", "kmp_change", r"\bappoint\w*|re-?appointment|change in (?:management|directorate|designation)|correction in (?:the )?name of")

# ── pharma ──────────────────────────────────────────────────────────────────
rule("fda_bad", "fda", r"warning letter|import alert|form 483 (?:with|observations)|\d+ observations|complete response letter|\bCRL\b|clinical hold|"
     r"FDA (?:rejects|declines|refuses)|trial (?:fails|failed|misses|missed)|did not meet (?:its )?primary endpoint", d=-1, mag=25)
rule("fda_good", "fda", r"(?:US ?FDA|FDA|USFDA|EMA|DCGI|CDSCO)\b.{0,40}(?:approv|clear|nod|green ?light|accept|grant)|(?:approv|nod|clearance)\w* (?:\w+ ){0,4}(?:from |by )?(?:the )?(?:US ?FDA|FDA|USFDA)|"
     r"zero observations|\bEIR\b|establishment inspection report|tentative approval|breakthrough (?:therapy )?designation|meets? (?:its )?primary endpoint|positive (?:topline|phase)", d=+1)

# ── capital raising and debt ────────────────────────────────────────────────
rule("fundraise", "fundraise", r"\bQIP\b|qualified institutions? placement|preferential (?:issue|allotment)|rights issue|raising of funds|fund ?rais|"
     r"public offering of (?:common )?(?:stock|shares)|follow-on offering|at-the-market|unregistered sales of equity|warrants? conversion|"
     r"further issue of (?:equity|shares)|\bFPO\b|allotment of [\d,]+ (?:equity )?shares .{0,60}(?:for cash|issue price|preferential)")
rule("debt", "debt_issue", r"non[- ]convertible debentures?|dollar (?:debt|bonds?)|debut (?:dollar )?(?:debt|bond)|bond sale|debt (?:raise|sale|issue|offering)|raises? (?:\S+ ){0,3}(?:via|through) bonds|\bNCDs?\b|commercial paper|bonds? (?:issue|allotment)|creation of (?:a )?direct financial obligation|"
     r"senior (?:secured |unsecured )?notes|term loan|credit facility|security cover|interest payment|redemption of|debenture")
rule("allot", "routine", r"allot\w* of .{0,40}(?:ESOP|ESOS|employee stock|esps)|ESOP|ESOS|employee stock option|allotment of [\d,]+ (?:equity )?shares|exercise of (?:stock )?options")

# ── listings, launches, exchange queries ────────────────────────────────────
rule("list_prem", "listing", r"(?:lists?|listed|listing|debuts?|debut)\b.{0,60}(?:premium|up \d|jumps|soars|higher)|premium to (?:the )?(?:IPO|issue) price", d=+1)
rule("list_disc", "listing", r"(?:lists?|listed|listing|debuts?)\b.{0,60}(?:discount|below (?:the )?(?:ipo|issue) price|down (?:by )?\d|lower|flat)|discount to (?:the )?(?:IPO|issue) price", d=-1)
rule("listing", "listing", r"listing (?:and|&) trading approval|in-principle approval|\bIPO\b|initial public offering|listing (?:of|on|at)|lists? on|set to (?:be )?list|"
     r"change in group of equity|trading members of the exchange are hereby informed|migration to (?:main ?board|nse)", d=0, mag=0)
rule("launch", "product", r"\blaunch\w*|unveil\w*|introduc\w* (?:\w+ ){0,3}(?:product|range|platform|service)|\bcommission(?:ing|ed)\b|"
     r"commercial (?:production|operations?)|capacity expansion|new plant|start(?:s|ed)? production")
rule("board_mtg", "board_meeting", r"board meeting|meeting of the board|board of directors .{0,40}(?:to be held|will be held|scheduled|meet)|"
     r"informed (?:the exchange|bse) that the meeting")
rule("routine_mtg", "routine", r"analyst|investor (?:meet|call|presentation|conference|day)|institutional investor|earnings call|conference call|transcript|"
     r"\bAGM\b|annual general meeting|\bEGM\b|extra ?-?ordinary general meeting|newspaper|conference|participation in|postal ballot|e-?voting|scrutini[sz]er|voting results?|annual report|business responsibility|"
     r"change (?:in|of) (?:the )?(?:registered |corporate )?(?:office )?address|registrar|share transfer agent|\bRTA\b|IEPF|unclaimed|unpaid dividend|"
     r"special window|re-?lodg|corporate governance|secretarial compliance|investor complaints|disclosure of related party|related party transactions|"
     r"intimation of (?:the )?schedule|press release|media release|incorporation of (?:its |a |an )?(?:new )?(?:wholly[- ]owned )?(?:step[- ]down )?subsidiar|regulation fd disclosure|"
     r"submission of matters to a vote|amendments? to (?:articles|bylaws)|financial statements and exhibits|other events|expense ratio")

# ── price action (news) — last, so an event in the same headline wins ───────
rule("px_down", "price_move", r"\b(?:shares?|stocks?|scrip)\b.{0,60}\b" + DOWN + r"\b|\b" + DOWN + r"\b.{0,30}\b(?:shares?|stocks?)\b|lower circuit|wipes? out|erod", d=-1, scope="news")
rule("px_head_up", "price_move", r"^(?:[A-Z][\w&.'’-]*\s){1,4}(?:soars?|jumps?|surges?|rall(?:y|ies)|climbs?|rises?|gains?|zooms?|spikes?|pops?|up \d)", d=+1, scope="news")
rule("px_head_down", "price_move", r"^(?:[A-Z][\w&.'’-]*\s){1,4}(?:plunges?|tumbles?|sinks?|slides?|falls?|drops?|slumps?|tanks?|crashes?|dips?|down \d|down on)", d=-1, scope="news")
rule("px_up", "price_move", r"\b(?:shares?|stocks?|scrip)\b.{0,60}\b" + UP + r"\b|\b" + UP + r"\b.{0,30}\b(?:shares?|stocks?)\b", d=+1, scope="news")
rule("px_high", "price_move", r"(?<!from )(?:record|new|52-week|all[- ]time|\d+-(?:month|year)) high|upper circuit|multibagger", d=+1, scope="news")

# EDGAR 8-K item → rule. Most severe item wins.
ITEM_8K = {
    "1.03": ("default", -1, 60), "4.02": ("restatement", -1, 55), "3.01": ("delisting", -1, 50),
    "4.01": ("auditor_exit", -1, 40), "2.06": ("results", -1, 25), "2.05": ("results", -1, 15),
    "1.02": ("partnership", -1, 10), "5.02": ("kmp_change", 0, 0), "2.01": ("mna", +1, 10),
    "1.01": ("partnership", +1, 8), "2.02": ("results", 0, 0), "3.02": ("fundraise", -1, 5),
    "2.03": ("debt_issue", 0, 0), "8.01": ("routine", 0, 0), "7.01": ("routine", 0, 0),
    "5.07": ("routine", 0, 0), "5.03": ("routine", 0, 0), "9.01": ("routine", 0, 0),
}
ITEM_ORDER = ["1.03", "4.02", "3.01", "4.01", "2.06", "2.05", "1.02", "2.01", "1.01", "3.02", "5.02", "2.02", "2.03"]

POLICY_NOISE = re.compile(r"money market operations|auction|repo \(vrr\)|reverse repo|treasury bills?|government stock|"
                          r"settlement programme|demat auction|\bSLB\b|egr segment|appeal no|lending and borrowing", re.I)
POLICY_RATES = re.compile(r"policy rate|repo rate|federal funds rate|fomc statement|monetary policy (?:statement|committee|decision)|"
                          r"bank rate|crr\b|cash reserve ratio|rate (?:cut|hike)", re.I)
OPAQUE = re.compile(r"^[\s\d.#,:/-]*$|^(?:as )?(?:enclosed|attached|per attachment|please (?:find|refer)(?: the)? (?:attached|enclosed)(?: \w+)?|"
                    r"intimation (?:is )?(?:attached|enclosed)|letter attached|details as per attachment(?: enclosed)?|"
                    r"announcement|intimation|disclosure|press|update|unit|nav|\.)\.?\s*$", re.I)
MIXED_UP = re.compile(r"\b" + UP + r"\b", re.I)
MIXED_DOWN = re.compile(r"\b" + DOWN + r"\b", re.I)
NON_LATIN = re.compile(r"[ऀ-ॿ]")


def _cls(typ, d=None, mag=None, conf=0.8, by="rule", rid=""):
    dd, mm, sev = TYPES[typ]
    d = dd if d is None else d
    mag = mm if mag is None else mag
    return dict(type=typ, dir=d, mag=mag if d else 0, conf=conf, by=by, rule=rid,
                severe=bool(sev and d < 0))


def classify_text(text, scope):
    # A notice of an upcoming board meeting is not the event itself: "meeting ... scheduled on
    # 4 Dec to consider buyback" scores as a half-weight buyback; any other purpose is a calendar item.
    # An outcome ("board approved ...") is the event and goes through the normal rules.
    if BM_NOTICE.search(text) and not BM_OUTCOME.search(text):
        m = BM_PURPOSE.search(text)
        inner = _first(text[m.end():], scope) if m else None
        if inner and inner[1] in BM_TYPES:
            rid, typ, d, mag = inner
            dd, mm, _ = TYPES[typ]
            return "bm_" + rid, typ, dd if d is None else d, round(0.5 * (mm if mag is None else mag))
        return "bm_notice", "board_meeting", 0, 0
    return _first(text, scope)


def _first(text, scope):
    for rid, typ, pat, d, mag, sc in R:
        if sc != "any" and sc != scope:
            continue
        if pat.search(text):
            return rid, typ, d, mag
    return None


BM_NOTICE = re.compile(r"(?:meeting of (?:the )?board|board (?:of directors )?meeting|board of directors)\b.{0,120}?"
                       r"(?:scheduled|convened|proposed to be held|to be held|will be held|will meet)"
                       r"|sets? (?:\w+ ){0,3}board meeting|board (?:to|will) (?:meet|consider|mull)", re.I)
BM_OUTCOME = re.compile(r"outcome|approved|considered and approved|took note|has approved|held on \w+ \d|at its meeting held", re.I)
BM_PURPOSE = re.compile(r"inter alia|to consider|consider and approve|for considering|to evaluate|to mull|to discuss", re.I)
BM_TYPES = {"buyback", "bonus_split", "dividend", "fundraise", "mna", "divestment"}


def classify(ev):
    """Rules only. Returns the cls dict (type 'unclear' with by='none' if nothing fires)."""
    kind, src = ev["kind"], ev["source"]
    title, summ = ev.get("title") or "", ev.get("summary") or ""

    if kind == "policy":
        t = f"{title} {summ}"
        if NON_LATIN.search(title) or POLICY_NOISE.search(title):
            return _cls("routine", conf=0.9, rid="policy_noise")
        if POLICY_RATES.search(t):
            return _cls("policy", conf=0.8, rid="policy_rates")
        hit = classify_text(title, "filing")
        if hit and ev.get("tickers") and hit[1] in ("regulator_action", "fraud_probe"):
            rid, typ, d, mag = hit
            return _cls(typ, d, mag, 0.85, rid=rid)
        return _cls("policy", conf=0.6, rid="policy_other")

    if src.startswith("edgar"):
        if src == "edgar_form4":
            return _cls("insider_trade", 0, 0, 0.9, rid="form4")  # direction needs the XML (code P/S)
        for it in ITEM_ORDER:
            if it in (ev.get("items") or []):
                typ, d, mag = ITEM_8K[it]
                return _cls(typ, d, mag, 0.85, rid=f"8k_{it}")
        form = ev.get("form", "")
        if form.startswith("SC 13"):
            return _cls("stake_change", 0, 0, 0.9, rid="sch13")
        return _cls("routine", conf=0.6, rid="8k_other")

    if src == "crisil_ratings":
        hit = classify_text(title, "filing")
        if hit:
            rid, typ, d, mag = hit
            return _cls(typ, d, mag, 0.95, rid="crisil_" + rid)
        return _cls("rating_action", conf=0.9, rid="crisil_other")

    if kind == "filing":   # BSE: the description is all we have; the PDF holds the detail
        text = summ if summ else title
        if OPAQUE.match(summ.strip()):
            return _cls("unclear", conf=0.0, by="none", rid="opaque")
        hit = classify_text(text, "filing")
        if hit:
            rid, typ, d, mag = hit
            return _cls(typ, d, mag, 0.85, rid=rid)
        return _cls("unclear", conf=0.0, by="none", rid="no_rule")

    # news and press releases: the headline carries the event
    if NON_LATIN.search(title):
        return _cls("routine", conf=0.9, rid="non_english")
    hit = classify_text(title, "news")
    if hit:
        rid, typ, d, mag = hit
        conf = 0.75 if kind == "news" else 0.85
        if typ == "price_move":
            conf = 0.6
            if MIXED_UP.search(title) and MIXED_DOWN.search(title):
                return _cls(typ, 0, 0, 0.6, rid=rid + "_mixed")
        return _cls(typ, d, mag, conf, rid=rid)
    return _cls("unclear", conf=0.0, by="none", rid="no_rule")


# ── Gemini fallback (free tier; optional) ───────────────────────────────────
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TYPES = [t for t in TYPES if t not in ("unclear", "policy")]
PER_RUN = 25          # items per call; one call per run
DAILY_CAP = 150       # calls per UTC day; rules-only past this


def gemini_candidates(events):
    """Mapped, non-noise items the rules left unclear and that carry enough words to judge."""
    out = []
    for ev in events:
        c = ev.get("cls") or {}
        if ev.get("noise") or c.get("by") != "none" or c.get("rule") == "opaque" or not ev.get("tickers"):
            continue
        text = ev["title"] if ev["kind"] != "filing" else (ev.get("summary") or "")
        if len(re.findall(r"[A-Za-z]{3,}", text)) < 5:
            continue
        out.append(ev)
    return out


def gemini_classify(events, state, log):
    """Classify up to PER_RUN unclear items in one call. Never raises; returns a short status."""
    key = os.environ.get("GEMINI_KEY", "").strip()
    if not key:
        return "skipped: no GEMINI_KEY"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day") != today:
        state.update(day=today, calls=0, items=0)
    if state["calls"] >= DAILY_CAP:
        return "skipped: daily cap"
    cands = gemini_candidates(events)[:PER_RUN]
    if not cands:
        return "nothing to send"
    lines = []
    for i, ev in enumerate(cands):
        text = ev["title"] if ev["kind"] != "filing" else f"{ev['title']}: {ev.get('summary', '')}"
        names = ", ".join(t["ticker"] for t in ev["tickers"][:3])
        lines.append(f"{i}. [{ev['kind']}; {names}] {text[:300]}")
    prompt = (
        "Classify each stock-market item by the corporate event it reports, for the named company.\n"
        f"Allowed types: {', '.join(GEMINI_TYPES)}.\n"
        "dir is +1 if the event is usually good for the stock, -1 if usually bad, 0 if neutral or unclear.\n"
        "Use 'routine' for administrative filings, 'roundup' for lists or market wraps, 'price_move' only "
        "when the item reports nothing but a price change. If unsure, use dir 0 and conf below 0.5.\n"
        "Return only JSON: [{\"i\": n, \"type\": \"...\", \"dir\": -1|0|1, \"conf\": 0..1}]\n\n" + "\n".join(lines))
    model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-flash-lite-latest"  # empty repo var → default
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    state["calls"] += 1
    try:
        r = requests.post(GEMINI_URL.format(model=model), json=body, timeout=40,
                          headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    except requests.RequestException as e:
        return f"error: {type(e).__name__}"
    if r.status_code != 200:
        if r.status_code == 429:
            state["calls"] = DAILY_CAP  # free-tier limit hit: rules only until tomorrow
        return f"error: http {r.status_code}"
    try:
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        rows = json.loads(txt)
    except (KeyError, IndexError, ValueError, TypeError):
        return "error: unreadable reply"
    n = 0
    for row in rows if isinstance(rows, list) else []:
        try:
            ev = cands[int(row["i"])]
            typ = str(row["type"])
            d = max(-1, min(1, int(row.get("dir", 0))))
            conf = max(0.0, min(1.0, float(row.get("conf", 0.5))))
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        if typ not in GEMINI_TYPES:
            continue
        c = _cls(typ, d, None, round(conf * 0.8, 2), by="gemini", rid="gemini")  # model output trusted less than rules
        if conf < 0.5:
            c.update(dir=0, mag=0, severe=False)
        ev["cls"] = c
        n += 1
    state["items"] = state.get("items", 0) + n
    log(f"gemini: {n}/{len(cands)} classified ({model}, call {state['calls']} today)")
    return f"ok: {n}/{len(cands)}"
