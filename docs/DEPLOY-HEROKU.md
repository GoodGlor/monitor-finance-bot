# Deploying to Heroku

Target: any Heroku app on the `heroku-24` stack (called `<your-app>` below).

There are two ways to run it, and neither needs a web dyno or an open port:

- **Worker dyno (recommended).** The `Procfile` starts `worker.py`, which long-polls
  Telegram so answers apply within seconds, and runs the mail pipeline, staleness check
  and digest every 10 minutes from inside the loop. Set `WORKER_ACTIVE=1` so a leftover
  Scheduler job stands down instead of competing for Telegram updates.
- **Scheduler only.** A one-off dyno runs `main.py` every 10 minutes. Cheaper, but an
  answer tapped in Telegram applies on the next run. The steps below set this up.

---

## Why it works on Heroku

Heroku's filesystem is ephemeral. Every scheduled run is a fresh dyno built from the
slug, and anything written to disk is discarded when it exits. Two things depended on a
durable disk:

- **`creds/`** is gitignored, so it is not in the slug at all. Both Google credential
  files now come from config vars and are written to disk at startup by
  `creds_bootstrap.py`.
- **`creds/telegram_state.json`** was rewritten on every run. It now lives in a hidden
  `_state` tab in the spreadsheet. Without this the `asked` ledger would be lost every
  run, the 12-hour cooldown could never apply, and **every pending row would be asked
  about again every 10 minutes**.

---

## One-time setup

### 1. Config vars

```bash
heroku config:set -a <your-app> \
  SPREADSHEET_ID="$(grep '^SPREADSHEET_ID=' .env | cut -d= -f2-)" \
  TELEGRAM_API="$(grep '^TELEGRAM_API=' .env | cut -d= -f2-)" \
  TZ="Europe/Kyiv"

heroku config:set -a <your-app> \
  GOOGLE_CREDENTIALS_JSON="$(cat creds/credentials.json)" \
  GOOGLE_TOKEN_JSON="$(cat creds/token.json)"
```

`TZ` is not cosmetic. Dynos run UTC, timestamps are rendered in local time, and a UTC
run would offset every new row by 3 hours from the rows already in the sheet.

### 2. Deploy

```bash
heroku git:remote -a <your-app>
git push heroku main
```

`.python-version` pins 3.14; the buildpack reads `uv.lock` directly.

### 3. Verify before scheduling

```bash
heroku run -a <your-app> python main.py
```

This is the real test. It should complete without prompting for a browser. If the token
didn't survive the copy you'll get a clear `RuntimeError` naming the fix rather than a
hang.

### 4. Schedule it

```bash
heroku addons:create scheduler:standard -a <your-app>   # free
heroku addons:open scheduler -a <your-app>
```

