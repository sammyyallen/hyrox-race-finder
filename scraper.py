#!/usr/bin/env python3
"""
HYROX Race Finder — Availability Scraper
=========================================
Fetches ticket category availability from HYROX's public vivenu storefronts
every 15 minutes and writes the result to availability.json, which the
frontend reads to display live-ish ticket status.

HOW IT WORKS
------------
HYROX runs regional vivenu storefronts (gb.hyrox.com, usa.hyrox.com etc).
Each event page loads ticket data from vivenu's public seller API endpoint:

  https://{seller}.hyrox.com/api/events/{eventId}

This is a PUBLIC endpoint — no API key needed — it's what the buy page
itself calls in the browser. We hit the same endpoint.

The response includes ticketCategories[], each with:
  - name (e.g. "HYROX MEN | SATURDAY")
  - available (bool or count)
  - status: "AVAILABLE" | "SOLD_OUT" | "HIDDEN" etc.
  - price

We normalise these into our division taxonomy and write availability.json.

SETUP
-----
  pip install requests schedule

RUN (keeps running, polls every 15 min):
  python scraper.py

RUN ONCE (for testing):
  python scraper.py --once

DEPLOY OPTIONS
--------------
  • Raspberry Pi / any Linux server: run with systemd or cron
  • Free GitHub Actions: schedule workflow every 15 min, commit JSON to repo
    (see github-action.yml produced alongside this file)
  • Hetzner CX11 (~£3/mo): cheapest VPS, just run as a service

OUTPUT
------
  availability.json — read by index.html
  scraper.log       — rolling log
"""

import requests
import json
import time
import logging
import re
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

OUTPUT_FILE = Path(__file__).parent / "availability.json"
POLL_INTERVAL_MINUTES = 15
REQUEST_TIMEOUT = 12  # seconds per request
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HYROXRaceFinder/1.0; +https://github.com/yourname/hyrox-race-finder)",
    "Accept": "application/json",
}

# ── Division normalisation ────────────────────────────────────────────────────
# Maps fragments found in vivenu ticket category names → our internal division IDs.
# Vivenu names look like "HYROX MEN | SATURDAY", "HYROX DOUBLES MIXED | FRIDAY" etc.
# Order matters — more specific patterns first.

DIVISION_PATTERNS = [
    # Pro doubles (must come before plain doubles)
    (r"PRO DOUBLES\s+(WOMEN|FEMALE)",    "pro-doubles-women"),
    (r"PRO DOUBLES\s+(MEN|MALE)",        "pro-doubles-men"),
    # Pro singles
    (r"PRO\s+(WOMEN|FEMALE)",            "pro-women"),
    (r"PRO\s+(MEN|MALE)",                "pro-men"),
    # Doubles
    (r"DOUBLES\s+(WOMEN|FEMALE)",        "doubles-women"),
    (r"DOUBLES\s+(MEN|MALE)",            "doubles-men"),
    (r"DOUBLES\s+MIXED",                 "doubles-mixed"),
    (r"DOUBLES",                         "doubles-mixed"),   # fallback
    # Relay
    (r"(WOMENS?|FEMALE)\s+RELAY",        "relay-women"),
    (r"(MENS?|MALE)\s+RELAY",            "relay-men"),
    (r"MIXED\s+RELAY",                   "relay-mixed"),
    (r"RELAY\s+(WOMEN|FEMALE)",          "relay-women"),
    (r"RELAY\s+(MEN|MALE)",              "relay-men"),
    (r"RELAY",                           "relay-mixed"),     # fallback
    # Adaptive
    (r"ADAPTIVE\s+(WOMEN|FEMALE)",       "adaptive-women"),
    (r"ADAPTIVE\s+(MEN|MALE)",           "adaptive-men"),
    # Open singles (must be last so "PRO MEN" doesn't match here)
    (r"\bWOMEN\b|\bFEMALE\b",            "womens-open"),
    (r"\bMEN\b|\bMALE\b",               "mens-open"),
]

def normalise_division(name: str) -> str | None:
    """Map a vivenu ticket category name to our internal division ID."""
    upper = name.upper()
    # Skip spectator, volunteer, photo, parking, flex add-on tickets
    skip_words = ["SPECTATOR", "ZUSCHAUER", "VOLUNTEER", "PHOTO", "PARKING",
                  "FLEX", "ADD-ON", "ADDON", "YOUNGSTAR", "CORPORATE", "CHARITY"]
    if any(w in upper for w in skip_words):
        return None
    for pattern, div_id in DIVISION_PATTERNS:
        if re.search(pattern, upper):
            return div_id
    return None

