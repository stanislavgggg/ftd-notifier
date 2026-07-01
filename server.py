"""
HTTP server for Slack slash commands.

Slack POSTs the command to /slack/commands. We verify the request signature
(HMAC over the raw body, with the app's Signing Secret) before trusting it, then
answer from the local store within Slack's 3-second window.

/health is for Railway's healthcheck. /slack/interactions is a stub so buttons
can be added later without reconfiguring the app.
"""
import hashlib
import hmac
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

import commands
import config

app = FastAPI(title="FTD Notifier")

_EMAIL_CACHE: dict[str, str] = {}


def _email_for(user_id: str | None) -> str | None:
    """Resolve a Slack user's account email via users.info (cached). Needs the
    bot token + users:read.email scope."""
    if not user_id or not config.SLACK_BOT_TOKEN:
        return None
    if user_id in _EMAIL_CACHE:
        return _EMAIL_CACHE[user_id]
    import requests
    try:
        r = requests.get("https://slack.com/api/users.info",
                         headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
                         params={"user": user_id}, timeout=8).json()
        email = ((r.get("user") or {}).get("profile") or {}).get("email")
        if email:
            email = email.lower()
            _EMAIL_CACHE[user_id] = email
        return email
    except Exception as e:
        print(f"   ⚠️ users.info failed: {e}")
        return None


def _user_allowed(user_id: str | None) -> bool:
    """Gate access by Slack user ID and/or account email. Empty allowlists =
    everyone. Email checks fail closed if the email can't be resolved."""
    if not (config.ALLOWED_USERS or config.ALLOWED_EMAILS):
        return True
    if user_id and user_id in config.ALLOWED_USERS:
        return True
    if config.ALLOWED_EMAILS:
        email = _email_for(user_id)
        return bool(email and email in config.ALLOWED_EMAILS)
    return False


def _verify_slack(raw_body: bytes, headers) -> bool:
    """True if the request is a genuine, recent Slack request."""
    secret = config.SLACK_SIGNING_SECRET
    if not secret:
        # No secret configured -> refuse command handling rather than trust blindly.
        return False
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 60 * 5:   # replay protection
            return False
    except ValueError:
        return False
    base = b"v0:" + ts.encode() + b":" + raw_body
    mine = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, sig)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return {"service": "ftd-notifier", "commands": "/ftd"}


@app.post("/slack/commands")
async def slack_commands(request: Request):
    raw = await request.body()
    if not _verify_slack(raw, request.headers):
        return PlainTextResponse("invalid signature", status_code=401)

    # Slack sends application/x-www-form-urlencoded
    from urllib.parse import parse_qs
    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
    if not _user_allowed(form.get("user_id")):
        return JSONResponse({"response_type": "ephemeral",
                             "text": "🔒 You don't have access to this bot. Ask an admin to add you."})
    text = form.get("text", "")

    try:
        return JSONResponse(commands.handle(text))
    except Exception as e:
        return JSONResponse({
            "response_type": "ephemeral",
            "text": f"⚠️ Couldn't build that report: {e}",
        })


@app.post("/slack/interactions")
async def slack_interactions(request: Request):
    """Handle button clicks and dropdown selections from the panel: figure out
    what was chosen, render the matching view, and update the message in place."""
    import json
    from urllib.parse import parse_qs

    import requests
    import commands

    raw = await request.body()
    if not _verify_slack(raw, request.headers):
        return PlainTextResponse("invalid signature", status_code=401)

    form = parse_qs(raw.decode())
    if "payload" not in form:
        return JSONResponse({})
    payload = json.loads(form["payload"][0])
    if not _user_allowed((payload.get("user") or {}).get("id")):
        return JSONResponse({})
    actions = payload.get("actions") or []
    if not actions:
        return JSONResponse({})
    a = actions[0]
    value = a.get("value") or (a.get("selected_option") or {}).get("value", "")
    if not value:
        return JSONResponse({})  # e.g. the "Open in Voonix" url button — nothing to render

    try:
        resp = commands.action_to_response(value)
    except Exception as e:
        return JSONResponse({"text": f"⚠️ {e}"})

    # Update in place: response_url for messages, views.publish for the Home tab.
    response_url = payload.get("response_url")
    if response_url:
        try:
            requests.post(response_url, json={"replace_original": True,
                                              "blocks": resp["blocks"]}, timeout=10)
        except Exception as e:
            print(f"   ⚠️ interaction update failed: {e}")
    else:
        _publish_home((payload.get("user") or {}).get("id"), resp["blocks"])
    return JSONResponse({})


def _publish_home(user_id: str | None, blocks: list | None = None):
    """Publish (or refresh) the App Home dashboard for a user. Requires a bot
    token; no-op without one. Defaults to today's overview when no blocks given."""
    if not config.SLACK_BOT_TOKEN or not user_id:
        return
    import requests
    import commands
    if blocks is None:
        blocks = commands.handle("today")["blocks"]
    try:
        requests.post("https://slack.com/api/views.publish",
                      headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
                      json={"user_id": user_id, "view": {"type": "home", "blocks": blocks[:100]}},
                      timeout=10)
    except Exception as e:
        print(f"   ⚠️ views.publish failed: {e}")


@app.post("/slack/events")
async def slack_events(request: Request):
    """Events API: URL verification handshake + publish the Home dashboard when a
    user opens the app's Home tab."""
    import json

    raw = await request.body()
    data = json.loads(raw.decode() or "{}")
    if data.get("type") == "url_verification":          # setup handshake
        return JSONResponse({"challenge": data.get("challenge", "")})
    if not _verify_slack(raw, request.headers):
        return PlainTextResponse("invalid signature", status_code=401)
    ev = data.get("event", {})
    if ev.get("type") == "app_home_opened":
        user = ev.get("user")
        if _user_allowed(user):
            _publish_home(user)
        else:
            _publish_home_denied(user)
    return JSONResponse({})


def _publish_home_denied(user_id: str | None):
    if not config.SLACK_BOT_TOKEN or not user_id:
        return
    import requests
    view = {"type": "home", "blocks": [{"type": "section", "text": {"type": "mrkdwn",
            "text": "🔒 *No access*\nYou don't have access to this dashboard. Ask an admin to add you."}}]}
    try:
        requests.post("https://slack.com/api/views.publish",
                      headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
                      json={"user_id": user_id, "view": view}, timeout=10)
    except Exception as e:
        print(f"   ⚠️ views.publish (denied) failed: {e}")
