"""
Restock Radar - stock checker
=============================
Two ways to run:

  python checker.py          loops forever, serves stock.json at
                             http://localhost:8000/stock.json for the dashboard
  python checker.py --once   one sweep, then exit (this is what GitHub Actions uses)

Either way it checks each store, writes stock.json, and sends a Discord
message the moment something goes from out-of-stock to in-stock.

SETUP: see SETUP.md. You only need to edit the CONFIG block below.
"""

import json, os, sys, time, threading, http.server, socketserver, functools, urllib.request, urllib.error, urllib.parse
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------------
# CONFIG - this is the only part you need to edit
# ----------------------------------------------------------------------------

# Webhooks are secrets - they never go in this file, or they'd be in git
# history forever. Each Discord channel has its own. Locally they're read from
# gitignored files; on GitHub Actions from repository secrets:
#
#   channel "ps5" -> webhook.txt      / DISCORD_WEBHOOK_URL
# (a "gpu" channel used to live here too; graphics cards were dropped
#  2026-09-26. Re-adding one is a webhook entry plus a product.)
#
def _load_webhook(channel=""):
    # ﻿ is a BOM - Windows tooling loves to prepend one, and Python's
    # strip() won't remove it, which yields "unknown url type: ﻿https"
    def clean(s):
        return s.strip().lstrip("﻿").strip()

    env_name = f"DISCORD_WEBHOOK_{channel.upper()}" if channel else "DISCORD_WEBHOOK_URL"
    from_env = clean(os.environ.get(env_name, ""))
    if from_env:
        return from_env
    fname = f"webhook_{channel}.txt" if channel else "webhook.txt"
    local = Path(__file__).parent / fname
    if local.exists():
        return clean(local.read_text(encoding="utf-8-sig"))
    return ""


# Each product carries a "channel" naming which Discord channel it reports to.
# Anything unrecognised falls back to the ps5 webhook rather than vanishing.
WEBHOOKS = {
    "ps5": _load_webhook(),
}

# Optional: a separate channel holding nothing but the status message, so it
# is the only thing there instead of buried under alerts. Unset = the status
# lives in the alert channel as before.
#
#   channel "ps5" -> webhook_ps5_status.txt / DISCORD_WEBHOOK_PS5_STATUS
STATUS_WEBHOOKS = {
    "ps5": _load_webhook("ps5_status"),
}

CHECK_EVERY_SECONDS = 120  # don't go below 60 - Walmart blocks fast pollers

# Who to mention on a restock. A plain message notifies nobody - Discord only
# raises a push notification (phone, watch, desktop badge) on a mention, which
# is why these alerts were silent everywhere but an already-open Discord window.
#   "@everyone"     - whole channel, works with no extra setup
#   "<@1234567890>" - just you; better if anyone else joins the server
#   ""              - no mention, the old silent behaviour
# Override with the DISCORD_PING env var / repo secret.
PING = os.environ.get("DISCORD_PING", "@everyone").strip()

PORT = 8000

