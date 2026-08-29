"""
Mr Wall Street — #economic-data bot
Posts the week's economic calendar (high/medium impact USD events) every Sunday,
using the free ForexFactory weekly feed.

Required env vars:
  DISCORD_WEBHOOK_ECONOMIC_DATA — Discord webhook URL for the #economic-data channel

Optional env vars:
  ECON_CURRENCIES — comma-separated list (default "USD")
  ECON_IMPACTS    — comma-separated list (default "High,Medium")
"""
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# CryptoCraft (same company) if you ever want a crypto-events channel:
#   https://nfs.faireconomy.media/cc_calendar_thisweek.json

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_ECONOMIC_DATA", "").strip()
CURRENCIES = {c.strip().upper() for c in os.environ.get("ECON_CURRENCIES", "USD").split(",")}
IMPACTS = {i.strip().title() for i in os.environ.get("ECON_IMPACTS", "High,Medium").split(",")}

IMPACT_EMOJI = {"High": "🔴", "Medium": "🟠", "Low": "🟡"}


def fetch_events():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "Mozilla/5.0 (MrWallStreetBot)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def format_week(events) -> list[str]:
    by_day = defaultdict(list)
    for ev in events:
        if ev.get("country", "").upper() not in CURRENCIES:
            continue
        if ev.get("impact", "").title() not in IMPACTS:
            continue
        # dates look like "2026-08-31T08:30:00-04:00" (US Eastern)
        try:
            dt = datetime.fromisoformat(ev["date"])
        except (KeyError, ValueError):
            continue
        by_day[dt.date()].append((dt, ev))

    lines = ["📅 **Economic Calendar — Week Ahead** (times: US Eastern)", ""]
    for day in sorted(by_day):
        lines.append(f"**{day.strftime('%A, %b %d')}**")
        for dt, ev in sorted(by_day[day], key=lambda x: x[0]):
            emoji = IMPACT_EMOJI.get(ev.get("impact", "").title(), "⚪")
            name = ev.get("title", "Unknown event")
            t = dt.strftime("%H:%M")
            extra = []
            if ev.get("forecast"):
                extra.append(f"forecast {ev['forecast']}")
            if ev.get("previous"):
                extra.append(f"prev {ev['previous']}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"{emoji} `{t}` {name}{suffix}")
        lines.append("")
    return lines


def post_to_discord(content: str):
    payload = {
        "username": "Mr Wall Street — Markets",
        "content": content[:2000],  # Discord hard limit per message
        "allowed_mentions": {"parse": []},
    }
    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "MrWallStreetBot"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    if not WEBHOOK:
        sys.exit("Missing DISCORD_WEBHOOK_ECONOMIC_DATA env var")

    events = fetch_events()
    lines = format_week(events)
    if len(lines) <= 2:
        print("No matching events this week.")
        return

    # split into <=2000-char chunks on line boundaries
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        status = post_to_discord(chunk)
        print(f"Posted chunk (HTTP {status}).")


if __name__ == "__main__":
    main()
