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

import json, os, sys, time, threading, http.server, socketserver, functools, urllib.request, urllib.parse
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
#   channel "gpu" -> webhook_gpu.txt  / DISCORD_WEBHOOK_GPU
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
    "gpu": _load_webhook("gpu"),
}

CHECK_EVERY_SECONDS = 120  # don't go below 60 - Walmart blocks fast pollers

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
        "name": "Best Buy · Stavanger Dr", "km": 3.0,
        "kind": "bestbuy", "store_id": "909", "postal_code": "A1A 5E8",
        "store_url": "https://stores.bestbuy.ca/en-ca/nl/st-johns/3-stavanger-dr",
        "method": "official API", "conf": "high",
    },
    "bb_aval": {
        "name": "Best Buy Express · Avalon Mall", "km": 2.2,
        "kind": "bestbuy", "store_id": "122", "postal_code": "A1B 1W3",
        "store_url": "https://stores.bestbuy.ca/en-ca/nl/st-john%27s/48-kenmount-rd-unit-0185",
        "method": "official API", "conf": "high",
    },
    "stap_stav": {
        "name": "Staples · Stavanger Dr", "km": 9.0,
        "kind": "staples", "store_id": "65", "postal_code": "A1A 5E8",
        "store_url": "https://stores.staples.ca/nl/st-johns/office-supplies-ca-65.html",
        "method": "official API", "conf": "high",
    },
    "stap_mtpearl": {
        "name": "Staples · Mount Pearl", "km": 0.1,
        "kind": "staples", "store_id": "101", "postal_code": "A1N 4Y9",
        "store_url": "https://stores.staples.ca/nl/mountpearl/office-supplies-ca-101.html",
        "method": "official API", "conf": "high",
    },
    "stap_kelsey": {
        "name": "Staples · Kelsey Dr", "km": 3.7,
        "kind": "staples", "store_id": "434", "postal_code": "A1B 5C8",
        "store_url": "https://stores.staples.ca/nl/st-johns/office-supplies-ca-434.html",
        "method": "official API", "conf": "high",
    },
    "stap_cb": {
        "name": "Staples · Corner Brook", "km": 680.0,
        "kind": "staples", "store_id": "218", "postal_code": "A2H 1R4",
        "store_url": "https://stores.staples.ca/nl/cornerbrook/office-supplies-ca-218.html",
        "method": "official API", "conf": "high",
    },
    "stap_wood": {
        "name": "Staples · Woodstock ON", "km": 2900.0,
        "kind": "staples", "store_id": "235", "postal_code": "N4V 1B8",
        "store_url": "https://stores.staples.ca/on/woodstock/office-supplies-ca-235.html",
        "method": "official API", "conf": "high",
    },
    # --- Ontario cluster: km is distance from Woodstock, not St. John's,
    # since these are only useful as a delivery/pickup order around there ---
    # Best Buy: full-size stores only. The Express/Mobile mall kiosks nearby
    # (Stratford, Argyle, Fairview, White Oaks, Masonville, Conestoga) are
    # phone-and-accessory counters and won't hold a console.
    "bb_on620": {
        "name": "Best Buy · Brantford ON", "km": 47.0,
        "kind": "bestbuy", "store_id": "620", "postal_code": "N3R 7J9",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/brantford/61-lynden-rd-unit-a",
        "method": "official API", "conf": "high",
    },
    "bb_on936": {
        "name": "Best Buy · London South ON", "km": 53.0,
        "kind": "bestbuy", "store_id": "936", "postal_code": "N6E 1M2",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/london/1080-wellington-rd",
        "method": "official API", "conf": "high",
    },
    "bb_on980": {
        "name": "Best Buy · North London ON", "km": 57.0,
        "kind": "bestbuy", "store_id": "980", "postal_code": "N5X 3Y2",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/london/1735-richmond-st-unit-1",
        "method": "official API", "conf": "high",
    },
    "bb_on995": {
        "name": "Best Buy · Cambridge ON", "km": 51.0,
        "kind": "bestbuy", "store_id": "995", "postal_code": "N1R 8K5",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/cambridge/28-pinebush-rd",
        "method": "official API", "conf": "high",
    },
    "bb_on935": {
        "name": "Best Buy · Kitchener ON", "km": 52.0,
        "kind": "bestbuy", "store_id": "935", "postal_code": "N2C 1X2",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/kitchener/215-fairway-rd-s",
        "method": "official API", "conf": "high",
    },
    "bb_on608": {
        "name": "Best Buy · Waterloo ON", "km": 60.0,
        "kind": "bestbuy", "store_id": "608", "postal_code": "N2L 6L3",
        "store_url": "https://stores.bestbuy.ca/en-ca/on/waterloo/580-king-st-n-bldg-b",
        "method": "official API", "conf": "high",
    },
    "stap_on260": {
        "name": "Staples · Tillsonburg ON", "km": 27.0,
        "kind": "staples", "store_id": "260", "postal_code": "N4G 5A7",
        "store_url": "https://stores.staples.ca/on/tillsonburg/office-supplies-ca-260.html",
        "method": "official API", "conf": "high",
    },
    "stap_on284": {
        "name": "Staples · Stratford ON", "km": 41.0,
        "kind": "staples", "store_id": "284", "postal_code": "N4Z 1A5",
        "store_url": "https://stores.staples.ca/on/stratford/office-supplies-ca-284.html",
        "method": "official API", "conf": "high",
    },
    "stap_on103": {
        "name": "Staples · Brantford ON", "km": 47.0,
        "kind": "staples", "store_id": "103", "postal_code": "N3R 7J2",
        "store_url": "https://stores.staples.ca/on/brantford/office-supplies-ca-103.html",
        "method": "official API", "conf": "high",
    },
    "stap_on9": {
        "name": "Staples · London East ON", "km": 50.0,
        "kind": "staples", "store_id": "9", "postal_code": "N5V 1P7",
        "store_url": "https://stores.staples.ca/on/london/office-supplies-ca-9.html",
        "method": "official API", "conf": "high",
    },
    "stap_on53": {
        "name": "Staples · Cambridge ON", "km": 51.0,
        "kind": "staples", "store_id": "53", "postal_code": "N1R 6J5",
        "store_url": "https://stores.staples.ca/on/cambridge/office-supplies-ca-53.html",
        "method": "official API", "conf": "high",
    },
    "stap_on5": {
        "name": "Staples · Kitchener S ON", "km": 52.0,
        "kind": "staples", "store_id": "5", "postal_code": "N2E 3W7",
        "store_url": "https://stores.staples.ca/on/kitchener/office-supplies-ca-5.html",
        "method": "official API", "conf": "high",
    },
    "stap_on8": {
        "name": "Staples · London S ON", "km": 53.0,
        "kind": "staples", "store_id": "8", "postal_code": "N6C 4P6",
        "store_url": "https://stores.staples.ca/on/london/office-supplies-ca-8.html",
        "method": "official API", "conf": "high",
    },
    "stap_on445": {
        "name": "Staples · Kitchener W ON", "km": 54.0,
        "kind": "staples", "store_id": "445", "postal_code": "N2N 0B1",
        "store_url": "https://stores.staples.ca/on/kitchener/office-supplies-ca-445.html",
        "method": "official API", "conf": "high",
    },
    "stap_on262": {
        "name": "Staples · London W ON", "km": 55.0,
        "kind": "staples", "store_id": "262", "postal_code": "N6L 1A6",
        "store_url": "https://stores.staples.ca/on/london/office-supplies-ca-262.html",
        "method": "official API", "conf": "high",
    },
    "stap_on67": {
        "name": "Staples · London N ON", "km": 57.0,
        "kind": "staples", "store_id": "67", "postal_code": "N5X 3Y2",
        "store_url": "https://stores.staples.ca/on/london/office-supplies-ca-67.html",
        "method": "official API", "conf": "high",
    },
    "stap_on120": {
        "name": "Staples · Waterloo ON", "km": 60.0,
        "kind": "staples", "store_id": "120", "postal_code": "N2J 4G8",
        "store_url": "https://stores.staples.ca/on/waterloo/office-supplies-ca-120.html",
        "method": "official API", "conf": "high",
    },
    "stap_on441": {
        "name": "Staples · St. Thomas ON", "km": 62.0,
        "kind": "staples", "store_id": "441", "postal_code": "N5P 1G4",
        "store_url": "https://stores.staples.ca/on/stthomas/office-supplies-ca-441.html",
        "method": "official API", "conf": "high",
    },
    "stap_on201": {
        "name": "Staples · Guelph N ON", "km": 66.0,
        "kind": "staples", "store_id": "201", "postal_code": "N1H 1G7",
        "store_url": "https://stores.staples.ca/on/guelph/office-supplies-ca-201.html",
        "method": "official API", "conf": "high",
    },
    "stap_on81": {
        "name": "Staples · Guelph S ON", "km": 68.0,
        "kind": "staples", "store_id": "81", "postal_code": "N1G 4Z1",
        "store_url": "https://stores.staples.ca/on/guelph/office-supplies-ca-81.html",
        "method": "official API", "conf": "high",
    },
    "stap_on59": {
        "name": "Staples · Ancaster ON", "km": 84.0,
        "kind": "staples", "store_id": "59", "postal_code": "L9K 1L6",
        "store_url": "https://stores.staples.ca/on/ancaster/office-supplies-ca-59.html",
        "method": "official API", "conf": "high",
    },
    "stap_on222": {
        "name": "Staples · Hamilton Mtn ON", "km": 95.0,
        "kind": "staples", "store_id": "222", "postal_code": "L9A 5C5",
        "store_url": "https://stores.staples.ca/on/hamilton/office-supplies-ca-222.html",
        "method": "official API", "conf": "high",
    },
    "stap_on456": {
        "name": "Staples · Hamilton E ON", "km": 97.0,
        "kind": "staples", "store_id": "456", "postal_code": "L9H 7K6",
        "store_url": "https://stores.staples.ca/on/hamilton/office-supplies-ca-456.html",
        "method": "official API", "conf": "high",
    },
    "wm_stav": {
        # Walmart Canada's inventory API returns a consistent 403 (Cloudflare
        # bot-blocked), confirmed 2026-09-05. check_walmart() is left in the
        # code in case that changes, but this store is "manual" until then.
        "name": "Walmart · Stavanger Dr", "km": 3.1,
        "kind": "manual",
        "method": "no feed · call to verify", "conf": "none",
    },
    "eb_aval": {
        "name": "EB Games · Avalon Mall", "km": 2.2,
        "kind": "manual",
        "method": "no feed · call to verify", "conf": "none",
    },
    "cost": {
        "name": "Costco · Blackmarsh Rd", "km": 4.6,
        "kind": "manual",
        "method": "no feed · call to verify", "conf": "none",
    },
}

