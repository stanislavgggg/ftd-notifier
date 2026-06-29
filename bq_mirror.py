"""
Optional BigQuery mirror.

When BQ_MIRROR=true and creds are present, every detected FTD batch is streamed
into `<project>.<dataset>.FtdEvents`. This is the data the future per-buyer
leaderboard reads — the notifier itself doesn't need it. All failures are
swallowed so a BigQuery hiccup can never block a Slack notification.

Table (auto-created on first use):
  ts            TIMESTAMP   -- when we detected it
  event_date    DATE        -- the Voonix calendar day the FTD belongs to
  site_id       STRING
  site_label    STRING
  brand         STRING
  ftd_delta     INT64       -- new FTDs in this batch
  deposit_delta FLOAT64     -- new deposit value in this batch (EUR)
  day_ftd       INT64       -- running brand+day FTD total after this batch
  buyer         STRING      -- NULL until buyer attribution exists
"""
import json
import logging

import config

log = logging.getLogger("bq_mirror")

_client = None
_ensured = False


def _get_client():
    global _client
    if _client is not None:
        return _client
    from google.cloud import bigquery
    from google.oauth2 import service_account

    info = json.loads(config.GOOGLE_APPLICATION_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    _client = bigquery.Client(credentials=creds, project=info["project_id"])
    return _client


def _table_id() -> str:
    return f"{config.BQ_PROJECT}.{config.BQ_DATASET}.{config.BQ_EVENTS_TABLE}"


def _ensure_table():
    global _ensured
    if _ensured:
        return
    from google.cloud import bigquery

    client = _get_client()
    schema = [
        bigquery.SchemaField("ts", "TIMESTAMP"),
        bigquery.SchemaField("event_date", "DATE"),
        bigquery.SchemaField("site_id", "STRING"),
        bigquery.SchemaField("site_label", "STRING"),
        bigquery.SchemaField("brand", "STRING"),
        bigquery.SchemaField("ftd_delta", "INT64"),
        bigquery.SchemaField("deposit_delta", "FLOAT64"),
        bigquery.SchemaField("day_ftd", "INT64"),
        bigquery.SchemaField("buyer", "STRING"),
    ]
    table = bigquery.Table(_table_id(), schema=schema)
    client.create_table(table, exists_ok=True)
    _ensured = True


def record(ev: dict):
    if not (config.BQ_MIRROR and config.GOOGLE_APPLICATION_CREDENTIALS_JSON):
        return
    try:
        _ensure_table()
        client = _get_client()
        buyer = None
        info = config.BUYER_MAP.get(ev["brand"])
        if isinstance(info, dict):
            buyer = info.get("name")
        elif isinstance(info, str):
            buyer = info
        errors = client.insert_rows_json(_table_id(), [{
            "ts": ev["ts"],
            "event_date": ev["date"],
            "site_id": ev["site_id"],
            "site_label": ev["site_label"],
            "brand": ev["brand"],
            "ftd_delta": ev["ftd_delta"],
            "deposit_delta": ev["deposit_delta"],
            "day_ftd": ev["day_ftd"],
            "buyer": buyer,
        }])
        if errors:
            log.warning("BQ insert errors: %s", errors)
    except Exception as e:
        log.warning("BQ mirror failed (non-fatal): %s", e)