# The stores you care about.
#   kind "bestbuy" -> uses Best Buy's public API (reliable)
#   kind "staples" -> uses Staples Canada's inventory API (reliable, no auth)
#   kind "walmart" -> uses Walmart Canada's store availability (works, but fragile)
#   kind "manual"  -> never checked automatically, always shows "call to verify"
STORES = {
    # store_url points at that one location's own page (address, phone, hours).
    # The product-page URL can't do this - every location of a chain shares one
    # product URL and the selected store lives in your browser session, not the
    # link, so a product link always opens with whatever store you last used.
    # postal_code is each store's OWN postal code. It doubles as the search
    # anchor for the Staples API (verified: every store is findable from its
    # own postal code) and as the value you paste into a retailer's
    # "find a store" box to switch to that exact location.
    "bb": {
        "region": "NL",
        "name": "Best Buy · Stavanger Dr", "km": 3.0,
        "kind": "bestbuy", "store_id": "909", "postal_code": "A1A 5E8",
        "store_url": "https://stores.bestbuy.ca/en-ca/nl/st-johns/3-stavanger-dr",
        "method": "official API", "conf": "high",
    },
    "bb_aval": {
        "region": "NL",
        "name": "Best Buy Express · Avalon Mall", "km": 2.2,
        "kind": "bestbuy", "store_id": "122", "postal_code": "A1B 1W3",
        "store_url": "https://stores.bestbuy.ca/en-ca/nl/st-john%27s/48-kenmount-rd-unit-0185",
        "method": "official API", "conf": "high",
    },
    "stap_stav": {
        "region": "NL",
        "name": "Staples · Stavanger Dr", "km": 9.0,
        "kind": "staples", "store_id": "65", "postal_code": "A1A 5E8",
        "store_url": "https://stores.staples.ca/nl/st-johns/office-supplies-ca-65.html",
        "method": "official API", "conf": "high",
    },
    "stap_mtpearl": {
        "region": "NL",
        "name": "Staples · Mount Pearl", "km": 0.1,
        "kind": "staples", "store_id": "101", "postal_code": "A1N 4Y9",
        "store_url": "https://stores.staples.ca/nl/mountpearl/office-supplies-ca-101.html",
        "method": "official API", "conf": "high",
    },
    "stap_kelsey": {
        "region": "NL",
        "name": "Staples · Kelsey Dr", "km": 3.7,
        "kind": "staples", "store_id": "434", "postal_code": "A1B 5C8",
        "store_url": "https://stores.staples.ca/nl/st-johns/office-supplies-ca-434.html",
        "method": "official API", "conf": "high",
    },
    "stap_cb": {
        "region": "NL",
        "name": "Staples · Corner Brook", "km": 680.0,
        "kind": "staples", "store_id": "218", "postal_code": "A2H 1R4",
        "store_url": "https://stores.staples.ca/nl/cornerbrook/office-supplies-ca-218.html",
        "method": "official API", "conf": "high",
    },
    "stap_wood": {
        "drive": 5, "dir": "S",
        "region": "ON",
        "name": "Staples · Woodstock ON", "km": 2900.0,
        "kind": "staples", "store_id": "235", "postal_code": "N4V 1B8",
        "store_url": "https://stores.staples.ca/on/woodstock/office-supplies-ca-235.html",
        "method": "official API", "conf": "high",
    },
    # --- Ontario cluster: km is distance from Woodstock, not St. John's,
    # since these are only useful as a delivery/pickup order around there ---
    # Best Buy: full-size stores only. The Express/Mobile mall kiosks nearby
    # (Stratford, Argyle, Fairview, White Oaks, Masonville, Conestoga) are
    # phone-and-accessory counters and won't hold a console. (Avalon Mall
    # Express above is kept anyway - it's 2 km away and the API treats it as
    # a real pickup point, so a ship-to-store unit there would show up.)
    "bb_on620": {
        "drive": 40, "dir": "E",
        "region": "ON",
        "name": "Best Buy · Brantford ON", "km": 47.0,
        "kind": "bestbuy", "store_id": "620", "postal_code": "N3R 7J9",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/brantford/61-lynden-rd-unit-a",
        "method": "official API", "conf": "high",
    },
    "bb_on936": {
        "drive": 40, "dir": "SW",
        "region": "ON",
        "name": "Best Buy · London South ON", "km": 53.0,
        "kind": "bestbuy", "store_id": "936", "postal_code": "N6E 1M2",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/london/1080-wellington-rd",
        "method": "official API", "conf": "high",
    },
    "bb_on980": {
        "drive": 55, "dir": "W",
        "region": "ON",
        "name": "Best Buy · North London ON", "km": 57.0,
        "kind": "bestbuy", "store_id": "980", "postal_code": "N5X 3Y2",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/london/1735-richmond-st-unit-1",
        "method": "official API", "conf": "high",
    },
    "bb_on995": {
        "drive": 40, "dir": "NE",
        "region": "ON",
        "name": "Best Buy · Cambridge ON", "km": 51.0,
        "kind": "bestbuy", "store_id": "995", "postal_code": "N1R 8K5",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/cambridge/28-pinebush-rd",
        "method": "official API", "conf": "high",
    },
    "bb_on935": {
        "drive": 45, "dir": "NE",
        "region": "ON",
        "name": "Best Buy · Kitchener ON", "km": 52.0,
        "kind": "bestbuy", "store_id": "935", "postal_code": "N2C 1X2",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/kitchener/215-fairway-rd-s",
        "method": "official API", "conf": "high",
    },
    "bb_on608": {
        "drive": 55, "dir": "NE",
        "region": "ON",
        "name": "Best Buy · Waterloo ON", "km": 60.0,
        "kind": "bestbuy", "store_id": "608", "postal_code": "N2L 6L3",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/waterloo/580-king-st-n-bldg-b",
        "method": "official API", "conf": "high",
    },
    # --- the 45-50 minute ring from Woodstock: far enough to be worth the
    # drive for a console, close enough that the unit is still there when you
    # arrive. Burlington is the edge (78 km / ~50 min); Oakville, Mississauga
    # and Toronto are 60-90 min and deliberately left out.
    "bb_on631": {
        "drive": 60, "dir": "NE",
        "region": "ON",
        "name": "Best Buy · Guelph ON", "km": 60.0,
        "kind": "bestbuy", "store_id": "631", "postal_code": "N1G 5L4",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/guelph/151-stone-rd-west",
        "method": "official API", "conf": "high",
    },
    "bb_on982": {
        "drive": 55, "dir": "E",
        "region": "ON",
        "name": "Best Buy · Ancaster ON", "km": 65.0,
        "kind": "bestbuy", "store_id": "982", "postal_code": "L9K 1J9",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/ancaster/14-martindale-crescent",
        "method": "official API", "conf": "high",
    },
    "bb_on942": {
        "drive": 70, "dir": "E",
        "region": "ON",
        "name": "Best Buy · Burlington ON", "km": 78.0,
        "kind": "bestbuy", "store_id": "942", "postal_code": "L7P 5C6",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/burlington/1200-brant-st-unit-1",
        "method": "official API", "conf": "high",
    },
    "bb_on984": {
        "drive": 65, "dir": "E",
        "region": "ON",
        "name": "Best Buy · Hamilton ON", "km": 85.0,
        "kind": "bestbuy", "store_id": "984", "postal_code": "L8J 0B4",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/hamilton/1779-stone-church-rd-e",
        "method": "official API", "conf": "high",
    },

    # --- the outer ring: 75-125 minutes out, so 2.5-4 hours return. Worth
    # it only when the unit is already paid for through buy-online-pickup,
    # which reserves it - never drive these on spec. They cost nothing to
    # watch, since every Best Buy store rides in the same batched request.
    "bb_on622": {
        "region": "ON",
        "drive": 75, "dir": "NE",
        "name": "Best Buy · Mississauga W ON", "km": 0.0,
        "kind": "bestbuy", "store_id": "622", "postal_code": "L5N 0A2",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/mississauga/2975-argentia-rd",
        "method": "official API", "conf": "high",
    },
    "bb_on926": {
        "region": "ON",
        "drive": 80, "dir": "NE",
        "name": "Best Buy · Mississauga Heartland ON", "km": 0.0,
        "kind": "bestbuy", "store_id": "926", "postal_code": "L5R 4G6",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/mississauga/6075-mavis-rd-unit-1",
        "method": "official API", "conf": "high",
    },
    "bb_on930": {
        "region": "ON",
        "drive": 85, "dir": "NE",
        "name": "Best Buy · Oakville ON", "km": 0.0,
        "kind": "bestbuy", "store_id": "930", "postal_code": "L6H 7E5",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/oakville/2500-winston-park-dr-unit-a",
        "method": "official API", "conf": "high",
    },
    "bb_on954": {
        "region": "ON",
        "drive": 90, "dir": "NE",
        "name": "Best Buy · Brampton ON", "km": 0.0,
        "kind": "bestbuy", "store_id": "954", "postal_code": "L6S 6G6",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/brampton/9200-airport-rd",
        "method": "official API", "conf": "high",
    },
    "bb_on938": {
        "region": "ON",
        "drive": 95, "dir": "NE",
        "name": "Best Buy · Sherway Etobicoke ON", "km": 0.0,
        "kind": "bestbuy", "store_id": "938", "postal_code": "M9C 1A7",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/etobicoke/167-north-queen-st",
        "method": "official API", "conf": "high",
    },
    "bb_on950": {
        "region": "ON",
        "drive": 105, "dir": "E",
        "name": "Best Buy · St Catharines ON", "km": 0.0,
        "kind": "bestbuy", "store_id": "950", "postal_code": "L2S 0C7",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/st-catharines/420-vansickle-rd-unit-l1",
        "method": "official API", "conf": "high",
    },
    "bb_on610": {
        "region": "ON",
        "drive": 110, "dir": "W",
        "name": "Best Buy · Sarnia ON", "km": 0.0,
        "kind": "bestbuy", "store_id": "610", "postal_code": "N7S 3X9",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/sarnia/unit-a-1380-exmouth-st",
        "method": "official API", "conf": "high",
    },
    "bb_on615": {
        "region": "ON",
        "drive": 120, "dir": "NE",
        "name": "Best Buy · Orangeville ON", "km": 0.0,
        "kind": "bestbuy", "store_id": "615", "postal_code": "L9W 2E8",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/orangeville/95-first-st",
        "method": "official API", "conf": "high",
    },
    "bb_on624": {
        "region": "ON",
        "drive": 125, "dir": "SW",
        "name": "Best Buy · Chatham ON", "km": 0.0,
        "kind": "bestbuy", "store_id": "624", "postal_code": "N7L 0E8",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/chatham/802-st-claire-st",
        "method": "official API", "conf": "high",
    },
    "stap_on260": {
        "drive": 40, "dir": "S",
        "region": "ON",
        "name": "Staples · Tillsonburg ON", "km": 27.0,
        "kind": "staples", "store_id": "260", "postal_code": "N4G 5A7",
        "store_url": "https://stores.staples.ca/on/tillsonburg/office-supplies-ca-260.html",
        "method": "official API", "conf": "high",
    },
    "stap_on284": {
        "drive": 35, "dir": "NW",
        "region": "ON",
        "name": "Staples · Stratford ON", "km": 41.0,
        "kind": "staples", "store_id": "284", "postal_code": "N4Z 1A5",
        "store_url": "https://stores.staples.ca/on/stratford/office-supplies-ca-284.html",
        "method": "official API", "conf": "high",
    },
    "stap_on103": {
        "drive": 40, "dir": "E",
        "region": "ON",
        "name": "Staples · Brantford ON", "km": 47.0,
        "kind": "staples", "store_id": "103", "postal_code": "N3R 7J2",
        "store_url": "https://stores.staples.ca/on/brantford/office-supplies-ca-103.html",
        "method": "official API", "conf": "high",
    },
    "stap_on9": {
        "drive": 40, "dir": "SW",
        "region": "ON",
        "name": "Staples · London East ON", "km": 50.0,
        "kind": "staples", "store_id": "9", "postal_code": "N5V 1P7",
        "store_url": "https://stores.staples.ca/on/london/office-supplies-ca-9.html",
        "method": "official API", "conf": "high",
    },
    "stap_on53": {
        "drive": 40, "dir": "NE",
        "region": "ON",
        "name": "Staples · Cambridge ON", "km": 51.0,
        "kind": "staples", "store_id": "53", "postal_code": "N1R 6J5",
        "store_url": "https://stores.staples.ca/on/cambridge/office-supplies-ca-53.html",
        "method": "official API", "conf": "high",
    },
    "stap_on5": {
        "drive": 45, "dir": "NE",
        "region": "ON",
        "name": "Staples · Kitchener S ON", "km": 52.0,
        "kind": "staples", "store_id": "5", "postal_code": "N2E 3W7",
        "store_url": "https://stores.staples.ca/on/kitchener/office-supplies-ca-5.html",
        "method": "official API", "conf": "high",
    },
    "stap_on8": {
        "drive": 45, "dir": "SW",
        "region": "ON",
        "name": "Staples · London S ON", "km": 53.0,
        "kind": "staples", "store_id": "8", "postal_code": "N6C 4P6",
        "store_url": "https://stores.staples.ca/on/london/office-supplies-ca-8.html",
        "method": "official API", "conf": "high",
    },
    "stap_on445": {
        "drive": 50, "dir": "NE",
        "region": "ON",
        "name": "Staples · Kitchener W ON", "km": 54.0,
        "kind": "staples", "store_id": "445", "postal_code": "N2N 0B1",
        "store_url": "https://stores.staples.ca/on/kitchener/office-supplies-ca-445.html",
        "method": "official API", "conf": "high",
    },
    "stap_on262": {
        "drive": 45, "dir": "SW",
        "region": "ON",
        "name": "Staples · London W ON", "km": 55.0,
        "kind": "staples", "store_id": "262", "postal_code": "N6L 1A6",
        "store_url": "https://stores.staples.ca/on/london/office-supplies-ca-262.html",
        "method": "official API", "conf": "high",
    },
    "stap_on67": {
        "drive": 55, "dir": "W",
        "region": "ON",
        "name": "Staples · London N ON", "km": 57.0,
        "kind": "staples", "store_id": "67", "postal_code": "N5X 3Y2",
        "store_url": "https://stores.staples.ca/on/london/office-supplies-ca-67.html",
        "method": "official API", "conf": "high",
    },
    "stap_on120": {
        "drive": 55, "dir": "NE",
        "region": "ON",
        "name": "Staples · Waterloo ON", "km": 60.0,
        "kind": "staples", "store_id": "120", "postal_code": "N2J 4G8",
        "store_url": "https://stores.staples.ca/on/waterloo/office-supplies-ca-120.html",
        "method": "official API", "conf": "high",
    },
    "stap_on441": {
        "drive": 60, "dir": "SW",
        "region": "ON",
        "name": "Staples · St. Thomas ON", "km": 62.0,
        "kind": "staples", "store_id": "441", "postal_code": "N5P 1G4",
        "store_url": "https://stores.staples.ca/on/stthomas/office-supplies-ca-441.html",
        "method": "official API", "conf": "high",
    },
    "stap_on201": {
        "drive": 65, "dir": "NE",
        "region": "ON",
        "name": "Staples · Guelph N ON", "km": 66.0,
        "kind": "staples", "store_id": "201", "postal_code": "N1H 1G7",
        "store_url": "https://stores.staples.ca/on/guelph/office-supplies-ca-201.html",
        "method": "official API", "conf": "high",
    },
    "stap_on81": {
        "drive": 60, "dir": "NE",
        "region": "ON",
        "name": "Staples · Guelph S ON", "km": 68.0,
        "kind": "staples", "store_id": "81", "postal_code": "N1G 4Z1",
        "store_url": "https://stores.staples.ca/on/guelph/office-supplies-ca-81.html",
        "method": "official API", "conf": "high",
    },
    "stap_on59": {
        "drive": 55, "dir": "E",
        "region": "ON",
        "name": "Staples · Ancaster ON", "km": 84.0,
        "kind": "staples", "store_id": "59", "postal_code": "L9K 1L6",
        "store_url": "https://stores.staples.ca/on/ancaster/office-supplies-ca-59.html",
        "method": "official API", "conf": "high",
    },
    "stap_on222": {
        "drive": 60, "dir": "E",
        "region": "ON",
        "name": "Staples · Hamilton Mtn ON", "km": 95.0,
        "kind": "staples", "store_id": "222", "postal_code": "L9A 5C5",
        "store_url": "https://stores.staples.ca/on/hamilton/office-supplies-ca-222.html",
        "method": "official API", "conf": "high",
    },
    "stap_on456": {
        "drive": 65, "dir": "E",
        "region": "ON",
        "name": "Staples · Hamilton E ON", "km": 97.0,
        "kind": "staples", "store_id": "456", "postal_code": "L9H 7K6",
        "store_url": "https://stores.staples.ca/on/hamilton/office-supplies-ca-456.html",
        "method": "official API", "conf": "high",
    },
    "stap_on14": {
        "drive": 70, "dir": "E",
        "region": "ON",
        "name": "Staples · Burlington Plains Rd ON", "km": 78.0,
        "kind": "staples", "store_id": "14", "postal_code": "L7T 4K1",
        "store_url": "https://stores.staples.ca/on/burlington/office-supplies-ca-14.html",
        "method": "official API", "conf": "high",
    },
    "stap_on229": {
        "drive": 70, "dir": "E",
        "region": "ON",
        "name": "Staples · Burlington Davidson Ct ON", "km": 85.0,
        "kind": "staples", "store_id": "229", "postal_code": "L7M 4X7",
        "store_url": "https://stores.staples.ca/on/burlington/office-supplies-ca-229.html",
        "method": "official API", "conf": "high",
    },
    "stap_on439": {
        "drive": 70, "dir": "E",
        "region": "ON",
        "name": "Staples · Hamilton Barton St ON", "km": 90.0,
        "kind": "staples", "store_id": "439", "postal_code": "L8H 2V4",
        "store_url": "https://stores.staples.ca/on/hamilton/office-supplies-ca-439.html",
        "method": "official API", "conf": "high",
    },
    # Ship-to-home, not a building. Best Buy returns online stock in the
    # same response as the store list, and it was being thrown away - yet
    # online is usually the first thing to come back, and you can order it
    # the second it does instead of driving anywhere.
    "bb_online": {
        "region": "WEB",
        "name": "Best Buy · Online (ships to you)", "km": 0.0,
        "kind": "bestbuy", "store_id": "online",
        "method": "official API", "conf": "high",
    },
    "wm_stav": {
        "region": "NL",
        # Walmart Canada's inventory API returns a consistent 403 (Cloudflare
        # bot-blocked), confirmed 2026-09-05. check_walmart() is left in the
        # code in case that changes, but this store is "manual" until then.
        "name": "Walmart · Stavanger Dr", "km": 3.1,
        "kind": "manual",
        "method": "no feed · call to verify", "conf": "none",
    },
    "eb_aval": {
        "region": "NL",
        "name": "EB Games · Avalon Mall", "km": 2.2,
        "kind": "manual",
        "method": "no feed · call to verify", "conf": "none",
    },
    "cost": {
        "region": "NL",
        "name": "Costco · Blackmarsh Rd", "km": 4.6,
        "kind": "manual",
        "method": "no feed · call to verify", "conf": "none",
    },
}

