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


def _seed_from_store():
    """Load the last-known FTD per (date, site, brand) for the lookback window
    from SQLite into the diff thresholds. This means a redeploy RESUMES detection
    (and catches up on any rise that happened while we were down) instead of
    silently re-baselining and swallowing the day's FTDs — the main reason pings
    went missing during active development."""
    global _baselined
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(max(1, config.LOOKBACK_DAYS))]
    seeded = 0
    for r in store.brand_state(dates):
        _seen[(r["date"], r["site_id"], r["brand"])] = {
            "ftd": r["ftd"], "deposit_value": r["deposit_value"]}
        seeded += 1
    if seeded:
        _baselined = True
        print(f"🌱 Seeded {seeded} thresholds from store — resuming detection (no silent baseline).")
    else:
        print("🌱 Store empty for the window — first cycle will be a silent baseline.")

# How many days each resumable backfill chunk scrapes before saving progress.
BACKFILL_CHUNK = 15
# Tracker scraping is ~150 requests/day per site, so save progress more often.
TRACKER_BACKFILL_CHUNK = 3
_last_tracker_scrape = 0.0  # epoch seconds of last tracker refresh


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
                # Journal the event so /ftd now can show the last hour.
                try:
                    store.record_event(ev["ts"], ev["site_id"], ev["site_label"],
                                       ev["brand"], ev["ftd_delta"], ev["deposit_delta"])
                except Exception as e:
                    print(f"   ⚠️ event journal failed: {e}")

        _seen[k] = {"ftd": r["ftd"], "deposit_value": r["deposit_value"]}

    # FOMO extras (both no-op unless enabled / past the configured hour).
    summary.check_records(rows, now)
    summary.maybe_post_daily_summary(now)
    summary.maybe_post_morning_report(now)

    # Movement diagnostic: today's scanned FTD per source. If these numbers never
    # rise between cycles, the polled data is frozen (Voonix cache) rather than a
    # diff bug — this line makes that visible in the deploy logs.
    today = now.date().isoformat()
    by_src: dict[str, int] = {}
    for r in rows:
        if r["date"] == today:
            by_src[r["site_label"]] = by_src.get(r["site_label"], 0) + r["ftd"]
    movement = " ".join(f"{k}={v}" for k, v in sorted(by_src.items())) or "—"
    print(f"   ↳ cycle done: {len(rows)} brand-days, {notifications} ping(s) | today FTD: {movement}")


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
    """Seed history so month/week commands aren't empty. Resumable: it only
    scrapes days NOT already in the store, in chunks, saving after each chunk —
    so a restart mid-way continues instead of starting over, and a deep (e.g.
    365-day) backfill survives interruptions."""
    if config.BACKFILL_DAYS <= 0:
        return
    import asyncio
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    wanted = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
              for i in range(config.BACKFILL_DAYS)]            # newest first
    have = store.existing_dates()
    todo = [d for d in wanted if d not in have]
    if not todo:
        print(f"↩️  Backfill skipped (all {len(wanted)} days already in store)")
        return
    print(f"⏳ Backfilling {len(todo)} missing day(s) of {len(wanted)} "
          f"({wanted[-1]} … {wanted[0]}) in chunks of {BACKFILL_CHUNK}...")
    done = 0
    for i in range(0, len(todo), BACKFILL_CHUNK):
        chunk = todo[i:i + BACKFILL_CHUNK]
        try:
            rows = asyncio.run(voonix_client.scrape_once(dates=chunk))
            store.upsert_rows(rows)
            done += len(chunk)
            print(f"   ✅ Backfill progress {done}/{len(todo)} days "
                  f"({chunk[-1]} … {chunk[0]}) — {len(rows)} brand-days saved")
        except Exception as e:
            print(f"   ⚠️ Backfill chunk failed ({chunk[-1]} … {chunk[0]}): {e} — continuing")
    print(f"✅ Backfill done: {done}/{len(todo)} missing days filled")


