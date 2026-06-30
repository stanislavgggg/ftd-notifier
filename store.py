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
    return _conn


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
               MAX(site_label)    AS site_label,
               SUM(ftd)           AS ftd,
               SUM(signups)       AS signups,
               SUM(deposit_value) AS deposit_value
        FROM brand_daily
        WHERE date BETWEEN ? AND ?
        GROUP BY brand
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
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    norm = [{"signups": 0, "deposits": 0, "deposit_value": 0.0, **r, "now": now} for r in rows]
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


def tracker_existing_dates() -> set:
    c = conn()
    return {r["date"] for r in c.execute("SELECT DISTINCT date FROM tracker_daily").fetchall()}


def tracker_grand_total(start: str, end: str) -> dict:
    r = conn().execute("""
        SELECT IFNULL(SUM(ftd),0) AS ftd, IFNULL(SUM(signups),0) AS signups
        FROM tracker_daily WHERE date BETWEEN ? AND ?
    """, (start, end)).fetchone()
    return {"ftd": r["ftd"], "signups": r["signups"]}


def tracker_leaderboard(start: str, end: str, limit: int = 10) -> list[dict]:
    cur = conn().execute("""
        SELECT campaign, MAX(site_label) AS site_label,
               SUM(ftd) AS ftd, SUM(signups) AS signups
        FROM tracker_daily
        WHERE date BETWEEN ? AND ?
        GROUP BY campaign
        ORDER BY ftd DESC, signups DESC
        LIMIT ?
    """, (start, end, limit))
    return [dict(r) for r in cur.fetchall()]


def tracker_search(query: str, start: str, end: str, limit: int = 25) -> list[dict]:
    """Campaigns whose name contains `query` (case-insensitive), with totals."""
    cur = conn().execute("""
        SELECT campaign, MAX(site_label) AS site_label,
               SUM(ftd) AS ftd, SUM(signups) AS signups
        FROM tracker_daily
        WHERE date BETWEEN ? AND ? AND campaign LIKE ? COLLATE NOCASE
        GROUP BY campaign
        ORDER BY ftd DESC, signups DESC
        LIMIT ?
    """, (start, end, f"%{query}%", limit))
    return [dict(r) for r in cur.fetchall()]
