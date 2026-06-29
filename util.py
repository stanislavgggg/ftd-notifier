"""Small shared helpers: money formatting + period -> date range."""
from datetime import datetime, timedelta, timezone


def eur(x: float) -> str:
    x = float(x or 0)
    return f"€{x:,.0f}" if x == int(x) else f"€{x:,.2f}"


def today_utc():
    return datetime.now(timezone.utc).date()


def parse_period(text: str) -> tuple[str, str, str]:
    """Map a free-text period to (start_iso, end_iso, label).

    Accepts: today, yesterday, week / 7d, month / mtd, 30d, Nd, or empty=today.
    """
    t = (text or "").strip().lower()
    today = today_utc()

    if t in ("", "today", "сегодня"):
        return today.isoformat(), today.isoformat(), "Today"
    if t in ("yesterday", "вчера"):
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat(), "Yesterday"
    if t in ("week", "7d", "неделя"):
        return (today - timedelta(days=6)).isoformat(), today.isoformat(), "Last 7 days"
    if t in ("month", "mtd", "месяц"):
        first = today.replace(day=1)
        return first.isoformat(), today.isoformat(), f"{today:%B} (MTD)"
    if t.endswith("d") and t[:-1].isdigit():
        n = max(1, int(t[:-1]))
        return (today - timedelta(days=n - 1)).isoformat(), today.isoformat(), f"Last {n} days"

    # Fallback: treat unknown input as today.
    return today.isoformat(), today.isoformat(), "Today"
