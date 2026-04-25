#!/usr/bin/env python3
"""
HYROX Race Finder — Scraper v9 (Production)
=============================================
CONFIRMED working endpoint:
  GET https://vivenu.com/api/public/events/{ID}
  → data['tickets'] = list of ticket objects
  → ticket names like 'HYROX MEN | Friday, 22 May 2026'
  → Berlin confirmed: 14 divisions from 165 tickets ✓

Also confirmed: /shop endpoint has same tickets[] with availability data.

HOW TO ADD A NEW EVENT ID:
  1. Go to the event ticket page (e.g. da.hyrox.com/event/...)
  2. Open DevTools → Network → Fetch/XHR → refresh the page
  3. Look for a request to: vivenu.com/api/public/events/XXXX (615kb+)
     OR: vivenu.com/api/public/events/XXXX/offers
  4. Copy the 24-char hex XXXX
  5. Paste it as vivenu_id below and re-deploy

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
TIMEOUT       = 20

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


def status_from_ticket(ticket):
    """
    Determine availability from a vivenu ticket object.
    Key fields: amount (remaining), active, soldOut, status
    """
    if not ticket.get("active", True):
        return "hidden"
    if ticket.get("soldOut") or ticket.get("isSoldOut"):
        return "soldout"
    status = (ticket.get("status") or "").upper()
    if status in ("SOLD_OUT", "SOLDOUT", "EXHAUSTED"):
        return "soldout"
    if status in ("HIDDEN", "DRAFT", "INACTIVE"):
        return "hidden"
    # amount = remaining ticket count
    amount = ticket.get("amount")
    if isinstance(amount, (int, float)):
        if amount <= 0:
            return "soldout"
        if amount <= 20:
            return "limited"
        return "available"
    return "available"


def extract_divisions(tickets, currency=""):
    """
    Convert a list of vivenu ticket objects into our division structure.
    Groups wave-day tickets by division, keeping the best (most available) status.
    e.g. 'HYROX MEN | Friday' + 'HYROX MEN | Saturday' → mens-open: available
    """
    div_best  = {}
    div_price = {}

    for ticket in tickets:
        name = ticket.get("name") or ""
        div  = normalise_division(name)
        if div is None:
            continue

        st = status_from_ticket(ticket)
        if st == "hidden":
            continue

        # Keep the best status across all wave days for this division
        current_rank = STATUS_RANK.get(div_best.get(div, "soldout"), 2)
        new_rank     = STATUS_RANK.get(st, 2)
        if div not in div_best or new_rank < current_rank:
            div_best[div] = st
            price = ticket.get("price")
            cur   = ticket.get("currency") or currency
            if price is not None:
                div_price[div] = {"amount": price, "currency": cur}

    return {
        div: {"status": st, "price": div_price.get(div)}
        for div, st in div_best.items()
    }


# ── Event registry ─────────────────────────────────────────────────────────────
# Add vivenu_id values as you discover them via browser DevTools.
# Instructions: open ticket page → F12 → Network → Fetch/XHR → refresh →
# find request to vivenu.com/api/public/events/XXXX → copy the 24-char hex XXXX.

EVENTS = [
    # ─── UK ──────────────────────────────────────────────────────────────────
    # Cardiff: race is 29 Apr–4 May — sold out. Check after Birmingham goes on sale.
    {"id": "cardiff",          "vivenu_id": None},
    {"id": "birmingham",       "vivenu_id": None},   # not yet on sale
    {"id": "london-excel",     "vivenu_id": None},   # not yet on sale

    # ─── Europe ───────────────────────────────────────────────────────────────
    {"id": "berlin",           "vivenu_id": "698272f225feb1c40eb86297"},  # ✓ confirmed
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

    # ─── North America ────────────────────────────────────────────────────────
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

    # ─── Asia-Pacific ─────────────────────────────────────────────────────────
    {"id": "sydney",           "vivenu_id": None},
    {"id": "hong-kong",        "vivenu_id": None},
    {"id": "chiba",            "vivenu_id": None},
    {"id": "incheon",          "vivenu_id": None},
    {"id": "seoul",            "vivenu_id": None},
    {"id": "hangzhou",         "vivenu_id": None},
    {"id": "jakarta",          "vivenu_id": None},
    {"id": "delhi",            "vivenu_id": None},

    # ─── Latin America ────────────────────────────────────────────────────────
    {"id": "buenos-aires",     "vivenu_id": None},

    # ─── Africa ───────────────────────────────────────────────────────────────
    {"id": "johannesburg-may", "vivenu_id": None},
    {"id": "johannesburg-nov", "vivenu_id": None},
    {"id": "cape-town-aug",    "vivenu_id": None},
]


# ── Fetch and parse one event ─────────────────────────────────────────────────

def fetch_event(vivenu_id, event_id):
    """
    Fetch the vivenu event object and extract ticket availability.
    Primary: GET https://vivenu.com/api/public/events/{id}  (tickets[] field)
    Fallback: GET https://vivenu.com/api/public/events/{id}/shop (also has tickets[])
    """
    for url in [
        f"https://vivenu.com/api/public/events/{vivenu_id}",
        f"https://vivenu.com/api/public/events/{vivenu_id}/shop",
    ]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            log.info(f"    {url} → {r.status_code} ({len(r.content)} bytes)")
            if r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, dict):
                continue

            tickets = data.get("tickets") or []
            if tickets:
                currency = data.get("currency", "")
                divisions = extract_divisions(tickets, currency)
                log.info(f"    {len(tickets)} tickets → {len(divisions)} divisions: {list(divisions.keys())}")
                return divisions

        except Exception as e:
            log.warning(f"    Error fetching {url}: {e}")

    log.warning(f"    [{event_id}]: No ticket data found")
    return {}


# ── Main scrape ───────────────────────────────────────────────────────────────

def run_scrape():
    known = [e for e in EVENTS if e.get("vivenu_id")]
    log.info(f"── Scrape v9 starting — {len(known)} events with IDs, "
             f"{len(EVENTS)-len(known)} pending ──")
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

        log.info(f"  [{event_id}]")
        divisions = fetch_event(vivenu_id, event_id)
        time.sleep(1.0)  # polite rate limiting

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
    log.info(f"── Done: {ok} live, {no_sale} pending IDs ──\n")


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
