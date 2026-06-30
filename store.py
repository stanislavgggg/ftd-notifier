"""
Fast local store (SQLite on the Railway volume).

The poller upserts the current cumulative daily numbers per (date, site, brand)
every cycle; slash commands read aggregates from here in <10ms so they answer
inside Slack's 3-second deadline without ever touching Voonix live.

One row per (date, site_id, brand) holding the LATEST seen cumulative values for
that day — re-running a day just overwrites it, so totals never double-count.
"""
import os
import sqlite3
import threading

import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db_path() -> str:
    # Prefer the persistent volume; fall back to /tmp if it isn't writable.
    for d in (config.STATE_DIR, "/tmp"):
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "ftd.db")
            open(p, "a").close()
            return p
        except Exception:
            continue
    return "/tmp/ftd.db"


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_db_path(), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS brand_daily (
                date          TEXT NOT NULL,
                site_id       TEXT NOT NULL,
                site_label    TEXT NOT NULL,
                brand         TEXT NOT NULL,
                ftd           INTEGER NOT NULL DEFAULT 0,
                signups       INTEGER NOT NULL DEFAULT 0,
                deposits      INTEGER NOT NULL DEFAULT 0,
                deposit_value REAL    NOT NULL DEFAULT 0,
                updated_at    TEXT,
                PRIMARY KEY (date, site_id, brand)
            )
        """)
        # Migrate DBs created before `signups` existed (no-op if already present).
        try:
            _conn.execute("ALTER TABLE brand_daily ADD COLUMN signups INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_bd_date ON brand_daily(date)")
        # Campaign/tracker level (date, site, campaign). Separate from brand_daily.
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS tracker_daily (
                date          TEXT NOT NULL,
                site_id       TEXT NOT NULL,
                site_label    TEXT NOT NULL,
                campaign      TEXT NOT NULL,
                ftd           INTEGER NOT NULL DEFAULT 0,
                signups       INTEGER NOT NULL DEFAULT 0,
                deposits      INTEGER NOT NULL DEFAULT 0,
                deposit_value REAL    NOT NULL DEFAULT 0,
                updated_at    TEXT,
                PRIMARY KEY (date, site_id, campaign)
            )
        """)
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_td_date ON tracker_daily(date)")
        _conn.commit()
        _migrate_clean_tracker_names(_conn)
    return _conn


def clean_tracker_name(raw: str) -> str:
    """Voonix's L3 'Campaign' value is '{tracker_id} {name}' — e.g.
    '535010 FB_LTLVHRES', 'custom_5409 LGmrk', 'LGmrk_LGmrk LGmrk_LGmrk'. The
    leading id token fragments the same tracker across links, so drop it and keep
    the name. Single-token values are returned unchanged."""
    raw = (raw or "").strip()
    parts = raw.split(None, 1)
    return parts[1].strip() if len(parts) == 2 else raw


def _migrate_clean_tracker_names(c: sqlite3.Connection):
    """One-time, idempotent: rewrite stored tracker campaign names through
    clean_tracker_name and merge fragments collapsing to the same name. No-op
    once every stored name is already clean."""
    try:
        rows = c.execute(
            "SELECT date, site_id, site_label, campaign, ftd, signups, deposits, deposit_value "
            "FROM tracker_daily").fetchall()
    except Exception:
        return
    if not rows or all(clean_tracker_name(r["campaign"]) == r["campaign"] for r in rows):
        return
    agg: dict = {}
    for r in rows:
        name = clean_tracker_name(r["campaign"])
        key = (r["date"], r["site_id"], name)
        a = agg.get(key)
        if a is None:
            a = {"date": r["date"], "site_id": r["site_id"], "site_label": r["site_label"],
                 "campaign": name, "ftd": 0, "signups": 0, "deposits": 0, "deposit_value": 0.0}
            agg[key] = a
        a["ftd"] += r["ftd"] or 0
        a["signups"] += r["signups"] or 0
        a["deposits"] += r["deposits"] or 0
        a["deposit_value"] += r["deposit_value"] or 0.0
        if r["site_label"]:
            a["site_label"] = r["site_label"]
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    c.execute("DELETE FROM tracker_daily")
    c.executemany(
        "INSERT INTO tracker_daily "
        "(date,site_id,site_label,campaign,ftd,signups,deposits,deposit_value,updated_at) "
        "VALUES (:date,:site_id,:site_label,:campaign,:ftd,:signups,:deposits,:deposit_value,:now)",
        [{**a, "now": now} for a in agg.values()])
    c.commit()
    print(f"🧹 Migrated tracker names: {len(rows)} rows → {len(agg)} clean campaigns")


