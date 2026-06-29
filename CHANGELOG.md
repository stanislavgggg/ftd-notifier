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