Add one job: `python main.py`, every **10 minutes** (Scheduler's minimum).

`OPENROUTER_API_KEY` is optional — without it the digest sends numbers only.

Free `:free` model variants respond **only when prompt logging is enabled** in
OpenRouter's privacy settings (openrouter.ai/settings/privacy). That is a condition of
their free tier and cannot be worked around in code. The digest sends aggregates and
merchant names, never transaction ids, dates, balances or transfer counterparties. The
free tier allows roughly 20 requests/minute with a daily cap; the digest makes at most
two requests a week.

Add a second job: `python digest.py`, **daily** at `06:00 UTC` (09:00 Kyiv in summer).
It runs every day and decides for itself whether today is a Monday or the 1st; on any
other day it exits after a single state read, so the cost is negligible.

It shares `TZ=Europe/Kyiv` with the pipeline — the weekday and day-of-month checks are
local-time. `TELEGRAM_CHAT_ID` is used if set; otherwise the chat id discovered by the
tagging loop is read from the `_state` tab. The digest keeps its own state in
`_state!A2`, separate from the tagging loop's `A1`, because the two jobs are different
dynos that can overlap.

---

## Cost

| Item | |
| --- | --- |
| Scheduler add-on | $0 |
| Dyno time | ~20s × 144 runs/day ≈ **24 dyno-hours/month** |
| Rate | Basic, $0.01/hour prorated to the second ≈ **$0.24/month** |

Scheduler offers **only the Basic dyno size** — there is no Eco option in the job editor,
so these runs bill per-second rather than drawing from the 1000-hour Eco pool. The pool
stays untouched and available for a worker dyno if the 10-minute latency ever becomes a
problem.

---

## Token lifetime

Publish the OAuth consent screen (**"In production"**), so
the refresh token does **not** expire on a timer and renews itself on every run. The
7-day expiry applies only to apps left in *Testing* status.

It can still be invalidated by a Google account password change, an explicit revoke, or
six months of total inactivity. Recovery is the same in every case: re-run the OAuth
flow on a machine with a browser, then

```bash
heroku config:set -a <your-app> \
  GOOGLE_TOKEN_JSON="$(python3 -c 'import json;print(json.dumps(json.load(open("creds/token.json"))))')"
```

You will not have to guess that this happened - see *Failure alerts*.

## Failure alerts

`TELEGRAM_CHAT_ID` is set so a run that dies can still reach you when Sheets is the
thing that broke (the chat id normally lives in the sheet). A failed run sends
`❌ Monitor Finance` with the exception, then re-raises so Heroku records the failure.

Alerts are throttled by the clock, not by stored state: one is sent only in the first 10
minutes of an hour. A failing run usually cannot record that it alerted, so a stateless
gate is the only reliable option. The effect is what an unattended job wants - a
persistent outage reports about once an hour instead of 144 times a day, and a transient
blip that clears within the hour stays silent.

---

## Operational notes

- **Run it on one machine only.** The Telegram `getUpdates` offset is consumed by
  whichever polls first, so two hosts would split your answers unpredictably. Duplicate
  rows can't happen — the `transaction_id` check prevents that regardless — but stop the
  local cron once Heroku is live.
- **Answers apply on the next run**, up to 10 minutes later. Telegram retains updates
  for 24 hours, so nothing is lost in between.
- **The `_state` tab is created automatically** on first write and is hidden. Don't edit
  it by hand; deleting it resets the chat id, offset, and the declined ledger.
- **State migrates itself once.** On the first run the `_state` cell is empty, so the
  store falls back to `creds/telegram_state.json` if present — which is how previously
  declined rows survive the move. Run it locally once before deploying, or set
  `GOOGLE_TOKEN_JSON` and let the first Heroku run start from defaults (you'd then need
  to `/start` the bot again and the declined rows would be re-asked once).
- **Logs:** `heroku logs -a <your-app> --tail`. A scheduled run that crashes writes to
  the log and disappears; Heroku will not alert you.

## Staleness alerts

A run that succeeds but finds no mail is indistinguishable from a healthy quiet period,
so a silently-broken feed (PrivatBank stopped sending, a Gmail filter started catching
the notifications) would otherwise never surface.

Once a day at **10:00 local**, the newest transaction date is read from the sheet. If it
is more than **48 hours** old, the bot sends `⚠️ Немає нових транзакцій уже N год.` The
check runs only in that daily window, so it costs one extra Sheets read per day rather
than one per run.

Dates are read as Sheets **serial numbers**, not formatted strings — the rendered form
follows the spreadsheet's locale (currently `M/D/YYYY`) and would misparse silently if
that changed. The maximum is taken across all rows, not the last row: rows are appended
in mail-arrival order and the live sheet already contains out-of-order pairs.

A failed read yields "unknown", which is treated as *not* stale — a Sheets hiccup must
not masquerade as a broken feed.

## Active hours

The pipeline does nothing outside **05:00–23:00 local** (`ACTIVE_START_HOUR` /
`ACTIVE_END_HOUR`, end exclusive, so the last working run starts at 22:50).

Scheduler cannot express a time window — it offers fixed intervals only — so **the dyno
still starts every 10 minutes all night** and exits immediately. This trims each
overnight run rather than eliminating it; the saving is a few seconds of dyno time per
run, not a third of the bill.

The real trade-off is latency: a transaction at 01:00 does not reach the sheet until
after 05:00, and a Telegram answer tapped overnight applies then too.

## Not covered

- **Nothing reconciles against real bank statements**, and the card is not stored, so a
  transaction that never produced an email cannot be noticed.
- **A partial feed is invisible.** Staleness only catches a feed that stops entirely; if
  one merchant's notifications stopped, everything would still look healthy.
