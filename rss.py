"""Minimal RSS 2.0 / Atom parser using only the standard library."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ATOM = "{http://www.w3.org/2005/Atom}"


def _text(el):
    return (el.text or "").strip() if el is not None else ""


DEFAULT_TZ = timezone.utc
# Extra formats seen on Indian feeds: BSE "26-Nov-2026 23:32:59", SEBI "26 Nov 2026 22:00:05 +0530"
# and "26 Nov, 2026 +0530", RBI "Thu, 26 Nov 2026 19:25:00" (no zone, IST).
_EXTRA_FORMATS = ["%d-%b-%Y %H:%M:%S", "%d %b %Y %H:%M:%S %z", "%d %b, %Y %z", "%d %b %Y %z",
                  "%a, %d %b %Y %H:%M:%S", "%d-%b-%Y"]


def _to_utc(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=DEFAULT_TZ).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _to_utc(datetime.fromisoformat(s))
    except ValueError:
        pass
    for fmt in _EXTRA_FORMATS:
        try:
            return _to_utc(datetime.strptime(s, fmt))
        except ValueError:
            continue
    try:
        return _to_utc(parsedate_to_datetime(s))
    except Exception:
        return None


def parse_feed(content, naive_tz=None):
    """Parse RSS/Atom. naive_tz: zone for timestamps that carry none (e.g. IST feeds)."""
    global DEFAULT_TZ
    DEFAULT_TZ = naive_tz or timezone.utc
    try:
        return _parse_feed(content)
    finally:
        DEFAULT_TZ = timezone.utc


def _parse_feed(content):
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []

    entries = []
    if root.tag.endswith("feed") or root.tag == ATOM + "feed":
        for entry in root.findall(".//" + ATOM + "entry"):
            title = _text(entry.find(ATOM + "title"))
            link_el = entry.find(ATOM + "link")
            link = link_el.get("href", "") if link_el is not None else ""
            summary = _text(entry.find(ATOM + "summary")) or _text(entry.find(ATOM + "content"))
            pub = _parse_date(_text(entry.find(ATOM + "published")) or _text(entry.find(ATOM + "updated")))
            entries.append({"title": title, "link": link, "summary": summary, "published": pub})
        return entries

    for item in root.findall(".//item"):
        title = _text(item.find("title"))
        link = _text(item.find("link"))
        summary = _text(item.find("description"))
        pub = _parse_date(_text(item.find("pubDate")))
        entries.append({"title": title, "link": link, "summary": summary, "published": pub})
    return entries
