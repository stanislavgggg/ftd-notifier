"""Small shared helpers: money formatting + period -> date range."""
from datetime import date, datetime, timedelta, timezone

# Month names + common abbreviations -> month number.
_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def eur(x: float) -> str:
    x = float(x or 0)
    return f"€{x:,.0f}" if x == int(x) else f"€{x:,.2f}"


def today_utc():
    return datetime.now(timezone.utc).date()


def parse_period(text: str) -> tuple[str, str, str]:
    """Map a free-text period to (start_iso, end_iso, label).

    Accepts: today, yesterday, week / 7d, month / mtd, 30d, Nd, a named month
    optionally with a year ("july", "july 2024", "dec 2025"), or empty=today.
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

    # Named month, optionally followed by a 4-digit year. Without a year we pick
    # the most recent occurrence: this year if the month has started, else last.
    toks = t.split()
    if toks and toks[0] in _MONTHS:
        m = _MONTHS[toks[0]]
        year = None
        if len(toks) > 1 and toks[1].isdigit() and len(toks[1]) == 4:
            year = int(toks[1])
        if year is None:
            year = today.year if m <= today.month else today.year - 1
        first = date(year, m, 1)
        last = date(year, 12, 31) if m == 12 else date(year, m + 1, 1) - timedelta(days=1)
        end = min(last, today)
        label = f"{first:%B %Y}" + (" (so far)" if end < last else "")
        return first.isoformat(), end.isoformat(), label

    if t.endswith("d") and t[:-1].isdigit():
        n = max(1, int(t[:-1]))
        return (today - timedelta(days=n - 1)).isoformat(), today.isoformat(), f"Last {n} days"

    # Fallback: treat unknown input as today.
    return today.isoformat(), today.isoformat(), "Today"
