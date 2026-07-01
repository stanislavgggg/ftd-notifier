# Changelog — ftd-slack-notifier

## v1.0 — initial
Near-real-time per-brand FTD notifications from Voonix into Slack.

- Reuses statparser's Voonix login + Gmail-2FA, but adds a **persistent session**
  (saved `storage_state`) so it can poll every few minutes without re-triggering
  the email 2FA code. Re-auths only when Voonix expires the session.
- **Per-brand, per-site** detail read straight from the L1 (advertiser) CSV —
  `VoonixChannelDaily` is day-level only and can't drive a brand feed.
- New-FTD detection by **diffing the cumulative daily counter** per
  `(date, site, brand)`; keyed by date so midnight rollover is safe.
- Scrapes **today + yesterday** (`LOOKBACK_DAYS=2`) so late-settling FTDs (the
  old `LOOKBACK_DAYS=1` undercount cause) still fire.
- **Silent baseline** on boot — a restart never replays the day as notifications.
- Columns matched by **name**, not index, so Voonix's column picker can't break it.
- EU/US number parsing borrowed from the AI-agent voonix router (no EUR mangling).
- Slack via **Incoming Webhook** (MVP) or **bot token** (for future @mentions).
- Optional **BigQuery mirror** to `FtdEvents` for the future buyer leaderboard.
- `DRY_RUN` / `RUN_ONCE` for safe testing before going live.

## v1.1 — slash commands + auto content
The service is now a long-running worker **and** a small web server in one
process (background poll thread + FastAPI).

- **Slash commands** `/ftd`, `/ftd today|yesterday|week|month|30d`,
  `/ftd sources [period]`, `/ftd brands [period]`, `/ftd help`. They read from a
  local **SQLite store** the poller keeps current, so they answer inside Slack's
  3-second deadline without touching Voonix live.
- Slack requests are verified with the app **Signing Secret** (HMAC over the raw
  body + 5-min replay window); unsigned/forged/stale requests get 401.
- **Daily recap** auto-posted at `DAILY_SUMMARY_HOUR_UTC` (by source + brand of
  the day) and one-off **record** pings when a source beats its all-time best
  single-day FTD count (`ENABLE_RECORDS`).
- **SQLite store** (`/data/ftd.db`): one row per (date, site, brand), upserted
  every cycle — idempotent, powers all period/source/brand aggregation.
- **One-time backfill** (`BACKFILL_DAYS`) seeds history so week/month commands
  aren't empty right after launch.
- `/health` + `/` endpoints for Railway.

## v1.2 — fix missing pings + clean output + drilldowns
- **FIX (no notifications):** the poller now busts Voonix's server-side report
  cache for today/yesterday before reading (`BUST_VOONIX_CACHE`, default on).
  A frozen cache meant every poll saw identical numbers, so no FTD rise was ever
  detected. Cache age is now logged every cycle, and a per-cycle "today FTD by
  source" line makes movement (or lack of it) visible in the deploy logs.
- **FIX (restart swallowed FTDs):** detection thresholds are seeded from the
  SQLite store on startup, so a redeploy resumes detection (and catches up on
  rises during downtime) instead of silently re-baselining the day.
- **Cleaner output:** tracker leaderboard/search drop 0-FTD/0-signup noise;
  long campaign/brand names truncated; overview shows € deposit again.
- **New drilldown commands:** `/ftd brand <name>` (one advertiser split by
  source + its trackers), `/ftd source <LABEL>` (brands + trackers under one
  source), `/ftd conv [period]` (signup→FTD conversion by source).
- Tracker rows now store their **brand** (advertiser), enabling the per-brand
  tracker view. Historical tracker rows get brands as they're re-scraped.

## v1.3 — full lists
- `/ftd trackers [period] all` (and `/ftd brands … all`) list EVERY tracker/brand
  with activity (FTD or signups) for the period, not just the top N. Add a number
  instead of `all` for a custom cap, e.g. `/ftd trackers month 100`.
- Long lists are split across multiple Slack blocks (each under the 3000-char
  limit); header shows how many have activity; overflow beyond ~12 blocks is
  noted so nothing silently disappears.