def upsert_rows(rows: list[dict]):
    """rows: [{date, site_id, site_label, brand, ftd, signups, deposits, deposit_value}]"""
    if not rows:
        return
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    # Fill defaults so rows from any scraper version (with or without signups)
    # never crash the write — a present value always wins over the default.
    norm = [{"signups": 0, "deposits": 0, "deposit_value": 0.0, **r, "now": now} for r in rows]
    with _lock:
        c = conn()
        c.executemany("""
            INSERT INTO brand_daily
              (date, site_id, site_label, brand, ftd, signups, deposits, deposit_value, updated_at)
            VALUES (:date, :site_id, :site_label, :brand, :ftd, :signups, :deposits, :deposit_value, :now)
            ON CONFLICT(date, site_id, brand) DO UPDATE SET
              site_label    = excluded.site_label,
              ftd           = excluded.ftd,
              signups       = excluded.signups,
              deposits      = excluded.deposits,
              deposit_value = excluded.deposit_value,
              updated_at    = excluded.updated_at
        """, norm)
        c.commit()


# --- read helpers ------------------------------------------------------------
def totals_by_source(start: str, end: str) -> list[dict]:
    c = conn()
    cur = c.execute("""
        SELECT site_label,
               SUM(ftd)           AS ftd,
               SUM(signups)       AS signups,
               SUM(deposit_value) AS deposit_value
        FROM brand_daily
        WHERE date BETWEEN ? AND ?
        GROUP BY site_label
        ORDER BY ftd DESC
    """, (start, end))
    return [dict(r) for r in cur.fetchall()]


def top_brands(start: str, end: str, limit: int = 10) -> list[dict]:
    c = conn()
    cur = c.execute("""
        SELECT brand,
               site_label,
               SUM(ftd)           AS ftd,
               SUM(signups)       AS signups,
               SUM(deposit_value) AS deposit_value
        FROM brand_daily
        WHERE date BETWEEN ? AND ?
        GROUP BY brand, site_label
        HAVING SUM(ftd) > 0
        ORDER BY ftd DESC, signups DESC
        LIMIT ?
    """, (start, end, limit))
    return [dict(r) for r in cur.fetchall()]


def grand_total(start: str, end: str) -> dict:
    c = conn()
    r = c.execute("""
        SELECT IFNULL(SUM(ftd), 0)           AS ftd,
               IFNULL(SUM(signups), 0)       AS signups,
               IFNULL(SUM(deposit_value), 0) AS deposit_value
        FROM brand_daily
        WHERE date BETWEEN ? AND ?
    """, (start, end)).fetchone()
    return dict(r)


def source_day_record(site_label: str, before_date: str) -> int:
    """Best single-day FTD count this source ever did, on days BEFORE `before_date`.
    Used to detect a new daily record. 0 if no history."""
    c = conn()
    r = c.execute("""
        SELECT IFNULL(MAX(day_ftd), 0) AS rec FROM (
            SELECT date, SUM(ftd) AS day_ftd
            FROM brand_daily
            WHERE site_label = ? AND date < ?
            GROUP BY date
        )
    """, (site_label, before_date)).fetchone()
    return int(r["rec"])


def earliest_date() -> str | None:
    c = conn()
    r = c.execute("SELECT MIN(date) AS d FROM brand_daily").fetchone()
    return r["d"] if r and r["d"] else None


def existing_dates() -> set:
    """Set of every date already present, so backfill can skip done days."""
    c = conn()
    cur = c.execute("SELECT DISTINCT date FROM brand_daily")
    return {r["date"] for r in cur.fetchall()}