def tracker_backfill():
    """One-time deep history for campaign-level trackers. Resumable PER SITE:
    each tracker-site's missing days are computed independently, so adding a new
    site later fills its history even though MAIL's dates are already present.
    Off when TRACKER_SITES or TRACKER_BACKFILL_DAYS is empty/0."""
    if not config.TRACKER_SITES or config.TRACKER_BACKFILL_DAYS <= 0:
        return
    import asyncio
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    wanted = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
              for i in range(config.TRACKER_BACKFILL_DAYS)]
    for site in config.TRACKER_SITES:                     # (site_id, site_label)
        site_id, label = site
        have = store.tracker_existing_dates(site_id=site_id)
        todo = [d for d in wanted if d not in have]
        if not todo:
            print(f"↩️  Tracker backfill {label}: all {len(wanted)} days present — skip")
            continue
        print(f"⏳ Tracker backfill {label}: {len(todo)}/{len(wanted)} missing day(s) "
              f"({todo[-1]} … {todo[0]}), chunks of {TRACKER_BACKFILL_CHUNK}. Slow (~min/day).")
        done = 0
        for i in range(0, len(todo), TRACKER_BACKFILL_CHUNK):
            chunk = todo[i:i + TRACKER_BACKFILL_CHUNK]
            try:
                rows = asyncio.run(voonix_client.scrape_trackers_once(dates=chunk, sites=[site]))
                store.upsert_tracker_rows(rows)
                done += len(chunk)
                print(f"   ✅ {label} {done}/{len(todo)} days "
                      f"({chunk[-1]} … {chunk[0]}) — {len(rows)} campaign-days saved")
            except Exception as e:
                print(f"   ⚠️ {label} chunk failed ({chunk[-1]} … {chunk[0]}): {e} — continuing")
        print(f"✅ Tracker backfill {label} done: {done}/{len(todo)} days filled")


def maybe_refresh_trackers():
    """Refresh today's + yesterday's trackers, but at most every
    TRACKER_REFRESH_HOURS — never on the 5-minute poll cadence (too heavy)."""
    global _last_tracker_scrape
    if not config.TRACKER_SITES:
        return
    now = time.time()
    if now - _last_tracker_scrape < config.TRACKER_REFRESH_HOURS * 3600:
        return
    import asyncio
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in (1, 0)]
    print(f"🔁 Tracker refresh for {days} ({[s[1] for s in config.TRACKER_SITES]})")
    try:
        rows = asyncio.run(voonix_client.scrape_trackers_once(dates=days))
        store.upsert_tracker_rows(rows)
        _last_tracker_scrape = now
        print(f"   ✅ Tracker refresh saved {len(rows)} campaign-days")
    except Exception as e:
        print(f"   ⚠️ Tracker refresh failed: {e}")


_last_resettle = 0.0


def maybe_resettle():
    """Every RESETTLE_HOURS, re-scrape the last RESETTLE_DAYS and refresh the
    store WITHOUT diffing/notifying — so frozen backfill days pick up Voonix's
    later revisions and month totals converge. Off when RESETTLE_DAYS<=0."""
    global _last_resettle
    if config.RESETTLE_DAYS <= 0:
        return
    now = time.time()
    if now - _last_resettle < config.RESETTLE_HOURS * 3600:
        return
    import asyncio
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(config.RESETTLE_DAYS)]
    print(f"♻️  Resettling {len(days)} days ({days[-1]} … {days[0]}) — store refresh only")
    try:
        rows = asyncio.run(voonix_client.scrape_once(dates=days))
        store.upsert_rows(rows)                 # refresh store; do NOT call process()
        _last_resettle = now
        print(f"   ✅ Resettle updated {len(rows)} brand-days")
    except Exception as e:
        print(f"   ⚠️ Resettle failed: {e}")


def poll_loop():
    _seed_from_store()
    backfill()
    while True:
        try:
            one_cycle()
            maybe_resettle()
        except Exception as e:
            print(f"❌ Cycle error (will retry next interval): {e}")
        if config.RUN_ONCE:
            return
        time.sleep(config.POLL_INTERVAL_SECONDS)


def tracker_loop():
    """Heavy campaign-level scraping on its OWN thread, so the slow deep
    backfill (hours) and periodic refresh (~minutes) never block the 5-minute
    brand poll or the live FTD pings. Deep backfill once, then slow refresh."""
    if not config.TRACKER_SITES:
        return
    global _last_tracker_scrape
    # On a cold start both threads may need to log in; let the brand poller grab
    # its 2FA code first so the two logins don't race for the same Gmail message.
    time.sleep(90)
    try:
        tracker_backfill()
        _last_tracker_scrape = time.time()  # backfill already covered recent days
    except Exception as e:
        print(f"❌ Tracker backfill error: {e}")
    while True:
        if config.RUN_ONCE:
            return
        time.sleep(300)  # re-check every 5 min; actual refresh gated by TRACKER_REFRESH_HOURS
        try:
            maybe_refresh_trackers()
        except Exception as e:
            print(f"❌ Tracker refresh error (will retry): {e}")


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
        tracker_loop()
        return

    # Brand polling + live pings and the heavy tracker scrape run on SEPARATE
    # threads so the slow tracker drilldown can never starve the FTD notifier.
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=tracker_loop, daemon=True).start()

    import uvicorn
    from server import app
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
