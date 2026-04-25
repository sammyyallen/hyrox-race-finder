#!/usr/bin/env python3
"""
HYROX Race Finder — Scraper v3
================================
Previous versions tried to discover vivenu slugs by visiting hyrox.com
event pages. hyrox.com blocks server IPs (GitHub Actions, AWS etc),
so every event came back "not_on_sale".

This version bypasses hyrox.com entirely. Instead it calls the vivenu
seller storefronts directly:

  https://gb.hyrox.com/api/events        ← returns ALL UK events at once
  https://da.hyrox.com/api/events        ← returns ALL DACH events at once
  https://usa.hyrox.com/api/events       ← etc.

These are the same public endpoints the ticket shop uses in your browser.
They are not protected and do not require visiting hyrox.com first.
We then match each vivenu event to our static catalogue by name/date.

Run once to test:   python scraper.py --once
Run continuously:   python scraper.py
"""

import requests
import json
import time
import re
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

OUTPUT_FILE      = Path(__file__).parent / "availability.json"
POLL_INTERVAL    = 15   # minutes
REQUEST_TIMEOUT  = 15   # seconds per request

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-GB,en;q=0.9",
}

# ── Seller storefronts to poll ─────────────────────────────────────────────────
# Each entry is a vivenu seller subdomain. We call /api/events on each,
# which returns every event that seller has listed — no slug needed.
SELLERS = [
    "gb",           # United Kingdom
    "da",           # Germany / Austria / Switzerland
    "france",       # France
    "benelux",      # Netherlands / Belgium
    "spain",        # Spain
    "ireland",      # Ireland
    "usa",          # United States
    "canada",       # Canada
    "australia",    # Australia
    "worlds",       # World Championships
    "baltics",      # Latvia / Lithuania / Estonia
    "korea",        # South Korea
    "japan",        # Japan
    "hongkong",     # Hong Kong
    "apac",         # Asia-Pacific (fallback)
    "latam",        # Latin America (fallback)
    "africa",       # Africa (fallback)
    "india",        # India
    "indonesia",    # Indonesia
    "china",        # China
    "norway",       # Norway
    "finland",      # Finland
    "poland",       # Poland
    "italy",        # Italy
    "portugal",     # Portugal
    "mexico",       # Mexico
    "argentina",    # Argentina
    "brazil",       # Brazil
    "sweden",       # Sweden (World Champs)
    "switzerland",  # Switzerland
]

# ── Division name → internal ID ───────────────────────────────────────────────
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


def normalise_division(name: str):
    upper = name.upper()
    if any(w in upper for w in SKIP_WORDS):
        return None
    for pattern, div_id in DIVISION_PATTERNS:
        if re.search(pattern, upper):
            return div_id
    return None


def status_from_category(cat: dict) -> str:
    status        = (cat.get("status") or "").upper()
    available_n   = cat.get("available")
    sold_out      = cat.get("soldOut", False)
    hidden        = cat.get("hidden", False) or status in ("HIDDEN", "DRAFT")
    if hidden:
        return "hidden"
    if sold_out or status in ("SOLD_OUT", "SOLDOUT", "EXHAUSTED"):
        return "soldout"
    if isinstance(available_n, (int, float)):
        if available_n <= 0:
            return "soldout"
        if available_n <= 20:
            return "limited"
    return "available"


# ── Our event catalogue — city keywords to match against vivenu event names ───
# Each entry needs enough keywords to uniquely identify it in the vivenu data.
# 'keywords': ALL must appear in the vivenu event name (case-insensitive).
# 'id': must match the id used in index.html.