def status_from_category(cat: dict) -> str:
    """
    Derive a simple status string from a vivenu ticketCategory object.
    Returns: 'available' | 'limited' | 'soldout' | 'hidden'
    """
    # vivenu exposes different shapes depending on version — handle both
    status = (cat.get("status") or "").upper()
    available_count = cat.get("available")       # integer or None
    sold_out = cat.get("soldOut", False)
    hidden = cat.get("hidden", False) or status == "HIDDEN"

    if hidden or status in ("HIDDEN", "DRAFT"):
        return "hidden"
    if sold_out or status in ("SOLD_OUT", "SOLDOUT", "EXHAUSTED"):
        return "soldout"
    if isinstance(available_count, (int, float)):
        if available_count <= 0:
            return "soldout"
        if available_count <= 20:
            return "limited"
    if status in ("AVAILABLE", "ON_SALE", ""):
        return "available"
    return "available"  # default optimistic


# ── Event registry ────────────────────────────────────────────────────────────
# Each entry tells the scraper WHERE to find the vivenu data for that event.
#
# seller:   the subdomain (gb → gb.hyrox.com, usa → usa.hyrox.com, etc.)
# event_id: the vivenu event ID — found in the URL of the buy page, e.g.
#           gb.hyrox.com/event/hyrox-cardiff-25-26-p7cgaq  →  "hyrox-cardiff-25-26-p7cgaq"
#           or sometimes a numeric/alphanumeric slug
#
# HOW TO FIND THE EVENT ID:
#   1. Go to the HYROX event page on hyrox.com and click "Buy Tickets"
#   2. You'll land on https://{seller}.hyrox.com/event/{event-id}
#   3. Copy the {event-id} slug and the {seller} subdomain

EVENTS_CONFIG = [
    # ── UK ────────────────────────────────────────────────────────────────────
    {
        "id": "cardiff",
        "seller": "gb",
        "event_slug": "hyrox-cardiff-25-26-p7cgaq",   # update if slug changes
    },
    {
        "id": "birmingham",
        "seller": "gb",
        "event_slug": "hyrox-birmingham-26-27",         # TBC — update when on sale
    },
    {
        "id": "london-excel",
        "seller": "gb",
        "event_slug": "hyrox-london-excel-26-27",       # TBC — update when on sale
    },

    # ── Europe ────────────────────────────────────────────────────────────────
    {
        "id": "berlin",
        "seller": "da",   # DACH region
        "event_slug": "gillettelabs-hyrox-berlin-season-25-26",
    },
    {
        "id": "hamburg",
        "seller": "da",
        "event_slug": "intersport-hyrox-hamburg-26-27",
    },
    {
        "id": "heerenveen",
        "seller": "benelux",
        "event_slug": "hyrox-heerenveen-season-25-26",
    },
    {
        "id": "amsterdam",
        "seller": "benelux",
        "event_slug": "hyrox-amsterdam-season-25-26",   # may be past
    },
    {
        "id": "utrecht",
        "seller": "benelux",
        "event_slug": "hyrox-utrecht-26-27",
    },
    {
        "id": "maastricht",
        "seller": "benelux",
        "event_slug": "hyrox-maastricht-26-27",
    },
    {
        "id": "riga",
        "seller": "baltics",
        "event_slug": "lemon-gym-hyrox-riga-season-25-26",
    },
    {
        "id": "barcelona-may",
        "seller": "spain",
        "event_slug": "biotherm-hyrox-barcelona-season-25-26",
    },
    {
        "id": "lyon",
        "seller": "france",
        "event_slug": "creapure-hyrox-lyon-season-25-26",
    },
    {
        "id": "paris-dec",
        "seller": "france",
        "event_slug": "fitness-park-hyrox-paris-s26-27",
    },
    {
        "id": "dublin",
        "seller": "ireland",
        "event_slug": "hyrox-dublin-26-27",
    },
    {
        "id": "stockholm-wc",
        "seller": "worlds",
        "event_slug": "puma-hyrox-world-championships-stockholm",
    },

    # ── North America ─────────────────────────────────────────────────────────
    {
        "id": "new-york",
        "seller": "usa",
        "event_slug": "nyu-langone-health-hyrox-new-york-season-25-26",
    },
    {
        "id": "washington",
        "seller": "usa",
        "event_slug": "amazfit-hyrox-washington-dc-26-27",
    },
    {
        "id": "salt-lake-city",
        "seller": "usa",
        "event_slug": "inbody-hyrox-salt-lake-city-26-27",
    },
    {
        "id": "boston",
        "seller": "usa",
        "event_slug": "hwpo-hyrox-boston-26-27",
    },
    {
        "id": "dallas",
        "seller": "usa",
        "event_slug": "hyrox-dallas-26-27",
    },
    {
        "id": "ottawa",
        "seller": "canada",
        "event_slug": "goodlife-hyrox-ottawa-season-25-26",
    },
    {
        "id": "toronto",
        "seller": "canada",
        "event_slug": "goodlife-hyrox-toronto-26-27",
    },

    # ── Asia-Pacific ──────────────────────────────────────────────────────────
    {
        "id": "sydney",
        "seller": "australia",
        "event_slug": "byd-hyrox-sydney-26-27",
    },
    {
        "id": "hong-kong",
        "seller": "hongkong",
        "event_slug": "cigna-hyrox-hong-kong-season-25-26",
    },
    {
        "id": "incheon",
        "seller": "korea",
        "event_slug": "airasia-hyrox-incheon-season-25-26",
    },
    {
        "id": "chiba",
        "seller": "japan",
        "event_slug": "airasia-hyrox-chiba-26-27",
    },
]

