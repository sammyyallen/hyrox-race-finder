#!/usr/bin/env python3
"""
HYROX Race Finder — Scraper v8
================================
The /offers endpoint only returns flex add-ons, not race tickets.

This version tries all plausible vivenu public endpoints for Berlin
to find where the actual ticket types (Men's Open, Women's Open etc.) live,
then logs the full response structure so we can parse correctly.

Run once:   python scraper.py --once
"""

import requests
import json
import re
import time
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

OUTPUT_FILE   = Path(__file__).parent / "availability.json"
POLL_INTERVAL = 15
TIMEOUT       = 15

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": "https://da.hyrox.com",
    "Referer": "https://da.hyrox.com/",
}

# ── Division mapping ──────────────────────────────────────────────────────────

DIVISION_PATTERNS = [
    (r"PRO\s+DOUBLES?\s+(WOMEN|FEMALE)", "pro-doubles-women"),
    (r"PRO\s+DOUBLES?\s+(MEN|MALE)",     "pro-doubles-men"),
    (r"PRO\s+(WOMEN|FEMALE)",            "pro-women"),
    (r"PRO\s+(MEN|MALE)",               "pro-men"),
    (r"DOUBLES?\s+(WOMEN|FEMALE)",       "doubles-women"),
    (r"DOUBLES?\s+(MEN|MALE)",          "doubles-men"),
    (r"DOUBLES?\s+MIXED",               "doubles-mixed"),
    (r"MIXED\s+DOUBLES?",               "doubles-mixed"),
    (r"DOUBLES?",                        "doubles-mixed"),
    (r"(WOMENS?|FEMALE)\s+RELAY",       "relay-women"),
    (r"(MENS?|MALE)\s+RELAY",          "relay-men"),
    (r"MIXED\s+RELAY",                  "relay-mixed"),
    (r"RELAY\s+(WOMEN|FEMALE)",         "relay-women"),
    (r"RELAY\s+(MEN|MALE)",            "relay-men"),
    (r"RELAY",                          "relay-mixed"),
    (r"ADAPTIVE\s+(WOMEN|FEMALE)",      "adaptive-women"),
    (r"ADAPTIVE\s+(MEN|MALE)",         "adaptive-men"),
    (r"\bWOMEN\b|\bFEMALE\b",          "womens-open"),
    (r"\bMEN\b|\bMALE\b",             "mens-open"),
]

SKIP_WORDS = [
    "SPECTATOR","ZUSCHAUER","VOLUNTEER","PHOTO","PARKING",
    "FLEX","ADD-ON","ADDON","YOUNGSTAR","CORPORATE","CHARITY",
    "MERCHANDISE","COACH","SUPPORTER","GUEST","LITE",
]

STATUS_RANK = {"available": 0, "limited": 1, "soldout": 2}


def normalise_division(name):
    upper = name.upper()
    if any(w in upper for w in SKIP_WORDS):
        return None
    for pattern, div_id in DIVISION_PATTERNS:
        if re.search(pattern, upper):
            return div_id
    return None


def status_from_item(item):
    if item.get("soldOut") or item.get("isSoldOut"):
        return "soldout"
    status = (item.get("status") or item.get("availabilityStatus") or "").upper()
    if status in ("SOLD_OUT", "SOLDOUT", "EXHAUSTED", "UNAVAILABLE"):
        return "soldout"
    if status in ("HIDDEN", "DRAFT", "INACTIVE"):
        return "hidden"
    if item.get("active") is False:
        return "hidden"
    for field in ("available", "availableAmount", "amount", "remainingQuantity", "stock"):
        val = item.get(field)
        if isinstance(val, (int, float)):
            if val <= 0:
                return "soldout"
            if val <= 20:
                return "limited"
            return "available"
    return "available"


# ── Known IDs ────────────────────────────────────────────────────────────────
BERLIN_ID = "698272f225feb1c40eb86297"

