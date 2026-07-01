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
    "`/ftd menu` — buttons & dropdowns (pin it as a remote)\n"
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


# --- interactive panel (buttons + dropdowns) --------------------------------
PERIODS = [("today", "Today"), ("yesterday", "Yesterday"), ("week", "7 days"), ("month", "Month")]
VIEWS = [("overview", "Overview"), ("sources", "Sources"), ("brands", "Brands"), ("trackers", "Trackers")]


def _pkey_from(period_text: str) -> str | None:
    t = (period_text or "").strip().lower()
    if t in ("", "today"):
        return "today"
    if t == "yesterday":
        return "yesterday"
    if t in ("week", "7d"):
        return "week"
    if t in ("month", "mtd"):
        return "month"
    return None  # non-standard period (july, 30d, …) — panel defaults to week


def _btn(text: str, value: str, action_id: str, primary: bool = False) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text},
         "value": value[:75], "action_id": action_id}
    if primary:
        b["style"] = "primary"
    return b


def _opt(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text[:75]}, "value": value[:75]}


def _select(action_id: str, placeholder: str, options: list[dict]) -> dict:
    return {"type": "static_select", "action_id": action_id,
            "placeholder": {"type": "plain_text", "text": placeholder}, "options": options}


EXT_PERIODS = [("week", "This week (7d)"), ("30d", "Last 30 days"),
               ("month", "This month"), ("last month", "Last month"),
               ("ytd", "This year")]


def _voonix_url(start: str, end: str) -> str:
    return f"{config.BASE_URL}/?p=siteearnings&start={start}&end={end}&submit_date=View"


def _panel(view: str, period_text: str, start: str, end: str) -> list[dict]:
    """Buttons + dropdowns so users click instead of typing long commands.
    `period_text` is the real current period (e.g. 'last month') so Refresh, view
    switches and entity picks preserve it; quick buttons jump to fixed periods."""
    pt = (period_text or "today").strip() or "today"
    pkey = _pkey_from(pt)
    blocks: list[dict] = [{"type": "divider"}]

    row1 = [_btn(lbl, f"nav:{view}:{k}", f"period_{k}", primary=(k == pkey)) for k, lbl in PERIODS]
    row1.append(_btn("🔄 Refresh", f"nav:{view}:{pt}", "refresh"))
    blocks.append({"type": "actions", "elements": row1})

    blocks.append({"type": "actions", "elements": [
        _btn(lbl, f"nav:{v}:{pt}", f"view_{v}", primary=(v == view)) for v, lbl in VIEWS]})

    selects = [_select("pick_period", "More periods →",
                       [_opt(lbl, f"nav:{view}:{tok}") for tok, lbl in EXT_PERIODS])]
    srcs = [r["site_label"] for r in store.totals_by_source(start, end) if r["ftd"] or r["signups"]]
    if srcs:
        selects.append(_select("pick_source", "Source →",
                               [_opt(s, f"pk:source:{pt}:{s}") for s in srcs[:100]]))
    brands = store.top_brands(start, end, 20)
    if brands:
        selects.append(_select("pick_brand", "Brand →",
                               [_opt(f"{_trunc(b['brand'], 40)} ({b['site_label']})",
                                     f"pk:brand:{pt}:{_trunc(b['brand'], 40)}") for b in brands]))
    trks = store.tracker_leaderboard(start, end, 20)
    if trks:
        selects.append(_select("pick_tracker", "Tracker →",
                               [_opt(_trunc(t["campaign"], 45), f"pk:tracker:{pt}:{_trunc(t['campaign'], 40)}")
                                for t in trks]))
    blocks.append({"type": "actions", "elements": selects})

    blocks.append({"type": "actions", "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": "Open in Voonix ↗"},
         "url": _voonix_url(start, end), "action_id": "open_voonix"}]})
    return blocks


def _tracker_search_blocks(name: str, start: str, end: str, label: str) -> list[dict]:
    rows = store.tracker_search(name, start, end)
    if not rows:
        return [_section(f'🎯 *Tracker "{name}" — {label}*\n_no matching campaigns_')]
    ftd = sum(int(r["ftd"]) for r in rows)
    su = sum(int(r["signups"]) for r in rows)
    head = (f'🎯 *Tracker "{name}" — {label}*  ·  {len(rows)} matched\n'
            f"{_n(ftd)} FTD · {_n(su)} signups")
    return _chunk_blocks(head, [_row_tracker(r) for r in rows])


