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