# The PS5 Pro's identifiers at each chain. A retailer uses one item number and
# one product page nationally, so these are per-chain, not per-store.
BESTBUY_PS5 = "19492009"
# Best Buy carries the Pro under two separate listings that hold stock
# independently - a restock can land on either. Both are Best Buy itself,
# not marketplace sellers, and both were read back from their search API.
BESTBUY_PS5_ALT = "18477929"
STAPLES_PS5 = "3103551"
STAPLES_PS5_URL = "https://www.staples.ca/products/3103551-en-sony-playstation-5-pro-console"
BESTBUY_PS5_URL = "https://www.bestbuy.ca/en-ca/product/playstation-5-pro-console/19492009"
BESTBUY_PS5_ALT_URL = "https://www.bestbuy.ca/en-ca/product/playstation-5-pro-console/18477929"

# The products you're hunting. `skus` maps a store key -> that store's own SKU.
# To add a Pokemon/One Piece box once you've picked one, copy this block and
# fill in the "skus" the same way (F12 method in SETUP.md step 3).
PRODUCTS = [
    {
        "id": "p5", "cat": "PS5", "tag": "CONSOLE", "channel": "ps5",
        "product": "PlayStation 5 Pro 2TB", "sku": "19492009", "msrp": 1099.99,
        # Walmart Canada doesn't carry the real PS5 Pro (only marketplace
        # resellers at inflated prices), so no "wm_stav" entry here.
        # one item number covers every location of a chain, so stores added
        # above are picked up here automatically
        "skus": {
            **{k: BESTBUY_PS5 for k, v in STORES.items() if v["kind"] == "bestbuy"},
            **{k: STAPLES_PS5 for k, v in STORES.items() if v["kind"] == "staples"},
        },
        # same product page for every store of that chain - only the
        # postal code/store picker on the page itself changes, not the URL
        "urls": {
            **{k: BESTBUY_PS5_URL for k, v in STORES.items() if v["kind"] == "bestbuy"},
            **{k: STAPLES_PS5_URL for k, v in STORES.items() if v["kind"] == "staples"},
        },
    },
    {
        "id": "p5alt", "cat": "PS5", "tag": "CONSOLE", "channel": "ps5",
        "product": "PlayStation 5 Pro 2TB (2nd listing)", "sku": BESTBUY_PS5_ALT,
        "msrp": 1099.95,
        # Best Buy only - Staples lists the console once
        "skus": {k: BESTBUY_PS5_ALT for k, v in STORES.items() if v["kind"] == "bestbuy"},
        "urls": {k: BESTBUY_PS5_ALT_URL for k, v in STORES.items() if v["kind"] == "bestbuy"},
    },
]

