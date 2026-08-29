"""
Mr Wall Street — #stock-earnings bot (Finnhub)

Two modes:
  python earnings.py preview  → Sunday: post the week-ahead earnings calendar
  python earnings.py results  → hourly during reporting windows: post fresh
                                 actual results vs estimates (beat/miss)

Required env vars:
  DISCORD_WEBHOOK_STOCK_EARNINGS — webhook URL for the #stock-earnings channel
  FINNHUB_API_KEY                — free key from finnhub.io

Only companies in WATCHLIST are posted, so the channel isn't flooded with
hundreds of small caps. Edit the list freely.
"""
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_STOCK_EARNINGS", "").strip()
API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "reported_earnings.json"

WATCHLIST = {
    # Mega/large-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD",
    "INTC", "QCOM", "MU", "TSM", "ORCL", "CRM", "ADBE", "NFLX", "SMCI",
    "PLTR", "SNOW", "UBER", "ABNB", "SHOP", "COIN", "HOOD", "MSTR", "SOFI",
    "RBLX", "NET", "CRWD", "PANW", "ZS", "DDOG", "SQ", "PYPL",
    # Financials
    "JPM", "BAC", "GS", "MS", "WFC", "C", "V", "MA", "AXP", "BLK", "SCHW",
    # Consumer / industrial / health
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "SBUX", "MCD", "DIS", "KO",
    "PEP", "PG", "JNJ", "UNH", "LLY", "PFE", "MRNA", "CVX", "XOM", "BA",
    "CAT", "GE", "F", "GM", "RIVN", "LCID", "DAL", "UAL", "MAR",
    # Meme / high-interest
    "GME", "AMC", "CVNA", "DJT", "RDDT",
}


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


def get_calendar(frm: date, to: date) -> list[dict]:
    data = api("calendar/earnings", {"from": frm.isoformat(), "to": to.isoformat()})
    return data.get("earningsCalendar", []) or []


SESSION_LABEL = {"bmo": "before open", "amc": "after close", "dmh": "during market"}


def fmt_num(x) -> str:
    if x is None:
        return "—"
    if abs(x) >= 1e9:
        return f"{x/1e9:.2f}B"
    if abs(x) >= 1e6:
        return f"{x/1e6:.1f}M"
    return f"{x:.2f}"


def preview():
    """Sunday: week-ahead earnings calendar."""
    today = date.today()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)  # next Monday
    friday = monday + timedelta(days=4)
    entries = [e for e in get_calendar(monday, friday) if e.get("symbol") in WATCHLIST]
    if not entries:
        print("No watchlist earnings next week.")
        return

    by_day = defaultdict(list)
    for e in entries:
        by_day[e.get("date", "")].append(e)

    lines = [f"🗓️ **Earnings Week Ahead** ({monday.strftime('%b %d')} – {friday.strftime('%b %d')})", ""]
    for day in sorted(by_day):
        d = date.fromisoformat(day)
        lines.append(f"**{d.strftime('%A, %b %d')}**")
        for e in sorted(by_day[day], key=lambda x: x.get("symbol", "")):
            when = SESSION_LABEL.get(e.get("hour", ""), "")
            est = f" · EPS est {fmt_num(e.get('epsEstimate'))}" if e.get("epsEstimate") is not None else ""
            lines.append(f"• **{e['symbol']}** ({when}){est}")
        lines.append("")
    send_chunked(lines)


def load_reported() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_reported(seen: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen)[-1000:]))


def results():
    """Post fresh results (actuals just released) for watchlist names."""
    today = date.today()
    entries = get_calendar(today - timedelta(days=1), today)
    seen = load_reported()
    fresh = []

    for e in entries:
        sym = e.get("symbol")
        if sym not in WATCHLIST:
            continue
        if e.get("epsActual") is None:
            continue  # not reported yet
        key = f"{sym}:{e.get('date')}"
        if key in seen:
            continue
        seen.add(key)

        eps_a, eps_e = e.get("epsActual"), e.get("epsEstimate")
        rev_a, rev_e = e.get("revenueActual"), e.get("revenueEstimate")
        beat = eps_e is not None and eps_a is not None and eps_a >= eps_e
        emoji = "🟢" if beat else "🔴"
        verdict = "BEAT" if beat else "MISS"
        line = (
            f"{emoji} **{sym}** earnings — **{verdict}**\n"
            f"   EPS: **{fmt_num(eps_a)}** vs est {fmt_num(eps_e)}\n"
            f"   Revenue: **{fmt_num(rev_a)}** vs est {fmt_num(rev_e)}"
        )
        fresh.append(line)

    if fresh:
        send_chunked(["💰 **Earnings Just Reported**", ""] + fresh)
    else:
        print("No new results.")
    save_reported(seen)


def main():
    if not WEBHOOK:
        sys.exit("Missing DISCORD_WEBHOOK_STOCK_EARNINGS env var")
    if not API_KEY:
        sys.exit("Missing FINNHUB_API_KEY env var")

    mode = sys.argv[1] if len(sys.argv) > 1 else "results"
    if mode == "preview":
        preview()
    elif mode == "results":
        results()
    else:
        sys.exit(f"Unknown mode: {mode} (use 'preview' or 'results')")


if __name__ == "__main__":
    main()