EVENT_CATALOGUE = [
    # UK
    {"id": "cardiff",        "keywords": ["cardiff"]},
    {"id": "birmingham",     "keywords": ["birmingham"]},
    {"id": "london-excel",   "keywords": ["london", "excel"]},

    # Europe
    {"id": "berlin",         "keywords": ["berlin"]},
    {"id": "hamburg",        "keywords": ["hamburg"]},
    {"id": "heerenveen",     "keywords": ["heerenveen"]},
    {"id": "maastricht",     "keywords": ["maastricht"]},
    {"id": "utrecht",        "keywords": ["utrecht"]},
    {"id": "riga",           "keywords": ["riga"]},
    {"id": "barcelona-may",  "keywords": ["barcelona"], "month": 5},
    {"id": "barcelona-nov",  "keywords": ["barcelona"], "month": 11},
    {"id": "valencia",       "keywords": ["valencia"]},
    {"id": "tenerife",       "keywords": ["tenerife"]},
    {"id": "lyon",           "keywords": ["lyon"]},
    {"id": "bordeaux",       "keywords": ["bordeaux"]},
    {"id": "nice-oct",       "keywords": ["nice"]},
    {"id": "paris-dec",      "keywords": ["paris"]},
    {"id": "lisboa",         "keywords": ["lisboa"]},
    {"id": "rimini",         "keywords": ["rimini"]},
    {"id": "rome",           "keywords": ["rome"]},
    {"id": "milan",          "keywords": ["milan"]},
    {"id": "frankfurt",      "keywords": ["frankfurt"]},
    {"id": "dusseldorf",     "keywords": ["sseldorf"]},  # ü/ue both covered
    {"id": "karlsruhe",      "keywords": ["karlsruhe"]},
    {"id": "dublin",         "keywords": ["dublin"]},
    {"id": "geneva",         "keywords": ["geneva"]},
    {"id": "oslo",           "keywords": ["oslo"]},
    {"id": "helsinki-may",   "keywords": ["helsinki"], "month": 5},
    {"id": "helsinki-dec",   "keywords": ["helsinki"], "month": 12},
    {"id": "gdansk",         "keywords": ["gda"]},       # covers Gdańsk/Gdansk
    {"id": "poznan",         "keywords": ["pozna"]},     # covers Poznań/Poznan
    {"id": "gent",           "keywords": ["gent"]},
    {"id": "stockholm-wc",   "keywords": ["stockholm"]},

    # North America
    {"id": "new-york",       "keywords": ["new york"]},
    {"id": "washington",     "keywords": ["washington"]},
    {"id": "salt-lake-city", "keywords": ["salt lake"]},
    {"id": "boston",         "keywords": ["boston"]},
    {"id": "dallas",         "keywords": ["dallas"]},
    {"id": "tampa",          "keywords": ["tampa"]},
    {"id": "denver",         "keywords": ["denver"]},
    {"id": "nashville",      "keywords": ["nashville"]},
    {"id": "anaheim",        "keywords": ["anaheim"]},
    {"id": "ottawa",         "keywords": ["ottawa"]},
    {"id": "toronto",        "keywords": ["toronto"]},
    {"id": "vancouver",      "keywords": ["vancouver"]},
    {"id": "mexico-city",    "keywords": ["mexico"]},

    # Asia-Pacific
    {"id": "sydney",         "keywords": ["sydney"]},
    {"id": "hong-kong",      "keywords": ["hong kong"]},
    {"id": "chiba",          "keywords": ["chiba"]},
    {"id": "incheon",        "keywords": ["incheon"]},
    {"id": "seoul",          "keywords": ["seoul"]},
    {"id": "hangzhou",       "keywords": ["hangzhou"]},
    {"id": "jakarta",        "keywords": ["jakarta"]},
    {"id": "delhi",          "keywords": ["delhi"]},

    # Latin America
    {"id": "buenos-aires",   "keywords": ["buenos"]},

    # Africa
    {"id": "johannesburg-may", "keywords": ["johannesburg"], "month": 5},
    {"id": "johannesburg-nov", "keywords": ["johannesburg"], "month": 11},
    {"id": "cape-town-aug",    "keywords": ["cape town"]},
]


def match_event(vivenu_event: dict) -> str | None:
    """
    Try to match a vivenu event object to one of our catalogue IDs.
    Uses the event name and start date month.
    Returns the catalogue id, or None if no match.
    """
    name = (vivenu_event.get("name") or "").lower()
    start = vivenu_event.get("start") or vivenu_event.get("startDate") or ""
    try:
        month = int(start[5:7]) if len(start) >= 7 else None
    except (ValueError, TypeError):
        month = None

    for entry in EVENT_CATALOGUE:
        keywords = entry["keywords"]
        if not all(kw.lower() in name for kw in keywords):
            continue
        # If catalogue entry specifies a month, require it to match
        if "month" in entry and month is not None and entry["month"] != month:
            continue
        return entry["id"]
    return None


