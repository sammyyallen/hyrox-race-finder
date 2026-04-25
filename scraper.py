#!/usr/bin/env python3
"""
HYROX Race Finder — Scraper v5
================================
BREAKTHROUGH: The real vivenu API endpoint is:
  https://vivenu.com/api/public/events/{EVENT_ID}/offers

Where EVENT_ID is a 24-character hex string like 698272f225feb1c40eb86297.
This is a PUBLIC endpoint — no auth needed.

Strategy:
1. For events where we know the ID, call the API directly.
2. For others, visit the seller storefront event page (da.hyrox.com/event/slug)
   and extract the 24-char hex event ID from the page source or network calls.
3. Then call /api/public/events/{id}/offers to get ticket availability.

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
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-GB,en;q=0.9",
}

VIVENU_API = "https://vivenu.com/api/public/events/{id}/offers"

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


def status_from_offer(offer):
    """
    The /offers endpoint returns offer objects with availability info.
    Fields vary but typically include: available, soldOut, status, amount.
    """
    # Check explicit sold out flags
    if offer.get("soldOut") or offer.get("isSoldOut"):
        return "soldout"

    status = (offer.get("status") or offer.get("availabilityStatus") or "").upper()
    if status in ("SOLD_OUT", "SOLDOUT", "EXHAUSTED", "UNAVAILABLE"):
        return "soldout"
    if status in ("HIDDEN", "DRAFT", "INACTIVE"):
        return "hidden"

    # Check numeric availability
    available = offer.get("available") or offer.get("availableAmount") or offer.get("amount")
    if isinstance(available, (int, float)):
        if available <= 0:
            return "soldout"
        if available <= 20:
            return "limited"

    # Check active flag
    if offer.get("active") is False:
        return "hidden"

    return "available"


# ── Event registry ─────────────────────────────────────────────────────────────
# vivenu_id: the 24-char hex ID from the /api/public/events/{id}/offers URL.
#            Found by: opening the ticket page in browser → F12 → Network →
#            look for a request to vivenu.com/api/public/events/XXXX/offers
#
# seller_url: fallback — the full URL of the event on the seller storefront
#             (e.g. https://da.hyrox.com/event/gillettelabs-hyrox-berlin-...)
#             The scraper will visit this page and extract the vivenu_id.
#
# HOW TO FIND THE vivenu_id FOR A NEW EVENT:
#   1. Open the ticket page (e.g. da.hyrox.com/event/...)
#   2. F12 → Network tab → filter "Fetch/XHR"  
#   3. Refresh page → look for request to vivenu.com/api/public/events/XXXX/offers
#   4. Copy the 24-char hex XXXX — that's the vivenu_id
#
# OR: View page source → Ctrl+F → search "api/public" or search for 24-char hex

EVENTS = [
    # ── UK ────────────────────────────────────────────────────────────────────
    {
        "id":         "cardiff",
        "vivenu_id":  None,   # sold out / past — will be discovered or skipped
        "seller_url": "https://gb.hyrox.com/event/hyrox-cardiff-25-26-p7cgaq",
    },
    {
        "id":         "birmingham",
        "vivenu_id":  None,
        "seller_url": None,   # not yet on sale
    },
    {
        "id":         "london-excel",
        "vivenu_id":  None,
        "seller_url": None,   # not yet on sale
    },

    # ── Europe ────────────────────────────────────────────────────────────────
    {
        "id":         "berlin",
        "vivenu_id":  "698272f225feb1c40eb86297",   # ✓ CONFIRMED from browser
        "seller_url": None,
    },
    {
        "id":         "hamburg",
        "vivenu_id":  None,
        "seller_url": "https://da.hyrox.com/event/intersport-hyrox-hamburg-26-27",
    },
    {
        "id":         "heerenveen",
        "vivenu_id":  None,
        "seller_url": "https://benelux.hyrox.com/event/hyrox-heerenveen-season-25-26",
    },
    {
        "id":         "maastricht",
        "vivenu_id":  None,
        "seller_url": "https://benelux.hyrox.com/event/hyrox-maastricht-26-27",
    },
    {
        "id":         "utrecht",
        "vivenu_id":  None,
        "seller_url": "https://benelux.hyrox.com/event/hyrox-utrecht-26-27",
    },
    {
        "id":         "gent",
        "vivenu_id":  None,
        "seller_url": "https://benelux.hyrox.com/event/hyrox-gent-26-27",
    },
    {
        "id":         "riga",
        "vivenu_id":  None,
        "seller_url": "https://baltics.hyrox.com/event/lemon-gym-hyrox-riga-season-25-26",
    },
    {
        "id":         "barcelona-may",
        "vivenu_id":  None,
        "seller_url": "https://spain.hyrox.com/event/biotherm-hyrox-barcelona-season-25-26",
    },
    {
        "id":         "barcelona-nov",
        "vivenu_id":  None,
        "seller_url": "https://spain.hyrox.com/event/hyrox-barcelona-nov-26-27",
    },
    {
        "id":         "valencia",
        "vivenu_id":  None,
        "seller_url": "https://spain.hyrox.com/event/hyrox-valencia-26-27",
    },
    {
        "id":         "tenerife",
        "vivenu_id":  None,
        "seller_url": "https://spain.hyrox.com/event/hyrox-tenerife-26-27",
    },
    {
        "id":         "lyon",
        "vivenu_id":  None,
        "seller_url": "https://france.hyrox.com/event/creapure-hyrox-lyon-season-25-26",
    },
    {
        "id":         "bordeaux",
        "vivenu_id":  None,
        "seller_url": "https://france.hyrox.com/event/hyrox-bordeaux-s26-27",
    },
    {
        "id":         "nice-oct",
        "vivenu_id":  None,
        "seller_url": "https://france.hyrox.com/event/hyrox-nice-s26-27",
    },
    {
        "id":         "paris-dec",
        "vivenu_id":  None,
        "seller_url": "https://france.hyrox.com/event/fitness-park-hyrox-paris-s26-27",
    },
    {
        "id":         "lisboa",
        "vivenu_id":  None,
        "seller_url": "https://portugal.hyrox.com/event/hyrox-lisboa-season-25-26",
    },
    {
        "id":         "rimini",
        "vivenu_id":  None,
        "seller_url": "https://italy.hyrox.com/event/hyrox-rimini-season-25-26",
    },
    {
        "id":         "rome",
        "vivenu_id":  None,
        "seller_url": "https://italy.hyrox.com/event/hyrox-rome-26-27",
    },
    {
        "id":         "milan",
        "vivenu_id":  None,
        "seller_url": "https://italy.hyrox.com/event/hyrox-milan-26-27",
    },
    {
        "id":         "frankfurt",
        "vivenu_id":  None,
        "seller_url": "https://da.hyrox.com/event/fitness-first-hyrox-frankfurt-26-27",
    },
    {
        "id":         "dusseldorf",
        "vivenu_id":  None,
        "seller_url": "https://da.hyrox.com/event/hyrox-dusseldorf-26-27",
    },
    {
        "id":         "karlsruhe",
        "vivenu_id":  None,
        "seller_url": "https://da.hyrox.com/event/hyrox-karlsruhe-26-27",
    },
    {
        "id":         "dublin",
        "vivenu_id":  None,
        "seller_url": "https://ireland.hyrox.com/event/hyrox-dublin-26-27",
    },
    {
        "id":         "geneva",
        "vivenu_id":  None,
        "seller_url": "https://switzerland.hyrox.com/event/hyrox-geneva-26-27",
    },
    {
        "id":         "oslo",
        "vivenu_id":  None,
        "seller_url": "https://norway.hyrox.com/event/hyrox-oslo-26-27",
    },
    {
        "id":         "helsinki-may",
        "vivenu_id":  None,
        "seller_url": "https://finland.hyrox.com/event/hyrox-helsinki-season-25-26",
    },
    {
        "id":         "helsinki-dec",
        "vivenu_id":  None,
        "seller_url": "https://finland.hyrox.com/event/hyrox-helsinki-dec-26-27",
    },
    {
        "id":         "gdansk",
        "vivenu_id":  None,
        "seller_url": "https://poland.hyrox.com/event/hyrox-gdansk-26-27",
    },
    {
        "id":         "poznan",
        "vivenu_id":  None,
        "seller_url": "https://poland.hyrox.com/event/hyrox-poznan-26-27",
    },
    {
        "id":         "stockholm-wc",
        "vivenu_id":  None,
        "seller_url": "https://worlds.hyrox.com/event/puma-hyrox-world-championships-stockholm",
    },

    # ── North America ─────────────────────────────────────────────────────────
    {
        "id":         "new-york",
        "vivenu_id":  None,
        "seller_url": "https://usa.hyrox.com/event/nyu-langone-health-hyrox-new-york-season-25-26",
    },
    {
        "id":         "washington",
        "vivenu_id":  None,
        "seller_url": "https://usa.hyrox.com/event/amazfit-hyrox-washington-dc-26-27",
    },
    {
        "id":         "salt-lake-city",
        "vivenu_id":  None,
        "seller_url": "https://usa.hyrox.com/event/inbody-hyrox-salt-lake-city-26-27",
    },
    {
        "id":         "boston",
        "vivenu_id":  None,
        "seller_url": "https://usa.hyrox.com/event/hwpo-hyrox-boston-26-27",
    },
    {
        "id":         "dallas",
        "vivenu_id":  None,
        "seller_url": "https://usa.hyrox.com/event/hyrox-dallas-26-27",
    },
    {
        "id":         "tampa",
        "vivenu_id":  None,
        "seller_url": "https://usa.hyrox.com/event/hyrox-tampa-26-27",
    },
    {
        "id":         "denver",
        "vivenu_id":  None,
        "seller_url": "https://usa.hyrox.com/event/hyrox-denver-26-27",
    },
    {
        "id":         "nashville",
        "vivenu_id":  None,
        "seller_url": "https://usa.hyrox.com/event/hyrox-nashville-26-27",
    },
    {
        "id":         "anaheim",
        "vivenu_id":  None,
        "seller_url": "https://usa.hyrox.com/event/hyrox-anaheim-26-27",
    },
    {
        "id":         "ottawa",
        "vivenu_id":  None,
        "seller_url": "https://canada.hyrox.com/event/goodlife-hyrox-ottawa-season-25-26",
    },
    {
        "id":         "toronto",
        "vivenu_id":  None,
        "seller_url": "https://canada.hyrox.com/event/goodlife-hyrox-toronto-26-27",
    },
    {
        "id":         "vancouver",
        "vivenu_id":  None,
        "seller_url": "https://canada.hyrox.com/event/hyrox-vancouver-26-27",
    },
    {
        "id":         "mexico-city",
        "vivenu_id":  None,
        "seller_url": "https://mexico.hyrox.com/event/hyrox-mexico-city-26-27",
    },

    # ── Asia-Pacific ──────────────────────────────────────────────────────────
    {
        "id":         "sydney",
        "vivenu_id":  None,
        "seller_url": "https://australia.hyrox.com/event/byd-hyrox-sydney-26-27",
    },
    {
        "id":         "hong-kong",
        "vivenu_id":  None,
        "seller_url": "https://hongkong.hyrox.com/event/cigna-hyrox-hong-kong-season-25-26",
    },
    {
        "id":         "chiba",
        "vivenu_id":  None,
        "seller_url": "https://japan.hyrox.com/event/airasia-hyrox-chiba-26-27",
    },
    {
        "id":         "incheon",
        "vivenu_id":  None,
        "seller_url": "https://korea.hyrox.com/event/airasia-hyrox-incheon-season-25-26",
    },
    {
        "id":         "seoul",
        "vivenu_id":  None,
        "seller_url": "https://korea.hyrox.com/event/airasia-hyrox-seoul-26-27",
    },
    {
        "id":         "hangzhou",
        "vivenu_id":  None,
        "seller_url": "https://china.hyrox.com/event/hyrox-hangzhou-26-27",
    },
    {
        "id":         "jakarta",
        "vivenu_id":  None,
        "seller_url": "https://indonesia.hyrox.com/event/airasia-hyrox-jakarta-26-27",
    },
    {
        "id":         "delhi",
        "vivenu_id":  None,
        "seller_url": "https://india.hyrox.com/event/masters-union-hyrox-delhi-26-27",
    },

    # ── Latin America ─────────────────────────────────────────────────────────
    {
        "id":         "buenos-aires",
        "vivenu_id":  None,
        "seller_url": "https://latam.hyrox.com/event/hyrox-buenos-aires-season-25-26",
    },

    # ── Africa ────────────────────────────────────────────────────────────────
    {
        "id":         "johannesburg-may",
        "vivenu_id":  None,
        "seller_url": "https://africa.hyrox.com/event/virgin-active-hyrox-johannesburg-25-26",
    },
    {
        "id":         "johannesburg-nov",
        "vivenu_id":  None,
        "seller_url": "https://africa.hyrox.com/event/virgin-active-hyrox-johannesburg-26-27",
    },
    {
        "id":         "cape-town-aug",
        "vivenu_id":  None,
        "seller_url": "https://africa.hyrox.com/event/virgin-active-hyrox-cape-town-26-27",
    },
]


# ── ID discovery from seller page ─────────────────────────────────────────────

def discover_vivenu_id(seller_url):
    """
    Fetch the seller storefront event page and extract the 24-char hex
    vivenu event ID. It appears in the page source in several places:
    - As part of a URL like /api/public/events/XXXX
    - In a JSON blob like {"eventId":"XXXX"}
    - In a script tag
    """
    if not seller_url:
        return None
    try:
        r = requests.get(seller_url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        log.info(f"    Page fetch: {seller_url} → {r.status_code} ({len(r.content)} bytes)")
        if r.status_code != 200:
            return None
        html = r.text

        # Pattern 1: appears in API URL references in JS/HTML
        # e.g. /api/public/events/698272f225feb1c40eb86297
        m = re.search(r'/api/public/events/([0-9a-f]{24})', html)
        if m:
            log.info(f"    Found vivenu ID via API URL pattern: {m.group(1)}")
            return m.group(1)

        # Pattern 2: eventId or _id in JSON
        m = re.search(r'"(?:eventId|_id|vivenuId)"\s*:\s*"([0-9a-f]{24})"', html)
        if m:
            log.info(f"    Found vivenu ID via JSON key: {m.group(1)}")
            return m.group(1)

        # Pattern 3: any 24-char hex string (broader fallback)
        matches = re.findall(r'\b([0-9a-f]{24})\b', html)
        if matches:
            # Take the most frequent one (likely the event ID)
            from collections import Counter
            most_common = Counter(matches).most_common(1)[0][0]
            log.info(f"    Found vivenu ID via hex pattern (most common): {most_common}")
            return most_common

        log.info(f"    No vivenu ID found in page source")
        return None

    except Exception as e:
        log.warning(f"    Error fetching {seller_url}: {e}")
        return None


# ── Fetch offers from vivenu public API ───────────────────────────────────────

def fetch_offers(vivenu_id):
    """
    Call the confirmed public endpoint:
    https://vivenu.com/api/public/events/{id}/offers
    Returns the JSON response or None.
    """
    url = VIVENU_API.format(id=vivenu_id)
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        log.info(f"    Offers API: {url} → {r.status_code} ({len(r.content)} bytes)")
        if r.status_code != 200:
            return None
        try:
            data = r.json()
            log.info(f"    Response keys: {list(data.keys()) if isinstance(data, dict) else 'list[' + str(len(data)) + ']'}")
            return data
        except Exception:
            preview = r.text[:300].replace('\n', ' ')
            log.info(f"    Not JSON: {preview!r}")
            return None
    except Exception as e:
        log.warning(f"    Offers API error: {e}")
        return None


# ── Parse offers response into divisions ──────────────────────────────────────

def parse_offers(data):
    """
    Parse the /offers response into {division_id: {status, price}}.
    The response structure may vary — we handle several shapes.
    """
    if data is None:
        return {}

    # Gather all offer/ticket items into a flat list
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Try common keys
        items = (
            data.get("offers") or
            data.get("tickets") or
            data.get("ticketTypes") or
            data.get("docs") or
            data.get("data") or
            []
        )
        # Also check nested under groups
        for group in data.get("groups") or []:
            items.extend(group.get("tickets") or group.get("offers") or [])

    if not items:
        log.info(f"    No items found in offers response")
        return {}

    log.info(f"    Parsing {len(items)} offer items")

    div_best  = {}
    div_price = {}

    for item in items:
        name = item.get("name") or item.get("title") or ""
        div  = normalise_division(name)
        if div is None:
            continue

        st = status_from_offer(item)
        if st == "hidden":
            continue

        if div not in div_best or STATUS_RANK.get(st, 2) < STATUS_RANK.get(div_best[div], 2):
            div_best[div] = st
            price = item.get("price") or item.get("basePrice") or item.get("gross")
            currency = item.get("currency") or ""
            if price is not None:
                div_price[div] = {"amount": price, "currency": currency}

    log.info(f"    → {len(div_best)} divisions found: {list(div_best.keys())}")
    return {
        div: {"status": st, "price": div_price.get(div)}
        for div, st in div_best.items()
    }


# ── Main scrape ───────────────────────────────────────────────────────────────

def run_scrape():
    log.info(f"── Scrape v5 starting ({len(EVENTS)} events) ──")
    results = {}

    for event in EVENTS:
        event_id  = event["id"]
        vivenu_id = event.get("vivenu_id")
        seller_url = event.get("seller_url")

        log.info(f"  [{event_id}]")

        # Step 1: get vivenu ID if we don't have it hardcoded
        if not vivenu_id and seller_url:
            vivenu_id = discover_vivenu_id(seller_url)
            time.sleep(0.5)

        if not vivenu_id:
            log.info(f"  [{event_id}]: No vivenu ID — skipping (not yet on sale)")
            results[event_id] = {
                "event_id":   event_id,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "status":     "not_on_sale",
                "divisions":  {},
            }
            continue

        # Step 2: fetch offers
        data = fetch_offers(vivenu_id)
        time.sleep(0.5)

        if data is None:
            results[event_id] = {
                "event_id":   event_id,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "status":     "api_error",
                "vivenu_id":  vivenu_id,
                "divisions":  {},
            }
            continue

        # Step 3: parse
        divisions = parse_offers(data)
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
