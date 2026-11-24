"""Fetch Google Flights results with a real (headless) Chrome browser.

Used for searches where Google builds results after the page loads, so the
simple scraper sees an empty page. The browser waits for the results to
appear, then reads the price, airlines, stops and duration of each one.

If a search finds nothing, a screenshot and the page HTML are saved to
debug/ so the problem can be diagnosed. GitHub keeps them with the run.
"""

from __future__ import annotations

import re
from pathlib import Path

DEBUG_DIR = Path(__file__).parent / "debug"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Each result row in Google Flights carries a long aria-label like:
# "From 4416 Australian dollars round trip total. 3 stops flight with
#  LATAM and Qantas. Leaves ... Total duration 34 hr. ..."
RESULT_SELECTOR = '[aria-label*="Australian dollars"]'

PRICE_RE = re.compile(r"From ([\d,]+) Australian dollars")
AIRLINE_RE = re.compile(r"flight with (.+?)\.")
STOPS_RE = re.compile(r"(\d+) stops?")
DUR_RE = re.compile(r"Total duration (\d+) hr(?: (\d+) min)?")
ROUTE_RE = re.compile(r"Leaves .*? at .*?\b([A-Z]{3})\b")
LAYOVER_RE = re.compile(r"Layover \(\d+ of \d+\) is a .*? in .*?\(([A-Z]{3})\)|Layover \(\d+ of \d+\) is a .*? at ([A-Z]{3})")
TEXT_PRICE_RE = re.compile(r"A\$\s?([\d,]+)")


class BrowserFetcher:
    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self.searches = 0

    def _start(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            user_agent=UA, locale="en-US", timezone_id="Australia/Perth",
            viewport={"width": 1366, "height": 900},
        )

    def close(self):
        for thing in (self._context, self._browser):
            try:
                thing and thing.close()
            except Exception:
                pass
        if self._pw:
            self._pw.stop()

    def fetch(self, url: str, label: str):
        """Return the cheapest itinerary dict for a Google Flights URL, or None."""
        if not self._browser:
            self._start()
        self.searches += 1
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            self._accept_consent(page)
            try:
                page.wait_for_selector(RESULT_SELECTOR, state="attached", timeout=40_000)
                page.wait_for_timeout(2500)  # let the rest of the list fill in
            except Exception:
                self._save_debug(page, label)
                title = (page.title() or "")[:60]
                raise RuntimeError(f"results never appeared; page title '{title}' (screenshot saved)")

            labels = page.eval_on_selector_all(
                RESULT_SELECTOR, "els => els.map(e => e.getAttribute('aria-label'))"
            )
            options = [o for o in (parse_label(l) for l in labels) if o]
            if not options:
                self._save_debug(page, label)
                raise RuntimeError("results appeared but couldn't be read (saved to debug/)")
            best = min(options, key=lambda o: o["price"])
            best["options_seen"] = len(options)
            return best
        finally:
            page.close()

    @staticmethod
    def _accept_consent(page):
        if "consent." not in page.url:
            return
        for name in ("Accept all", "I agree", "Reject all"):
            btn = page.get_by_role("button", name=name)
            if btn.count():
                btn.first.click()
                page.wait_for_load_state("domcontentloaded")
                return

    @staticmethod
    def _save_debug(page, label):
        DEBUG_DIR.mkdir(exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9-]+", "_", label)
        try:
            page.screenshot(path=str(DEBUG_DIR / f"{stem}.png"), full_page=True)
            (DEBUG_DIR / f"{stem}.html").write_text(page.content())
        except Exception:
            pass


def parse_label(text: str | None):
    """Turn one result's aria-label into a dict, or None if it isn't a flight."""
    if not text:
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    price = int(m.group(1).replace(",", ""))
    airlines = AIRLINE_RE.search(text)
    if "Nonstop" in text:
        stops = 0
    else:
        s = STOPS_RE.search(text)
        stops = int(s.group(1)) if s else None
    d = DUR_RE.search(text)
    minutes = int(d.group(1)) * 60 + int(d.group(2) or 0) if d else None
    layovers = [a or b for a, b in LAYOVER_RE.findall(text)]
    return {
        "price": price,
        "airlines": (airlines.group(1).replace(" and ", ", ") if airlines else ""),
        "stops": stops,
        "route": "–".join(layovers) if layovers else "",
        "depart": "",
        "flying_minutes": minutes,
    }
