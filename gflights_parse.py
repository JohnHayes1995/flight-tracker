"""Read every flight from a Google Flights results page.

The fast-flights library only reads Google's "other flights" list. Google
often puts the cheapest flight in the "top flights" list instead, so this
reads both, and skips any single result it can't read rather than failing
the whole search.
"""

from __future__ import annotations

import json

from selectolax.lexbor import LexborHTMLParser


def _time(v):
    padded = [*(v or []), None, None]
    return (padded[0] or 0, padded[1] or 0)


def parse_all(html: str):
    """Return (flights, error). flights is a list of dicts; error is a string or None."""
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        return [], "results data missing from page"
    js = script.text()
    try:
        data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    except IndexError:
        return [], "results data unreadable"
    if data.endswith("errorHasStatus: true"):
        return [], None  # Google says no flights
    payload = json.loads(data)

    out = []
    for section in (2, 3):  # 2 = top flights, 3 = other flights
        try:
            items = payload[section][0] or []
        except (IndexError, TypeError):
            items = []
        for k in items:
            try:
                flight = k[0]
                price = k[1][0][1]
                segs = []
                for s in flight[2]:
                    segs.append({
                        "from": s[3], "to": s[6],
                        "dep": _time(s[8]), "duration": s[11] or 0,
                    })
                if isinstance(price, (int, float)) and price > 0 and segs:
                    out.append({"price": price, "airlines": flight[1] or [], "segments": segs})
            except (IndexError, TypeError, KeyError):
                continue
    return out, None