OUT = Path(__file__).parent / "stock.json"

# ----------------------------------------------------------------------------
# store checkers
# ----------------------------------------------------------------------------

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


_bestbuy_cache = {}   # sku -> {store_id: (qty, price)}, reset each sweep


def check_bestbuy(sku, store_id):
    """Best Buy Canada availability. Returns (qty, price) or (None, None).

    The endpoint takes a pipe-separated list of stores and names each one back
    in the response, so a single request covers the whole chain. Results are
    cached per sweep exactly the way Staples is: 12 stores cost one request
    per product instead of 12. That headroom is the point - checking every
    15 minutes across a dozen stores is what would otherwise earn us the
    HTTP 403 rate-limit the workflow logs warn about.

    NOTE: verify this endpoint before trusting it - Best Buy changes it
    occasionally. Open a product page, press F12, watch the Network tab and
    look for the request that carries 'availability' in its URL.
    """
    cache = _bestbuy_cache.get(sku)
    if cache is None:
        # "online" is a pseudo-store standing for ship-to-home, not a real
        # location, so it must not go into the locations list
        ids = [v["store_id"] for v in STORES.values()
               if v["kind"] == "bestbuy" and v["store_id"] != "online"]
        url = ("https://www.bestbuy.ca/ecomm-api/availability/products"
               "?accept=application%2Fvnd.bestbuy.standardproduct.v1%2Bjson"
               "&accept-language=en-CA&locations=" + "%7C".join(ids) +
               f"&postalCode=A1A1A1&skus={sku}")
        data = _get_json(url)
        cache = {}
        for av in data.get("availabilities", []):
            price = (av.get("pricing") or {}).get("regularPrice")
            for loc in av.get("pickup", {}).get("locations", []):
                qty = loc.get("quantityOnHand")
                if qty is None:
                    # older shape only says yes/no, so treat "yes" as a single
                    # unit - enough to fire the alert and let you phone them
                    qty = 1 if loc.get("hasInventory") else 0
                cache[str(loc["locationKey"])] = (int(qty), price)
            # the same response carries ship-to-home stock; "online" is the
            # pseudo-store that reports it
            ship = av.get("shipping") or {}
            online = ship.get("quantityRemaining")
            if online is None:
                online = 1 if ship.get("purchasable") else 0
            cache["online"] = (int(online), price)
        _bestbuy_cache[sku] = cache
    # a store missing from the response is unknown, not out of stock
    return cache.get(str(store_id), (None, None))