def process_categories(categories: list, currency: str = "") -> dict:
    """
    Normalise a list of vivenu ticket categories into our division structure.
    Returns dict of {division_id: {status, price}}.
    """
    div_best: dict[str, str] = {}
    div_price: dict[str, dict] = {}

    for cat in categories:
        name = cat.get("name", "")
        div  = normalise_division(name)
        if div is None:
            continue
        st = status_from_category(cat)
        if st == "hidden":
            continue
        # Keep best (most available) status across wave days
        if div not in div_best or STATUS_RANK.get(st, 2) < STATUS_RANK.get(div_best[div], 2):
            div_best[div] = st
            price = cat.get("price")
            if price is not None:
                div_price[div] = {
                    "amount":   price,
                    "currency": cat.get("currency") or currency or "",
                }

    return {
        div: {"status": st, "price": div_price.get(div)}
        for div, st in div_best.items()
    }


# ── Fetch all events from one seller ─────────────────────────────────────────

def fetch_seller_events(seller: str) -> list:
    """
    Call the vivenu seller storefront events list endpoint.
    Returns a list of vivenu event objects, or [] on failure.
    """
    url = f"https://{seller}.hyrox.com/api/events"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return []   # seller subdomain doesn't exist — normal
        resp.raise_for_status()
        data = resp.json()
        # vivenu returns either a list directly or {docs: [...]}
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("docs") or data.get("events") or data.get("data") or []
        return []
    except Exception as e:
        log.warning(f"  [{seller}] fetch error: {e}")
        return []


# ── Main scrape ───────────────────────────────────────────────────────────────

def run_scrape():
    log.info(f"── Scrape starting ({len(SELLERS)} sellers) ──")

    # results keyed by our catalogue event id
    results: dict[str, dict] = {}

    for seller in SELLERS:
        vivenu_events = fetch_seller_events(seller)
        if not vivenu_events:
            log.info(f"  [{seller}]: no events returned (may not exist or no events yet)")
            time.sleep(0.5)
            continue

        log.info(f"  [{seller}]: {len(vivenu_events)} vivenu events found")

        for ve in vivenu_events:
            event_id = match_event(ve)
            if event_id is None:
                log.debug(f"    Unmatched: {ve.get('name', '?')!r}")
                continue

            # Don't overwrite a good result from another seller
            if event_id in results and results[event_id]["status"] == "ok":
                continue

            categories = (
                ve.get("ticketCategories") or
                ve.get("categories") or
                ve.get("priceCategories") or
                []
            )
            currency = ve.get("currency", "")
            divisions = process_categories(categories, currency)

            results[event_id] = {
                "event_id":   event_id,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "status":     "ok",
                "seller":     seller,
                "slug":       ve.get("slug") or ve.get("_id") or "",
                "divisions":  divisions,
            }
            log.info(
                f"    ✓ {event_id}: "
                f"{len(divisions)} divisions from {ve.get('name', '?')!r}"
            )

        time.sleep(0.8)   # polite gap between sellers

    # Mark any catalogue events we didn't find as not_on_sale
    for entry in EVENT_CATALOGUE:
        eid = entry["id"]
        if eid not in results:
            results[eid] = {
                "event_id":   eid,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "status":     "not_on_sale",
                "divisions":  {},
            }

    output = {
        "scraped_at":    datetime.now(timezone.utc).isoformat(),
        "poll_interval": POLL_INTERVAL,
        "events":        results,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))

    ok      = sum(1 for v in results.values() if v["status"] == "ok")
    no_sale = sum(1 for v in results.values() if v["status"] == "not_on_sale")
    log.info(f"── Done: {ok} matched with data, {no_sale} not yet on sale ──\n")


def main():
    parser = argparse.ArgumentParser(description="HYROX ticket availability scraper")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    if args.once:
        run_scrape()
        return

    log.info(f"Scraper started — polling every {POLL_INTERVAL} minutes")
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
