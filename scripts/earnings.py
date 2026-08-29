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


def row_for(e: dict, mcap) -> str:
    sym = f"${e.get('symbol', '?')}"
    when = TIME_WORD.get(e.get("hour", ""), "—")
    return (f"{sym:<10}{num_short(mcap):>8}{num_short(e.get('epsEstimate'), money=False):>9}"
            f"{num_short(e.get('revenueEstimate')):>9}  {when}")


TABLE_HEADER = (f"{'COMPANY':<10}{'MCAP':>8}{'EPS EST':>9}{'REV EST':>9}  TIMING\n"
                f"{'-'*10}{'-'*8}{'-'*9}{'-'*9}--{'-'*11}")


def day_blocks(day_iso: str, entries: list[dict], mcaps: dict) -> list[str]:
    """One Discord message (or more) per day: bold header + fenced table."""
    d = date.fromisoformat(day_iso)
    header = f"__**{d.strftime('%A, %B %d')}**__"
    rows = [row_for(e, mcaps.get(e.get("symbol")))
            for e in sorted(entries, key=lambda e: -(mcaps.get(e.get("symbol")) or 0))]
    blocks, batch = [], []
    for row in rows:
        batch.append(row)
        if sum(len(r) + 1 for r in batch) > 1700:  # keep under Discord limit
            blocks.append(header + "\n```\n" + TABLE_HEADER + "\n" + "\n".join(batch) + "\n```")
            header = ""  # only first block carries the day name
            batch = []
    if batch:
        blocks.append((header + "\n" if header else "") +
                      "```\n" + TABLE_HEADER + "\n" + "\n".join(batch) + "\n```")
    return blocks


def build_table(entries: list[dict], title: str) -> list[str]:
    """Returns a list of ready-to-send Discord messages."""
    if not entries:
        return []
    mcaps = get_mcaps(sorted({e["symbol"] for e in entries if e.get("symbol")}))
    kept = [e for e in entries if keep(e, mcaps.get(e.get("symbol")))]
    if not kept:
        return []
    by_day = defaultdict(list)
    for e in kept:
        by_day[e.get("date", "")].append(e)

    messages = [title]
    for day in sorted(by_day):
        messages.extend(day_blocks(day, by_day[day], mcaps))
    return messages


# ---- modes ----------------------------------------------------------------------
def preview():
    today = date.today()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    friday = monday + timedelta(days=4)
    entries = get_calendar(monday, friday)
    msgs = build_table(entries,
        f"🗓️ **EARNINGS WEEK AHEAD** ({monday.strftime('%b %d')} – {friday.strftime('%b %d')})")
    if msgs:
        send_messages(msgs)
    else:
        print("Nothing above the cutoff next week.")


def today_mode():
    t = date.today()
    entries = [e for e in get_calendar(t, t) if e.get("date") == t.isoformat()]
    msgs = build_table(entries, f"📌 **TODAY'S EARNINGS** — {t.strftime('%A, %B %d')}")
    if msgs:
        send_messages(msgs)
    else:
        print("No earnings above the cutoff today.")


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
