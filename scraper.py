#!/usr/bin/env python3
"""
HYROX Race Finder — Scraper v4
================================
This version adds full diagnostic output so we can see exactly what
each API call returns. Check the GitHub Actions log after running.

Strategy: call each HYROX seller storefront at multiple endpoint patterns
until we find one that returns JSON event data. Log everything.
"""

import requests
import json
import time
import re
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

# Try to look like a real browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
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
    "SPECTATOR","ZUSCHAUER","VOLUNTEER","PHOTO","PARKING",
    "FLEX","ADD-ON","ADDON","YOUNGSTAR","CORPORATE","CHARITY",
    "MERCHANDISE","COACH","SUPPORTER","GUEST",
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


def status_from_ticket(ticket):
    """vivenu ticket objects have 'amount' (remaining) and 'active' fields."""
    if not ticket.get("active", True):
        return "hidden"
    amount = ticket.get("amount")
    if amount is not None:
        if amount <= 0:
            return "soldout"
        if amount <= 20:
            return "limited"
    return "available"


# ── Seller → event ID mappings ─────────────────────────────────────────────────
# For each seller we know their subdomain and a list of (event_id, keywords)
# to match against event names returned by the API.

SELLERS = {
    "gb": [
        ("cardiff",      ["cardiff"]),
        ("birmingham",   ["birmingham"]),
        ("london-excel", ["london", "excel"]),
    ],
    "da": [
        ("berlin",       ["berlin"]),
        ("hamburg",      ["hamburg"]),
        ("frankfurt",    ["frankfurt"]),
        ("dusseldorf",   ["sseldorf"]),
        ("karlsruhe",    ["karlsruhe"]),
    ],
    "france": [
        ("lyon",         ["lyon"]),
        ("bordeaux",     ["bordeaux"]),
        ("nice-oct",     ["nice"]),
        ("paris-dec",    ["paris"]),
    ],
    "benelux": [
        ("heerenveen",   ["heerenveen"]),
        ("maastricht",   ["maastricht"]),
        ("utrecht",      ["utrecht"]),
        ("gent",         ["gent"]),
    ],
    "spain": [
        ("barcelona-may",["barcelona"]),
        ("barcelona-nov",["barcelona"]),
        ("valencia",     ["valencia"]),
        ("tenerife",     ["tenerife"]),
    ],
    "ireland": [
        ("dublin",       ["dublin"]),
    ],
    "usa": [
        ("new-york",       ["new york"]),
        ("washington",     ["washington"]),
        ("salt-lake-city", ["salt lake"]),
        ("boston",         ["boston"]),
        ("dallas",         ["dallas"]),
        ("tampa",          ["tampa"]),
        ("denver",         ["denver"]),
        ("nashville",      ["nashville"]),
        ("anaheim",        ["anaheim"]),
    ],
    "canada": [
        ("ottawa",       ["ottawa"]),
        ("toronto",      ["toronto"]),
        ("vancouver",    ["vancouver"]),
    ],
    "australia": [
        ("sydney",       ["sydney"]),
    ],
    "worlds": [
        ("stockholm-wc", ["stockholm"]),
    ],
    "baltics": [
        ("riga",         ["riga"]),
    ],
    "ireland": [
        ("dublin",       ["dublin"]),
    ],
    "portugal": [
        ("lisboa",       ["lisboa"]),
    ],
    "italy": [
        ("rimini",       ["rimini"]),
        ("rome",         ["rome"]),
        ("milan",        ["milan"]),
    ],
    "norway": [
        ("oslo",         ["oslo"]),
    ],
    "finland": [
        ("helsinki-may", ["helsinki"]),
        ("helsinki-dec", ["helsinki"]),
    ],
    "poland": [
        ("gdansk",       ["gda"]),
        ("poznan",       ["pozna"]),
    ],
    "korea": [
        ("incheon",      ["incheon"]),
        ("seoul",        ["seoul"]),
    ],
    "japan": [
        ("chiba",        ["chiba"]),
    ],
    "hongkong": [
        ("hong-kong",    ["hong kong"]),
    ],
    "mexico": [
        ("mexico-city",  ["mexico"]),
    ],
    "argentina": [
        ("buenos-aires", ["buenos"]),
    ],
    "africa": [
        ("johannesburg-may", ["johannesburg"]),
        ("johannesburg-nov", ["johannesburg"]),
        ("cape-town-aug",    ["cape town"]),
    ],
    "switzerland": [
        ("geneva",       ["geneva"]),
    ],
    "sweden": [
        ("stockholm-wc", ["stockholm"]),
    ],
    "indonesia": [
        ("jakarta",      ["jakarta"]),
    ],
    "india": [
        ("delhi",        ["delhi"]),
    ],
    "china": [
        ("hangzhou",     ["hangzhou"]),
    ],
}


