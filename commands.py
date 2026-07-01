"""
Slash command handling: turn `/ftd <args>` into a Slack response (blocks).

Overview / periods:
  /ftd [today|yesterday|week|month|30d|<N>d|<month name>]
Leaderboards:
  /ftd sources [period]      totals by traffic source
  /ftd brands  [period]      brand leaderboard
  /ftd trackers [period]     campaign/tracker leaderboard
Drilldowns (answer "one thing across everything" without tab-hopping in Voonix):
  /ftd brand  <name> [period]   one advertiser: split by source + its trackers
  /ftd source <LABEL> [period]  one source: its brands + its trackers
  /ftd tracker <name> [period]  one tracker (name search)
  /ftd conv [period]            signup→FTD conversion by source
  /ftd help
All reads come from the local SQLite store, so responses are instant.
"""
import config
import store
import util

HELP = (
    "*FTD bot*\n"
    "`/ftd [today|yesterday|week|month|30d|july …]` — overview\n"
    "`/ftd sources [period]` · `/ftd brands [period]` · `/ftd trackers [period]` — leaderboards\n"
    "`/ftd brand <name> [period]` — one advertiser: by source + top trackers\n"
    "`/ftd source <MAIL|META|COM> [period]` — one source: brands + trackers\n"
    "`/ftd tracker <name> [period]` — one tracker (e.g. `/ftd tracker LG week`)\n"
    "`/ftd conv [period]` — signup→FTD conversion by source\n"
    "`/ftd help`"
)


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _trunc(s: str, n: int = 26) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _conv_pct(ftd: int, su: int) -> str:
    return f" · {ftd / su * 100:.0f}% conv" if su else ""


# --- line builders -----------------------------------------------------------
def _sources_lines(start: str, end: str) -> str:
    rows = store.totals_by_source(start, end)
    rows = [r for r in rows if r["ftd"] or r["signups"]]
    if not rows:
        return "_no data for this period yet_"
    return "\n".join(
        f"• *{r['site_label']}* — {int(r['ftd'])} FTD · {util.eur(r['deposit_value'])}"
        f" · {int(r['signups'])} signups" for r in rows)


def _brands_lines(start: str, end: str, limit: int = 10) -> str:
    rows = store.top_brands(start, end, limit)
    if not rows:
        return "_no FTDs in this period yet_"
    medals = ["🥇", "🥈", "🥉"]
    out = []
    for i, r in enumerate(rows):
        tag = medals[i] if i < 3 else f"{i+1}."
        out.append(f"{tag} *{_trunc(r['brand'])}* ({r['site_label']}) — "
                   f"{int(r['ftd'])} FTD · {util.eur(r['deposit_value'])}")
    return "\n".join(out)


def _multi_tracker_sites() -> bool:
    return len({lbl for _, lbl in config.TRACKER_SITES}) > 1


def _trk_site(r: dict) -> str:
    return f" ({r['site_label']})" if _multi_tracker_sites() else ""


def _trackers_lines(start: str, end: str, limit: int = 10) -> str:
    rows = store.tracker_leaderboard(start, end, limit)
    if not rows:
        return "_no tracker data for this period yet_"
    medals = ["🥇", "🥈", "🥉"]
    out = []
    for i, r in enumerate(rows):
        tag = medals[i] if i < 3 else f"{i+1}."
        out.append(f"{tag} *{_trunc(r['campaign'])}*{_trk_site(r)} — "
                   f"{int(r['ftd'])} FTD · {int(r['signups'])} signups")
    return "\n".join(out)


# --- overview ----------------------------------------------------------------
def _overview(start: str, end: str, label: str) -> list[dict]:
    tot = store.grand_total(start, end)
    header = (f"📊 *FTD — {label}*\n"
              f"Total: *{int(tot['ftd'])} FTD* · {util.eur(tot['deposit_value'])}"
              f" · {int(tot['signups'])} signups")
    blocks = [
        _section(header),
        _section("*By source*\n" + _sources_lines(start, end)),
        _section("*Top brands*\n" + _brands_lines(start, end, 5)),
    ]
    if config.TRACKER_SITES:
        blocks.append(_section("*Top trackers*\n" + _trackers_lines(start, end, 5)))
    return blocks