def check_walmart(sku, store_id):
    """Walmart Canada. Fragile - they rate-limit and change shape often.

    Find your store id: walmart.ca -> set your store -> the id appears in the
    page's network requests and in your cookies.
    """
    url = f"https://www.walmart.ca/api/product-page/find-in-store?upc={sku}&lang=en"
    data = _get_json(url, {"Referer": "https://www.walmart.ca/"})
    for info in data.get("info", []):
        if str(info.get("id")) == str(store_id):
            avail = info.get("availableToSellQty")
            price = (info.get("sellPrice") or {}).get("value")
            return (int(avail) if avail is not None else 0), price
    return None, None


_staples_cache = {}   # sku -> {store_id: qty}, reset each sweep
# store_id -> local phone number, harvested from the same response. Staples
# will hold an item if you phone the store, and unlike Best Buy - whose every
# store page lists one national 1-866 number - these are direct lines.
_staples_phone = {}


def check_staples(sku, store_id, postal_code):
    """Staples Canada inventory API. No auth needed.

    One call returns every store near the given postal code, not just the one
    asked for, so results are cached per sweep - checking 20 nearby stores
    costs a handful of requests rather than 20.

    postal_code must be near store_id: the API only returns stores within
    range of it, ignoring store_number if it's out of range.
    """
    cache = _staples_cache.setdefault(sku, {})
    if store_id in cache:
        return cache[store_id], None

    body = json.dumps({
        "locale": "en-CA", "location": "PickInStore",
        "postal_code": postal_code, "store_number": store_id,
        "items": [{"sku": sku, "quantity": 1000}],
    }).encode()
    req = urllib.request.Request(
        "https://api.staples.ca/ecommerce/inventory/v2.0/request",
        data=body, headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())

    # absorb every store this response mentions, not just the one we wanted
    for sid, info in data.get("availability", {}).get(sku, {}).items():
        cache[sid] = int(info.get("availableqty", 0))
        digits = "".join(c for c in str(info.get("phoneNumber", "")) if c.isdigit())
        if len(digits) == 10:
            _staples_phone[sid] = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"

    return cache.get(store_id), None