# The PS5 Pro's identifiers at each chain. A retailer uses one item number and
# one product page nationally, so these are per-chain, not per-store.
BESTBUY_PS5 = "19492009"
STAPLES_PS5 = "3103551"
RTX5080_URL = "https://www.bestbuy.ca/en-ca/product/nvidia-geforce-rtx-5080-16gb-gddr7-video-card/18931347"
RTX5090_URL = "https://www.bestbuy.ca/en-ca/product/nvidia-geforce-rtx-5090-32gb-gddr7-video-card/18931348"
STAPLES_PS5_URL = "https://www.staples.ca/products/3103551-en-sony-playstation-5-pro-console"
BESTBUY_PS5_URL = "https://www.bestbuy.ca/en-ca/product/playstation-5-pro-console/18477929"

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
        "id": "gpu1", "cat": "GPU", "tag": "GRAPHICS CARD", "channel": "gpu",
        # this is the plain NVIDIA reference card at true MSRP - other AIB
        # models (MSI/ASUS/ZOTAC/PNY) sell for $2000-3000+ right now and
        # aren't worth chasing for resale margin the way this one is
        "product": "NVIDIA GeForce RTX 5080 16GB GDDR7", "sku": "18931347", "msrp": 1449.99,
        # Staples Canada only sells prebuilt systems, not standalone cards
        "skus": {k: "18931347" for k, v in STORES.items() if v["kind"] == "bestbuy"},
        "urls": {k: RTX5080_URL for k, v in STORES.items() if v["kind"] == "bestbuy"},
    },
    {
        "id": "gpu2", "cat": "GPU", "tag": "GRAPHICS CARD", "channel": "gpu",
        "product": "NVIDIA GeForce RTX 5090 32GB GDDR7", "sku": "18931348", "msrp": 2899.99,
        "skus": {k: "18931348" for k, v in STORES.items() if v["kind"] == "bestbuy"},
        "urls": {k: RTX5090_URL for k, v in STORES.items() if v["kind"] == "bestbuy"},
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


def check_bestbuy(sku, store_id):
    """Best Buy Canada availability. Returns (qty, price) or (None, None).

    NOTE: verify this endpoint before trusting it - Best Buy changes it
    occasionally. Open a product page, press F12, watch the Network tab and
    look for the request that carries 'availability' in its URL.
    """
    url = ("https://www.bestbuy.ca/ecomm-api/availability/products"
           f"?accept=application%2Fvnd.bestbuy.standardproduct.v1%2Bjson"
           f"&accept-language=en-CA&locations={store_id}&postalCode=A1A1A1&skus={sku}")
    data = _get_json(url)
    for av in data.get("availabilities", []):
        pickup = av.get("pickup", {})
        qty = pickup.get("quantityRemaining")
        purchasable = pickup.get("purchasable")
        price = (av.get("pricing") or {}).get("regularPrice")
        if qty is None:
            qty = 1 if purchasable else 0
        return int(qty), price
    return None, None


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


def notify(text, channel="ps5"):
    hook = webhook_for(channel)
    if not hook:
        print(f"  [no webhook for '{channel}']", text)
        return
    body = json.dumps({"content": text}).encode()
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
    lines = [f"**RESTOCK RADAR** · updated {now:%b %d, %H:%M}"]
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
        # anything in stock floats to the top, then nearest first
        rows.sort(key=lambda h: (h["status"] == "out", STORES[h["store"]]["km"]))
        for h in rows:
            store = STORES[h["store"]]
            icon = ICON.get(h["status"], "⬜")
            if h["status"] == "out":
                lines.append(f"{icon} {store['name']}")
            else:
                postal = store.get("postal_code", "")
                tail = f" · `{postal}`" if postal else ""
                lines.append(f"{icon} **{store['name']} — {h['qty']} in stock**{tail}")

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


def push_status(text, channel):
    """Edit that channel's status message, or post a new one and remember its id."""
    hook = webhook_for(channel)
    if not hook:
        return
    body = json.dumps({"content": text}).encode()
    headers = {**UA, "Content-Type": "application/json"}

    ids = _load_status_ids()
    msg_id = ids.get(channel, "")
    if msg_id:
        try:
            req = urllib.request.Request(
                f"{hook}/messages/{msg_id}", data=body,
                headers=headers, method="PATCH")
            urllib.request.urlopen(req, timeout=15)
            print(f"  [{channel}] status message updated")
            return
        except Exception as e:
            # message was probably deleted - fall through and post a fresh one
            print(f"  [{channel}] couldn't edit status message, posting a new one:", e)

    try:
        req = urllib.request.Request(hook + "?wait=true", data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            new_id = json.loads(r.read().decode()).get("id", "")
        if new_id:
            ids[channel] = new_id
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
                """The 'how do I act on this' lines shared by both alerts."""
                out = []
                if store.get("postal_code"):
                    # paste this into the retailer's "find a store" box to
                    # switch to this location - their store picker is
                    # session-based, so no link can do it for you
                    out.append(f"Set store with postal code: `{store['postal_code']}`")
                product_url = product.get("urls", {}).get(store_key)
                if product_url:
                    out.append(f"Buy: {product_url}")
                if store.get("store_url"):
                    out.append(f"Store (address & phone): {store['store_url']}")
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
                       product.get("channel", "ps5"))
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


if __name__ == "__main__":
    for _ch, _url in WEBHOOKS.items():
        if not _url:
            print(f"WARNING: no webhook for '{_ch}' - its alerts will fall back "
                  f"to the ps5 channel (set DISCORD_WEBHOOK_{_ch.upper()} or "
                  f"create webhook_{_ch}.txt)\n")

    load_previous_state()

    if "--once" in sys.argv:
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
