"""
Mr Wall Street — #economic-data bot
Sunday: posts the week's economic calendar as clean centered text tables
(code blocks — they scroll horizontally on mobile, never wrap).
Source: free ForexFactory weekly feed (times are US Eastern / New York).

Columns: TIME (ET) · CUR · IMPACT · EVENT · FORECAST · PREVIOUS
Filter: High + Medium impact, ALL currencies.

Required env var:
  DISCORD_WEBHOOK_ECONOMIC_DATA — webhook URL of the #economic-data channel
Optional:
  ECON_IMPACTS — default "High,Medium"
"""
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEXTWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_ECONOMIC_DATA", "").strip()
IMPACTS = {i.strip().title() for i in os.environ.get("ECON_IMPACTS", "High,Medium").split(",")}


def ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def fetch_events(retries=3, url=FEED_URL, soft=False):
    """soft=True: return None on failure instead of raising (the nextweek feed
    404s during ForexFactory's weekend rollover - that must never crash us)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (MrWallStreetBot)"})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
    if soft:
        print(f"[warn] feed fetch failed ({url.rsplit('/',1)[-1]}): {last}")
        return None
    raise last


def parse(events):
    """-> {date: [(datetime_et, event_dict)]} filtered by impact, sorted by time."""
    by_day = defaultdict(list)
    for ev in events:
        if ev.get("impact", "").title() not in IMPACTS:
            continue
        try:
            dt = datetime.fromisoformat(ev["date"])  # feed times are US Eastern
        except (KeyError, ValueError):
            continue
        by_day[dt.date()].append((dt, ev))
    for day in by_day:
        by_day[day].sort(key=lambda x: x[0])
    return by_day


# Fixed column widths (same for every day so all tables share the same right edge)
# headers centered; text values left-aligned; numeric values right-aligned
COLS = [("TIME", 10, "r"), ("CURRENCY", 8, "c"), ("IMPACT", 6, "c"),
        ("EVENT", 34, "c"), ("FORECAST", 8, "r"), ("PREVIOUS", 8, "r")]
GAP = "    "
TABLE_WIDTH = sum(w for _, w, _ in COLS) + len(GAP) * (len(COLS) - 1)


def _cell(s, w, a):
    s = str(s)
    if len(s) > w:
        s = s[: w - 1] + "…"
    if a == "l":
        return s.ljust(w)
    if a == "c":
        return s.center(w)
    return s.rjust(w)


def _cells(dt, ev):
    t = (dt.strftime("%-I:%M%p").lower() + " ET") if (dt.hour or dt.minute) else "All day"
    return [t,
            ev.get("country", "").upper(),
            ev.get("impact", "").title(),
            str(ev.get("title", "")),
            str(ev.get("forecast") or "—"),
            str(ev.get("previous") or "—")]


def day_table_message(day, items) -> str:
    title = f"__**{day.strftime('%A')}, {ordinal(day.day)} {day.strftime('%B')}**__"
    header = GAP.join(str(h).center(w) for h, w, _ in COLS)
    sep = "─" * TABLE_WIDTH
    lines = []
    for dt, ev in items:
        lines.append(GAP.join(_cell(c, w, a) for c, (_, w, a) in zip(_cells(dt, ev), COLS)))
    return title + "\n```\n" + header + "\n" + sep + "\n" + "\n".join(lines) + "\n```"




# ---- daily / weekly recap (uses actuals captured by economic_live.py) ---------
ACTUALS_FILE = __import__("pathlib").Path(__file__).resolve().parent.parent / "state" / "econ_actuals.json"

RCOLS = [("TIME", 10, "r"), ("CURRENCY", 8, "c"), ("EVENT", 34, "c"),
         ("FORECAST", 8, "r"), ("ACTUAL", 8, "r")]
RWIDTH = sum(w for _, w, _ in RCOLS) + len(GAP) * (len(RCOLS) - 1)


def load_actuals():
    try:
        return json.loads(ACTUALS_FILE.read_text())
    except Exception:
        return {}


def backfill_actuals(day, items, actuals):
    """For events still missing an actual, ask the official APIs directly at
    recap time (BLS/BEA published hours ago even if the live session missed its
    window). Covers USD events with handlers + CAD Overnight Rate via the Bank
    of Canada Valet API. Best-effort: any failure leaves the dash in place."""
    try:
        import economic_live as L
    except Exception as e:
        print(f"[warn] backfill unavailable: {e}")
        return actuals
    for dt, ev in items:
        cur = ev.get("country", "").upper()
        title = str(ev.get("title", ""))
        key = f"{day.isoformat()}|{cur}|{title}"
        if actuals.get(key):
            continue
        try:
            if cur == "USD":
                h = L.match_handler(title)
                if not h or isinstance(h, L.FedHandler):
                    continue
                r = h.value_latest() if hasattr(h, "value_latest") else None
                if r is not None:
                    actuals[key] = L.fmt_actual(*r)
                    print(f"backfilled {title}: {actuals[key]}")
            elif cur == "CAD" and "Overnight Rate" in title:
                raw = urllib.request.urlopen(urllib.request.Request(
                    "https://www.bankofcanada.ca/valet/observations/V39079/json?recent=1",
                    headers={"User-Agent": "MrWallStreetBot"}), timeout=20).read()
                obs = json.loads(raw)["observations"]
                if obs:
                    actuals[key] = f"{float(obs[-1]['V39079']['v']):.2f}%"
                    print(f"backfilled {title}: {actuals[key]}")
        except Exception as e:
            print(f"[warn] backfill {title}: {e}")

    # ---- FMP fallback: global actuals (any currency) for whatever is still
    # missing — ISM, Claims, CHF CPI, EUR/GBP/JPY events, tudo. ----
    try:
        import fmp
        from datetime import timedelta as _tdf, timezone as _tzf
        missing = [(dt, ev) for dt, ev in items if not actuals.get(
            f"{day.isoformat()}|{ev.get('country', '').upper()}|{ev.get('title', '')}")]
        if missing and fmp.API_KEY:
            rows = fmp.fetch((day - _tdf(days=1)).isoformat(), (day + _tdf(days=1)).isoformat())
            for dt, ev in missing:
                cur = ev.get("country", "").upper()
                title = str(ev.get("title", ""))
                key = f"{day.isoformat()}|{cur}|{title}"
                try:
                    when = dt.astimezone(_tzf.utc)
                except Exception:
                    when = None
                a, _est = fmp.find_actual(rows, title, cur, when_utc=when)
                if a is not None:
                    actuals[key] = fmp.fmt_like(a, ev.get("forecast"))
                    print(f"backfilled via FMP {title}: {actuals[key]}")
    except Exception as e:
        print(f"[warn] FMP backfill: {e}")
    return actuals


def recap_table_message(day, items, actuals) -> str:
    title = f"__**{day.strftime('%A')}, {ordinal(day.day)} {day.strftime('%B')}**__"
    header = GAP.join(str(h).center(w) for h, w, _ in RCOLS)
    sep = "─" * RWIDTH
    lines = []
    for dt, ev in items:
        t = (dt.strftime("%-I:%M%p").lower() + " ET") if (dt.hour or dt.minute) else "All day"
        key = f"{day.isoformat()}|{ev.get('country','').upper()}|{ev.get('title','')}"
        cells = [t, ev.get("country", "").upper(), str(ev.get("title", "")),
                 str(ev.get("forecast") or "—"), str(actuals.get(key, "—"))]
        lines.append(GAP.join(_cell(c, w, a) for c, (_, w, a) in zip(cells, RCOLS)))
    return title + "\n```\n" + header + "\n" + sep + "\n" + "\n".join(lines) + "\n```"


def split_message(msg: str) -> list[str]:
    """Split an over-long day table into <=1900-char chunks, repeating the fence."""
    if len(msg) <= 1900:
        return [msg]
    head, _, body = msg.partition("```\n")
    body = body.rsplit("```", 1)[0]
    lines = body.split("\n")
    header, sep, rows = lines[0], lines[1], lines[2:]
    out, batch = [], []
    first = True
    for row in rows:
        batch.append(row)
        if sum(len(r) + 1 for r in batch) > 1500:
            prefix = head if first else ""
            out.append(prefix + "```\n" + header + "\n" + sep + "\n" + "\n".join(batch) + "\n```")
            first = False
            batch = []
    if batch:
        prefix = head if first else ""
        out.append(prefix + "```\n" + header + "\n" + sep + "\n" + "\n".join(batch) + "\n```")
    return out


def post_text(content: str):
    payload = {"content": content[:2000], "allowed_mentions": {"parse": []}}
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "MrWallStreetBot"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    if not WEBHOOK:
        sys.exit("Missing DISCORD_WEBHOOK_ECONOMIC_DATA env var")
    mode = sys.argv[1] if len(sys.argv) > 1 else "week"
    by_day = parse(fetch_events())
    if not by_day:
        print("No matching events this week.")
        return

    if mode == "today":
        # HORARIO E CHAVE: the calendar day is the UTC day (posted at 00:00 UTC
        # sharp). The +30min bias guards the just-before-midnight dispatch race,
        # so a run starting 23:5x still posts the NEW day's calendar.
        from datetime import timedelta as _td, timezone as _tz
        today_utc = (datetime.now(_tz.utc) + _td(minutes=30)).date()
        if today_utc not in by_day:
            # UTC day may live in next week's ET feed (Sunday 00:00 UTC case)
            nb_events = fetch_events(retries=1, url=NEXTWEEK_URL, soft=True)
            by_day = parse(nb_events) if nb_events else {}
            if today_utc not in by_day:
                print("No events today.")
                return
        post_text("**TODAY'S CALENDAR**")
        time.sleep(1)
        for chunk in split_message(day_table_message(today_utc, by_day[today_utc])):
            status = post_text(chunk)
            print(f"Posted today chunk (HTTP {status}).")
            time.sleep(1)
        return

    if mode == "tomorrow":
        from zoneinfo import ZoneInfo
        from datetime import timedelta as _td
        # runs in the US evening: "tomorrow" = the ET day after the anchored business day
        base = (datetime.now(ZoneInfo("America/New_York")) - _td(hours=8)).date()
        target = base + _td(days=1)
        if target not in by_day:
            print("No events tomorrow.")
            return
        post_text("**TOMORROW'S CALENDAR**")
        time.sleep(1)
        for chunk in split_message(day_table_message(target, by_day[target])):
            print(f"Posted tomorrow chunk (HTTP {post_text(chunk)}).")
            time.sleep(1)
        return

    if mode in ("recap", "weekrecap"):
        from zoneinfo import ZoneInfo
        from datetime import timedelta as _td
        actuals = load_actuals()
        # anchored: intended post ~21:05 ET. A late start past midnight ET
        # must still recap the day we were meant to recap, not tomorrow.
        today_et = (datetime.now(ZoneInfo("America/New_York")) - _td(hours=8)).date()
        if mode == "recap":
            if today_et not in by_day:
                print("No events today.")
                return
            actuals = backfill_actuals(today_et, by_day[today_et], actuals)
            try:
                ACTUALS_FILE.parent.mkdir(parents=True, exist_ok=True)
                ACTUALS_FILE.write_text(json.dumps(actuals, indent=0))
            except Exception:
                pass
            post_text("**DAILY RECAP**")
            time.sleep(1)
            for chunk in split_message(recap_table_message(today_et, by_day[today_et], actuals)):
                print(f"Posted recap (HTTP {post_text(chunk)}).")
                time.sleep(1)
        else:
            monday = today_et - __import__("datetime").timedelta(days=today_et.weekday())
            days = [d for d in sorted(by_day) if monday <= d <= today_et]
            if not days:
                print("No events this week.")
                return
            for day in days:  # fill any hole in the week before posting
                actuals = backfill_actuals(day, by_day[day], actuals)
            try:
                ACTUALS_FILE.parent.mkdir(parents=True, exist_ok=True)
                ACTUALS_FILE.write_text(json.dumps(actuals, indent=0))
            except Exception:
                pass
            post_text("**WEEKLY RECAP**")
            time.sleep(1)
            for day in days:
                for chunk in split_message(recap_table_message(day, by_day[day], actuals)):
                    print(f"Posted {day} recap (HTTP {post_text(chunk)}).")
                    time.sleep(1)
        return

    # Weekly calendar posts at 00:00 UTC Sunday (= Saturday evening ET), when
    # the "thisweek" feed still holds the ENDING week - roll to the nextweek
    # feed. During ForexFactory's weekend rollover the nextweek file can 404
    # for a while, so retry patiently (up to ~30 min) instead of crashing.
    from datetime import timedelta as _tdw, timezone as _tzw
    _today_utc = (datetime.now(_tzw.utc) + _tdw(minutes=30)).date()
    if not by_day or max(by_day) <= _today_utc:
        fresh = None
        for attempt in range(30):
            nb_events = fetch_events(retries=1, url=NEXTWEEK_URL, soft=True)
            nb = parse(nb_events) if nb_events else {}
            if nb and max(nb) > _today_utc:
                fresh = nb
                break
            tw_events = fetch_events(retries=1, soft=True)   # thisweek may have rolled
            tw = parse(tw_events) if tw_events else {}
            if tw and max(tw) > _today_utc:
                fresh = tw
                break
            print(f"[warn] feeds still show the old week (attempt {attempt+1}/30) - retrying in 60s")
            time.sleep(60)
        if fresh:
            by_day = fresh
        else:
            print("[warn] feeds never rolled over - posting what we have.")

    days = sorted(by_day)
    post_text("**WEEKLY CALENDAR**")
    time.sleep(1)
    for day in days:
        for chunk in split_message(day_table_message(day, by_day[day])):
            status = post_text(chunk)
            print(f"Posted {day} chunk (HTTP {status}).")
            time.sleep(1)


if __name__ == "__main__":
    main()