def status_for(qty):
    if qty is None:
        return None
    if qty <= 0:
        return "out"
    return "low" if qty <= 2 else "in"


# ----------------------------------------------------------------------------
# discord
# ----------------------------------------------------------------------------

def webhook_for(channel):
    """Fall back to the ps5 channel so a mis-tagged product still reaches you."""
    return WEBHOOKS.get(channel) or WEBHOOKS.get("ps5", "")


def notify(text, channel="ps5", ping=False):
    hook = webhook_for(channel)
    if not hook:
        print(f"  [no webhook for '{channel}']", text)
        return
    if ping and PING:
        text = f"{PING} {text}"
    # allowed_mentions is not optional: without it Discord renders a webhook's
    # @everyone as literal text and notifies nobody. The empty parse list on the
    # quieter alerts stops a product name containing an @ from pinging the
    # channel by accident.
    body = json.dumps({
        "content": text,
        "allowed_mentions": {"parse": ["everyone", "users", "roles"] if ping else []},
    }).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                hook, data=body,
                headers={**UA, "Content-Type": "application/json"}),
            timeout=15)
    except Exception as e:
        print("  discord failed:", e)


# Each channel keeps one pinned message showing the current state of its own
# products, rewritten in place each sweep so it never spams. Message ids live
# in status_message.json ({channel: id}) so later runs know what to edit.
STATUS_FILE = Path(__file__).parent / "status_message.json"
LEGACY_STATUS_FILE = Path(__file__).parent / "status_message.txt"

ICON = {"in": "\U0001F7E2", "low": "\U0001F7E1", "out": "⬜"}


def build_status(hits, now, channel):
    # Discord renders <t:epoch:R> as a live "3 minutes ago" that keeps
    # counting up on its own between sweeps, and <t:epoch:t> as a clock in
    # whoever is reading it's own timezone - which beats printing the
    # runner's UTC and leaving everyone to do the subtraction.
    stamp = int(now.timestamp())
    lines = [f"**RESTOCK RADAR** · checked <t:{stamp}:R> at <t:{stamp}:t>",
             f"next check <t:{stamp + 900}:R>"]
    by_pid = {}
    for h in hits:
        by_pid.setdefault(h["pid"], []).append(h)

    for product in PRODUCTS:
        if product.get("channel", "ps5") != channel:
            continue
        rows = by_pid.get(product["id"], [])
        if not rows:
            continue
        lines.append(f"\n**{product['product']}** · ${product['msrp']}")
        # Newfoundland and Ontario are separate hunting grounds with a
        # different person driving to each, so they are listed apart rather
        # than interleaved. Distance is not a useful sort once they're split -
        # in stock first, then alphabetical.
        for region, heading in (("WEB", "Online"), ("NL", "Newfoundland"),
                                ("ON", "Ontario")):
            here = [h for h in rows if STORES[h["store"]].get("region") == region]
            if not here:
                continue
            label = heading
            if region == "ON":
                label += " · drive from Woodstock (Knightsbridge Rd)"
            lines.append(f"__{label}__")
            # nearest first where we know the drive, so the list reads as a
            # route: what you would pass on the way to anything further out
            here.sort(key=lambda h: (h["status"] == "out",
                                     STORES[h["store"]].get("drive", 9999),
                                     STORES[h["store"]]["name"]))
            for h in here:
                store = STORES[h["store"]]
                icon = ICON.get(h["status"], "⬜")
                trip = ""
                if store.get("drive"):
                    trip = f" · ~{store['drive']} min {store['dir']}"
                if h["status"] == "out":
                    lines.append(f"{icon} {store['name']}{trip}")
                else:
                    postal = store.get("postal_code", "")
                    tail = f" · `{postal}`" if postal else ""
                    lines.append(f"{icon} **{store['name']} — {h['qty']} in stock**{trip}{tail}")

    manual = [s["name"] for s in STORES.values() if s["kind"] == "manual"]
    if manual:
        lines.append("\n*No feed — call to check: " + ", ".join(manual) + "*")
    return "\n".join(lines)


def _load_status_ids():
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    # carry over the single-channel file from before channels existed
    if LEGACY_STATUS_FILE.exists():
        return {"ps5": LEGACY_STATUS_FILE.read_text(encoding="utf-8").strip()}
    return {}


