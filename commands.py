"""
Slash command handling: turn `/ftd <args>` into a Slack response (blocks).

Subcommands:
  /ftd                      -> today's overview (sources + top brands)
  /ftd today|yesterday|week|month|30d|<N>d
  /ftd sources [period]     -> breakdown by source only
  /ftd brands  [period]     -> brand leaderboard only
  /ftd help
All reads come from the local SQLite store, so responses are instant.
"""
import config
import store
import util

HELP = (
    "*FTD bot — commands*\n"
    "`/ftd` — today's overview\n"
    "`/ftd today | yesterday | week | month | 30d` — overview for a period\n"
    "`/ftd sources [period]` — totals by traffic source\n"
    "`/ftd brands [period]` — brand leaderboard\n"
    "`/ftd help` — this message"
)


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _sources_lines(start: str, end: str) -> str:
    rows = store.totals_by_source(start, end)
    if not rows:
        return "_no data for this period yet_"
    out = []
    for r in rows:
        out.append(f"• *{r['site_label']}* — {int(r['ftd'])} FTD · {util.eur(r['deposit_value'])}")
    return "\n".join(out)


def _brands_lines(start: str, end: str, limit: int = 10) -> str:
    rows = store.top_brands(start, end, limit)
    if not rows:
        return "_no FTDs in this period yet_"
    medals = ["🥇", "🥈", "🥉"]
    out = []
    for i, r in enumerate(rows):
        tag = medals[i] if i < 3 else f"{i+1}."
        out.append(f"{tag} *{r['brand']}* ({r['site_label']}) — {int(r['ftd'])} FTD · {util.eur(r['deposit_value'])}")
    return "\n".join(out)


def _overview(start: str, end: str, label: str) -> list[dict]:
    tot = store.grand_total(start, end)
    header = f"📊 *FTD — {label}*\nTotal: *{int(tot['ftd'])} FTD* · {util.eur(tot['deposit_value'])}"
    return [
        _section(header),
        _section("*By source*\n" + _sources_lines(start, end)),
        _section("*Top brands*\n" + _brands_lines(start, end, 5)),
    ]


def handle(text: str) -> dict:
    """Return a Slack slash-command response dict (blocks + response_type)."""
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
    else:
        # No subcommand or a bare period word -> overview for that period.
        start, end, label = util.parse_period(text)
        blocks = _overview(start, end, label)

    return {"response_type": config.COMMAND_RESPONSE_TYPE, "blocks": blocks}
