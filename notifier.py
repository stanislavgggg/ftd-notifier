"""
FTD → Slack notifier — main worker.

Loop:
  1. Scrape per-brand FTD counts for every configured site (today + yesterday).
  2. Compare each (date, site, brand) count to what we saw last cycle.
  3. For any increase, post a FOMO message to Slack (and mirror to BigQuery).
  4. Sleep, repeat.

Counter, not events:
  Voonix reports a *cumulative daily* FTD count per brand. A rise from N to N+k
  means k new FTDs since the last poll. Keying state by (date, site, brand)
  makes midnight rollover safe (a new day starts at its own 0) and lets a late
  FTD on yesterday still fire (settle-lag).

First cycle = silent baseline:
  On boot we record current counts WITHOUT notifying, so a restart never
  replays the whole day. Only rises observed *after* the baseline ping Slack.
"""
import time
from datetime import datetime, timezone

import config
import slack
import voonix_client

try:
    import bq_mirror
except Exception:  # google libs optional unless BQ_MIRROR=true
    bq_mirror = None

# (date, site_id, brand) -> {"ftd": int, "deposit_value": float}
_seen: dict[tuple[str, str, str], dict] = {}
_baselined = False


def _key(r: dict) -> tuple[str, str, str]:
    return (r["date"], r["site_id"], r["brand"])


def _day_totals(rows: list[dict], date: str, site_id: str) -> tuple[int, float]:
    ftd = sum(r["ftd"] for r in rows if r["date"] == date and r["site_id"] == site_id)
    dep = sum(r["deposit_value"] for r in rows if r["date"] == date and r["site_id"] == site_id)
    return ftd, dep


def process(rows: list[dict]):
    global _baselined
    if not _baselined:
        for r in rows:
            _seen[_key(r)] = {"ftd": r["ftd"], "deposit_value": r["deposit_value"]}
        _baselined = True
        watching = sum(1 for r in rows if r["ftd"])
        print(f"📌 Baseline set: {len(rows)} brand-days ({watching} with FTDs). "
              f"Notifications start from the next rise.")
        return

    notifications = 0
    for r in rows:
        k = _key(r)
        prev = _seen.get(k)
        prev_ftd = prev["ftd"] if prev else 0
        prev_dep = prev["deposit_value"] if prev else 0.0

        if r["ftd"] > prev_ftd:
            ftd_delta = r["ftd"] - prev_ftd
            deposit_delta = max(0.0, r["deposit_value"] - prev_dep)
            day_ftd, day_dep = _day_totals(rows, r["date"], r["site_id"])

            ev = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "date": r["date"],
                "site_id": r["site_id"],
                "site_label": r["site_label"],
                "brand": r["brand"],
                "ftd_delta": ftd_delta,
                "deposit_delta": round(deposit_delta, 2),
                "day_ftd": day_ftd,
                "day_deposit": round(day_dep, 2),
            }

            # Optional noise gate: skip €0 deposit FTDs if a floor is set.
            if config.MIN_DEPOSIT_EUR > 0 and deposit_delta < config.MIN_DEPOSIT_EUR:
                pass
            else:
                if slack.post(ev):
                    notifications += 1
                if bq_mirror:
                    bq_mirror.record(ev)

        # Always advance state (even on a downward correction) so the next real
        # rise is measured from the correct base.
        _seen[k] = {"ftd": r["ftd"], "deposit_value": r["deposit_value"]}

    print(f"   ↳ cycle done: {len(rows)} brand-days scanned, {notifications} notification(s) sent")


def one_cycle():
    import asyncio
    hour = datetime.now(timezone.utc).hour
    if not config.in_active_window(hour):
        print(f"😴 Outside active window (UTC hour {hour}) — skipping scrape")
        return
    print(f"🔎 Polling {len(config.SITES)} site(s) at {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    rows = asyncio.run(voonix_client.scrape_once())
    process(rows)


def main():
    print("=" * 60)
    print("FTD → Slack notifier starting")
    print(f"  sites:    {config.SITES}")
    print(f"  interval: {config.POLL_INTERVAL_SECONDS}s   lookback: {config.LOOKBACK_DAYS}d")
    print(f"  slack:    {'webhook' if config.SLACK_WEBHOOK_URL else ('bot-token' if config.SLACK_BOT_TOKEN else 'NONE')}"
          f"{'  (DRY_RUN)' if config.DRY_RUN else ''}")
    print(f"  bq mirror:{'on' if (config.BQ_MIRROR and bq_mirror) else 'off'}")
    print("=" * 60)

    if config.RUN_ONCE:
        one_cycle()
        return

    while True:
        try:
            one_cycle()
        except Exception as e:
            print(f"❌ Cycle error (will retry next interval): {e}")
        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
