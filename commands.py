"""
Slash command handling: turn `/ftd <args>` into a Slack response (blocks).

Formatting: compact one-line rows (no blank lines between them), each section
titled with an emoji anchor so headers stand out from the bold row names. Counts
use thousands separators. Order conveys rank (no medals/numbers).
All reads come from the local SQLite store, so responses are instant.
"""
import config
import store
import util

HELP = (
    "*FTD bot*\n"
    "`/ftd [today|yesterday|week|month|30d|july …]` — overview\n"
    "`/ftd sources [period]` · `/ftd brands [period]` · `/ftd trackers [period]` — leaderboards\n"
    "   add `all` (or a number) for the full list, e.g. `/ftd trackers month all`\n"
    "`/ftd brand <name> [period]` — one advertiser: by source + top trackers\n"
    "`/ftd source <MAIL|META|COM> [period]` — one source: brands + trackers\n"
    "`/ftd tracker <name> [period]` — one tracker (e.g. `/ftd tracker LG week`)\n"
    "`/ftd conv [period]` — signup→FTD conversion by source\n"
    "`/ftd help`"
)


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _trunc(s: str, n: int = 30) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _n(x) -> str:
    return f"{int(x):,}"


def _conv_pct(ftd: int, su: int) -> str:
    return f" · {ftd / su * 100:.0f}% conv" if su else ""


def _split_modifier(tokens: list[str], default: int = 10) -> tuple[str, int | None]:
    toks = list(tokens)
    limit: int | None = default
    if toks:
        last = toks[-1].lower()
        if last in ("all", "full", "все", "всё"):
            limit, toks = None, toks[:-1]
        elif last.isdigit():
            limit, toks = max(1, int(last)), toks[:-1]
    return " ".join(toks), limit


def _multi_tracker_sites() -> bool:
    return len({lbl for _, lbl in config.TRACKER_SITES}) > 1


def _trk_site(r: dict) -> str:
    return f" ({r['site_label']})" if _multi_tracker_sites() else ""


def _chunk_blocks(header: str, lines: list[str], per_chars: int = 2800,
                  max_blocks: int = 12) -> list[dict]:
    """Header + one-line rows across Slack section blocks (rows joined by a single
    newline). Splits before the 3000-char limit; caps at max_blocks with a note."""
    if not lines:
        return [_section(header + "\n_nothing with activity in this period_")]
    blocks: list[dict] = []
    buf = header
    truncated = 0
    for idx, ln in enumerate(lines):
        add = "\n" + ln
        if len(buf) + len(add) > per_chars:
            blocks.append(_section(buf))
            if len(blocks) >= max_blocks:
                truncated = len(lines) - idx
                buf = ""
                break
            buf = ln
        else:
            buf += add
    if buf:
        blocks.append(_section(buf))
    if truncated:
        blocks.append(_section(f"_…and {truncated} more — narrow the period or add "
                               f"a number, e.g. `100`._"))
    return blocks


# --- one-line row builders ---------------------------------------------------
def _row_source(r: dict) -> str:
    return (f"*{r['site_label']}* — {_n(r['ftd'])} FTD · {util.eur(r['deposit_value'])}"
            f" · {_n(r['signups'])} signups")


def _row_brand(r: dict) -> str:
    return (f"*{_trunc(r['brand'])}* ({r['site_label']}) — {_n(r['ftd'])} FTD · "
            f"{util.eur(r['deposit_value'])} · {_n(r['signups'])} signups")


def _row_tracker(r: dict) -> str:
    return f"*{_trunc(r['campaign'])}*{_trk_site(r)} — {_n(r['ftd'])} FTD · {_n(r['signups'])} signups"


def _rows(rows: list[dict], row_fn, empty: str) -> str:
    return "\n".join(row_fn(r) for r in rows) if rows else f"_{empty}_"


