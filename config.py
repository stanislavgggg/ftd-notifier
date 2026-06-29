"""
Configuration for the FTD → Slack notifier.

Everything is env-driven so it deploys on Railway exactly like statparser.
Only VOONIX_USER / VOONIX_PASS / GMAIL_* / SLACK_WEBHOOK_URL are strictly
required; the rest have sane defaults.
"""
import json
import os


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- Voonix login (same creds the statparser already uses) -------------------
BASE_URL  = os.environ.get("VOONIX_BASE_URL", "https://gggroup.voonix.net")
LOGIN_URL = f"{BASE_URL}/"
VOONIX_USER    = os.environ["VOONIX_USER"]
VOONIX_PASS    = os.environ["VOONIX_PASS"]
GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASS"]

# --- Which sites/brands to watch ---------------------------------------------
# "82:MAIL,29:META" -> scrape site 82 labelled MAIL and site 29 labelled META.
# The label is what shows in the Slack header; the id is the Voonix site id.
def _parse_sites(raw: str) -> list[tuple[str, str]]:
    out = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            sid, label = chunk.split(":", 1)
            out.append((sid.strip(), label.strip()))
        else:
            out.append((chunk, f"site {chunk}"))
    return out

SITES = _parse_sites(os.environ.get("SITES", "82:MAIL,29:META"))

# --- Cadence -----------------------------------------------------------------
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "900"))   # 15 min
# Also re-check yesterday so late-settling FTDs still fire a notification.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))                      # today + yesterday
# Optional active window in UTC hours, e.g. "6-23". Outside it the loop sleeps
# without scraping. Empty = run 24/7.
ACTIVE_HOURS_UTC = os.environ.get("ACTIVE_HOURS_UTC", "").strip()

# --- Slack -------------------------------------------------------------------
# Simplest path: an Incoming Webhook URL (one channel, no scopes).
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
# Richer path (optional, for later @mentions / threads): a bot token + channel.
SLACK_BOT_TOKEN   = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL     = os.environ.get("SLACK_CHANNEL", "").strip()

# --- Behaviour flags ---------------------------------------------------------
DRY_RUN   = _bool("DRY_RUN", False)        # print instead of posting to Slack
RUN_ONCE  = _bool("RUN_ONCE", False)       # one poll cycle then exit (cron mode)
HEADLESS  = _bool("HEADLESS", True)
# Smallest deposit that earns a notification (filters €0 / junk FTD rows).
MIN_DEPOSIT_EUR = float(os.environ.get("MIN_DEPOSIT_EUR", "0"))

# --- Session / state persistence ---------------------------------------------
# Put a Railway volume here so the logged-in session survives restarts and we
# don't re-trigger 2FA on every redeploy. Falls back to /tmp if not writable.
STATE_DIR = os.environ.get("STATE_DIR", "/data")

# --- Optional BigQuery mirror (feeds the future leaderboard / buyer layer) ---
BQ_MIRROR = _bool("BQ_MIRROR", False)
BQ_PROJECT = os.environ.get("BQ_PROJECT", "x-fabric-494718-d1")
BQ_DATASET = os.environ.get("BQ_DATASET", "datasetmailchimp")
BQ_EVENTS_TABLE = os.environ.get("BQ_EVENTS_TABLE", "FtdEvents")
GOOGLE_APPLICATION_CREDENTIALS_JSON = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS_JSON", ""
)

# --- Buyer attribution (future) ----------------------------------------------
# No per-buyer split exists yet. When it does (sub-id / campaign -> buyer), set
# BUYER_MAP to a JSON object keyed by brand or campaign, e.g.
#   {"iWildCasino": {"name": "Marija", "slack_id": "U0123ABC"}}
# and the notifier will append "by <@U0123ABC>" to that brand's pings.
try:
    BUYER_MAP = json.loads(os.environ.get("BUYER_MAP", "{}"))
except Exception:
    BUYER_MAP = {}


# --- Slash commands + web server ---------------------------------------------
# Signing Secret from the Slack app's Basic Information page. Required for
# /ftd commands (without it the command endpoint rejects everything).
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "").strip()
PORT = int(os.environ.get("PORT", "8080"))           # Railway sets PORT
# "in_channel" = command replies are visible to everyone (FOMO); "ephemeral" =
# only the person who typed it sees the reply.
COMMAND_RESPONSE_TYPE = os.environ.get("COMMAND_RESPONSE_TYPE", "in_channel").strip()

# --- Auto content ------------------------------------------------------------
# UTC hour to post the day recap (e.g. 21 = 21:00 UTC). -1 disables it.
DAILY_SUMMARY_HOUR_UTC = int(os.environ.get("DAILY_SUMMARY_HOUR_UTC", "-1"))
# UTC hour to post YESTERDAY's final recap in the morning. 5 ≈ 07:00 Spain
# (CEST, summer) / 06:00 (CET, winter). -1 disables it.
MORNING_REPORT_HOUR_UTC = int(os.environ.get("MORNING_REPORT_HOUR_UTC", "5"))
ENABLE_RECORDS = _bool("ENABLE_RECORDS", True)

# --- One-time history backfill (so week/month commands aren't empty at launch)
# >0 -> on startup, scrape the last N days per site into the store (idempotent).
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "0"))


def in_active_window(hour_utc: int) -> bool:
    if not ACTIVE_HOURS_UTC:
        return True
    try:
        lo, hi = (int(x) for x in ACTIVE_HOURS_UTC.split("-", 1))
        return lo <= hour_utc <= hi
    except Exception:
        return True
