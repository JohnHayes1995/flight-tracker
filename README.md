# Flight price tracker

Checks Google Flights on a schedule, keeps a price history, and flags fares
at or near their lowest. Results are on the dashboard at `docs/index.html`.

## What runs when
- **Perth to Adelaide**: 3 times a day (6:23am, 12:23pm, 6:23pm Perth).
  4 one-way fares, plus 4 return fares for the first 14 days.
- **Bogotá to Perth**: once a day. 28 return fares: departures every 3 days
  from 18 Jan to 27 Feb 2027, returning about 4 or 5 weeks after arrival,
  max 40 hours each way.

Fares are flagged after 14 days and at least 8 checks:
- **Lowest yet**: matches or beats every earlier check
- **Good price**: in the cheapest 10% of all checks

## Changing trips
Edit `config.json` on GitHub. Changes apply from the next run.

## Running a check now
Actions tab > Check flight prices > Run workflow.

## If checks start failing
The check log on the dashboard shows failures. If every check fails for a
day or more, Google is probably blocking the scraper. The data source is
the `fetch_cheapest()` function in `tracker.py`; swap it for a paid API
and keep all your history.

## Notes
- Uses the open-source `fast-flights` library (pinned to 3.1.0), which reads
  Google Flights pages. It is unofficial and can break when Google changes
  its site.
- Prices are Google's quoted totals for all passengers in AUD. Always
  confirm on Google Flights before booking.
