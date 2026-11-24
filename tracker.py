"""Flight price tracker.

Runs on a schedule (GitHub Actions). Each run:
  1. works out which trips are due a check,
  2. fetches the cheapest fare for every route/date in those trips,
  3. appends the results to data/history.json,
  4. rebuilds the dashboard at docs/index.html.

Usage:
  python tracker.py            # normal scheduled run
  python tracker.py --force    # check every trip now, ignoring the schedule
  python tracker.py --build    # rebuild the dashboard only, no searches
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "data" / "history.json"
TEMPLATE_PATH = ROOT / "dashboard_template.html"
DASHBOARD_PATH = ROOT / "docs" / "index.html"

MAX_RUN_LOG = 300


# ---------------------------------------------------------------- data source
#
# Everything that talks to the outside world lives in fetch_cheapest().
# To switch to a paid API later, replace the body of this one function so it
# returns the same dict shape. History and dashboard code don't change.

def fetch_cheapest(legs, trip_type, passengers, seat, currency, max_minutes=None):
    """Return the cheapest itinerary for a search, or None if nothing found.

    legs: list of (from_airport, to_airport, "YYYY-MM-DD")
    """
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    from fast_flights.exceptions import FlightsNotFound

    query = create_query(
        flights=[
            FlightQuery(date=d, from_airport=a, to_airport=b, max_duration_minutes=max_minutes)
            for a, b, d in legs
        ],
        trip=trip_type,
        seat=seat,
        passengers=Passengers(adults=passengers),
        currency=currency,
        language="en",
    )
    try:
        results = get_flights(query)
    except FlightsNotFound:
        return None

    priced = [r for r in results if isinstance(r.price, (int, float)) and r.price > 0]
    if not priced:
        return None
    best = min(priced, key=lambda r: r.price)
    segs = best.flights
    route = [segs[0].from_airport.code] + [s.to_airport.code for s in segs] if segs else []
    dep = segs[0].departure.time if segs else None
    return {
        "price": int(best.price),
        "airlines": ", ".join(best.airlines) if best.airlines else "",
        "stops": max(len(segs) - 1, 0),
        "route": "–".join(route),
        "depart": f"{dep[0]:02d}:{dep[1]:02d}" if dep else "",
        "flying_minutes": sum(s.duration or 0 for s in segs),
        "options_seen": len(priced),
    }


# ---------------------------------------------------------------- searches

def build_searches(trip, history, now):
    """List every search a trip needs on this run."""
    out = []
    for leg in trip.get("one_way", []):
        for d in leg["dates"]:
            key = f"{trip['id']}|ow|{leg['from']}-{leg['to']}|{d}"
            out.append({
                "key": key, "trip": trip["id"], "kind": "one-way",
                "from": leg["from"], "to": leg["to"], "date": d, "return_date": None,
                "legs": [(leg["from"], leg["to"], d)],
            })

    rt = trip.get("round_trip")
    if rt and not round_trip_expired(trip, history, now):
        pairs = []
        if "outbound_dates" in rt:
            pairs = [(o, r, None) for o in rt["outbound_dates"] for r in rt["return_dates"]]
        else:
            start = date.fromisoformat(rt["outbound_range"][0])
            end = date.fromisoformat(rt["outbound_range"][1])
            labels = rt.get("return_labels") or [f"{n} days" for n in rt["return_after_days"]]
            d = start
            while d <= end:
                for n, label in zip(rt["return_after_days"], labels):
                    pairs.append((d.isoformat(), (d + timedelta(days=n)).isoformat(), label))
                d += timedelta(days=rt["every_days"])
        for o, r, label in pairs:
            key = f"{trip['id']}|rt|{rt['from']}-{rt['to']}|{o}|{r}"
            out.append({
                "key": key, "trip": trip["id"], "kind": "return",
                "from": rt["from"], "to": rt["to"], "date": o, "return_date": r,
                "stay_label": label,
                "legs": [(rt["from"], rt["to"], o), (rt["to"], rt["from"], r)],
            })
    return out


def round_trip_expired(trip, history, now):
    """Adelaide return fares are only tracked for the first N days."""
    limit = trip.get("round_trip", {}).get("stop_after_days")
    if not limit:
        return False
    first = history.get("trip_started", {}).get(trip["id"])
    if not first:
        return False
    return now - datetime.fromisoformat(first) > timedelta(days=limit)


def trip_is_due(trip, history, now):
    last = history.get("last_checked", {}).get(trip["id"])
    if not last:
        return True
    gap_hours = 24 / trip["checks_per_day"] * 0.6
    return now - datetime.fromisoformat(last) >= timedelta(hours=gap_hours)


# ---------------------------------------------------------------- analysis

def percentile(values, pct):
    s = sorted(values)
    if not s:
        return None
    k = (len(s) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def analyse(series, cfg, now):
    obs = series["obs"]
    prices = [o["price"] for o in obs]
    if not prices:
        return {"status": "no-data"}
    first = datetime.fromisoformat(obs[0]["ts"])
    days = (now - first).total_seconds() / 86400
    warm = days >= cfg["warmup_days"] and len(prices) >= cfg["min_observations"]
    current = prices[-1]
    prior = prices[:-1]
    low = min(prices)
    typical = statistics.median(prices)
    good = percentile(prices, cfg["good_price_percentile"])

    if not warm:
        status = "warming"
    elif prior and current <= min(prior):
        status = "lowest"
    elif current <= good:
        status = "good"
    elif current > typical * 1.1:
        status = "high"
    else:
        status = "normal"

    return {
        "status": status,
        "current": current,
        "lowest": low,
        "lowest_ts": next(o["ts"] for o in obs if o["price"] == low),
        "typical": round(typical),
        "good_below": round(good) if good is not None else None,
        "checks": len(prices),
        "tracking_days": round(days, 1),
        "flags_from": (first + timedelta(days=cfg["warmup_days"])).isoformat(),
        "latest": obs[-1],
    }


# ---------------------------------------------------------------- storage

def load_history():
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text())
    return {"series": {}, "runs": [], "last_checked": {}, "trip_started": {}}


def save_history(history):
    history["runs"] = history["runs"][-MAX_RUN_LOG:]
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------- run

def run_checks(cfg, history, force=False):
    now = datetime.now(timezone.utc)
    pause = cfg.get("pause_between_searches_seconds", [4, 10])
    for trip in cfg["trips"]:
        if not force and not trip_is_due(trip, history, now):
            print(f"[{trip['id']}] not due yet, skipping")
            continue
        history["trip_started"].setdefault(trip["id"], now.isoformat())
        searches = build_searches(trip, history, now)
        ok, empty, failed, errors = 0, 0, 0, []
        max_minutes = int(trip["max_travel_hours"] * 60) if trip.get("max_travel_hours") else None
        print(f"[{trip['id']}] {len(searches)} searches")

        for i, s in enumerate(searches):
            if i:
                time.sleep(random.uniform(*pause))
            result, err = None, None
            for attempt in range(3):
                try:
                    result = fetch_cheapest(
                        s["legs"], "round-trip" if s["kind"] == "return" else "one-way",
                        trip["passengers"], trip["seat"], cfg["currency"], max_minutes,
                    )
                    err = None
                    break
                except Exception as e:  # network errors, blocks, parser changes
                    err = f"{type(e).__name__}: {str(e)[:160]}"
                    time.sleep(15 * (attempt + 1))
            label = f"{s['from']}-{s['to']} {s['date']}" + (f" / {s['return_date']}" if s["return_date"] else "")
            if err:
                failed += 1
                errors.append(f"{label}: {err}")
                print(f"  FAIL {label}: {err}")
                continue
            if result is None:
                empty += 1
                print(f"  none {label}")
                continue
            ok += 1
            series = history["series"].setdefault(s["key"], {
                k: s.get(k) for k in ("trip", "kind", "from", "to", "date", "return_date", "stay_label")
            } | {"obs": []})
            series["obs"].append({"ts": now.isoformat(), **result})
            print(f"  {result['price']:>6} {label} ({result['airlines']})")

        history["runs"].append({
            "ts": now.isoformat(), "trip": trip["id"], "searches": len(searches),
            "ok": ok, "empty": empty, "failed": failed, "errors": errors[:10],
        })
        if ok:
            history["last_checked"][trip["id"]] = now.isoformat()
    return now


def build_dashboard(cfg, history):
    now = datetime.now(timezone.utc)
    trips = []
    for trip in cfg["trips"]:
        rows = []
        for key, s in history["series"].items():
            if s["trip"] != trip["id"]:
                continue
            rows.append({
                "key": key, "kind": s["kind"], "from": s["from"], "to": s["to"],
                "date": s["date"], "return_date": s.get("return_date"),
                "stay_label": s.get("stay_label"),
                "points": [[o["ts"], o["price"]] for o in s["obs"]],
                **analyse(s, cfg, now),
            })
        rows.sort(key=lambda r: (r["kind"], r["from"], r["date"], r["return_date"] or ""))
        trips.append({
            "id": trip["id"], "name": trip["name"], "passengers": trip["passengers"],
            "origin": (trip.get("one_way") or [trip.get("round_trip", {})])[0].get("from"),
            "checks_per_day": trip["checks_per_day"],
            "max_travel_hours": trip.get("max_travel_hours"),
            "last_checked": history["last_checked"].get(trip["id"]),
            "round_trip_ended": round_trip_expired(trip, history, now),
            "rows": rows,
        })
    payload = {
        "generated": now.isoformat(), "currency": cfg["currency"], "timezone": cfg["timezone"],
        "warmup_days": cfg["warmup_days"], "trips": trips,
        "runs": history["runs"][-40:][::-1],
    }
    html = TEMPLATE_PATH.read_text().replace(
        "/*__DATA__*/null", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    )
    DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_PATH.write_text(html)
    print(f"dashboard written: {DASHBOARD_PATH}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="check every trip now")
    p.add_argument("--build", action="store_true", help="rebuild dashboard only")
    args = p.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    history = load_history()
    if not args.build:
        run_checks(cfg, history, force=args.force)
        save_history(history)
    build_dashboard(cfg, history)

    last = history["runs"][-1] if history["runs"] else None
    if last and not args.build and last["searches"] and last["failed"] == last["searches"]:
        print("Every search in the last trip failed — Google may be blocking this runner.")
        sys.exit(1)


if __name__ == "__main__":
    main()