# --- Tracker (campaign-level) store ----------------------------------------

def upsert_tracker_rows(rows: list[dict]):
    """rows: [{date, site_id, site_label, campaign, ftd, signups, deposits, deposit_value}]"""
    if not rows:
        return
    # The same campaign name can appear under multiple logins within one
    # site/day. The PK is (date, site_id, campaign), so without pre-aggregation
    # the executemany would let each later duplicate OVERWRITE the earlier one
    # and silently drop FTDs. Sum duplicates into a single row first.
    agg: dict = {}
    for r in rows:
        campaign = clean_tracker_name(r["campaign"])
        key = (r["date"], r["site_id"], campaign)
        a = agg.get(key)
        if a is None:
            a = {"date": r["date"], "site_id": r["site_id"],
                 "site_label": r.get("site_label", ""), "campaign": campaign,
                 "ftd": 0, "signups": 0, "deposits": 0, "deposit_value": 0.0}
            agg[key] = a
        a["ftd"] += r.get("ftd") or 0
        a["signups"] += r.get("signups") or 0
        a["deposits"] += r.get("deposits") or 0
        a["deposit_value"] += r.get("deposit_value") or 0.0
        if r.get("site_label"):
            a["site_label"] = r["site_label"]
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    norm = [{**a, "now": now} for a in agg.values()]
    with _lock:
        c = conn()
        c.executemany("""
            INSERT INTO tracker_daily
              (date, site_id, site_label, campaign, ftd, signups, deposits, deposit_value, updated_at)
            VALUES (:date, :site_id, :site_label, :campaign, :ftd, :signups, :deposits, :deposit_value, :now)
            ON CONFLICT(date, site_id, campaign) DO UPDATE SET
              site_label    = excluded.site_label,
              ftd           = excluded.ftd,
              signups       = excluded.signups,
              deposits      = excluded.deposits,
              deposit_value = excluded.deposit_value,
              updated_at    = excluded.updated_at
        """, norm)
        c.commit()


def tracker_existing_dates(site_id: str | None = None) -> set:
    """Dates already stored. When site_id is given, only that site's dates — so
    backfill can resume per-site (adding a new site won't be masked by MAIL's
    already-filled dates)."""
    c = conn()
    if site_id:
        cur = c.execute("SELECT DISTINCT date FROM tracker_daily WHERE site_id=?", (site_id,))
    else:
        cur = c.execute("SELECT DISTINCT date FROM tracker_daily")
    return {r["date"] for r in cur.fetchall()}


def tracker_grand_total(start: str, end: str) -> dict:
    r = conn().execute("""
        SELECT IFNULL(SUM(ftd),0) AS ftd, IFNULL(SUM(signups),0) AS signups
        FROM tracker_daily WHERE date BETWEEN ? AND ?
    """, (start, end)).fetchone()
    return {"ftd": r["ftd"], "signups": r["signups"]}


def tracker_leaderboard(start: str, end: str, limit: int = 10) -> list[dict]:
    cur = conn().execute("""
        SELECT campaign, site_label,
               SUM(ftd) AS ftd, SUM(signups) AS signups
        FROM tracker_daily
        WHERE date BETWEEN ? AND ?
        GROUP BY campaign, site_label
        ORDER BY ftd DESC, signups DESC
        LIMIT ?
    """, (start, end, limit))
    return [dict(r) for r in cur.fetchall()]


def tracker_search(query: str, start: str, end: str, limit: int = 25) -> list[dict]:
    """Campaigns whose name contains `query` (case-insensitive), with totals."""
    cur = conn().execute("""
        SELECT campaign, site_label,
               SUM(ftd) AS ftd, SUM(signups) AS signups
        FROM tracker_daily
        WHERE date BETWEEN ? AND ? AND campaign LIKE ? COLLATE NOCASE
        GROUP BY campaign, site_label
        ORDER BY ftd DESC, signups DESC
        LIMIT ?
    """, (start, end, f"%{query}%", limit))
    return [dict(r) for r in cur.fetchall()]
