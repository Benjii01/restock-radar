# Setup — plain version

## Is this actually possible?

Partly. Being honest about each store, because it changes what you should expect:

| Store | Can a script check it? | Reality |
|---|---|---|
| Best Buy | Yes | Public API. Reliable, near real-time. This is your best source, and it covers the PS5 Pro. |
| Walmart | Mostly | Their site exposes per-store stock. Works, but they block fast polling and change the page often, so it breaks every few months and needs fixing. |
| EB Games / GameStop | No | No feed. Their site shows "check store" but there's nothing a script can read reliably. |
| Costco | No | They don't publish warehouse stock anywhere. |
| Local card shops | No | Most post on Facebook or Instagram instead. |

So: consoles at Best Buy — genuinely solvable. Sealed Pokémon and One Piece at Walmart — solvable with maintenance. EB Games and Costco — you have to phone them, which is why the dashboard has a CONFIRM button per store.

One more honest point on speed. Even Best Buy's number updates every few minutes, and dedicated scalper tools run hundreds of proxies. You won't win a pure speed race against those. What you can win: Best Buy pickup slots at 6am, and the stores bots ignore entirely because there's no feed to hit.

---

## What you do

### 1. Install Python

Download from python.org. During install, tick **"Add Python to PATH"**. That checkbox matters.

Then in VS Code, open the Extensions panel (the four-squares icon in the left bar) and install the **Python** extension by Microsoft. Restart VS Code afterwards.

Download this project (the download button in the chat) and unzip it somewhere you'll remember. In VS Code: **File → Open Folder**, pick that folder. You should see `checker.py` and `SETUP.md` in the sidebar.

### 2. Set up Discord (this is the part that alerts your phone)

Make sure you have the Discord app installed on your phone and notifications turned on for it.

1. In Discord, go to any server you're in (or make a new one — right-click the server list, **Add a Server**), open a text channel
2. Click the gear icon next to the channel name (**Edit Channel**) → **Integrations** → **Webhooks** → **New Webhook**
3. Click it, then **Copy Webhook URL** — it looks like `https://discord.com/api/webhooks/12345.../abcDEF...`
4. Open `checker.py` and paste it into the top:

```python
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/12345.../abcDEF..."
```

### 3. Find your store IDs and product SKUs

This is the fiddly part, and it's unavoidable — every store numbers its own shelves.

**Best Buy:** open the PS5 Pro page on bestbuy.ca, set your store to Stavanger Drive. The SKU is in the URL (the long number at the end). For the store ID, press F12, click the Network tab, reload, and look for a request with `availability` in it — your store ID is in that URL.

**Walmart:** same idea on walmart.ca. The product page URL ends in the item number.

Put those into the `STORES` and `PRODUCTS` blocks in `checker.py`, replacing every line marked `TODO`.

### 4. Run it

Open `checker.py` in VS Code and press the **▷ play button** in the top-right corner. A terminal panel opens at the bottom and starts printing.

Or use the terminal directly — **Terminal → New Terminal**, then:

```
python checker.py
```

Leave it running. It checks every two minutes, prints what it found, and pushes to your phone when something appears. `Ctrl+C` in the terminal stops it.

You should see something like:

```
serving http://localhost:8000/stock.json
checking every 120 seconds - leave this window open

[09:14:02] 3 rows written
```

If instead you get `ModuleNotFoundError` or `python is not recognized`, Python either isn't installed or wasn't added to PATH — reinstall and tick that box.

### 5. Connect the dashboard

Open the dashboard, find the **feedUrl** setting, and paste:

```
http://localhost:8000/stock.json
```

The badge at the top should flip from **DEMO DATA** to **LIVE**.

---

## Expect to debug step 3

The store IDs and SKUs are where this will go wrong first, and the error messages from the script are your guide. If a check fails it prints the reason rather than crashing.

If a retailer's endpoint has changed shape since this was written, paste `checker.py` and the error into a Claude chat and ask it to fix that one function. The rest of the setup stays put.

## If you want it running while you sleep

Right now it only checks while your PC is on and the window is open. Two upgrades, in order of effort:

**Windows Task Scheduler** — start the script automatically at login. Free, still needs the PC awake.

**A $5/month cloud box** (Hetzner, DigitalOcean) — runs 24/7. Restocks often land overnight, so this is the single biggest improvement you can make after step 4 works.
