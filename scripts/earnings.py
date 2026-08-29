"""
Mr Wall Street — #stock-earnings bot (Finnhub)

Modes:
  python earnings.py preview  → Sunday: week-ahead table, grouped by day
  python earnings.py today    → each morning: today's earnings table
  python earnings.py results  → hourly in reporting windows: BEAT/MISS posts

Line format (QuarterChart style):
  🇺🇸 $AVGO · $780B · EPS est $3.30 · Rev est $15.2B · After close

Filters: market cap >= MIN_MCAP_B (billions USD, default 5).
Companies whose market cap is unavailable are kept if their revenue
estimate is >= $1B (so internationals without profile data still show).

Required env vars:
  DISCORD_WEBHOOK_STOCK_EARNINGS — webhook URL of the #stock-earnings channel
  FINNHUB_API_KEY                — free key from finnhub.io
Optional:
  MIN_MCAP_B                    — market-cap cutoff in $B (default "5")
"""
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_STOCK_EARNINGS", "").strip()
API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
MIN_MCAP_B = float(os.environ.get("MIN_MCAP_B", "5"))

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
REPORTED_FILE = STATE_DIR / "reported_earnings.json"
MCAP_CACHE_FILE = STATE_DIR / "mcap_cache.json"

# ---- region by ticker suffix -------------------------------------------------
EU = "🇪🇺"  # all European Union listings show the EU flag, per Mr Wall Street
SUFFIX_FLAG = {
    "SS": "🇨🇳", "SZ": "🇨🇳", "HK": "🇭🇰", "T": "🇯🇵", "KS": "🇰🇷", "KQ": "🇰🇷",
    "L": "🇬🇧", "SW": "🇨🇭", "OL": "🇳🇴",  # non-EU Europe keeps its own flag
    "PA": EU, "DE": EU, "F": EU, "MI": EU, "AS": EU, "BR": EU, "MC": EU,
    "LS": EU, "ST": EU, "CO": EU, "HE": EU, "VI": EU, "WA": EU, "PR": EU,
    "AT": EU, "IR": EU,
    "TO": "🇨🇦", "V": "🇨🇦", "AX": "🇦🇺", "NZ": "🇳🇿", "SA": "🇧🇷", "MX": "🇲🇽",
    "JK": "🇮🇩", "BK": "🇹🇭", "SI": "🇸🇬", "KL": "🇲🇾", "TW": "🇹🇼", "TWO": "🇹🇼",
    "NS": "🇮🇳", "BO": "🇮🇳", "IS": "🇹🇷", "TA": "🇮🇱", "JO": "🇿🇦",
}

def flag(symbol: str) -> str:
    if "." in symbol:
        suf = symbol.rsplit(".", 1)[1].upper()
        if suf in ("A", "B", "C"):  # US share classes like BF.B, BRK.B
            return "🇺🇸"
        return SUFFIX_FLAG.get(suf, "🌍")
    return "🇺🇸"

SESSION_LABEL = {"bmo": "Before open", "amc": "After close", "dmh": "During market"}