# ─────────────────────────────────────────────────────────────────────────────

def fetch_event_availability(config: dict) -> dict:
    """
    Hit the vivenu seller API for one event and return normalised availability.
    Tries two URL patterns vivenu uses — the slug-based and the ID-based.
    """
    seller   = config["seller"]
    slug     = config["event_slug"]
    event_id = config["id"]

    # Pattern 1: the seller storefront event endpoint (most common for HYROX)
    url = f"https://{seller}.hyrox.com/api/events/{slug}"

    result = {
        "event_id":   event_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "url":        url,
        "status":     "error",
        "error":      None,
        "divisions":  {},
        "raw_categories": [],
    }

    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)

        # Some events return 404 when not yet on sale — that's useful info
        if resp.status_code == 404:
            result["status"] = "not_on_sale"
            log.info(f"  {event_id}: not yet on sale (404)")
            return result

        resp.raise_for_status()
        data = resp.json()

    except requests.exceptions.Timeout:
        result["error"] = "timeout"
        log.warning(f"  {event_id}: timeout")
        return result
    except requests.exceptions.ConnectionError:
        result["error"] = "connection_error"
        log.warning(f"  {event_id}: connection error")
        return result
    except Exception as e:
        result["error"] = str(e)[:200]
        log.warning(f"  {event_id}: {e}")
        return result

    # vivenu returns either the event object directly or wraps it
    event_data = data if isinstance(data, dict) else {}
    categories = (
        event_data.get("ticketCategories")
        or event_data.get("categories")
        or event_data.get("priceCategories")
        or []
    )

    result["status"] = "ok"
    result["raw_categories"] = [
        {
            "name":      c.get("name", ""),
            "available": c.get("available"),
            "price":     c.get("price"),
            "currency":  c.get("currency") or event_data.get("currency"),
            "status":    c.get("status", ""),
            "soldOut":   c.get("soldOut", False),
            "hidden":    c.get("hidden", False),
        }
        for c in categories
    ]

    # Normalise into division buckets
    # If a division appears across multiple wave days, we take the BEST status
    # (available beats limited beats soldout) — we don't want to show "sold out"
    # if only Tuesday is sold out but Friday still has spots
    STATUS_RANK = {"available": 0, "limited": 1, "soldout": 2, "hidden": 3}
    div_best: dict[str, str] = {}
    div_price: dict[str, dict] = {}

    for cat in result["raw_categories"]:
        name = cat["name"]
        div = normalise_division(name)
        if div is None:
            continue
        st = status_from_category(cat)
        if st == "hidden":
            continue

        # Keep best (lowest rank = most available) status across wave days
        existing_rank = STATUS_RANK.get(div_best.get(div, "soldout"), 2)
        new_rank = STATUS_RANK.get(st, 2)
        if div not in div_best or new_rank < existing_rank:
            div_best[div] = st
            if cat.get("price") is not None:
                div_price[div] = {
                    "amount":   cat["price"],
                    "currency": cat.get("currency") or "",
                }

    for div, st in div_best.items():
        result["divisions"][div] = {
            "status": st,
            "price":  div_price.get(div),
        }

    log.info(f"  {event_id}: ok — {len(result['divisions'])} divisions found")
    return result


def run_scrape() -> None:
    """Scrape all events and write availability.json"""
    log.info(f"── Scrape run starting ({len(EVENTS_CONFIG)} events) ──")
    results = {}

    for cfg in EVENTS_CONFIG:
        event_result = fetch_event_availability(cfg)
        results[cfg["id"]] = event_result
        # Be polite — small delay between requests
        time.sleep(0.8)

    output = {
        "scraped_at":    datetime.now(timezone.utc).isoformat(),
        "poll_interval": POLL_INTERVAL_MINUTES,
        "events":        results,
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    ok_count  = sum(1 for v in results.values() if v["status"] == "ok")
    err_count = sum(1 for v in results.values() if v["status"] == "error")
    log.info(f"── Done. {ok_count} ok, {err_count} errors. Written to {OUTPUT_FILE} ──\n")


def main():
    parser = argparse.ArgumentParser(description="HYROX availability scraper")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    if args.once:
        run_scrape()
        return

    log.info(f"Scraper started — polling every {POLL_INTERVAL_MINUTES} minutes")
    run_scrape()  # immediate first run

    try:
        import schedule
    except ImportError:
        log.error("'schedule' not installed. Run: pip install schedule")
        return

    schedule.every(POLL_INTERVAL_MINUTES).minutes.do(run_scrape)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