# --- overview ----------------------------------------------------------------
def _overview(start: str, end: str, label: str) -> list[dict]:
    tot = store.grand_total(start, end)
    src = [r for r in store.totals_by_source(start, end) if r["ftd"] or r["signups"]]
    blocks = [
        _section(f"📊 *FTD — {label}*\n"
                 f"{_n(tot['ftd'])} FTD · {util.eur(tot['deposit_value'])} · {_n(tot['signups'])} signups"),
        _section("🌍 *By source*\n" + _rows(src, _row_source, "no data yet")),
        _section("🏆 *Brands*\n" + _rows(store.top_brands(start, end, 5), _row_brand, "no FTDs yet")),
    ]
    if config.TRACKER_SITES:
        blocks.append(_section("🎯 *Trackers*\n"
                               + _rows(store.tracker_leaderboard(start, end, 5), _row_tracker, "no tracker data yet")))
    return blocks


# --- drilldowns --------------------------------------------------------------
def _brand_blocks(name: str, start: str, end: str, label: str) -> list[dict]:
    by_src = store.brand_by_source(name, start, end)
    if not by_src:
        return [_section(f"🎰 *{name} — {label}*\n_no data_")]
    ftd = sum(int(r["ftd"]) for r in by_src)
    su = sum(int(r["signups"]) for r in by_src)
    dep = sum(float(r["deposit_value"]) for r in by_src)
    disp = by_src[0].get("brand") or name
    blocks = [
        _section(f"🎰 *{_trunc(disp, 40)} — {label}*\n{_n(ftd)} FTD · {util.eur(dep)} · {_n(su)} signups"),
        _section("🌍 *By source*\n" + _rows(by_src, _row_source, "no data")),
    ]
    trk = store.brand_trackers(name, start, end, 10)
    if trk:
        blocks.append(_section("🎯 *Top trackers*\n" + _rows(trk, _row_tracker, "—")))
    elif config.TRACKER_SITES:
        blocks.append(_section("_No tracker breakdown yet for this brand — it fills "
                               "in as trackers are re-scraped with brand tags._"))
    return blocks


def _source_blocks(label_in: str, start: str, end: str, label: str) -> list[dict]:
    site = label_in.upper()
    brands = store.source_brands(site, start, end, 15)
    if not brands:
        return [_section(f"📡 *{site} — {label}*\n_no data (try MAIL / META / COM)_")]
    ftd = sum(int(r["ftd"]) for r in brands)
    su = sum(int(r["signups"]) for r in brands)
    dep = sum(float(r["deposit_value"]) for r in brands)
    brand_rows = "\n".join(
        f"*{_trunc(r['brand'])}* — {_n(r['ftd'])} FTD · {util.eur(r['deposit_value'])}"
        f" · {_n(r['signups'])} signups" for r in brands)
    blocks = [
        _section(f"📡 *{site} — {label}*\n{_n(ftd)} FTD · {util.eur(dep)} · {_n(su)} signups"),
        _section("🏆 *Brands*\n" + brand_rows),
    ]
    trk = store.source_trackers(site, start, end, 10)
    if trk:
        tl = "\n".join(f"*{_trunc(r['campaign'])}* — {_n(r['ftd'])} FTD · {_n(r['signups'])} signups"
                       for r in trk)
        blocks.append(_section("🎯 *Trackers*\n" + tl))
    return blocks


def _conv_blocks(start: str, end: str, label: str) -> list[dict]:
    rows = store.conversion_by_source(start, end)
    if not rows:
        return [_section(f"📈 *Conversion — {label}*\n_no data_")]
    body = "\n".join(
        f"*{r['site_label']}* — {_n(r['signups'])} signups → {_n(r['ftd'])} FTD"
        f"{_conv_pct(int(r['ftd']), int(r['signups']))}" for r in rows)
    return [_section(f"📈 *Signup→FTD conversion — {label}*\n" + body)]


