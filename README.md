# FTD → Slack notifier (the "money channel")

Watches Voonix in near-real-time and drops a FOMO ping into Slack every time a
brand takes a **new first-time deposit**:

> 💰 **NEW FTD — META**
> 🎰 **iWildCasino**  +2 FTDs  ·  €550 deposited
> 📊 META today: **5 FTD** · €1,400 deposits

It reuses the same Voonix login + Gmail-2FA approach as `statparser`, but keeps
the session warm so it can poll every few minutes without re-triggering 2FA.

---

## How it works

1. **Scrape** the per-advertiser (brand) earnings for each configured site,
   for today **and** yesterday (so late-settling FTDs still fire).
2. **Diff** each `(date, site, brand)` FTD count against the previous cycle.
   Voonix gives a *cumulative daily* count, so a rise = new FTDs.
3. **Notify** Slack for every rise; optionally mirror the event to BigQuery.
4. **Sleep** `POLL_INTERVAL_SECONDS`, repeat.

The first cycle after a (re)start is a **silent baseline** — it records current
counts without notifying, so a redeploy never replays the day.

---

## Setup

### 1. Create the Slack channel + webhook
1. In Slack, create the channel, e.g. `#money`.
2. Go to <https://api.slack.com/apps> → **Create New App** → *From scratch*.
3. **Incoming Webhooks** → toggle **On** → **Add New Webhook to Workspace** →
   pick `#money`.
4. Copy the webhook URL (`https://hooks.slack.com/services/...`) → that's
   `SLACK_WEBHOOK_URL`.

> Later, when you want to @-mention buyers by name, switch to a **bot token**
> (`SLACK_BOT_TOKEN` + `SLACK_CHANNEL`, scope `chat:write`) and fill `BUYER_MAP`.

### 2. Env vars (Railway → Variables)
See `.env.example`. Minimum to go live:
`VOONIX_USER`, `VOONIX_PASS`, `GMAIL_USER`, `GMAIL_APP_PASS`, `SLACK_WEBHOOK_URL`.
Reuse the exact same Voonix/Gmail values the `statparser` service already uses.

### 3. Add a Railway volume (recommended)
Mount a volume at **`/data`** so the logged-in Voonix session survives restarts
and you don't get a 2FA email on every redeploy. Without it, it still works —
it just re-authenticates on each cold start.

### 4. Deploy
Push to GitHub and connect the repo in Railway (Dockerfile build), or
`railway up`. It's a long-running worker (`restartPolicyType = "ALWAYS"`).

---

## Test before going live
Set `DRY_RUN=true` and `RUN_ONCE=true`, deploy (or run locally). It logs into
Voonix once, prints the FTD rows it sees, and prints the Slack messages it
*would* send — without posting. Flip both back to `false` when happy.

```bash
pip install -r requirements.txt && playwright install chromium
export VOONIX_USER=... VOONIX_PASS=... GMAIL_USER=... GMAIL_APP_PASS=...
export SITES="82:MAIL,29:META" DRY_RUN=true RUN_ONCE=true
python notifier.py
```

---

## Tuning
| Var | Default | Notes |
|-----|---------|-------|
| `SITES` | `82:MAIL,29:META` | `id:LABEL` pairs. 82 = MAIL, 29 = META. |
| `POLL_INTERVAL_SECONDS` | `900` | 15 min. Lower = snappier, more Voonix load. |
| `LOOKBACK_DAYS` | `2` | today + yesterday; catches late FTDs. |
| `ACTIVE_HOURS_UTC` | _(off)_ | e.g. `6-23` to pause overnight. |
| `MIN_DEPOSIT_EUR` | `0` | skip FTDs below this deposit value. |

---

## The buyer-names step (later)
Right now Voonix gives us **site → brand**, no buyer split. When you add a
sub-id / campaign that identifies the buyer, two things make the names appear:

1. Turn on the L3/campaign scrape and key events by buyer (the event model
   already carries an optional `buyer` field).
2. Fill `BUYER_MAP`, e.g.
   `{"iWildCasino":{"name":"Marija","slack_id":"U0123ABC"}}` — the ping becomes
   `… +2 FTDs · €550 · by @Marija`.

Set `BQ_MIRROR=true` now (with the same service-account JSON the dashboard uses)
and every FTD is logged to `FtdEvents` — that's the table the leaderboard will
read once buyer attribution lands.
