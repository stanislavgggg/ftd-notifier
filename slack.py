"""
Slack delivery + FOMO message formatting.

Two transports, auto-selected:
  * SLACK_WEBHOOK_URL  -> simplest, one channel, no scopes (recommended MVP).
  * SLACK_BOT_TOKEN + SLACK_CHANNEL -> chat.postMessage (needed later for real
    @mentions of buyers and threading).
"""
import json
import random

import requests

import config

# A little variety so the channel doesn't feel robotic.
_HYPE = ["💰", "🤑", "🔥", "🚀", "💸", "🎉", "📈"]
_LEADS = [
    "NEW FTD",
    "Fresh deposit landed",
    "Money in",
    "Cha-ching",
    "Another one",
]


def _eur(x: float) -> str:
    return f"€{x:,.0f}" if x == int(x) else f"€{x:,.2f}"


def _buyer_suffix(brand: str) -> str:
    """If a buyer mapping exists for this brand, render '· <@SLACK_ID>' / '· Name'.
    No-op until BUYER_MAP is populated (per-buyer split is a future step)."""
    info = config.BUYER_MAP.get(brand)
    if not info:
        return ""
    if isinstance(info, dict):
        if info.get("slack_id"):
            return f"  ·  by <@{info['slack_id']}>"
        if info.get("name"):
            return f"  ·  by {info['name']}"
    if isinstance(info, str):
        return f"  ·  by {info}"
    return ""


def build_blocks(ev: dict) -> tuple[str, list]:
    """Build (fallback_text, blocks) for one detected FTD batch.
    ev = {site_label, brand, ftd_delta, deposit_delta, day_ftd, day_deposit}"""
    hype = random.choice(_HYPE)
    lead = random.choice(_LEADS)
    n = ev["ftd_delta"]
    plural = "FTD" if n == 1 else "FTDs"

    headline = f"{hype} *{lead} — {ev['site_label']}*"
    brand_line = f"🎰 *{ev['brand']}*  +{n} {plural}"
    if ev["deposit_delta"] > 0:
        brand_line += f"  ·  {_eur(ev['deposit_delta'])} deposited"
    brand_line += _buyer_suffix(ev["brand"])

    running = (
        f"📊 {ev['site_label']} today: *{ev['day_ftd']} FTD* · "
        f"{_eur(ev['day_deposit'])} deposits"
    )

    fallback = f"{lead} — {ev['site_label']}: {ev['brand']} +{n} {plural}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{headline}\n{brand_line}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": running}]},
    ]
    return fallback, blocks


def post(ev: dict) -> bool:
    fallback, blocks = build_blocks(ev)

    if config.DRY_RUN:
        print(f"[DRY_RUN] {fallback}")
        for b in blocks:
            print("         ", json.dumps(b, ensure_ascii=False))
        return True

    try:
        if config.SLACK_BOT_TOKEN and config.SLACK_CHANNEL:
            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
                json={"channel": config.SLACK_CHANNEL, "text": fallback, "blocks": blocks},
                timeout=15,
            )
            ok = resp.ok and resp.json().get("ok", False)
            if not ok:
                print(f"   ⚠️ Slack API error: {resp.text[:200]}")
            return ok
        elif config.SLACK_WEBHOOK_URL:
            resp = requests.post(
                config.SLACK_WEBHOOK_URL,
                json={"text": fallback, "blocks": blocks},
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"   ⚠️ Slack webhook error {resp.status_code}: {resp.text[:200]}")
            return resp.status_code == 200
        else:
            print(f"   ⚠️ No Slack transport configured — would have sent: {fallback}")
            return False
    except Exception as e:
        print(f"   ⚠️ Slack post failed: {e}")
        return False