- Brand/tracker leaderboards now include signup-only rows (activity = FTD OR
  signups); pure 0/0 rows stay excluded.

## v1.4 — reconciliation + settling convergence
- `/ftd trackers` now shows a reconciliation footer: "Tracked X of Y FTD · … the
  rest is traffic with no tracker tag" — the L3-vs-site gap is expected (untagged
  traffic can't be attributed to a campaign) and is now explicit, not "missing".
- `RESETTLE_DAYS`/`RESETTLE_HOURS` (opt-in): periodically re-scrape recent days
  and refresh the store WITHOUT firing pings, so month totals converge with a
  fresh Voonix query as late data settles (frozen backfill days get updated).

## v1.5 — readability
- Command output redesigned for scannability: removed medals/numbering and
  decorative emoji, each row is a bold name + metrics on its own line with a
  blank line between rows, and counts use thousands separators. Sections stay in
  separate Slack blocks. `all` lists chunk the same way.

## v1.6 — compact layout
- Removed blank lines between rows (one-line rows, tight). Section titles now
  carry an emoji anchor (🌍 By source, 🏆 Top brands, 🎯 Top trackers) so they
  stand out from the bold row names instead of blending in. Sections remain
  separate Slack blocks for group spacing.

## v1.7 — header wording
- Overview/drilldown section titles renamed "Top brands"→"Brands",
  "Top trackers"→"Trackers" (consistent with the source drilldown). The
  `/ftd trackers` command header still shows Top/All based on the limit.

## v1.8 — drop reconciliation footer
- Removed the "Tracked X of Y … no tracker tag" line from /ftd trackers. It was
  misleading when tracker data for the period hadn't been refreshed yet (e.g.
  today, since trackers refresh every ~6h) — showing "0 of N" as if data were
  lost. The command now just lists the trackers it has.

## v1.9 — interactive panel (buttons + dropdowns)
- Every response now carries a control panel: period buttons (Today/Yesterday/
  7 days/Month), view buttons (Overview/Sources/Brands/Trackers), and Source/
  Brand/Tracker dropdowns so you pick a name from a list instead of typing it.
  Clicks re-render the message in place.
- `/slack/interactions` implemented (signature-verified) to route button/dropdown
  actions; requires enabling Interactivity in the Slack app (Request URL
  = https://<app>.up.railway.app/slack/interactions).

## v2.0 — max convenience panel
- `/ftd menu`: a compact, pinnable control panel.
- Panel now includes a 🔄 Refresh button, a "More periods" dropdown (this week,
  last 30 days, this month, last month, this year), and an "Open in Voonix ↗"
  deep link to the raw report for the current period.
- Navigation preserves the real current period (incl. last month / this year)
  across view switches and entity picks — buttons/dropdowns re-render in place.
- parse_period gained "last month" and "this year"/"ytd".

## v2.1 — App Home, sort, /ftd now
- App Home tab: opening the bot's Home publishes a live dashboard (today's
  overview + full panel). /slack/events added (URL-verification + app_home_opened);
  panel clicks in the Home tab refresh via views.publish. Requires SLACK_BOT_TOKEN
  + enabling Home Tab and Event Subscriptions in the Slack app.
- Sort dropdown on the panel (FTD / Signups / Deposits) re-sorts the current
  leaderboard/overview; store leaderboards gained an order_by param.
- /ftd now [minutes]: new FTDs in the last hour (default 60), by source + brand.
  Backed by a new ftd_events journal the notifier writes on each detected rise.

## v2.2 — access control
- ALLOWED_USERS env var (comma/space-separated Slack user IDs) gates commands,
  button/dropdown interactions, and the App Home dashboard. Empty = everyone
  (default). Denied users get a polite "no access" message / Home view.

## v2.3 — allowlist by email
- ALLOWED_EMAILS env var (comma/space-separated) gates access by Slack account
  email, resolved via users.info (cached). Requires bot token + users:read.email.
  Case-insensitive; email checks fail closed if unresolved. ALLOWED_USERS (IDs)
  still works and both can be combined.