# --- drilldowns --------------------------------------------------------------
def _brand_blocks(name: str, start: str, end: str, label: str) -> list[dict]:
    by_src = store.brand_by_source(name, start, end)
    if not by_src:
        return [_section(f"🎰 *Brand \"{name}\" — {label}*  ·  no data")]
    ftd = sum(int(r["ftd"]) for r in by_src)
    su = sum(int(r["signups"]) for r in by_src)
    dep = sum(float(r["deposit_value"]) for r in by_src)
    disp = by_src[0].get("brand") or name
    head = (f"🎰 *{_trunc(disp, 40)} — {label}*\n"
            f"Total: *{ftd} FTD* · {util.eur(dep)} · {su} signups")
    src_lines = "\n".join(
        f"• *{r['site_label']}* — {int(r['ftd'])} FTD · {util.eur(r['deposit_value'])}"
        f" · {int(r['signups'])} signups" for r in by_src)
    blocks = [_section(head), _section("*By source*\n" + src_lines)]

    trk = store.brand_trackers(name, start, end, 10)
    if trk:
        tl = "\n".join(f"• *{_trunc(r['campaign'])}*{_trk_site(r)} — "
                       f"{int(r['ftd'])} FTD · {int(r['signups'])} signups" for r in trk)
        blocks.append(_section("*Top trackers*\n" + tl))
    elif config.TRACKER_SITES:
        blocks.append(_section("_No tracker breakdown yet for this brand — it "
                               "fills in as trackers are re-scraped with brand tags._"))
    return blocks


def _source_blocks(label_in: str, start: str, end: str, label: str) -> list[dict]:
    site = label_in.upper()
    brands = store.source_brands(site, start, end, 15)
    if not brands:
        return [_section(f"📡 *Source \"{site}\" — {label}*  ·  no data "
                         f"(try MAIL / META / COM)")]
    ftd = sum(int(r["ftd"]) for r in brands)
    su = sum(int(r["signups"]) for r in brands)
    dep = sum(float(r["deposit_value"]) for r in brands)
    head = (f"📡 *{site} — {label}*\n"
            f"Total: *{ftd} FTD* · {util.eur(dep)} · {su} signups")
    bl = "\n".join(f"• *{_trunc(r['brand'])}* — {int(r['ftd'])} FTD · "
                   f"{util.eur(r['deposit_value'])} · {int(r['signups'])} signups"
                   for r in brands)
    blocks = [_section(head), _section("*Brands*\n" + bl)]
    trk = store.source_trackers(site, start, end, 10)
    if trk:
        tl = "\n".join(f"• *{_trunc(r['campaign'])}* — {int(r['ftd'])} FTD · "
                       f"{int(r['signups'])} signups" for r in trk)
        blocks.append(_section("*Trackers*\n" + tl))
    return blocks


def _conv_blocks(start: str, end: str, label: str) -> list[dict]:
    rows = store.conversion_by_source(start, end)
    if not rows:
        return [_section(f"📈 *Conversion — {label}*  ·  no data")]
    lines = []
    for r in rows:
        ftd, su = int(r["ftd"]), int(r["signups"])
        lines.append(f"• *{r['site_label']}* — {su} signups → {ftd} FTD{_conv_pct(ftd, su)}")
    return [_section(f"📈 *Signup→FTD conversion — {label}*\n" + "\n".join(lines))]


# --- router ------------------------------------------------------------------
def handle(text: str) -> dict:
    parts = (text or "").strip().split()
    sub = parts[0].lower() if parts else ""

    if sub == "help":
        blocks = [_section(HELP)]
    elif sub == "sources":
        start, end, label = util.parse_period(" ".join(parts[1:]))
        blocks = [_section(f"📈 *Sources — {label}*\n" + _sources_lines(start, end))]
    elif sub == "brands":
        start, end, label = util.parse_period(" ".join(parts[1:]))
        blocks = [_section(f"🏆 *Brands — {label}*\n" + _brands_lines(start, end, 10))]
    elif sub == "trackers":
        start, end, label = util.parse_period(" ".join(parts[1:]))
        tot = store.tracker_grand_total(start, end)
        head = (f"📊 *Trackers — {label}*\n"
                f"Total: *{int(tot['ftd'])} FTD* · {int(tot['signups'])} signups")
        blocks = [_section(head), _section(_trackers_lines(start, end, 10))]
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
                blocks = [_section(f'📊 *Tracker "{parts[1]}" — {label}*  ·  no matching campaigns')]
            else:
                ftd = sum(int(r["ftd"]) for r in rows)
                su = sum(int(r["signups"]) for r in rows)
                head = (f'📊 *Tracker "{parts[1]}" — {label}*\n'
                        f"Matched {len(rows)} campaign{'s' if len(rows) != 1 else ''}\n"
                        f"Total: *{ftd} FTD* · {su} signups")
                body = "\n".join(f"• *{_trunc(r['campaign'])}*{_trk_site(r)} — "
                                 f"{int(r['ftd'])} FTD · {int(r['signups'])} signups" for r in rows)
                blocks = [_section(head), _section(body)]
    else:
        start, end, label = util.parse_period(text)
        blocks = _overview(start, end, label)

    return {"response_type": config.COMMAND_RESPONSE_TYPE, "blocks": blocks}