# --- router ------------------------------------------------------------------
def handle(text: str) -> dict:
    parts = (text or "").strip().split()
    sub = parts[0].lower() if parts else ""

    if sub == "help":
        blocks = [_section(HELP)]
    elif sub == "sources":
        start, end, label = util.parse_period(" ".join(parts[1:]))
        src = [r for r in store.totals_by_source(start, end) if r["ftd"] or r["signups"]]
        blocks = [_section(f"🌍 *Sources — {label}*\n" + _rows(src, _row_source, "no data yet"))]
    elif sub == "brands":
        period, limit = _split_modifier(parts[1:])
        start, end, label = util.parse_period(period)
        rows = store.top_brands(start, end, limit)
        tot = store.grand_total(start, end)
        head = (f"🏆 *Brands — {label}*  ·  {len(rows)} shown\n"
                f"{_n(tot['ftd'])} FTD · {util.eur(tot['deposit_value'])} · {_n(tot['signups'])} signups")
        blocks = _chunk_blocks(head, [_row_brand(r) for r in rows])
    elif sub == "trackers":
        period, limit = _split_modifier(parts[1:])
        start, end, label = util.parse_period(period)
        rows = store.tracker_leaderboard(start, end, limit)
        tot = store.tracker_grand_total(start, end)
        site = store.grand_total(start, end)
        scope = "All" if limit is None else "Top"
        head = (f"🎯 *{scope} trackers — {label}*  ·  {len(rows)} shown\n"
                f"{_n(tot['ftd'])} FTD · {_n(tot['signups'])} signups")
        blocks = _chunk_blocks(head, [_row_tracker(r) for r in rows])
        u_ftd = int(site["ftd"]) - int(tot["ftd"])
        u_su = int(site["signups"]) - int(tot["signups"])
        if u_ftd > 0 or u_su > 0:
            blocks.append(_section(
                f"_Tracked {_n(tot['ftd'])} of {_n(site['ftd'])} FTD · "
                f"{_n(tot['signups'])} of {_n(site['signups'])} signups. The rest "
                f"({_n(max(0,u_ftd))} FTD · {_n(max(0,u_su))} signups) is traffic with "
                f"no tracker tag (or not yet backfilled)._"))
    elif sub == "brand":
        if len(parts) < 2:
            blocks = [_section("Usage: `/ftd brand <name> [period]` — e.g. `/ftd brand iWild week`")]
        else:
            start, end, label = util.parse_period(" ".join(parts[2:]))
            blocks = _brand_blocks(parts[1], start, end, label)
    elif sub == "source":
        if len(parts) < 2:
            blocks = [_section("Usage: `/ftd source <MAIL|META|COM> [period]`")]
        else:
            start, end, label = util.parse_period(" ".join(parts[2:]))
            blocks = _source_blocks(parts[1], start, end, label)
    elif sub == "conv":
        start, end, label = util.parse_period(" ".join(parts[1:]))
        blocks = _conv_blocks(start, end, label)
    elif sub == "tracker":
        if len(parts) < 2:
            blocks = [_section("Usage: `/ftd tracker <name> [period]` — e.g. `/ftd tracker LG week`")]
        else:
            start, end, label = util.parse_period(" ".join(parts[2:]))
            rows = store.tracker_search(parts[1], start, end)
            if not rows:
                blocks = [_section(f'🎯 *Tracker "{parts[1]}" — {label}*\n_no matching campaigns_')]
            else:
                ftd = sum(int(r["ftd"]) for r in rows)
                su = sum(int(r["signups"]) for r in rows)
                head = (f'🎯 *Tracker "{parts[1]}" — {label}*  ·  {len(rows)} matched\n'
                        f"{_n(ftd)} FTD · {_n(su)} signups")
                blocks = _chunk_blocks(head, [_row_tracker(r) for r in rows])
    else:
        start, end, label = util.parse_period(text)
        blocks = _overview(start, end, label)

    return {"response_type": config.COMMAND_RESPONSE_TYPE, "blocks": blocks}
