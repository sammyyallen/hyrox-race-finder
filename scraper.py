#!/usr/bin/env python3
"""
HYROX Race Finder — Scraper v6
================================
Confirmed working:
  GET https://vivenu.com/api/public/events/{ID}/offers
  → 200, response keys: ['bundles', 'products', 'entitlements']
  → tickets are in response['entitlements']

For ID discovery, we search vivenu's public search API by event name
which avoids needing to visit seller pages with unknown slugs.

Run once:   python scraper.py --once
Continuous: python scraper.py
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
    "Origin": "https://gb.hyrox.com",
    "Referer": "https://gb.hyrox.com/",
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
    "SPECTATOR", "ZUSCHAUER", "VOLUNTEER", "PHOTO", "PARKING",
    "FLEX", "ADD-ON", "ADDON", "YOUNGSTAR", "CORPORATE", "CHARITY",
    "MERCHANDISE", "COACH", "SUPPORTER", "GUEST",
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


def status_from_entitlement(item):
    """
    Parse availability from a vivenu entitlement/ticket object.
    The /offers endpoint returns entitlements with these fields.
    """
    # Explicit sold out
    if item.get("soldOut") or item.get("isSoldOut"):
        return "soldout"

    status = (item.get("status") or item.get("availabilityStatus") or "").upper()
    if status in ("SOLD_OUT", "SOLDOUT", "EXHAUSTED", "UNAVAILABLE"):
        return "soldout"
    if status in ("HIDDEN", "DRAFT", "INACTIVE"):
        return "hidden"

    # Active flag
    if item.get("active") is False:
        return "hidden"

    # Numeric availability — check several field names vivenu uses
    for field in ("available", "availableAmount", "amount", "remainingQuantity", "quantity"):
        val = item.get(field)
        if isinstance(val, (int, float)):
            if val <= 0:
                return "soldout"
            if val <= 20:
                return "limited"
            return "available"

    return "available"


# ── Event registry ─────────────────────────────────────────────────────────────
# vivenu_id: 24-char hex ID — hardcode these as you discover them.
#
# HOW TO FIND THE ID FOR ANY EVENT:
#   1. Go to the event's ticket page (on da.hyrox.com, gb.hyrox.com, etc.)
#   2. Open browser DevTools → Network → filter "Fetch/XHR"
#   3. Refresh — look for a request to vivenu.com/api/public/events/XXXX/offers
#   4. The XXXX is the vivenu_id — paste it below.
#
# search_terms: used to find the event via vivenu's search API as a fallback.

EVENTS = [
    # ── UK ────────────────────────────────────────────────────────────────────
    # Cardiff is end of April — likely sold out / just finished
    {"id": "cardiff",      "vivenu_id": None,                       "search": "hyrox cardiff 2026"},
    {"id": "birmingham",   "vivenu_id": None,                       "search": "hyrox birmingham 2026"},
    {"id": "london-excel", "vivenu_id": None,                       "search": "hyrox london excel 2026"},

    # ── Europe ────────────────────────────────────────────────────────────────
    # Berlin: CONFIRMED ID from browser network inspection
    {"id": "berlin",       "vivenu_id": "698272f225feb1c40eb86297", "search": "hyrox berlin 2026"},
    {"id": "hamburg",      "vivenu_id": None,                       "search": "hyrox hamburg 2026"},
    {"id": "heerenveen",   "vivenu_id": None,                       "search": "hyrox heerenveen 2026"},
    {"id": "maastricht",   "vivenu_id": None,                       "search": "hyrox maastricht 2026"},
    {"id": "utrecht",      "vivenu_id": None,                       "search": "hyrox utrecht 2026"},
    {"id": "gent",         "vivenu_id": None,                       "search": "hyrox gent 2026"},
    {"id": "riga",         "vivenu_id": None,                       "search": "hyrox riga 2026"},
    {"id": "barcelona-may","vivenu_id": None,                       "search": "hyrox barcelona 2026 may"},
    {"id": "barcelona-nov","vivenu_id": None,                       "search": "hyrox barcelona 2026 november"},
    {"id": "valencia",     "vivenu_id": None,                       "search": "hyrox valencia 2026"},
    {"id": "tenerife",     "vivenu_id": None,                       "search": "hyrox tenerife 2026"},
    {"id": "lyon",         "vivenu_id": None,                       "search": "hyrox lyon 2026"},
    {"id": "bordeaux",     "vivenu_id": None,                       "search": "hyrox bordeaux 2026"},
    {"id": "nice-oct",     "vivenu_id": None,                       "search": "hyrox nice 2026"},
    {"id": "paris-dec",    "vivenu_id": None,                       "search": "hyrox paris 2026 december"},
    {"id": "lisboa",       "vivenu_id": None,                       "search": "hyrox lisboa 2026"},
    {"id": "rimini",       "vivenu_id": None,                       "search": "hyrox rimini 2026"},
    {"id": "rome",         "vivenu_id": None,                       "search": "hyrox rome 2026"},
    {"id": "milan",        "vivenu_id": None,                       "search": "hyrox milan 2026"},
    {"id": "frankfurt",    "vivenu_id": None,                       "search": "hyrox frankfurt 2026"},
    {"id": "dusseldorf",   "vivenu_id": None,                       "search": "hyrox dusseldorf 2026"},
    {"id": "karlsruhe",    "vivenu_id": None,                       "search": "hyrox karlsruhe 2026"},
    {"id": "dublin",       "vivenu_id": None,                       "search": "hyrox dublin 2026"},
    {"id": "geneva",       "vivenu_id": None,                       "search": "hyrox geneva 2026"},
    {"id": "oslo",         "vivenu_id": None,                       "search": "hyrox oslo 2026"},
    {"id": "helsinki-may", "vivenu_id": None,                       "search": "hyrox helsinki 2026 may"},
    {"id": "helsinki-dec", "vivenu_id": None,                       "search": "hyrox helsinki 2026 december"},
    {"id": "gdansk",       "vivenu_id": None,                       "search": "hyrox gdansk 2026"},
    {"id": "poznan",       "vivenu_id": None,                       "search": "hyrox poznan 2026"},
    {"id": "stockholm-wc", "vivenu_id": None,                       "search": "hyrox world championships stockholm 2026"},

    # ── North America ─────────────────────────────────────────────────────────
    {"id": "new-york",       "vivenu_id": None, "search": "hyrox new york 2026"},
    {"id": "washington",     "vivenu_id": None, "search": "hyrox washington 2026"},
    {"id": "salt-lake-city", "vivenu_id": None, "search": "hyrox salt lake city 2026"},
    {"id": "boston",         "vivenu_id": None, "search": "hyrox boston 2026"},
    {"id": "dallas",         "vivenu_id": None, "search": "hyrox dallas 2026"},
    {"id": "tampa",          "vivenu_id": None, "search": "hyrox tampa 2026"},
    {"id": "denver",         "vivenu_id": None, "search": "hyrox denver 2026"},
    {"id": "nashville",      "vivenu_id": None, "search": "hyrox nashville 2026"},
    {"id": "anaheim",        "vivenu_id": None, "search": "hyrox anaheim 2026"},
    {"id": "ottawa",         "vivenu_id": None, "search": "hyrox ottawa 2026"},
    {"id": "toronto",        "vivenu_id": None, "search": "hyrox toronto 2026"},
    {"id": "vancouver",      "vivenu_id": None, "search": "hyrox vancouver 2026"},
    {"id": "mexico-city",    "vivenu_id": None, "search": "hyrox mexico city 2026"},

    # ── Asia-Pacific ──────────────────────────────────────────────────────────
    {"id": "sydney",     "vivenu_id": None, "search": "hyrox sydney 2026"},
    {"id": "hong-kong",  "vivenu_id": None, "search": "hyrox hong kong 2026"},
    {"id": "chiba",      "vivenu_id": None, "search": "hyrox chiba 2026"},
    {"id": "incheon",    "vivenu_id": None, "search": "hyrox incheon 2026"},
    {"id": "seoul",      "vivenu_id": None, "search": "hyrox seoul 2026"},
    {"id": "hangzhou",   "vivenu_id": None, "search": "hyrox hangzhou 2026"},
    {"id": "jakarta",    "vivenu_id": None, "search": "hyrox jakarta 2026"},
    {"id": "delhi",      "vivenu_id": None, "search": "hyrox delhi 2026"},

    # ── Latin America ─────────────────────────────────────────────────────────
    {"id": "buenos-aires", "vivenu_id": None, "search": "hyrox buenos aires 2026"},

    # ── Africa ────────────────────────────────────────────────────────────────
    {"id": "johannesburg-may", "vivenu_id": None, "search": "hyrox johannesburg 2026 may"},
    {"id": "johannesburg-nov", "vivenu_id": None, "search": "hyrox johannesburg 2026 november"},
    {"id": "cape-town-aug",    "vivenu_id": None, "search": "hyrox cape town 2026"},
]


# ── Vivenu search API — find event ID by name ─────────────────────────────────

def search_vivenu_id(search_terms):
    """
    Try vivenu's public search endpoint to find an event by name.
    Returns a 24-char hex ID or None.
    """
    # vivenu has a public search/events endpoint used by their embed widget
    urls = [
        f"https://vivenu.com/api/public/search/events?q={requests.utils.quote(search_terms)}&top=5",
        f"https://vivenu.com/api/public/events?q={requests.utils.quote(search_terms)}&top=5",
        f"https://vivenu.com/api/events?q={requests.utils.quote(search_terms)}&top=5",
    ]

    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            log.info(f"    Search: {url} → {r.status_code} ({len(r.content)} bytes)")
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except Exception:
                continue

            # Extract IDs from response
            docs = []
            if isinstance(data, list):
                docs = data
            elif isinstance(data, dict):
                docs = data.get("docs") or data.get("events") or data.get("results") or []

            if docs:
                event_id = docs[0].get("_id") or docs[0].get("id") or docs[0].get("eventId")
                if event_id and re.match(r'^[0-9a-f]{24}$', str(event_id)):
                    log.info(f"    Found via search: {event_id}")
                    return event_id
        except Exception as e:
            log.debug(f"    Search error: {e}")

    return None


# ── Fetch and parse offers ────────────────────────────────────────────────────

def fetch_and_parse(vivenu_id, event_id):
    """
    Call /api/public/events/{id}/offers and parse the entitlements.
    Returns dict of {division_id: {status, price}} or None on error.
    """
    url = f"https://vivenu.com/api/public/events/{vivenu_id}/offers"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        log.info(f"    Offers: {url} → {r.status_code} ({len(r.content)} bytes)")
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:
        log.warning(f"    Error: {e}")
        return None

    log.info(f"    Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")

    # ── CONFIRMED: tickets live in data['entitlements'] ──────────────────────
    items = []
    if isinstance(data, dict):
        # Try all known keys in priority order
        for key in ("entitlements", "tickets", "offers", "ticketTypes", "docs", "data"):
            candidate = data.get(key) or []
            if candidate:
                log.info(f"    Using key '{key}' with {len(candidate)} items")
                items = candidate
                break
        # Also check groups
        if not items:
            for group in (data.get("groups") or []):
                items.extend(group.get("entitlements") or group.get("tickets") or [])
    elif isinstance(data, list):
        items = data

    if not items:
        log.info(f"    No ticket items found — logging full response structure:")
        log.info(f"    {json.dumps(data, default=str)[:500]}")
        return {}

    log.info(f"    Parsing {len(items)} items")
    div_best  = {}
    div_price = {}

    for item in items:
        # entitlements may have the name nested under ticketType or directly
        name = (
            item.get("name") or
            item.get("title") or
            (item.get("ticketType") or {}).get("name") or
            ""
        )

        div = normalise_division(name)
        if div is None:
            continue

        st = status_from_entitlement(item)
        if st == "hidden":
            continue

        if div not in div_best or STATUS_RANK.get(st, 2) < STATUS_RANK.get(div_best[div], 2):
            div_best[div] = st
            # Price can be at top level or nested
            price = (
                item.get("price") or
                item.get("basePrice") or
                item.get("gross") or
                (item.get("ticketType") or {}).get("price")
            )
            currency = item.get("currency") or (item.get("ticketType") or {}).get("currency") or ""
            if price is not None:
                div_price[div] = {"amount": price, "currency": currency}

    log.info(f"    → {len(div_best)} divisions: {list(div_best.keys())}")
    return {
        div: {"status": st, "price": div_price.get(div)}
        for div, st in div_best.items()
    }


# ── Main scrape ───────────────────────────────────────────────────────────────

def run_scrape():
    log.info(f"── Scrape v6 starting ({len(EVENTS)} events) ──")
    results = {}

    for event in EVENTS:
        event_id  = event["id"]
        vivenu_id = event.get("vivenu_id")

        log.info(f"  [{event_id}]")

        # Discover vivenu ID if not hardcoded
        if not vivenu_id:
            vivenu_id = search_vivenu_id(event.get("search", event_id))
            time.sleep(0.5)

        if not vivenu_id:
            log.info(f"  [{event_id}]: No ID found — not yet on sale")
            results[event_id] = {
                "event_id":   event_id,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "status":     "not_on_sale",
                "divisions":  {},
            }
            continue

        # Fetch and parse
        divisions = fetch_and_parse(vivenu_id, event_id)
        time.sleep(0.5)

        if divisions is None:
            results[event_id] = {
                "event_id":   event_id,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "status":     "api_error",
                "vivenu_id":  vivenu_id,
                "divisions":  {},
            }
        else:
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

    ok      = sum(1 for v in results.values() if v.get("status") == "ok")
    no_sale = sum(1 for v in results.values() if v.get("status") == "not_on_sale")
    errors  = sum(1 for v in results.values() if v.get("status") == "api_error")
    log.info(f"── Done: {ok} ok, {no_sale} not on sale, {errors} errors ──\n")


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
