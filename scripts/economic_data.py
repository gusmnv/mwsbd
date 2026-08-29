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
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_ECONOMIC_DATA", "").strip()
IMPACTS = {i.strip().title() for i in os.environ.get("ECON_IMPACTS", "High,Medium").split(",")}


def ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def fetch_events():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "Mozilla/5.0 (MrWallStreetBot)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


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


W_T, W_C, W_I, W_F, W_P = 7, 3, 6, 8, 8
TABLE_WIDTH = W_T + W_C + W_I + W_F + W_P + 4 * 2


def _C(s, w):
    s = str(s)
    if len(s) > w:
        s = s[: w - 1] + "…"
    return s.center(w)


def _L(s, w):
    s = str(s)
    if len(s) > w:
        s = s[: w - 1] + "…"
    return s.ljust(w)


def day_table_message(day, items) -> str:
    """Two lines per event, <=40 chars wide so phones never wrap:
       time/cur/impact/forecast/previous row + indented event-name row."""
    title = (f"__**{day.strftime('%A')}, {ordinal(day.day)} {day.strftime('%B')}**__"
             f"  ·  times ET (NY)")
    header = (_L("TIME", W_T) + "  " + _L("CUR", W_C) + "  " + _C("IMPACT", W_I)
              + "  " + _C("FORECAST", W_F) + "  " + _C("PREV", W_P))
    sep = "-" * TABLE_WIDTH
    lines = [header, sep]
    for dt, ev in items:
        t = dt.strftime("%-I:%M%p").lower() if (dt.hour or dt.minute) else "All day"
        lines.append(_L(t, W_T) + "  " + _L(ev.get("country", "").upper(), W_C) + "  "
                     + _C(ev.get("impact", "").title(), W_I) + "  "
                     + _C(ev.get("forecast") or "—", W_F) + "  "
                     + _C(ev.get("previous") or "—", W_P))
        lines.append("  " + str(ev.get("title", ""))[:38])
        lines.append("")
    while lines and not lines[-1].strip():
        lines.pop()
    return title + "\n```\n" + "\n".join(lines) + "\n```"


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
    payload = {"username": "Mr Wall Street — Markets",
               "content": content[:2000], "allowed_mentions": {"parse": []}}
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
    by_day = parse(fetch_events())
    if not by_day:
        print("No matching events this week.")
        return
    days = sorted(by_day)
    post_text(f"📅 **ECONOMIC CALENDAR — WEEK AHEAD** "
              f"({days[0].strftime('%b %d')} – {days[-1].strftime('%b %d')})  ·  All times ET (New York)")
    time.sleep(1)
    for day in days:
        for chunk in split_message(day_table_message(day, by_day[day])):
            status = post_text(chunk)
            print(f"Posted {day} chunk (HTTP {status}).")
            time.sleep(1)


if __name__ == "__main__":
    main() main()
