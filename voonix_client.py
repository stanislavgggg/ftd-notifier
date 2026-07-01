"""
Voonix client.

Reuses the proven login + Gmail-2FA + CSV-download approach from statparser, but
adds a PERSISTENT SESSION so we can poll every few minutes WITHOUT re-triggering
the email 2FA code each time:

  * After a full (2FA) login we save the browser's storage_state (cookies) to
    disk. Every later poll restores it and goes straight to the data.
  * Only when Voonix has expired the session (it bounces us to the login form)
    do we pay the 2FA cost again, then re-save the state.

Each poll uses a short-lived browser restored from that state — we do NOT keep
Chromium running for hours (avoids memory creep), but we DO keep the session.
"""
import asyncio
import csv
import email
import imaplib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from playwright.async_api import async_playwright

import config

STATE_FILE = None  # resolved lazily (see _state_file)
DOWNLOAD_DIR = "/tmp/voonix_ftd"
_TRK_CSV_SAMPLED = False  # log L3 campaign CSV structure once, to fix name parsing


def _state_file() -> str:
    global STATE_FILE
    if STATE_FILE:
        return STATE_FILE
    for d in (config.STATE_DIR, "/tmp"):
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, ".write_test")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            STATE_FILE = os.path.join(d, "voonix_state.json")
            return STATE_FILE
        except Exception:
            continue
    STATE_FILE = "/tmp/voonix_state.json"
    return STATE_FILE


def parse_num(val, typ=float):
    """EU/US-aware numeric parse (copied from the AI-agent voonix router so EUR
    amounts like '1.234,56' don't get mangled)."""
    s = str(val).strip().replace("€", "").replace("$", "").replace("\xa0", "").replace(" ", "")
    if not s:
        return typ(0)
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        s = s.replace(",", ".") if re.fullmatch(r"-?\d+,\d{1,2}", s) else s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return typ(0)
    return int(f) if typ is int else float(f)


def utc_dates(lookback_days: int) -> list[str]:
    """Today and the trailing days, UTC, newest first."""
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(max(1, lookback_days))]