def render_pick(kind: str, name: str, pkey: str) -> dict:
    """Render a source/brand/tracker drilldown chosen from a dropdown (name may
    contain spaces, so this bypasses the text grammar). Always includes the panel."""
    start, end, label = util.parse_period(pkey)
    if kind == "source":
        blocks, view = _source_blocks(name, start, end, label), "sources"
    elif kind == "brand":
        blocks, view = _brand_blocks(name, start, end, label), "brands"
    else:
        blocks, view = _tracker_search_blocks(name, start, end, label), "trackers"
    blocks += _panel(view, pkey, start, end)
    return {"response_type": config.COMMAND_RESPONSE_TYPE, "blocks": blocks}


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
        blocks.append(_section("🎯 *Trackers*\n" + _rows(trk, _row_tracker, "—")))
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
def handle(text: str, with_panel: bool = True) -> dict:
    parts = (text or "").strip().split()
    sub = parts[0].lower() if parts else ""
    view, period_text, start, end = "overview", "", None, None

    if sub == "help":
        blocks = [_section(HELP)]
    elif sub == "menu":
        period_text = "today"
        start, end, label = util.parse_period(period_text)
        view = "overview"
        tot = store.grand_total(start, end)
        blocks = [_section(
            "📋 *FTD control panel*\n"
            f"Today: {_n(tot['ftd'])} FTD · {util.eur(tot['deposit_value'])} · {_n(tot['signups'])} signups\n"
            "Pick a period, view, or an entity below — no typing needed.")]
    elif sub == "sources":
        period_text = " ".join(parts[1:])
        start, end, label = util.parse_period(period_text)
        view = "sources"
        src = [r for r in store.totals_by_source(start, end) if r["ftd"] or r["signups"]]
        blocks = [_section(f"🌍 *Sources — {label}*\n" + _rows(src, _row_source, "no data yet"))]
    elif sub == "brands":
        period_text, limit = _split_modifier(parts[1:])
        start, end, label = util.parse_period(period_text)
        view = "brands"
        rows = store.top_brands(start, end, limit)
        tot = store.grand_total(start, end)
        head = (f"🏆 *Brands — {label}*  ·  {len(rows)} shown\n"
                f"{_n(tot['ftd'])} FTD · {util.eur(tot['deposit_value'])} · {_n(tot['signups'])} signups")
        blocks = _chunk_blocks(head, [_row_brand(r) for r in rows])
    elif sub == "trackers":
        period_text, limit = _split_modifier(parts[1:])
        start, end, label = util.parse_period(period_text)
        view = "trackers"
        rows = store.tracker_leaderboard(start, end, limit)
        tot = store.tracker_grand_total(start, end)
        scope = "All" if limit is None else "Top"
        head = (f"🎯 *{scope} trackers — {label}*  ·  {len(rows)} shown\n"
                f"{_n(tot['ftd'])} FTD · {_n(tot['signups'])} signups")
        blocks = _chunk_blocks(head, [_row_tracker(r) for r in rows])
    elif sub == "brand":
        if len(parts) < 2:
            blocks = [_section("Usage: `/ftd brand <name> [period]` — e.g. `/ftd brand iWild week`")]
        else:
            period_text = " ".join(parts[2:])
            start, end, label = util.parse_period(period_text)
            view = "brands"
            blocks = _brand_blocks(parts[1], start, end, label)
    elif sub == "source":
        if len(parts) < 2:
            blocks = [_section("Usage: `/ftd source <MAIL|META|COM> [period]`")]
        else:
            period_text = " ".join(parts[2:])
            start, end, label = util.parse_period(period_text)
            view = "sources"
            blocks = _source_blocks(parts[1], start, end, label)
    elif sub == "conv":
        period_text = " ".join(parts[1:])
        start, end, label = util.parse_period(period_text)
        view = "sources"
        blocks = _conv_blocks(start, end, label)
    elif sub == "tracker":
        if len(parts) < 2:
            blocks = [_section("Usage: `/ftd tracker <name> [period]` — e.g. `/ftd tracker LG week`")]
        else:
            period_text = " ".join(parts[2:])
            start, end, label = util.parse_period(period_text)
            view = "trackers"
            blocks = _tracker_search_blocks(parts[1], start, end, label)
    else:
        period_text = text
        start, end, label = util.parse_period(period_text)
        view = "overview"
        blocks = _overview(start, end, label)

    if with_panel and start is not None:
        blocks = blocks + _panel(view, period_text, start, end)
    return {"response_type": config.COMMAND_RESPONSE_TYPE, "blocks": blocks}


def action_to_response(value: str) -> dict:
    """Map a clicked button / selected dropdown option value to a rendered
    response (with panel). Values:
      nav:<view>:<pkey>            -> that view for that period
      pk:<source|brand|tracker>:<pkey>:<name>  -> a drilldown (name may have spaces)"""
    p = value.split(":", 3)
    if p[0] == "nav" and len(p) >= 3:
        view, pk = p[1], p[2]
        return handle(pk if view == "overview" else f"{view} {pk}")
    if p[0] == "pk" and len(p) == 4:
        return render_pick(p[1], p[3], p[2])
    return handle("today")