# Discord rejects a message body over its limit outright, and push_status
# treats that like any other failed edit - three retries, then "leaving pin
# alone until next run", every run, forever. That is exactly how the PS5
# status message quietly froze once the store list grew past 2000 characters
# while the shorter GPU one kept updating. Sending the status as an embed
# raises the ceiling to 4096, and _fit() guarantees we stay under it.
STATUS_LIMIT = 4096


def _fit(text, limit=STATUS_LIMIT):
    """Trim to fit, dropping out-of-stock lines last-first.

    An empty checkbox is the least useful line in the message, so those go
    before anything else. Nothing in stock is ever dropped.
    """
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    note, dropped = "", 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(ICON["out"]):
            lines.pop(i)
            dropped += 1
            note = f"\n*+{dropped} more out of stock*"
            if len("\n".join(lines)) + len(note) <= limit:
                break
    return "\n".join(lines)[:limit - len(note)] + note


def push_status(text, channel):
    """Edit that channel's status message, or post a new one and remember its id."""
    # A webhook can only edit its own messages, so a dedicated status channel
    # keeps its id under its own key - switching either way just posts fresh
    # rather than trying to edit a message the other webhook owns.
    hook = STATUS_WEBHOOKS.get(channel) or webhook_for(channel)
    key = f"{channel}_status" if STATUS_WEBHOOKS.get(channel) else channel
    if not hook:
        return
    body = json.dumps({"content": "", "embeds": [{"description": _fit(text)}]}).encode()
    headers = {**UA, "Content-Type": "application/json"}

    ids = _load_status_ids()
    msg_id = ids.get(key, "")
    if msg_id:
        # Only a 404 means the pinned message is actually gone. Anything else
        # (connection reset, timeout, 429, 5xx) is a blip - posting a fresh
        # message on those would orphan the user's pin, which is exactly what
        # happened once. Retry a couple of times, then leave it for next run.
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    f"{hook}/messages/{msg_id}", data=body,
                    headers=headers, method="PATCH")
                urllib.request.urlopen(req, timeout=15)
                print(f"  [{channel}] status message updated")
                return
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    print(f"  [{channel}] pinned message is gone (404) - posting a new one")
                    break
                print(f"  [{channel}] edit attempt {attempt + 1} failed: HTTP {e.code}")
            except Exception as e:
                print(f"  [{channel}] edit attempt {attempt + 1} failed: {e}")
            time.sleep(3)
        else:
            print(f"  [{channel}] couldn't edit status message - leaving pin alone until next run")
            return

    try:
        req = urllib.request.Request(hook + "?wait=true", data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            new_id = json.loads(r.read().decode()).get("id", "")
        if new_id:
            ids[key] = new_id
            STATUS_FILE.write_text(json.dumps(ids, indent=2), encoding="utf-8")
            print(f"  [{channel}] posted new status message - pin it in Discord")
    except Exception as e:
        print(f"  [{channel}] status message failed:", e)


# ----------------------------------------------------------------------------
# main loop
# ----------------------------------------------------------------------------

previous = {}   # (pid, store) -> (status, qty)
alerts = []     # newest first, capped


def load_previous_state():
    """Rebuild `previous` and `alerts` from the last stock.json.

    Matters for --once runs (GitHub Actions), where the process starts fresh
    every time. Without this, every run would look like a first run and
    re-alert on anything currently in stock.
    """
    if not OUT.exists():
        return
    try:
        data = json.loads(OUT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print("  couldn't read previous state:", e)
        return
    for hit in data.get("hits", []):
        previous[(hit["pid"], hit["store"])] = (hit["status"], hit.get("qty"))
    alerts.extend(data.get("alerts", []))
    print(f"  restored {len(previous)} store states, {len(alerts)} past alerts")


def sweep():
    hits = []
    now = datetime.now()
    _staples_cache.clear()   # stock moves between sweeps - never reuse across them
    _bestbuy_cache.clear()

    for product in PRODUCTS:
        for store_key, store in STORES.items():
            if store["kind"] == "manual":
                continue
            sku = product.get("skus", {}).get(store_key)
            if not sku:
                continue

            try:
                if store["kind"] == "bestbuy":
                    qty, price = check_bestbuy(sku, store["store_id"])
                elif store["kind"] == "staples":
                    qty, price = check_staples(sku, store["store_id"], store["postal_code"])
                else:
                    qty, price = check_walmart(sku, store["store_id"])
            except Exception as e:
                print(f"  {store['name']}: check failed ({e})")
                continue

            state = status_for(qty)
            if state is None:
                continue

            hits.append({
                "pid": product["id"], "store": store_key,
                "qty": qty, "price": price or product["msrp"],
                "status": state, "seenSecs": 0,
            })

            key = (product["id"], store_key)
            was_state, was_qty = previous.get(key, (None, None))
            previous[key] = (state, qty)

            def where_to_buy():
                """The 'how do I act on this' lines shared by both alerts.

                Ordered for speed on a phone. There is no store picker to
                deep-link: Best Buy searches within 50km of a postal code that
                it keeps in a cookie, and the URL never changes when you set
                one (confirmed 2026-09-26). So the code has to be pasted by
                hand every time, which makes it the first thing you need and
                the reason it leads here - long-press the backticks to copy.
                Online stock is the only genuinely one-tap case: no postal
                code is involved, so the link alone finishes the job.
                """
                out = []
                if store.get("drive"):
                    # round trip, since that is the number that decides
                    # whether the trip is worth making at all
                    out.append(f"Drive: ~{store['drive']} min {store['dir']} "
                               f"(~{store['drive'] * 2} min return)")
                if store.get("postal_code"):
                    out.append(f"1. Copy: `{store['postal_code']}`")
                product_url = product.get("urls", {}).get(store_key)
                if product_url:
                    step = "2. Open" if store.get("postal_code") else "Buy now"
                    out.append(f"{step}: {product_url}")
                    if store.get("postal_code"):
                        out.append('3. Paste it under "Pick Up" and hit Check')
                if store.get("store_url"):
                    out.append(f"Store (address & phone): {store['store_url']}")
                phone = _staples_phone.get(store.get("store_id", ""))
                if phone and store["kind"] == "staples":
                    # Staples will put one aside if you ring the store - the
                    # closest thing to holding stock that actually exists
                    out.append(f"Call to hold: {phone}")
                return out

            if was_state in (None, "out") and state in ("in", "low"):
                line = f"{qty} unit(s) · ${price or product['msrp']}"
                alerts.insert(0, {
                    "pid": product["id"], "store": store_key,
                    "time": now.strftime("%H:%M"),
                    "tag": "IN" if state == "in" else "LOW",
                    "status": state, "extra": line,
                })
                notify("\n".join(["**IN STOCK**", product["product"],
                                  store["name"], line] + where_to_buy()),
                       product.get("channel", "ps5"), ping=True)
                print(f"  ALERT {product['product']} @ {store['name']} - {line}")
            elif (state in ("in", "low") and was_state in ("in", "low")
                    and was_qty is not None and qty != was_qty):
                direction = "dropped" if qty < was_qty else "went up"
                line = f"{was_qty} → {qty} unit(s)"
                alerts.insert(0, {
                    "pid": product["id"], "store": store_key,
                    "time": now.strftime("%H:%M"), "tag": "QTY",
                    "status": state, "extra": line,
                })
                notify("\n".join([f"**STOCK {direction.upper()}**", product["product"],
                                  store["name"], line] + where_to_buy()),
                       product.get("channel", "ps5"))
                print(f"  ALERT qty {direction} {product['product']} @ {store['name']} - {line}")
            elif was_state in ("in", "low") and state == "out":
                alerts.insert(0, {
                    "pid": product["id"], "store": store_key,
                    "time": now.strftime("%H:%M"), "tag": "GONE",
                    "status": "out", "extra": "sold out",
                })
                # no ping - there is nothing left to act on, this just keeps
                # the channel from ending on a stale "1 unit" message
                line = f"{was_qty} → 0 unit(s)" if was_qty is not None else "0 unit(s)"
                notify("\n".join(["**SOLD OUT**", product["product"],
                                  store["name"], line]),
                       product.get("channel", "ps5"))
                print(f"  ALERT sold out {product['product']} @ {store['name']}")

    del alerts[60:]

    OUT.write_text(json.dumps({
        "stores": {k: {kk: vv for kk, vv in v.items()
                       if kk in ("name", "km", "method", "conf", "store_url", "postal_code")}
                   for k, v in STORES.items()},
        "products": [{k: v for k, v in p.items() if k != "skus"} for p in PRODUCTS],
        "hits": hits,
        "alerts": alerts,
        "updated": now.isoformat(),
    }, indent=2), encoding="utf-8")

    print(f"[{now:%H:%M:%S}] {len(hits)} rows written")

    for channel in dict.fromkeys(p.get("channel", "ps5") for p in PRODUCTS):
        push_status(build_status(hits, now, channel), channel)


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(OUT.parent))

    class Server(socketserver.TCPServer):
        allow_reuse_address = True

        def finish_request(self, request, client_address):
            self.RequestHandlerClass(request, client_address, self)

    class CORS(handler.func):
        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, *a):
            pass

    with Server(("", PORT), functools.partial(CORS, directory=str(OUT.parent))) as httpd:
        print(f"serving http://localhost:{PORT}/stock.json")
        httpd.serve_forever()