def try_get_json(url):
    """
    Attempt a GET request. Returns (data, status_code, error_msg).
    Logs the raw response for diagnostics.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        log.info(f"    GET {url} → {r.status_code} ({len(r.content)} bytes)")
        if r.status_code != 200:
            return None, r.status_code, f"HTTP {r.status_code}"
        try:
            data = r.json()
            return data, 200, None
        except Exception:
            # Not JSON — log the first 200 chars to see what we're getting
            preview = r.text[:200].replace('\n', ' ')
            log.info(f"    Not JSON. Preview: {preview!r}")
            return None, 200, "not_json"
    except requests.exceptions.ConnectionError as e:
        log.warning(f"    Connection error: {e}")
        return None, 0, "connection_error"
    except requests.exceptions.Timeout:
        log.warning(f"    Timeout")
        return None, 0, "timeout"
    except Exception as e:
        log.warning(f"    Error: {e}")
        return None, 0, str(e)


def fetch_seller_events(seller):
    """
    Try several URL patterns to get event data from a seller storefront.
    Returns list of vivenu event dicts, or [].
    """
    base = f"https://{seller}.hyrox.com"

    # These are the URL patterns vivenu storefronts use internally.
    # We try them in order until one works.
    urls = [
        f"{base}/api/events?top=100",
        f"{base}/api/events?top=50",
        f"{base}/api/events",
        f"{base}/api/sellers/me/events?top=100",
        f"{base}/api/sellers/me/events",
    ]

    for url in urls:
        data, status, err = try_get_json(url)
        if data is None:
            continue

        # vivenu can return list directly or {docs: [...]}
        if isinstance(data, list) and len(data) > 0:
            log.info(f"    ✓ Found {len(data)} events as list")
            return data
        if isinstance(data, dict):
            docs = data.get("docs") or data.get("events") or data.get("data") or []
            if docs:
                log.info(f"    ✓ Found {len(docs)} events in dict")
                return docs
            # Log what keys we got so we understand the structure
            log.info(f"    Dict keys: {list(data.keys())}")

    return []


def match_events(vivenu_events, seller_catalogue):
    """Match vivenu events to our catalogue IDs using name keywords."""
    results = {}
    for event_id, keywords in seller_catalogue:
        for ve in vivenu_events:
            name = (ve.get("name") or "").lower()
            if all(kw.lower() in name for kw in keywords):
                # Process ticket availability
                tickets = ve.get("tickets") or []
                if not tickets:
                    # Try nested structure
                    for group in (ve.get("groups") or []):
                        tickets.extend(group.get("tickets") or [])

                div_best = {}
                div_price = {}
                for t in tickets:
                    tname = t.get("name") or ""
                    div = normalise_division(tname)
                    if div is None:
                        continue
                    st = status_from_ticket(t)
                    if st == "hidden":
                        continue
                    if div not in div_best or STATUS_RANK.get(st, 2) < STATUS_RANK.get(div_best[div], 2):
                        div_best[div] = st
                        if t.get("price") is not None:
                            div_price[div] = {
                                "amount":   t["price"],
                                "currency": ve.get("currency", ""),
                            }

                results[event_id] = {
                    "event_id":   event_id,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "status":     "ok",
                    "seller":     seller,
                    "vivenu_name": ve.get("name"),
                    "divisions":  {
                        div: {"status": st, "price": div_price.get(div)}
                        for div, st in div_best.items()
                    },
                }
                n = len(results[event_id]["divisions"])
                log.info(f"    ✓ Matched '{ve.get('name')}' → {event_id} ({n} divisions)")
                break  # found a match for this event_id, move on

    return results


# ── All catalogue IDs (for marking not_on_sale at the end) ────────────────────
ALL_EVENT_IDS = set()
for seller_events in SELLERS.values():
    for event_id, _ in seller_events:
        ALL_EVENT_IDS.add(event_id)


def run_scrape():
    log.info(f"── Scrape v4 starting ({len(SELLERS)} sellers) ──")
    results = {}

    for seller, catalogue in SELLERS.items():
        log.info(f"  [{seller}] Fetching events...")
        vivenu_events = fetch_seller_events(seller)

        if vivenu_events:
            matched = match_events(vivenu_events, catalogue)
            results.update(matched)
        else:
            log.info(f"  [{seller}]: No events returned from any endpoint")

        time.sleep(1.0)

    # Mark anything we didn't find as not_on_sale
    for event_id in ALL_EVENT_IDS:
        if event_id not in results:
            results[event_id] = {
                "event_id":   event_id,
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

    ok      = sum(1 for v in results.values() if v.get("status") == "ok")
    no_sale = sum(1 for v in results.values() if v.get("status") == "not_on_sale")
    log.info(f"── Done: {ok} matched, {no_sale} not yet on sale ──\n")


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