# All events — IDs to be filled in as we discover them
EVENTS = [
    {"id": "cardiff",          "vivenu_id": None},
    {"id": "birmingham",       "vivenu_id": None},
    {"id": "london-excel",     "vivenu_id": None},
    {"id": "berlin",           "vivenu_id": BERLIN_ID},
    {"id": "hamburg",          "vivenu_id": None},
    {"id": "heerenveen",       "vivenu_id": None},
    {"id": "maastricht",       "vivenu_id": None},
    {"id": "utrecht",          "vivenu_id": None},
    {"id": "gent",             "vivenu_id": None},
    {"id": "riga",             "vivenu_id": None},
    {"id": "barcelona-may",    "vivenu_id": None},
    {"id": "barcelona-nov",    "vivenu_id": None},
    {"id": "valencia",         "vivenu_id": None},
    {"id": "tenerife",         "vivenu_id": None},
    {"id": "lyon",             "vivenu_id": None},
    {"id": "bordeaux",         "vivenu_id": None},
    {"id": "nice-oct",         "vivenu_id": None},
    {"id": "paris-dec",        "vivenu_id": None},
    {"id": "lisboa",           "vivenu_id": None},
    {"id": "rimini",           "vivenu_id": None},
    {"id": "rome",             "vivenu_id": None},
    {"id": "milan",            "vivenu_id": None},
    {"id": "frankfurt",        "vivenu_id": None},
    {"id": "dusseldorf",       "vivenu_id": None},
    {"id": "karlsruhe",        "vivenu_id": None},
    {"id": "dublin",           "vivenu_id": None},
    {"id": "geneva",           "vivenu_id": None},
    {"id": "oslo",             "vivenu_id": None},
    {"id": "helsinki-may",     "vivenu_id": None},
    {"id": "helsinki-dec",     "vivenu_id": None},
    {"id": "gdansk",           "vivenu_id": None},
    {"id": "poznan",           "vivenu_id": None},
    {"id": "stockholm-wc",     "vivenu_id": None},
    {"id": "new-york",         "vivenu_id": None},
    {"id": "washington",       "vivenu_id": None},
    {"id": "salt-lake-city",   "vivenu_id": None},
    {"id": "boston",           "vivenu_id": None},
    {"id": "dallas",           "vivenu_id": None},
    {"id": "tampa",            "vivenu_id": None},
    {"id": "denver",           "vivenu_id": None},
    {"id": "nashville",        "vivenu_id": None},
    {"id": "anaheim",          "vivenu_id": None},
    {"id": "ottawa",           "vivenu_id": None},
    {"id": "toronto",          "vivenu_id": None},
    {"id": "vancouver",        "vivenu_id": None},
    {"id": "mexico-city",      "vivenu_id": None},
    {"id": "sydney",           "vivenu_id": None},
    {"id": "hong-kong",        "vivenu_id": None},
    {"id": "chiba",            "vivenu_id": None},
    {"id": "incheon",          "vivenu_id": None},
    {"id": "seoul",            "vivenu_id": None},
    {"id": "hangzhou",         "vivenu_id": None},
    {"id": "jakarta",          "vivenu_id": None},
    {"id": "delhi",            "vivenu_id": None},
    {"id": "buenos-aires",     "vivenu_id": None},
    {"id": "johannesburg-may", "vivenu_id": None},
    {"id": "johannesburg-nov", "vivenu_id": None},
    {"id": "cape-town-aug",    "vivenu_id": None},
]


# ── Endpoint discovery for Berlin ─────────────────────────────────────────────

def probe_berlin_endpoints():
    """
    Try every plausible vivenu endpoint for Berlin to find where
    the actual ticket types live. Log the full response for each hit.
    """
    vid = BERLIN_ID
    endpoints = [
        f"https://vivenu.com/api/public/events/{vid}",
        f"https://vivenu.com/api/public/events/{vid}/tickets",
        f"https://vivenu.com/api/public/events/{vid}/ticketTypes",
        f"https://vivenu.com/api/public/events/{vid}/ticket-types",
        f"https://vivenu.com/api/public/events/{vid}/categories",
        f"https://vivenu.com/api/public/events/{vid}/priceCategories",
        f"https://vivenu.com/api/public/events/{vid}/price-categories",
        f"https://vivenu.com/api/public/events/{vid}/checkout",
        f"https://vivenu.com/api/public/events/{vid}/shop",
        f"https://da.hyrox.com/api/public/events/{vid}",
        f"https://da.hyrox.com/api/public/events/{vid}/offers",
        f"https://da.hyrox.com/api/public/events/{vid}/tickets",
        f"https://da.hyrox.com/api/events/{vid}",
        f"https://da.hyrox.com/api/events/{vid}/tickets",
    ]

    log.info(f"── Probing {len(endpoints)} endpoints for Berlin ──")
    for url in endpoints:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            size = len(r.content)
            log.info(f"  {r.status_code} ({size:6d} bytes)  {url}")
            if r.status_code == 200 and size > 100:
                try:
                    data = r.json()
                    # Log top-level structure
                    if isinstance(data, dict):
                        keys = list(data.keys())
                        log.info(f"    Keys: {keys}")
                        # Look for anything that might be ticket types
                        for k in keys:
                            v = data[k]
                            if isinstance(v, list) and len(v) > 0:
                                first = v[0]
                                if isinstance(first, dict):
                                    name = first.get("name", "")
                                    log.info(f"    {k}[0].name = {name!r}  (list of {len(v)})")
                    elif isinstance(data, list) and len(data) > 0:
                        first = data[0]
                        if isinstance(first, dict):
                            name = first.get("name", "")
                            log.info(f"    list[0].name = {name!r}  (list of {len(data)})")
                except Exception:
                    log.info(f"    Not JSON or parse error")
        except Exception as e:
            log.info(f"  ERR  {url}: {e}")
        time.sleep(0.3)