def _arg(name, default):
    """Read "--name value" out of argv."""
    if name in sys.argv:
        at = sys.argv.index(name)
        if at + 1 < len(sys.argv):
            return sys.argv[at + 1]
    return default


if __name__ == "__main__":
    # Windows consoles default to cp1252, which has no arrow character - so
    # printing a "2 -> 1 unit(s)" alert would kill the sweep mid-run. The
    # webhook payloads are UTF-8 regardless; this only affects the console.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    for _ch, _url in WEBHOOKS.items():
        if not _url:
            print(f"WARNING: no webhook for '{_ch}' - its alerts will fall back "
                  f"to the ps5 channel (set DISCORD_WEBHOOK_{_ch.upper()} or "
                  f"create webhook_{_ch}.txt)\n")

    load_previous_state()

    if "--for" in sys.argv:
        # One Actions run that keeps sweeping, rather than one sweep per cron
        # firing. GitHub silently drops crons tighter than */30 (tried */10 on
        # 2026-09-09: zero runs in two hours), so a 10-15 minute cadence has to
        # come from inside a single run the scheduler is happy to start.
        #   python checker.py --for 25 --every 900   -> sweeps at 0 and 15 min
        minutes = float(_arg("--for", "25"))
        every = float(_arg("--every", CHECK_EVERY_SECONDS))
        deadline = time.monotonic() + minutes * 60
        swept = 0
        while True:
            try:
                sweep()
            except Exception as e:
                print("sweep error:", e)
            swept += 1
            # stop once another interval wouldn't fit - overrunning would
            # collide with the next cron firing, and the commit step still
            # has to run after this
            if time.monotonic() + every > deadline:
                break
            time.sleep(every)
        print(f"{swept} sweep(s) over {minutes:g} min - exiting before the next run")
    elif "--once" in sys.argv:
        sweep()
    else:
        threading.Thread(target=serve, daemon=True).start()
        print("checking every", CHECK_EVERY_SECONDS, "seconds - leave this window open\n")
        while True:
            try:
                sweep()
            except Exception as e:
                print("sweep error:", e)
            time.sleep(CHECK_EVERY_SECONDS)
