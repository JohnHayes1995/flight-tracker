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

def build_query(legs, trip_type, passengers, seat, currency, max_minutes=None):
    from fast_flights import FlightQuery, Passengers, create_query

    return create_query(
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


def fetch_cheapest(legs, trip_type, passengers, seat, currency, max_minutes=None):
    """Return the cheapest itinerary for a search, or None if nothing found.

    legs: list of (from_airport, to_airport, "YYYY-MM-DD")
    """
    from fast_flights import get_flights
    from fast_flights.exceptions import FlightsNotFound

    query = build_query(legs, trip_type, passengers, seat, currency, max_minutes)
    fetch_cheapest.last_url = query.url()
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
    if not obs:
        return {"status": "no-data"}
    # Our own checks, plus Google's price history for days we didn't check.
    own_days = {o["ts"][:10] for o in obs}
    google = [(f"{d}T12:00:00+00:00", p) for d, p in series.get("google_history", []) if d not in own_days]
    points = sorted([(o["ts"], o["price"]) for o in obs] + google)
    prices = [p for _, p in points]
    first = datetime.fromisoformat(points[0][0])
    days = (now - first).total_seconds() / 86400
    warm = days >= cfg["warmup_days"] and len(prices) >= cfg["min_observations"]
    current = obs[-1]["price"]
    prior = [p for ts, p in points if ts != obs[-1]["ts"]]
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
        "lowest_ts": next(ts for ts, p in points if p == low),
        "google_days": len(google),
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
        total_combos = len(searches)
        per_run = trip.get("searches_per_run")
        if per_run:
            # Rotate: check the fares that have gone longest without a check.
            def last_seen(x):
                obs = history["series"].get(x["key"], {}).get("obs", [])
                return obs[-1]["ts"] if obs else ""
            searches = sorted(searches, key=last_seen)[:per_run]
        budget = trip.get("monthly_search_budget")
        if budget:
            month = now.strftime("%Y-%m")
            used = history.setdefault("api_usage", {}).get(month, 0)
            if used >= budget:
                print(f"[{trip['id']}] monthly budget of {budget} searches used; skipping")
                history["runs"].append({"ts": now.isoformat(), "trip": trip["id"], "searches": 0,
                    "ok": 0, "empty": 0, "failed": 0, "errors": [f"Monthly limit of {budget} searches reached"]})
                continue
            searches = searches[:budget - used]
        ok, empty, failed, errors = 0, 0, 0, []
        max_minutes = int(trip["max_travel_hours"] * 60) if trip.get("max_travel_hours") else None
        print(f"[{trip['id']}] {len(searches)} searches" + (f" (rotating through {total_combos})" if per_run else ""))

        browser = None
        if trip.get("method") == "browser":
            from browser_fetch import BrowserFetcher
            browser = BrowserFetcher()

        def search_once(s, cap):
            trip_type = "round-trip" if s["kind"] == "return" else "one-way"
            if trip.get("method") == "serpapi":
                import serpapi_fetch
                if budget:
                    usage = history.setdefault("api_usage", {})
                    usage[now.strftime("%Y-%m")] = usage.get(now.strftime("%Y-%m"), 0) + 1
                return serpapi_fetch.fetch(s["legs"], s["kind"], trip["passengers"], cfg["currency"], cap,
                                          trip.get("max_stops"))
            if browser:
                url = build_query(s["legs"], trip_type, trip["passengers"], trip["seat"],
                                  cfg["currency"], cap).url()
                fetch_cheapest.last_url = url
                r = browser.fetch(url, f"{s['from']}-{s['to']}_{s['date']}_{s['return_date']}")
                if r and not r["route"].startswith(s["from"]):
                    r["route"] = "–".join([s["from"]] + ([r["route"]] if r["route"] else []) + [s["to"]])
                return r
            return fetch_cheapest(s["legs"], trip_type, trip["passengers"], trip["seat"],
                                  cfg["currency"], cap)

        for i, s in enumerate(searches):
            if i:
                time.sleep(random.uniform(*pause))
            if i >= 4 and ok == 0:
                errors.append("Stopped early: the first 4 searches found nothing.")
                print("  first 4 searches found nothing; skipping the rest of this trip")
                failed = len(searches) - ok - empty
                break
            result, err = None, None
            for attempt in range(2):
                try:
                    result = search_once(s, max_minutes)
                    err = None
                    break
                except Exception as e:  # network errors, blocks, parser changes
                    err = f"{type(e).__name__}: {str(e)[:160]}"
                    time.sleep(10)
            if result is None and not err and not browser and trip.get("method") != "serpapi":
                # Google sometimes returns a page with no results; try once more.
                time.sleep(random.uniform(*pause))
                try:
                    result = search_once(s, max_minutes)
                except Exception:
                    result = None
            label = f"{s['from']}-{s['to']} {s['date']}" + (f" / {s['return_date']}" if s["return_date"] else "")
            if err:
                failed += 1
                errors.append(f"{label}: {err}")
                print(f"  FAIL {label}: {err}")
                continue
            if result is None:
                empty += 1
                print(f"  none {label}")
                print(f"       check in a browser: {getattr(fetch_cheapest, 'last_url', '')}")
                continue
            ok += 1
            series = history["series"].setdefault(s["key"], {
                k: s.get(k) for k in ("trip", "kind", "from", "to", "date", "return_date", "stay_label")
            } | {"obs": []})
            google = result.pop("_google_history", None)
            if google:
                merged = dict(series.get("google_history", []))
                for ts, price in google:
                    if isinstance(ts, (int, float)) and isinstance(price, (int, float)):
                        day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
                        merged[day] = int(price)
                series["google_history"] = sorted(merged.items())
            series["obs"].append({"ts": now.isoformat(), **result})
            print(f"  {result['price']:>6} {label} ({result['airlines']})")

        if browser:
            browser.close()
        history["runs"].append({
            "ts": now.isoformat(), "trip": trip["id"], "searches": len(searches),
            "ok": ok, "empty": empty, "failed": failed, "errors": errors[:10],
        })
        if ok:
            history["last_checked"][trip["id"]] = now.isoformat()
        save_history(history)
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
                "google_points": [[f"{d}T12:00:00+00:00", p] for d, p in s.get("google_history", [])],
                **analyse(s, cfg, now),
            })
        rows.sort(key=lambda r: (r["kind"], r["from"], r["date"], r["return_date"] or ""))
        trips.append({
            "id": trip["id"], "name": trip["name"], "passengers": trip["passengers"],
            "origin": (trip.get("one_way") or [trip.get("round_trip", {})])[0].get("from"),
            "checks_per_day": trip["checks_per_day"],
            "searches_per_run": trip.get("searches_per_run"),
            "total_combos": len(build_searches(trip, history, now)),
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
    sys.stdout.reconfigure(line_buffering=True)  # show log lines live in GitHub Actions
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
