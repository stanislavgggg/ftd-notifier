"""
Auto content that makes the channel feel alive, posted via the webhook:

  * Daily summary — once a day at DAILY_SUMMARY_HOUR_UTC, a recap of the day by
    source + the day's best brand.
  * Records — when a source beats its own best-ever single-day FTD count, a one
    -off "🏆 record" ping (fired at most once per source per day).
"""
import json
from datetime import datetime, timezone

import config
import slack
import store
import util

_daily_posted_for: str | None = None          # date we already posted a summary for
_record_fired: set[tuple[str, str]] = set()    # (date, site_label) already announced


def _post_raw(text: str, blocks: list):
    if config.DRY_RUN:
        print(f"[DRY_RUN summary] {text}")
        for b in blocks:
            print("         ", json.dumps(b, ensure_ascii=False))
        return
    payload = {"text": text, "blocks": blocks}
    try:
        if config.SLACK_BOT_TOKEN and config.SLACK_CHANNEL:
            import requests
            requests.post("https://slack.com/api/chat.postMessage",
                          headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
                          json={"channel": config.SLACK_CHANNEL, **payload}, timeout=15)
        elif config.SLACK_WEBHOOK_URL:
            import requests
            requests.post(config.SLACK_WEBHOOK_URL, json=payload, timeout=15)
    except Exception as e:
        print(f"   ⚠️ summary post failed: {e}")


def post_daily_summary(date: str):
    tot = store.grand_total(date, date)
    sources = store.totals_by_source(date, date)
    brands = store.top_brands(date, date, 1)

    lines = [f"📅 *Day recap — {date}*",
             f"Total: *{int(tot['ftd'])} FTD* · {int(tot['signups'])} signups"]
    if sources:
        lines.append("")
        for s in sources:
            lines.append(f"• *{s['site_label']}* — {int(s['ftd'])} FTD · {int(s['signups'])} signups")
    if brands and brands[0]["ftd"]:
        b = brands[0]
        lines.append(f"\n👑 Brand of the day: *{b['brand']}* — {int(b['ftd'])} FTD · {int(b['signups'])} signups")

    text = f"Day recap {date}: {int(tot['ftd'])} FTD"
    _post_raw(text, [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}])


def maybe_post_daily_summary(now: datetime):
    """Post yesterday's recap once we're past DAILY_SUMMARY_HOUR_UTC."""
    global _daily_posted_for
    if config.DAILY_SUMMARY_HOUR_UTC < 0:
        return
    if now.hour < config.DAILY_SUMMARY_HOUR_UTC:
        return
    date = now.date().isoformat()
    if _daily_posted_for == date:
        return
    post_daily_summary(date)
    _daily_posted_for = date


def check_records(rows: list[dict], now: datetime):
    """Fire a one-off ping when a source beats its all-time best single-day FTDs."""
    if not config.ENABLE_RECORDS:
        return
    date = now.date().isoformat()
    # current FTD-per-source for today
    by_source: dict[str, int] = {}
    for r in rows:
        if r["date"] == date:
            by_source[r["site_label"]] = by_source.get(r["site_label"], 0) + r["ftd"]
    for label, today_ftd in by_source.items():
        if (date, label) in _record_fired or today_ftd <= 0:
            continue
        prev_best = store.source_day_record(label, date)
        if prev_best > 0 and today_ftd > prev_best:
            _record_fired.add((date, label))
            text = f"🏆 New record — {label}: {today_ftd} FTD today"
            _post_raw(text, [{"type": "section", "text": {"type": "mrkdwn",
                      "text": f"🏆 *New daily record — {label}!*\n"
                              f"*{today_ftd} FTD* today, beating the previous best of {prev_best}. 🔥"}}])
