"""
FTD → Slack notifier — main process.

Runs two things in one service:
  1. A background poll loop that scrapes Voonix, posts new-FTD pings, keeps the
     SQLite store current, and posts the daily recap / record pings.
  2. A FastAPI server (server.py) that answers Slack slash commands from that
     same store.

Counter, not events:
  Voonix reports a *cumulative daily* FTD count per brand. A rise from N to N+k
  means k new FTDs since the last poll. State is keyed by (date, site, brand) so
  midnight rollover is safe and a late FTD on yesterday still fires.

First cycle = silent baseline:
  On boot we record current counts WITHOUT notifying, so a restart never replays
  the day. Only rises observed *after* the baseline ping Slack. (The store is
  still updated on the baseline cycle, so commands have data immediately.)
"""
import threading
import time
from datetime import datetime, timezone

import config
import slack
import store
import summary
import voonix_client

try:
    import bq_mirror
except Exception:
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

    # Always keep the store current so /ftd commands work from cycle one.
    store.upsert_rows(rows)

    now = datetime.now(timezone.utc)

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
                "ts": now.isoformat(),
                "date": r["date"],
                "site_id": r["site_id"],
                "site_label": r["site_label"],
                "brand": r["brand"],
                "ftd_delta": ftd_delta,
                "deposit_delta": round(deposit_delta, 2),
                "day_ftd": day_ftd,
                "day_deposit": round(day_dep, 2),
            }

            if config.MIN_DEPOSIT_EUR > 0 and deposit_delta < config.MIN_DEPOSIT_EUR:
                pass
            else:
                if slack.post(ev):
                    notifications += 1
                if bq_mirror:
                    bq_mirror.record(ev)

        _seen[k] = {"ftd": r["ftd"], "deposit_value": r["deposit_value"]}

    # FOMO extras (both no-op unless enabled / past the configured hour).
    summary.check_records(rows, now)
    summary.maybe_post_daily_summary(now)
    summary.maybe_post_morning_report(now)

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


def backfill():
    """One-time history seed so week/month commands aren't empty at launch."""
    if config.BACKFILL_DAYS <= 0:
        return
    import asyncio
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    target_oldest = (today - timedelta(days=config.BACKFILL_DAYS)).isoformat()
    have = store.earliest_date()
    if have and have <= target_oldest:
        print(f"↩️  Backfill skipped (store already has data back to {have})")
        return
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(config.BACKFILL_DAYS)]
    print(f"⏳ Backfilling {len(dates)} days into the store ({dates[-1]} … {dates[0]})...")
    try:
        rows = asyncio.run(voonix_client.scrape_once(dates=dates))
        store.upsert_rows(rows)
        print(f"✅ Backfill done: {len(rows)} brand-days stored")
    except Exception as e:
        print(f"⚠️ Backfill failed (non-fatal): {e}")


def poll_loop():
    backfill()
    while True:
        try:
            one_cycle()
        except Exception as e:
            print(f"❌ Cycle error (will retry next interval): {e}")
        if config.RUN_ONCE:
            return
        time.sleep(config.POLL_INTERVAL_SECONDS)


def main():
    print("=" * 60)
    print("FTD → Slack notifier starting")
    print(f"  sites:    {config.SITES}")
    print(f"  interval: {config.POLL_INTERVAL_SECONDS}s   lookback: {config.LOOKBACK_DAYS}d")
    print(f"  slack:    {'webhook' if config.SLACK_WEBHOOK_URL else ('bot-token' if config.SLACK_BOT_TOKEN else 'NONE')}"
          f"{'  (DRY_RUN)' if config.DRY_RUN else ''}")
    print(f"  commands: {'ON (signing secret set)' if config.SLACK_SIGNING_SECRET else 'off (no signing secret)'}")
    print(f"  daily recap: {'hour '+str(config.DAILY_SUMMARY_HOUR_UTC)+' UTC' if config.DAILY_SUMMARY_HOUR_UTC>=0 else 'off'}"
          f"   records: {'on' if config.ENABLE_RECORDS else 'off'}")
    print(f"  morning report (yesterday): {'hour '+str(config.MORNING_REPORT_HOUR_UTC)+' UTC' if config.MORNING_REPORT_HOUR_UTC>=0 else 'off'}")
    print(f"  bq mirror:{'on' if (config.BQ_MIRROR and bq_mirror) else 'off'}")
    print("=" * 60)

    if config.RUN_ONCE:
        poll_loop()
        return

    # Poll in the background; serve slash commands in the foreground.
    threading.Thread(target=poll_loop, daemon=True).start()

    import uvicorn
    from server import app
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
