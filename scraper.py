#!/usr/bin/env python3
"""
HYROX Race Finder — Scraper v7
================================
CONFIRMED API structure from Berlin response:
  GET https://vivenu.com/api/public/events/{ID}/offers
  → {"bundles": [], "products": [...], "entitlements": []}
  → tickets are in products[].variants[]
  → each variant has: name, price, (availability TBD from full response)

For IDs: must be hardcoded — vivenu search API returns 404, seller pages
return 404 due to unpredictable slugs. IDs are found by:
  1. Open ticket page in browser
  2. F12 → Network → Fetch/XHR → look for vivenu.com/api/public/events/XXXX/offers
  3. Copy the 24-char hex XXXX

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
    "SPECTATOR", "ZUSCHAUER", "VOLUNTEER", "PHOTO", "PARKING",
    "FLEX", "ADD-ON", "ADDON", "YOUNGSTAR", "CORPORATE", "CHARITY",
    "MERCHANDISE", "COACH", "SUPPORTER", "GUEST", "LITE",
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


def status_from_variant(variant):
    """Parse availability from a vivenu product variant."""
    if variant.get("soldOut") or variant.get("isSoldOut"):
        return "soldout"
    status = (variant.get("status") or variant.get("availabilityStatus") or "").upper()
    if status in ("SOLD_OUT", "SOLDOUT", "EXHAUSTED", "UNAVAILABLE"):
        return "soldout"
    if status in ("HIDDEN", "DRAFT", "INACTIVE"):
        return "hidden"
    if variant.get("active") is False:
        return "hidden"
    for field in ("available", "availableAmount", "amount", "remainingQuantity", "stock"):
        val = variant.get(field)
        if isinstance(val, (int, float)):
            if val <= 0:
                return "soldout"
            if val <= 20:
                return "limited"
            return "available"
    return "available"


# ── Event registry ─────────────────────────────────────────────────────────────
# vivenu_id: 24-char hex ID — found via browser DevTools.
# HOW TO FIND: open ticket page → F12 → Network → Fetch/XHR → refresh →
#   look for request to vivenu.com/api/public/events/XXXX/offers → copy XXXX
#
# IDs confirmed so far:
#   berlin: 698272f225feb1c40eb86297  (confirmed from log)
#
# To add more: check each event's ticket page in browser as described above
# and paste the ID into the vivenu_id field below.

EVENTS = [
    # ── UK ─────────────────────────── add IDs as you find them
    {"id": "cardiff",      "vivenu_id": None},  # likely sold out / just ended
    {"id": "birmingham",   "vivenu_id": None},  # not yet on sale
    {"id": "london-excel", "vivenu_id": None},  # not yet on sale

    # ── Europe ─────────────────────── add IDs as you find them
    {"id": "berlin",       "vivenu_id": "698272f225feb1c40eb86297"},  # ✓ confirmed
    {"id": "hamburg",      "vivenu_id": None},
    {"id": "heerenveen",   "vivenu_id": None},
    {"id": "maastricht",   "vivenu_id": None},
    {"id": "utrecht",      "vivenu_id": None},
    {"id": "gent",         "vivenu_id": None},
    {"id": "riga",         "vivenu_id": None},
    {"id": "barcelona-may","vivenu_id": None},
    {"id": "barcelona-nov","vivenu_id": None},
    {"id": "valencia",     "vivenu_id": None},
    {"id": "tenerife",     "vivenu_id": None},
    {"id": "lyon",         "vivenu_id": None},
    {"id": "bordeaux",     "vivenu_id": None},
    {"id": "nice-oct",     "vivenu_id": None},
    {"id": "paris-dec",    "vivenu_id": None},
    {"id": "lisboa",       "vivenu_id": None},
    {"id": "rimini",       "vivenu_id": None},
    {"id": "rome",         "vivenu_id": None},
    {"id": "milan",        "vivenu_id": None},
    {"id": "frankfurt",    "vivenu_id": None},
    {"id": "dusseldorf",   "vivenu_id": None},
    {"id": "karlsruhe",    "vivenu_id": None},
    {"id": "dublin",       "vivenu_id": None},
    {"id": "geneva",       "vivenu_id": None},
    {"id": "oslo",         "vivenu_id": None},
    {"id": "helsinki-may", "vivenu_id": None},
    {"id": "helsinki-dec", "vivenu_id": None},
    {"id": "gdansk",       "vivenu_id": None},
    {"id": "poznan",       "vivenu_id": None},
    {"id": "stockholm-wc", "vivenu_id": None},

    # ── North America ──────────────── add IDs as you find them
    {"id": "new-york",       "vivenu_id": None},
    {"id": "washington",     "vivenu_id": None},
    {"id": "salt-lake-city", "vivenu_id": None},
    {"id": "boston",         "vivenu_id": None},
    {"id": "dallas",         "vivenu_id": None},
    {"id": "tampa",          "vivenu_id": None},
    {"id": "denver",         "vivenu_id": None},
    {"id": "nashville",      "vivenu_id": None},
    {"id": "anaheim",        "vivenu_id": None},
    {"id": "ottawa",         "vivenu_id": None},
    {"id": "toronto",        "vivenu_id": None},
    {"id": "vancouver",      "vivenu_id": None},
    {"id": "mexico-city",    "vivenu_id": None},

    # ── Asia-Pacific ───────────────── add IDs as you find them
    {"id": "sydney",     "vivenu_id": None},
    {"id": "hong-kong",  "vivenu_id": None},
    {"id": "chiba",      "vivenu_id": None},
    {"id": "incheon",    "vivenu_id": None},
    {"id": "seoul",      "vivenu_id": None},
    {"id": "hangzhou",   "vivenu_id": None},
    {"id": "jakarta",    "vivenu_id": None},
    {"id": "delhi",      "vivenu_id": None},

    # ── Latin America ──────────────── add IDs as you find them
    {"id": "buenos-aires", "vivenu_id": None},

    # ── Africa ─────────────────────── add IDs as you find them
    {"id": "johannesburg-may", "vivenu_id": None},
    {"id": "johannesburg-nov", "vivenu_id": None},
    {"id": "cape-town-aug",    "vivenu_id": None},
]


# ── Fetch and parse ───────────────────────────────────────────────────────────

def fetch_and_parse(vivenu_id, event_id):
    """
    Call /api/public/events/{id}/offers and parse products[].variants[].
    Logs the full structure if parsing yields 0 divisions, so we can debug.
    """
    url = f"https://vivenu.com/api/public/events/{vivenu_id}/offers"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        log.info(f"    {url} → {r.status_code} ({len(r.content)} bytes)")
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:
        log.warning(f"    Error: {e}")
        return None

    log.info(f"    Top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")

    div_best  = {}
    div_price = {}

    def process_variant(variant_name, variant, parent_currency=""):
        """Try to match a variant to a division and record its status."""
        div = normalise_division(variant_name)
        if div is None:
            return
        st = status_from_variant(variant)
        if st == "hidden":
            return
        if div not in div_best or STATUS_RANK.get(st, 2) < STATUS_RANK.get(div_best[div], 2):
            div_best[div] = st
            price = variant.get("price") or variant.get("basePrice") or variant.get("gross")
            currency = variant.get("currency") or parent_currency
            if price is not None:
                div_price[div] = {"amount": price, "currency": currency}

    if isinstance(data, dict):
        currency = data.get("currency", "")

        # ── CONFIRMED: products[].variants[] contains ticket types ────────────
        products = data.get("products") or []
        log.info(f"    products count: {len(products)}")
        for product in products:
            product_name = product.get("name", "")
            product_currency = product.get("currency") or currency
            variants = product.get("variants") or []
            for variant in variants:
                # Variant name may be on the variant or inherited from product
                vname = variant.get("name") or product_name
                process_variant(vname, variant, product_currency)
                # Also try the product name itself if variant name is generic
                if vname != product_name:
                    process_variant(product_name, variant, product_currency)

        # ── Also check entitlements (were empty for Berlin but may vary) ──────
        entitlements = data.get("entitlements") or []
        log.info(f"    entitlements count: {len(entitlements)}")
        for item in entitlements:
            name = item.get("name") or (item.get("ticketType") or {}).get("name") or ""
            process_variant(name, item, currency)

        # ── Also check bundles ────────────────────────────────────────────────
        bundles = data.get("bundles") or []
        for bundle in bundles:
            bname = bundle.get("name", "")
            process_variant(bname, bundle, currency)

    elif isinstance(data, list):
        for item in data:
            name = item.get("name", "")
            process_variant(name, item)

    log.info(f"    → {len(div_best)} divisions: {list(div_best.keys())}")

    # If still 0 divisions, log the full product names so we can see what's there
    if len(div_best) == 0 and isinstance(data, dict):
        products = data.get("products") or []
        for p in products[:5]:
            log.info(f"    Product: {p.get('name')!r} | variants: {[v.get('name') for v in (p.get('variants') or [])]}")

    return {
        div: {"status": st, "price": div_price.get(div)}
        for div, st in div_best.items()
    }


# ── Main scrape ───────────────────────────────────────────────────────────────

def run_scrape():
    log.info(f"── Scrape v7 starting ({len(EVENTS)} events) ──")
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
        divisions = fetch_and_parse(vivenu_id, event_id)
        time.sleep(0.8)

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