# ── Parse tickets from event data (once we know which endpoint works) ─────────

def extract_divisions(items, currency=""):
    """Extract division availability from a flat list of ticket/offer items."""
    div_best  = {}
    div_price = {}

    for item in items:
        name = item.get("name") or item.get("title") or ""
        div  = normalise_division(name)
        if div is None:
            continue
        st = status_from_item(item)
        if st == "hidden":
            continue
        if div not in div_best or STATUS_RANK.get(st, 2) < STATUS_RANK.get(div_best[div], 2):
            div_best[div] = st
            price = item.get("price") or item.get("basePrice") or item.get("gross")
            cur   = item.get("currency") or currency
            if price is not None:
                div_price[div] = {"amount": price, "currency": cur}

    return {
        div: {"status": st, "price": div_price.get(div)}
        for div, st in div_best.items()
    }


def fetch_event(vivenu_id):
    """
    Fetch ticket availability for a known vivenu event ID.
    Tries the event endpoint directly (which should include tickets[]).
    """
    # Primary: the event object itself should have tickets[]
    url = f"https://vivenu.com/api/public/events/{vivenu_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        log.info(f"    Event endpoint: {r.status_code} ({len(r.content)} bytes)")
        if r.status_code == 200:
            data = r.json()
            currency = data.get("currency", "") if isinstance(data, dict) else ""
            # Try tickets[] directly on event
            tickets = data.get("tickets") or []
            if tickets:
                log.info(f"    Found {len(tickets)} tickets on event object")
                return extract_divisions(tickets, currency)
            # Fall through to other keys
            for key in ("ticketTypes", "categories", "priceCategories"):
                items = data.get(key) or []
                if items:
                    log.info(f"    Found {len(items)} items under '{key}'")
                    return extract_divisions(items, currency)
    except Exception as e:
        log.warning(f"    Event endpoint error: {e}")

    return {}


# ── Main scrape ───────────────────────────────────────────────────────────────

def run_scrape():
    # Always probe Berlin first to find the correct endpoint
    probe_berlin_endpoints()

    log.info(f"\n── Main scrape starting ({len(EVENTS)} events) ──")
    results = {}

    for event in EVENTS:
        event_id  = event["id"]
        vivenu_id = event.get("vivenu_id")

        if not vivenu_id:
            results[event_id] = {
                "event_id":   event_id,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "status":     "not_on_sale",
                "divisions":  {},
            }
            continue

        log.info(f"  [{event_id}] vivenu_id={vivenu_id}")
        divisions = fetch_event(vivenu_id)
        time.sleep(0.8)

        results[event_id] = {
            "event_id":   event_id,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "status":     "ok",
            "vivenu_id":  vivenu_id,
            "divisions":  divisions,
        }
        log.info(f"  [{event_id}]: ✓ {len(divisions)} divisions")

    output = {
        "scraped_at":    datetime.now(timezone.utc).isoformat(),
        "poll_interval": POLL_INTERVAL,
        "events":        results,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    ok = sum(1 for v in results.values() if v.get("status") == "ok")
    log.info(f"── Done: {ok} with vivenu IDs ──\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        run_scrape()
        return
    log.info(f"Scraper started — polling every {POLL_INTERVAL} min")
    run_scrape()
    try:
        import schedule
    except ImportError:
        log.error("Run: pip install schedule")
        return
    schedule.every(POLL_INTERVAL).minutes.do(run_scrape)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