# ---- helpers -----------------------------------------------------------------
def api(path: str, params: dict) -> object:
    qs = "&".join(f"{k}={v}" for k, v in {**params, "token": API_KEY}.items())
    url = f"https://finnhub.io/api/v1/{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "MrWallStreetBot"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def post_to_discord(content: str):
    payload = {
        "username": "Mr Wall Street — Earnings",
        "content": content[:2000],
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


def send_messages(msgs: list[str]):
    """Send a list of pre-built messages, in order."""
    for m in msgs:
        if m.strip():
            status = post_to_discord(m)
            print(f"Posted message (HTTP {status}).")
            time.sleep(1)


def send_chunked(lines: list[str]):
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
        time.sleep(1)


def load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return default
    return default


def save_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


def fmt_money(x) -> str:
    """1234500000 -> $1.23B ; 45300000 -> $45.3M"""
    if x is None:
        return "—"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "—"
    a = abs(x)
    if a >= 1e12:
        return f"${x/1e12:.2f}T"
    if a >= 1e9:
        return f"${x/1e9:.2f}B"
    if a >= 1e6:
        return f"${x/1e6:.1f}M"
    return f"${x:,.0f}"


def fmt_eps(x) -> str:
    if x is None:
        return "—"
    try:
        return f"${float(x):.2f}"
    except (TypeError, ValueError):
        return "—"


# ---- market cap (Finnhub profile2, cached) ------------------------------------
def get_mcaps(symbols: list[str]) -> dict:
    """Return {symbol: market cap in $ (float) or None}. Cached in state/."""
    cache = load_json(MCAP_CACHE_FILE, {})  # {sym: [mcap_musd, yyyymmdd]}
    today_key = date.today().strftime("%Y%m%d")
    out = {}
    fresh_calls = 0
    for sym in symbols:
        hit = cache.get(sym)
        if hit and (int(today_key) - int(hit[1])) <= 7:  # cache 7 days
            out[sym] = hit[0] * 1e6 if hit[0] else None
            continue
        try:
            prof = api("stock/profile2", {"symbol": sym})
            mc = prof.get("marketCapitalization")  # in $ millions
            cache[sym] = [mc, today_key]
            out[sym] = mc * 1e6 if mc else None
        except Exception:
            out[sym] = None
        fresh_calls += 1
        if fresh_calls % 50 == 0:
            time.sleep(60)  # stay under free-tier rate limit
        else:
            time.sleep(0.35)
    save_json(MCAP_CACHE_FILE, cache)
    return out


# ---- calendar ------------------------------------------------------------------
def get_calendar(frm: date, to: date) -> list[dict]:
    entries = []
    try:
        data = api("calendar/earnings", {"from": frm.isoformat(), "to": to.isoformat(),
                                         "international": "true"})
        entries = data.get("earningsCalendar", []) or []
    except Exception:
        pass
    if not entries:  # fallback without the international flag
        data = api("calendar/earnings", {"from": frm.isoformat(), "to": to.isoformat()})
        entries = data.get("earningsCalendar", []) or []
    return entries


def keep(entry: dict, mcap) -> bool:
    """Filter: mcap >= cutoff, or unknown mcap but revenue est >= $1B."""
    if mcap is not None:
        return mcap >= MIN_MCAP_B * 1e9
    rev = entry.get("revenueEstimate")
    return rev is not None and float(rev) >= 1e9


FLAG_CODE = {"🇺🇸": "US", "🇨🇳": "CN", "🇭🇰": "HK", "🇯🇵": "JP", "🇰🇷": "KR",
             "🇬🇧": "GB", "🇪🇺": "EU", "🇨🇭": "CH", "🇳🇴": "NO", "🇨🇦": "CA",
             "🇦🇺": "AU", "🇳🇿": "NZ", "🇧🇷": "BR", "🇲🇽": "MX", "🇮🇩": "ID",
             "🇹🇭": "TH", "🇸🇬": "SG", "🇲🇾": "MY", "🇹🇼": "TW", "🇮🇳": "IN",
             "🇹🇷": "TR", "🇮🇱": "IL", "🇿🇦": "ZA", "🌍": "INT"}

TIME_CODE = {"bmo": "BMO", "amc": "AMC", "dmh": "MKT"}


def num_short(x, money=True) -> str:
    """Compact numbers for table columns: 780.0B / 15.2B / 3.30 / —"""
    if x is None:
        return "—"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "—"
    a = abs(x)
    if a >= 1e12:
        return f"{x/1e12:.2f}T"
    if a >= 1e9:
        return f"{x/1e9:.1f}B"
    if a >= 1e6:
        return f"{x/1e6:.0f}M"
    return f"{x:.2f}"


TIME_WORD = {"bmo": "Before open", "amc": "After close", "dmh": "In market"}

# ---- table rendered as image (clean, quarterchart-style) ----------------------
FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def render_day_image(day_iso: str, entries: list[dict], mcaps: dict) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    import io

    # order: Before open first, then in-market, then After close, unknown last;
    # inside each group, biggest market cap first
    session_order = {"bmo": 0, "dmh": 1, "amc": 2}
    rows = sorted(entries, key=lambda e: (session_order.get(e.get("hour", ""), 3),
                                          -(mcaps.get(e.get("symbol")) or 0)))
    W = 1180
    title_h, header_h, row_h, footer_h = 84, 56, 60, 44
    H = title_h + header_h + row_h * len(rows) + footer_h

    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f_title = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 34)
    f_head  = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 22)
    f_cell  = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 23)
    f_tick  = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 23)
    f_ftr   = ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 17)

    dt = date.fromisoformat(day_iso)
    d.text((40, 24), dt.strftime("%A %m/%d"), font=f_title, fill=(20, 24, 31))

    # column x anchors: ticker left; numbers right-aligned; timing right
    X_TICK, X_MCAP, X_EPS, X_REV, X_TIME = 50, 430, 660, 950, 1140
    y = title_h
    d.rectangle([30, y, W - 30, y + header_h], fill=(246, 247, 249))
    ty = y + 16
    d.text((X_TICK, ty), "Company", font=f_head, fill=(55, 63, 75))
    d.text((X_MCAP, ty), "Market cap", font=f_head, fill=(55, 63, 75), anchor="ra")
    d.text((X_EPS, ty), "EPS estimate", font=f_head, fill=(55, 63, 75), anchor="ra")
    d.text((X_REV, ty), "Revenue estimate", font=f_head, fill=(55, 63, 75), anchor="ra")
    d.text((X_TIME, ty), "Timing", font=f_head, fill=(55, 63, 75), anchor="ra")

    y += header_h
    for i, e in enumerate(rows):
        if i:
            d.line([(30, y), (W - 30, y)], fill=(233, 236, 240), width=2)
        cy = y + 16
        sym = f"${e.get('symbol', '?')}"
        d.text((X_TICK, cy), sym, font=f_tick, fill=(23, 92, 211))
        d.text((X_MCAP, cy), fmt_money(mcaps.get(e.get("symbol"))), font=f_cell,
               fill=(30, 34, 40), anchor="ra")
        d.text((X_EPS, cy), fmt_eps(e.get("epsEstimate")), font=f_cell,
               fill=(30, 34, 40), anchor="ra")
        d.text((X_REV, cy), fmt_money(e.get("revenueEstimate")), font=f_cell,
               fill=(30, 34, 40), anchor="ra")
        d.text((X_TIME, cy), TIME_WORD.get(e.get("hour", ""), "—"), font=f_cell,
               fill=(30, 34, 40), anchor="ra")
        y += row_h

    d.text((W // 2, H - 22), "MR WALL STREET  ·  @mrofwallstreet",
           font=f_ftr, fill=(150, 158, 168), anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def post_image(png: bytes, filename: str, content: str = ""):
    """Post an image to the Discord webhook (multipart upload)."""
    import uuid
    boundary = uuid.uuid4().hex
    payload = {"username": "Mr Wall Street — Earnings",
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


def build_day_images(entries: list[dict]):
    """Returns [(day_iso, png_bytes)] for kept entries, grouped by day."""
    if not entries:
        return []
    mcaps = get_mcaps(sorted({e["symbol"] for e in entries if e.get("symbol")}))
    kept = [e for e in entries if keep(e, mcaps.get(e.get("symbol")))]
    if not kept:
        return []
    by_day = defaultdict(list)
    for e in kept:
        by_day[e.get("date", "")].append(e)
    return [(day, render_day_image(day, by_day[day], mcaps)) for day in sorted(by_day)]


# ---- modes ----------------------------------------------------------------------
def preview():
    today = date.today()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    friday = monday + timedelta(days=4)
    entries = get_calendar(monday, friday)
    images = build_day_images(entries)
    if not images:
        print("Nothing above the cutoff next week.")
        return
    post_to_discord(f"🗓️ **EARNINGS WEEK AHEAD** ({monday.strftime('%b %d')} – {friday.strftime('%b %d')})")
    time.sleep(1)
    for day, png in images:
        status = post_image(png, f"earnings-{day}.png")
        print(f"Posted {day} (HTTP {status}).")
        time.sleep(1)


def today_mode():
    t = date.today()
    entries = [e for e in get_calendar(t, t) if e.get("date") == t.isoformat()]
    images = build_day_images(entries)
    if not images:
        print("No earnings above the cutoff today.")
        return
    for day, png in images:
        status = post_image(png, f"earnings-{day}.png", content="📌 **TODAY'S EARNINGS**")
        print(f"Posted {day} (HTTP {status}).")


def results():
    t = date.today()
    entries = get_calendar(t - timedelta(days=1), t)
    reported = set(load_json(REPORTED_FILE, []))
    candidates = [e for e in entries if e.get("epsActual") is not None
                  and f"{e.get('symbol')}:{e.get('date')}" not in reported]
    if not candidates:
        print("No new results.")
        return
    mcaps = get_mcaps(sorted({e["symbol"] for e in candidates if e.get("symbol")}))
    fresh = []
    for e in candidates:
        sym = e.get("symbol")
        key = f"{sym}:{e.get('date')}"
        if not keep(e, mcaps.get(sym)):
            reported.add(key)  # below cutoff — never post it
            continue
        reported.add(key)
        eps_a, eps_e = e.get("epsActual"), e.get("epsEstimate")
        rev_a, rev_e = e.get("revenueActual"), e.get("revenueEstimate")
        beat = eps_e is not None and eps_a is not None and float(eps_a) >= float(eps_e)
        emoji, verdict = ("🟢", "BEAT") if beat else ("🔴", "MISS")
        fresh.append(
            f"{emoji} {flag(sym)} **${sym}** — **{verdict}**\n"
            f"   EPS: **{fmt_eps(eps_a)}** vs est {fmt_eps(eps_e)}\n"
            f"   Revenue: **{fmt_money(rev_a)}** vs est {fmt_money(rev_e)}"
        )
    if fresh:
        send_chunked(["💰 **EARNINGS JUST REPORTED**", ""] + fresh)
    else:
        print("No new results above the cutoff.")
    save_json(REPORTED_FILE, sorted(reported)[-2000:])


def main():
    if not WEBHOOK:
        sys.exit("Missing DISCORD_WEBHOOK_STOCK_EARNINGS env var")
    if not API_KEY:
        sys.exit("Missing FINNHUB_API_KEY env var")
    mode = sys.argv[1] if len(sys.argv) > 1 else "results"
    if mode == "preview":
        preview()
    elif mode == "today":
        today_mode()
    elif mode == "results":
        results()
    else:
        sys.exit(f"Unknown mode: {mode} (use 'preview', 'today' or 'results')")


if __name__ == "__main__":
    main()
