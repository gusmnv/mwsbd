"""
Mr Wall Street — #economic-data bot
Sunday: posts the week's economic calendar as one clean table image per day.
Source: free ForexFactory weekly feed (times are US Eastern).

Columns: Time (ET) · Cur · Importance · Event · Forecast · Previous
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
from pathlib import Path

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_ECONOMIC_DATA", "").strip()
IMPACTS = {i.strip().title() for i in os.environ.get("ECON_IMPACTS", "High,Medium").split(",")}

DEJAVU = "/usr/share/fonts/truetype/dejavu"
PLEX = Path(__file__).resolve().parent.parent / "fonts" / "IBMPlexSans.ttf"

IMPACT_COLOR = {"High": (200, 42, 42), "Medium": (223, 138, 21), "Low": (150, 158, 168)}


def _font(size: int, weight: int):
    from PIL import ImageFont
    if PLEX.exists():
        try:
            f = ImageFont.truetype(str(PLEX), size)
            f.set_variation_by_axes([weight, 100])  # [Weight, Width]
            return f
        except Exception:
            pass
    name = "DejaVuSans-Bold.ttf" if weight >= 600 else "DejaVuSans.ttf"
    from PIL import ImageFont
    return ImageFont.truetype(f"{DEJAVU}/{name}", size)


def ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def fetch_events():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "Mozilla/5.0 (MrWallStreetBot)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def parse(events):
    """-> {date: [(datetime_et, event_dict)]} filtered by impact."""
    by_day = defaultdict(list)
    for ev in events:
        if ev.get("impact", "").title() not in IMPACTS:
            continue
        try:
            dt = datetime.fromisoformat(ev["date"])  # already US Eastern
        except (KeyError, ValueError):
            continue
        by_day[dt.date()].append((dt, ev))
    for day in by_day:
        by_day[day].sort(key=lambda x: x[0])
    return by_day


def render_day_image(day, items) -> bytes:
    from PIL import Image, ImageDraw
    import io

    W = 1280
    title_h, header_h, row_h, bottom_pad = 92, 58, 56, 24
    H = title_h + header_h + row_h * len(items) + bottom_pad

    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f_title = _font(36, 700)
    f_head = _font(20, 700)
    f_cell = _font(21, 400)
    f_evt = _font(21, 600)

    d.text((40, 26), f"{day.strftime('%A')}, {ordinal(day.day)} {day.strftime('%B')}",
           font=f_title, fill=(17, 21, 28))
    d.text((W - 40, 44), "All times ET (New York)", font=_font(18, 400),
           fill=(120, 128, 138), anchor="ra")

    X_TIME, X_CUR, X_IMP, X_EVT, X_FC, X_PREV = 50, 165, 250, 380, 1120, 1240
    y = title_h
    d.rounded_rectangle([30, y, W - 30, y + header_h], radius=10, fill=(246, 247, 249))
    ty = y + 17
    d.text((X_TIME, ty), "Time", font=f_head, fill=(31, 41, 55))
    d.text((X_CUR, ty), "Cur", font=f_head, fill=(31, 41, 55))
    d.text((X_IMP, ty), "Impact", font=f_head, fill=(31, 41, 55))
    d.text((X_EVT, ty), "Event", font=f_head, fill=(31, 41, 55))
    d.text((X_FC, ty), "Forecast", font=f_head, fill=(31, 41, 55), anchor="ra")
    d.text((X_PREV, ty), "Previous", font=f_head, fill=(31, 41, 55), anchor="ra")

    y += header_h
    for i, (dt, ev) in enumerate(items):
        if i:
            d.line([(30, y), (W - 30, y)], fill=(209, 215, 223), width=2)
        cy = y + 15
        t = dt.strftime("%-I:%M%p").lower() if dt.hour or dt.minute else "All day"
        d.text((X_TIME, cy), t, font=f_cell, fill=(30, 34, 40))
        d.text((X_CUR, cy), ev.get("country", "").upper(), font=f_evt, fill=(30, 34, 40))
        imp = ev.get("impact", "").title()
        d.text((X_IMP, cy), imp, font=f_evt, fill=IMPACT_COLOR.get(imp, (30, 34, 40)))
        title = ev.get("title", "")
        if len(title) > 52:
            title = title[:51] + "…"
        d.text((X_EVT, cy), title, font=f_evt, fill=(17, 21, 28))
        d.text((X_FC, cy), str(ev.get("forecast") or "—"), font=f_cell,
               fill=(30, 34, 40), anchor="ra")
        d.text((X_PREV, cy), str(ev.get("previous") or "—"), font=f_cell,
               fill=(30, 34, 40), anchor="ra")
        y += row_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def post_image(png: bytes, filename: str, content: str = ""):
    import uuid
    boundary = uuid.uuid4().hex
    payload = {"username": "Mr Wall Street — Markets",
               "content": content[:2000], "allowed_mentions": {"parse": []}}
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
        f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
        f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n"
    ).encode() + png + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        WEBHOOK, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "MrWallStreetBot"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


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
              f"({days[0].strftime('%b %d')} – {days[-1].strftime('%b %d')})")
    time.sleep(1)
    for day in days:
        png = render_day_image(day, by_day[day])
        status = post_image(png, f"econ-{day.isoformat()}.png")
        print(f"Posted {day} (HTTP {status}).")
        time.sleep(1)


if __name__ == "__main__":
    main()
