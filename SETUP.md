# Restock Radar

Watches Canadian retailers for stock on specific products and messages Discord
the moment something appears. Runs itself on GitHub Actions every 10 minutes —
no PC required.

Built for St. John's NL and the Woodstock ON area, but the store list is just
config.

## What it actually checks

| Retailer | Works? | How |
|---|---|---|
| **Staples** | Yes | Open inventory API, no auth. One call returns every store near a postal code, so 22 stores cost ~7 requests. Most reliable source here. |
| **Best Buy** | Yes | Open availability API, no auth. Store IDs can't be discovered from a postal code — they have to be captured from the site (see below). |
| **Walmart** | No | Hard-blocked. Returns 403 through plain requests, session cookies, headless Chromium, and stealth-patched Chromium alike. Left in the code in case that changes; marked `manual` for now. |
| **EB Games** | No | Sells the PS5 Pro but has no public feed and 403s. |
| **Costco** | No | Publishes no per-warehouse stock anywhere. Their console bundles are online-only regardless. |

Stores marked `manual` are never checked — they're listed in the Discord
summary as a reminder to phone them.

## Setup

### Secrets

Webhooks never go in the code. They're read from, in order:

1. Environment variable — `DISCORD_WEBHOOK_URL`, and optionally `DISCORD_WEBHOOK_PS5_STATUS`
2. A local gitignored file — `webhook.txt`, `webhook_ps5_status.txt`

On GitHub they're repository secrets (Settings → Secrets and variables →
Actions). Locally, create the files — they're in `.gitignore` and must stay
there. **Do not paste a webhook into `checker.py`**; this repo is public and it
would be in the history permanently.

### Running it

```
python checker.py           # loop forever, serve stock.json on :8000
python checker.py --once    # single sweep - what GitHub Actions runs
```

GitHub Actions handles the real schedule (`.github/workflows/check.yml`).
Each run does one sweep and commits `stock.json` and `status_message.json`
back, which is how state survives between runs — without it, every run would
look like a first run and re-alert on everything already in stock.

## How Discord is used

Each product has a `channel`, and each channel maps to its own webhook:

- **One pinned status message per channel**, rewritten in place each sweep.
  Editing a message sends no notification, so it stays silently current. Its id
  lives in `status_message.json`. If you delete the message, the next run posts
  a fresh one — re-pin it.
- **Optionally, a status-only channel.** Set `DISCORD_WEBHOOK_PS5_STATUS` to a
  webhook for a second channel (e.g. `#ps5-status`) and the status message goes
  there instead, so it's the only thing in that channel rather than buried under
  alerts. Alerts and pings stay where they are.
- **New messages for events**, which do notify: something coming in stock, and
  quantity changes while in stock (3 → 1), and selling out (no ping).

Alerts include the product link, that store's own page (address and phone), and
the store's postal code — you need the postal code because retailer store
pickers are session-cookie based, so no link can preselect a store for you.
Paste it into their "find a store" box.

## Adding things

**A Staples store** — add it to `STORES` with `kind: "staples"`, its store
number, and its own postal code. Everything else wires itself up: the product
SKU and URL maps are built from `STORES`, since one item number covers a whole
chain. Find store numbers by querying the API with a postal code near them.

**A Best Buy store** — same, but the store id must come from the site. Open a
product page, put a postal code in the Pick Up box, F12 → Network → filter
`availability`, click Check, and read `locationKey` values out of the response.
One capture usually returns several nearby stores.

**A product** — copy a block in `PRODUCTS`. Best Buy SKUs are the number at the
end of the product URL; Staples item numbers are on the product page.

## Known limits

- **Discord caps messages at 2000 characters.** The PS5 Pro summary is ~980 with
  30 stores. Another large product would exceed it and the status update would
  fail silently. Needs splitting before the list grows much further.
- **Scheduled runs drift.** GitHub queues them under load, so 10 minutes is
  really 10-15.
- **Watch for `check failed (HTTP Error 403)`** in the Actions logs. That means
  a retailer is rate-limiting — raise the cron interval.
- **GitHub disables scheduled workflows after 60 days of repo inactivity.** If
  checks go quiet, that's the first thing to check.
- Expect the retailer endpoints to change shape eventually. When one breaks, the
  failure is printed rather than thrown, so the rest keeps working.
