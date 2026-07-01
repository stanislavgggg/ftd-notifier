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

    # Update the original message in place (works for ephemeral and in-channel).
    response_url = payload.get("response_url")
    if response_url:
        try:
            requests.post(response_url, json={"replace_original": True,
                                              "blocks": resp["blocks"]}, timeout=10)
        except Exception as e:
            print(f"   ⚠️ interaction update failed: {e}")
    return JSONResponse({})