def _get_2fa_code(timeout=90, not_before: float | None = None) -> str:
    """Read the 6-digit Voonix login code from Gmail (same logic as statparser:
    only accepts a code newer than the login click, so overlapping logins don't
    grab a stale code)."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(config.GMAIL_USER, config.GMAIL_APP_PASS)
    mail.select("inbox")
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, data = mail.search(None, "FROM", '"no-reply@voonix.net"')
        for mid in reversed(data[0].split()):
            _, msg_data = mail.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            if not_before is not None:
                try:
                    sent = parsedate_to_datetime(msg.get("Date"))
                    if sent and sent.timestamp() < not_before - 10:
                        continue
                except Exception:
                    pass
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html"):
                        body = part.get_payload(decode=True).decode(errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")
            codes = re.findall(r"\b(\d{6})\b", body)
            if codes:
                mail.store(mid, "+FLAGS", "\\Seen")
                mail.logout()
                return codes[0]
        time.sleep(5)
    mail.logout()
    raise RuntimeError("2FA code not received within 90s")


async def _looks_logged_in(page) -> bool:
    """We're authed if there's no password field and no 'verification' prompt."""
    if await page.query_selector('input[name="password"]'):
        return False
    body = (await page.inner_text("body")).lower()
    if "erification" in body or "login" == (await page.title() or "").lower():
        return False
    return True


async def _full_login(page):
    print("🔐 Full login (2FA)...")
    await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    await page.fill('input[name="username"]', config.VOONIX_USER)
    await page.fill('input[name="password"]', config.VOONIX_PASS)
    login_ts = time.time()
    await page.click('input[type="submit"][value="Login"]')
    await page.wait_for_timeout(5000)
    body = await page.inner_text("body")
    if "erification" in body:
        code = _get_2fa_code(not_before=login_ts)
        await page.locator('input[type="text"]').first.fill(code)
        await page.click('input[type="submit"], button[type="submit"]')
        await page.wait_for_timeout(5000)
    print(f"✅ Logged in | {page.url}")


async def _navigate(page, params: str):
    # Absolute URL — a relative '/?...' can't be resolved when the page is still
    # on about:blank (first probe before login), which threw a DOMException.
    url = config.BASE_URL.rstrip("/") + "/" + params.lstrip("/")
    await page.evaluate("(u) => { window.location.href = u; }", url)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    await page.wait_for_timeout(1000)


async def _download_l1_csv(page, site_id: str, date_str: str, bust: bool = False) -> tuple[list[str], list[list[str]]] | None:
    """Download the L1 (per-advertiser) earnings CSV for one site+day."""
    params = f"?p=siteearnings&start={date_str}&end={date_str}&site={site_id}&&submit"
    path = f"{DOWNLOAD_DIR}/l1_{site_id}_{date_str}.csv"
    # Voonix renders the CSV button as <a class="buttons-csv …"> — we accept any
    # element whose class list contains "buttons-csv" (DataTables standard class).
    CSV_SELECTOR = "a.buttons-csv, button.buttons-csv"
    for attempt in range(3):
        try:
            await _navigate(page, params)
            # Fresh data before reading: log cache age and (for recent days) clear it.
            await _handle_cache(page, bust=bust)
            if bust:
                await _navigate(page, params)  # reload the now-uncached report
            await page.wait_for_selector(CSV_SELECTOR, timeout=20000)
            async with page.expect_download(timeout=30000) as dl:
                await page.evaluate(
                    f"() => document.querySelector('{CSV_SELECTOR}')"
                    ".dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}))"
                )
            download = await dl.value
            await download.save_as(path)
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            if not rows:
                return None
            return rows[0], rows[1:]
        except Exception as e:
            print(f"   ⚠️ L1 retry {attempt+1}/3 (site={site_id} {date_str}): {e}")
            await page.wait_for_timeout(2500)
    # Final failure — dump page HTML so we can see what Voonix actually showed.
    try:
        html = await page.content()
        dump = f"{DOWNLOAD_DIR}/debug_{site_id}_{date_str}.html"
        with open(dump, "w", encoding="utf-8") as f:
            f.write(html)
        # Print a compact diagnostic: title + all button/link texts on the page.
        title = await page.title()
        btns = await page.evaluate(
            "() => [...document.querySelectorAll('a,button')].map(e=>e.className+' | '+e.innerText.trim()).filter(t=>t.length<120)"
        )
        print(f"   🔍 Page title: '{title}'")
        print(f"   🔍 Buttons/links on page ({len(btns)} total):")
        for b in btns[:30]:
            print(f"       {b}")
        print(f"   🔍 Full HTML saved to {dump}")
    except Exception as de:
        print(f"   🔍 Debug dump failed: {de}")
    print(f"   ❌ No L1 CSV (site={site_id} {date_str})")
    return None


async def _handle_cache(page, bust: bool):
    """Log Voonix's cache age and, when `bust`, clear it so we read fresh data.

    Voonix caches the siteearnings report server-side ("Cache active - created N
    ago" banner). A frozen cache is why intraday FTD rises are invisible. We
    always LOG the age (proves freshness in the deploy logs); when busting we
    click the on-page "Clear cache" control and reload. All best-effort — never
    raises, so a UI change can't break the scrape (it just logs and continues)."""
    try:
        age = await page.evaluate(
            """() => {
                const el = [...document.querySelectorAll('*')]
                  .find(e => /cache active/i.test(e.textContent||'') && e.children.length < 3);
                return el ? el.textContent.replace(/\\s+/g,' ').trim().slice(0,80) : null;
            }"""
        )
        if age:
            print(f"      🕒 {age}")
    except Exception:
        pass
    if not bust:
        return
    try:
        clicked = await page.evaluate(
            """() => {
                const el = [...document.querySelectorAll('a,button,input')]
                  .find(e => /clear cache/i.test((e.textContent||'') + ' ' + (e.value||'')));
                if (el) { el.click(); return true; }
                return false;
            }"""
        )
        if clicked:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
    except Exception as e:
        print(f"      ⚠️ cache clear skipped: {e}")


def _col(header: list[str], *names: str) -> int:
    """Index of the first header cell matching any of `names` (case-insensitive)."""
    norm = [h.strip().lower() for h in header]
    for n in names:
        if n.lower() in norm:
            return norm.index(n.lower())
    return -1


def _parse_brand_rows(header: list[str], rows: list[list[str]]) -> list[dict]:
    """Turn an L1 CSV into [{brand, ftd, signups, deposits, deposit_value}] per
    advertiser. Columns are matched by NAME (Voonix's column picker reorders)."""
    i_name = 0  # first column is always the advertiser name
    i_ftd  = _col(header, "FTD")
    i_su   = _col(header, "Signups", "Sign ups", "Sign-ups")
    i_dep  = _col(header, "Deposits")
    i_depv = _col(header, "Deposit value")
    out = []
    for r in rows:
        if not any(c.strip() for c in r):
            continue
        first = r[i_name].strip().lower() if r else ""
        if first in ("site", "advertiser", "account", "campaign", "login", ""):
            continue  # skip the repeated header / totals row
        brand = r[i_name].strip()
        ftd = parse_num(r[i_ftd], int) if 0 <= i_ftd < len(r) else 0
        out.append({
            "brand": brand,
            "ftd": ftd,
            "signups": parse_num(r[i_su], int) if 0 <= i_su < len(r) else 0,
            "deposits": parse_num(r[i_dep], int) if 0 <= i_dep < len(r) else 0,
            "deposit_value": parse_num(r[i_depv], float) if 0 <= i_depv < len(r) else 0.0,
        })
    return out


async def _discover_sites(page, date_str: str) -> list[tuple[str, str]]:
    """Read every site (id + display name) from the all-sites earnings table.

    The site names are links carrying `?...&site=<id>`. We scan them, dedupe by
    id and drop numeric-only labels, so new traffic sources are picked up
    automatically without anyone editing the SITES env var.
    """
    await _navigate(page, f"?p=siteearnings&start={date_str}&end={date_str}&&submit")
    try:
        await page.wait_for_selector("a.buttons-csv, table tbody tr", timeout=20000)
    except Exception:
        pass
    found = await page.evaluate(
        """
        () => {
            const out = {};
            document.querySelectorAll('a[href*="site="]').forEach(a => {
                const m = a.href.match(/[?&]site=(\\d+)/);
                const name = (a.textContent || '').trim();
                // Skip drilldown links (they also carry adve=/login=) and blanks.
                if (m && name && !/[?&](adve|login)=/.test(a.href) && !/^\\d+$/.test(name)) {
                    out[m[1]] = name;
                }
            });
            return out;
        }
        """
    )
    return [(sid, name) for sid, name in found.items()]


def _ts_params(site_id: str, adve_id: str | None, login_id: str | None, date_str: str) -> str:
    s = f"?p=siteearnings&start={date_str}&end={date_str}&site={site_id}"
    if adve_id:
        s += f"&adve={adve_id}"
    if login_id:
        s += f"&login={login_id}"
    return s + "&&submit"


async def _get_link_ids(page, params: str, key: str) -> list[tuple[str, str]]:
    """Extract unique (id, name) for every link carrying `key`=<digits>
    (key is 'adve' or 'login'). Waits for DataTables to finish rendering, then
    scans ALL anchors on the page. Logs a diagnostic line when it finds nothing,
    so the deploy logs reveal whether it's a render/timing or a selector issue."""
    await _navigate(page, params)
    # The table (and its CSV button) is injected by DataTables AFTER load, so an
    # immediate query can see an empty tbody. Wait for the button to appear.
    try:
        await page.wait_for_selector("a.buttons-csv, button.buttons-csv", timeout=20000)
    except Exception:
        pass
    info = await page.evaluate(
        """
        (key) => {
            const re = new RegExp('[?&]' + key + '=(\\\\d+)');
            const ids = [];
            const sample = [];
            document.querySelectorAll('a').forEach(a => {
                const href = a.href || '';
                const m = href.match(re);
                if (m) ids.push({id: m[1], name: (a.textContent || '').trim()});
                else if (href.indexOf('siteearnings') !== -1 && sample.length < 6)
                    sample.push(href);
            });
            return {
                rows: document.querySelectorAll('table tbody tr').length,
                anchors: document.querySelectorAll('a').length,
                ids: ids,
                sample: sample,
            };
        }
        """,
        key,
    )
    seen, out = set(), []
    for x in info["ids"]:
        if x["id"] not in seen:
            seen.add(x["id"])
            out.append((x["id"], x["name"]))
    if not out:
        print(f"      ⚠️ {key}: 0 links (tbody rows={info['rows']}, "
              f"anchors={info['anchors']}) :: {params}")
        for h in info.get("sample", []):
            print(f"         sample href: {h}")
    return out


async def _download_csv(page, params: str) -> tuple[list[str], list[list[str]]] | None:
    """Navigate to `params` and download its CSV. Returns (header, rows) or None.
    Never raises on transient errors so one bad node can't kill a long run."""
    path = f"{DOWNLOAD_DIR}/lvl.csv"
    for attempt in range(3):
        try:
            await _navigate(page, params)
            await page.wait_for_selector("a.buttons-csv, button.buttons-csv", timeout=20000)
            async with page.expect_download(timeout=30000) as dl:
                await page.evaluate(
                    "() => document.querySelector('a.buttons-csv, button.buttons-csv')"
                    ".dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}))"
                )
            download = await dl.value
            await download.save_as(path)
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            return (rows[0], rows[1:]) if rows else None
        except Exception:
            await page.wait_for_timeout(2500)
    return None


async def _open_logged_in(p, probe_date: str):
    """Launch a browser, restore the saved session (or log in), return handles.
    Used by the tracker scraper, which runs on its own thread — so it keeps a
    SEPARATE session-state file to avoid racing the brand poller's writes."""
    state_path = os.path.join(os.path.dirname(_state_file()), "voonix_state_trackers.json")
    have_state = os.path.exists(state_path)
    browser = await p.chromium.launch(
        headless=config.HEADLESS, args=["--no-sandbox", "--disable-setuid-sandbox"]
    )
    ctx_kwargs = dict(
        accept_downloads=True,
        viewport={"width": 1280, "height": 900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    )
    if have_state:
        ctx_kwargs["storage_state"] = state_path
    context = await browser.new_context(**ctx_kwargs)
    page = await context.new_page()
    page.set_default_timeout(60000)
    await _navigate(page, f"?p=siteearnings&start={probe_date}&end={probe_date}&&submit")
    try:
        await page.wait_for_selector(
            'a.buttons-csv, button.buttons-csv, input[name="password"]', timeout=25000
        )
    except Exception:
        pass
    if await page.query_selector('input[name="password"]'):
        print("🔓 No active session — logging in.")
        await _full_login(page)
        try:
            await context.storage_state(path=state_path)
        except Exception as e:
            print(f"   ⚠️ Could not save session state: {e}")
    else:
        print("🔑 Existing session is valid — skipping login.")
    return browser, context, page, state_path


async def scrape_trackers_once(dates: list[str] | None = None,
                               sites: list[tuple[str, str]] | None = None) -> list[dict]:
    """Deep campaign-level scrape: for each tracker-site and day, walk
    advertisers → logins → campaigns and return campaign rows.
    Each item: {date, site_id, site_label, campaign, ftd, signups, deposits, deposit_value}.
    This is heavy (~150 requests/day per site) — call it on a slow cadence only."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    global _TRK_CSV_SAMPLED
    dates = dates or utc_dates(config.LOOKBACK_DAYS)
    sites = sites if sites is not None else config.TRACKER_SITES
    if not sites:
        return []
    results: list[dict] = []
    async with async_playwright() as p:
        browser, context, page, state_path = await _open_logged_in(p, dates[0])
        for site_id, site_label in sites:
            for date_str in dates:
                adve_list = await _get_link_ids(page, _ts_params(site_id, None, None, date_str), "adve")
                print(f"   📅 {date_str} {site_label}: {len(adve_list)} advertisers")
                for adve_id, adve_name in adve_list:
                    login_list = await _get_link_ids(
                        page, _ts_params(site_id, adve_id, None, date_str), "login")
                    for login_id, _ in login_list:
                        got = await _download_csv(
                            page, _ts_params(site_id, adve_id, login_id, date_str))
                        if not got:
                            continue
                        header, rows = got
                        if not _TRK_CSV_SAMPLED and rows:
                            _TRK_CSV_SAMPLED = True
                            print(f"   🧪 L3 CSV header: {header}")
                            for sr in rows[:3]:
                                print(f"   🧪 L3 row[0]={sr[0]!r}  cells={sr[:6]}")
                        for r in _parse_brand_rows(header, rows):  # row[0] = campaign name
                            results.append({
                                "date": date_str, "site_id": site_id, "site_label": site_label,
                                "campaign": r["brand"], "brand": adve_name,
                                "ftd": r["ftd"], "signups": r["signups"],
                                "deposits": r["deposits"], "deposit_value": r["deposit_value"],
                            })
        try:
            await context.storage_state(path=state_path)
        except Exception:
            pass
        await browser.close()
    return results


async def scrape_once(dates: list[str] | None = None) -> list[dict]:
    """One poll: for every configured site and lookback day, return brand rows.
    Each item: {date, site_id, site_label, brand, ftd, deposits, deposit_value}.
    Reuses a saved session; re-auths (2FA) only if Voonix expired it.
    Pass `dates` to scrape an explicit list (used for the one-time backfill)."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dates = dates or utc_dates(config.LOOKBACK_DAYS)
    state_path = _state_file()
    have_state = os.path.exists(state_path)
    results: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=config.HEADLESS, args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        ctx_kwargs = dict(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        if have_state:
            ctx_kwargs["storage_state"] = state_path
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        page.set_default_timeout(60000)

        # Establish a logged-in session. Navigate to the all-sites earnings page,
        # then wait for EITHER the CSV export button (already authenticated) OR the
        # login form's password field (session dead / first run). This removes a
        # redirect race where the old probe was checked before Voonix finished
        # bouncing us to the login page, which silently skipped the login.
        probe = f"?p=siteearnings&start={dates[0]}&end={dates[0]}&&submit"
        await _navigate(page, probe)
        try:
            await page.wait_for_selector(
                'a.buttons-csv, button.buttons-csv, input[name="password"]', timeout=25000
            )
        except Exception:
            pass
        if await page.query_selector('input[name="password"]'):
            print("🔓 No active session — logging in.")
            await _full_login(page)
            try:
                await context.storage_state(path=state_path)
                print(f"💾 Session saved → {state_path}")
            except Exception as e:
                print(f"   ⚠️ Could not save session state: {e}")
        else:
            print("🔑 Existing session is valid — skipping login.")

        # Decide which sites to scrape. By default, auto-discover EVERY site from
        # the all-sites table so new traffic sources are picked up without editing
        # SITES. Fall back to the configured list if discovery fails or looks wrong.
        sites = config.SITES
        if config.AUTO_DISCOVER_SITES:
            try:
                discovered = await _discover_sites(page, dates[0])
                configured_ids = {sid for sid, _ in config.SITES}
                discovered_ids = {sid for sid, _ in discovered}
                if discovered and configured_ids.issubset(discovered_ids):
                    sites = discovered
                    print(f"🧭 Auto-discovered {len(sites)} sites: {[s[1] for s in sites]}")
                else:
                    print(f"🧭 Discovery found {sorted(discovered_ids)} but it's missing "
                          f"configured {sorted(configured_ids)} — using SITES instead.")
            except Exception as e:
                print(f"🧭 Auto-discover failed ({e}) — using configured SITES.")

        # Only bust the cache for recent days (today/yesterday) — those change
        # intraday. Settled/backfill days are read from cache (fast, harmless).
        recent = set(utc_dates(2))
        for site_id, site_label in sites:
            for date_str in dates:
                bust = config.BUST_VOONIX_CACHE and date_str in recent
                got = await _download_l1_csv(page, site_id, date_str, bust=bust)
                if not got:
                    continue
                header, rows = got
                for row in _parse_brand_rows(header, rows):
                    row.update({"date": date_str, "site_id": site_id, "site_label": site_label})
                    results.append(row)

        # Refresh the saved cookies so the session keeps rolling.
        try:
            await context.storage_state(path=state_path)
        except Exception:
            pass
        await browser.close()
    return results


if __name__ == "__main__":
    # Manual smoke test: print what we'd see right now.
    for r in asyncio.run(scrape_once()):
        if r["ftd"]:
            print(r)
