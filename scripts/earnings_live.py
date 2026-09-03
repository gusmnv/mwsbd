"""
Mr Wall Street — LIVE earnings watcher (#stock-earnings)

Runs as a continuous session during the hot windows (pre-market / after-hours):
  Layer 1 — SEC EDGAR: polls the real-time 8-K feed every cycle. The second an
            expected reporter files, posts an instant alert.
  Layer 2 — Finnhub: polls actuals every cycle; the moment EPS/revenue land,
            posts the full numbers vs estimates with surprise percentages.

Only companies >= MIN_MCAP_B (default $5B), same rule as everything else.

Usage:  python earnings_live.py            (runs for LIVE_MINUTES, default 120)

Env vars: DISCORD_WEBHOOK_STOCK_EARNINGS, FINNHUB_API_KEY,
          LIVE_MINUTES (optional), MIN_MCAP_B (optional)
"""
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import earnings as E  # reuse api(), get_calendar(), get_mcaps(), keep(), formatters, posting

import os
LIVE_MINUTES = int(os.environ.get("LIVE_MINUTES", "120"))
POLL_SECONDS = 2          # EDGAR checked every cycle (2s) — instant BREAKING alerts
FINNHUB_EVERY = 8         # Finnhub numbers checked every 8th cycle (~16s), respects rate limits

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
ALERTED_FILE = STATE_DIR / "edgar_alerted.json"

EDGAR_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
              "&type=8-K&company=&dateb=&owner=include&count=100&output=atom")
CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_UA = {"User-Agent": "MrWallStreetBot contact alternartivebull@gmail.com"}


def fetch(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "MrWallStreetBot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_cik_map(tickers: set) -> dict:
    """ticker -> zero-padded CIK, for the tickers we care about today."""
    data = json.loads(fetch(CIK_MAP_URL, SEC_UA).decode())
    out = {}
    for row in data.values():
        t = row.get("ticker", "").upper()
        if t in tickers:
            out[t] = int(row.get("cik_str", 0))
    return out


def edgar_recent_ciks() -> set:
    """CIKs present in the latest real-time 8-K feed."""
    try:
        xml = fetch(EDGAR_FEED, SEC_UA).decode(errors="replace")
    except Exception as e:
        print(f"[warn] EDGAR feed failed: {e}")
        return set()
    return {int(m) for m in re.findall(r"\((\d{10})\)", xml)}


def main():
    if not E.WEBHOOK or not E.API_KEY:
        sys.exit("Missing DISCORD_WEBHOOK_STOCK_EARNINGS or FINNHUB_API_KEY")

    et_now = datetime.now(ZoneInfo("America/New_York"))
    today = et_now.date()
    deadline = time.time() + LIVE_MINUTES * 60

    # --- session setup: who is expected to report today? -----------------------
    entries = E.get_calendar(today - timedelta(days=1), today)
    symbols = sorted({e["symbol"] for e in entries if e.get("symbol")})
    mcaps = E.get_mcaps(symbols)
    expected = [e for e in entries if E.keep(e, mcaps.get(e.get("symbol")))]
    exp_tickers = {e["symbol"] for e in expected}
    print(f"Session start {et_now:%F %T} ET — watching {len(exp_tickers)} companies: "
          f"{' '.join(sorted(exp_tickers)) or '(none)'}")
    if not exp_tickers:
        return

    cik_by_ticker = {}
    try:
        cik_by_ticker = load_cik_map(exp_tickers)
    except Exception as e:
        print(f"[warn] CIK map failed ({e}) — EDGAR alerts disabled this session")
    ticker_by_cik = {v: k for k, v in cik_by_ticker.items()}

    alerted = set(E.load_json(ALERTED_FILE, []))
    reported = set(E.load_json(E.REPORTED_FILE, []))

    # Seed: whatever is ALREADY in the EDGAR feed at startup was not filed "just
    # now" - mark it seen silently so a late-started session never lies with
    # "has just reported". Only filings appearing AFTER this moment get the alert.
    if ticker_by_cik:
        try:
            for cik in edgar_recent_ciks() & set(ticker_by_cik):
                alerted.add(f"{ticker_by_cik[cik]}:{today.isoformat()}")
            print(f"Seeded {len(alerted)} pre-session filings (no alerts for those).")
        except Exception as e:
            print(f"[warn] EDGAR seed failed: {e}")

    # --- live loop --------------------------------------------------------------
    cycle = 0
    while time.time() < deadline:
        # Layer 1: EDGAR instant alerts (every cycle, 2s)
        if ticker_by_cik:
            for cik in edgar_recent_ciks() & set(ticker_by_cik):
                sym = ticker_by_cik[cik]
                key = f"{sym}:{today.isoformat()}"
                if key in alerted:
                    continue
                alerted.add(key)
                try:
                    E.post_to_discord(f"🚨 **BREAKING: ${sym} reported earnings just now** — numbers incoming...")
                    print(f"ALERT {sym}")
                except Exception as e:
                    print(f"[warn] alert post failed: {e}")

        # Layer 2: Finnhub actuals (throttled to every FINNHUB_EVERY cycles)
        cycle += 1
        if cycle % FINNHUB_EVERY != 1:
            time.sleep(POLL_SECONDS)
            continue
        try:
            cal = E.get_calendar(today - timedelta(days=1), today)
        except Exception as e:
            print(f"[warn] finnhub poll failed: {e}")
            cal = []
        for e in cal:
            sym = e.get("symbol")
            key = f"{sym}:{e.get('date')}"
            if sym not in exp_tickers or key in reported or e.get("epsActual") is None:
                continue
            reported.add(key)
            eps_a, eps_e = e.get("epsActual"), e.get("epsEstimate")
            rev_a, rev_e = e.get("revenueActual"), e.get("revenueEstimate")
            beat = eps_e is not None and eps_a is not None and float(eps_a) >= float(eps_e)
            emoji, verdict = ("🟢", "BEAT") if beat else ("🔴", "MISS")
            try:
                E.post_to_discord(
                    f"{emoji} **${sym}** — **{verdict}**\n"
                    f"   EPS: **{E.fmt_eps(eps_a)}** vs est {E.fmt_eps(eps_e)} ({E.pct_s(eps_a, eps_e)})\n"
                    f"   Revenue: **{E.fmt_money(rev_a)}** vs est {E.fmt_money(rev_e)} ({E.pct_s(rev_a, rev_e)})"
                )
                print(f"NUMBERS {sym}")
            except Exception as ex:
                print(f"[warn] numbers post failed: {ex}")
                reported.discard(key)  # retry next cycle

        time.sleep(POLL_SECONDS)

    # --- persist state ------------------------------------------------------------
    E.save_json(ALERTED_FILE, sorted(alerted)[-1000:])
    E.save_json(E.REPORTED_FILE, sorted(reported)[-2000:])
    print("Session ended.")


if __name__ == "__main__":
    main()
