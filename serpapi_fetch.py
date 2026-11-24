"""Fetch Google Flights results through SerpApi (serpapi.com).

Needs the API key in the SERPAPI_KEY environment variable. On GitHub this
comes from the repository secret of the same name, so the key never appears
in the code or the repo.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://serpapi.com/search.json"
NO_RESULTS = "hasn't returned any results"


def fetch(legs, kind, passengers, currency, max_minutes=None, max_stops=None):
    """Return the cheapest itinerary dict, or None if Google has no flights."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        raise RuntimeError("SERPAPI_KEY secret is not set")
    (a, b, out_date) = legs[0]
    params = {
        "engine": "google_flights",
        "api_key": key,
        "departure_id": a,
        "arrival_id": b,
        "outbound_date": out_date,
        "type": "1" if kind == "return" else "2",
        "adults": str(passengers),
        "travel_class": "1",
        "currency": currency,
        "hl": "en",
        "gl": "au",
    }
    if kind == "return":
        params["return_date"] = legs[1][2]
    if max_minutes:
        params["max_duration"] = str(max_minutes)
    if max_stops is not None:
        # SerpApi: 1 = nonstop only, 2 = up to 1 stop, 3 = up to 2 stops
        params["stops"] = str(min(max_stops, 2) + 1)

    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=90) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        if NO_RESULTS in body:
            return None
        raise RuntimeError(f"SerpApi HTTP {e.code}: {body}")

    if data.get("error"):
        if NO_RESULTS in data["error"]:
            return None
        raise RuntimeError(f"SerpApi: {data['error'][:200]}")

    options = [o for o in data.get("best_flights", []) + data.get("other_flights", [])
               if isinstance(o.get("price"), (int, float))]
    if not options:
        return None
    best = min(options, key=lambda o: o["price"])
    segs = best.get("flights", [])
    route = ([segs[0]["departure_airport"]["id"]] + [s["arrival_airport"]["id"] for s in segs]) if segs else []
    airlines = []
    for s in segs:
        if s.get("airline") and s["airline"] not in airlines:
            airlines.append(s["airline"])
    dep = segs[0]["departure_airport"].get("time", "") if segs else ""

    insights = data.get("price_insights") or {}
    return {
        "price": int(best["price"]),
        "airlines": ", ".join(airlines),
        "stops": max(len(segs) - 1, 0),
        "route": "–".join(route),
        "depart": dep[-5:] if dep else "",
        "flying_minutes": best.get("total_duration"),
        "options_seen": len(options),
        "google_level": insights.get("price_level"),
        "google_typical": insights.get("typical_price_range"),
        # Google's own price history for this search: [[unix_seconds, price], ...]
        "_google_history": insights.get("price_history") or [],
    }
