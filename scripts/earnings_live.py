"""
mwsbd — LIVE earnings watcher (#stock-earnings)

Runs as a continuous session during the hot windows (pre-market / after-hours).
NO teaser posts - one message per company, the full numbers, as fast as possible:
  Layer 0 - SEC EDGAR: detects the 8-K the second it is filed -> symbol goes
            "hot" and the pollers hammer for its numbers.
  Layer 1 - FMP (paid): actuals land here within minutes of the press release.
            Polled every ~4s (every 2s while something is hot).
  Layer 2 - Finnhub: slower backup (~20-30 min lag), polled every ~16s.

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
LIVE_MINUTES = int(os.environ.get("LIVE_MINUTES") or "120")  # 'or' guards empty-string env
POLL_SECONDS = 2          # EDGAR checked every cycle (2s)
FINNHUB_EVERY = 8         # Finnhub numbers checked every 8th cycle (~16s), respects rate limits

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
ALERTED_FILE = STATE_DIR / "edgar_alerted.json"

FMP_KEY = os.environ.get("FMP_API_KEY", "").strip()

def fmp_actuals(frm, to):
    """FMP stable earnings-calendar rows for the window — actuals land here
    much faster than Finnhub (minutes vs ~half an hour)."""
    if not FMP_KEY:
        return []
    url = (f"https://financialmodelingprep.com/stable/earnings-calendar"
           f"?from={frm.isoformat()}&to={to.isoformat()}&apikey={FMP_KEY}")
    try:
        return json.loads(fetch(url, timeout=15).decode())
    except Exception as e:
        print(f"[warn] fmp poll failed: {e}")
        return []


EDGAR_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
              "&type=8-K&company=&dateb=&owner=include&count=100&output=atom")
CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_UA = {"User-Agent": "mwsbd/1.0 (contact: alternartivebull@gmail.com)"}


def fetch(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "mwsbd/1.0"})
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

    # estimates as previewed (Finnhub calendar) - keeps our numbers consistent
    est_by_sym = {e["symbol"]: e for e in expected}

    def post_numbers(sym, day_iso, eps_a, eps_e, rev_a, rev_e, source):
        key = f"{sym}:{day_iso}"
        if key in reported:
            return
        prev = est_by_sym.get(sym, {})
        eps_e = prev.get("epsEstimate") if prev.get("epsEstimate") is not None else eps_e
        rev_e = prev.get("revenueEstimate") if prev.get("revenueEstimate") is not None else rev_e
        beat = eps_e is not None and eps_a is not None and float(eps_a) >= float(eps_e)
        emoji, verdict = ("\U0001f7e2", "BEAT") if beat else ("\U0001f534", "MISS")
        reported.add(key)
        try:
            E.post_to_discord(
                f"{emoji} **${sym}** \u2014 **{verdict}**\n"
                f"   EPS: **{E.fmt_eps(eps_a)}** vs est {E.fmt_eps(eps_e)} ({E.pct_s(eps_a, eps_e)})\n"
                f"   Revenue: **{E.fmt_money(rev_a)}** vs est {E.fmt_money(rev_e)} ({E.pct_s(rev_a, rev_e)})"
            )
            print(f"NUMBERS {sym} via {source}")
        except Exception as ex:
            print(f"[warn] numbers post failed: {ex}")
            reported.discard(key)  # retry next cycle

    # --- live loop ------------------------------------------------------------
    hot = set()      # symbols whose 8-K is filed but numbers not yet posted
    cycle = 0
    while time.time() < deadline:
        # Layer 0: EDGAR filing detector (every cycle, 2s) - marks symbols hot
        if ticker_by_cik:
            for cik in edgar_recent_ciks() & set(ticker_by_cik):
                sym = ticker_by_cik[cik]
                key = f"{sym}:{today.isoformat()}"
                if key in alerted:
                    continue
                alerted.add(key)
                hot.add(sym)
                print(f"FILED {sym} - hammering for numbers")
        hot -= {k.split(":")[0] for k in reported}

        cycle += 1

        # Layer 1: FMP actuals - every 2nd cycle (~4s), EVERY cycle while hot
        if hot or cycle % 2 == 1:
            for r in fmp_actuals(today - timedelta(days=1), today):
                sym = r.get("symbol")
                if sym not in exp_tickers or r.get("epsActual") is None:
                    continue
                post_numbers(sym, r.get("date") or today.isoformat(),
                             r.get("epsActual"), r.get("epsEstimated"),
                             r.get("revenueActual"), r.get("revenueEstimated"), "FMP")

        # Layer 2: Finnhub backup (every FINNHUB_EVERY cycles, ~16s)
        if cycle % FINNHUB_EVERY == 1:
            try:
                cal = E.get_calendar(today - timedelta(days=1), today)
            except Exception as e:
                cal = []
                print(f"[warn] finnhub poll failed: {e}")
            for e in cal:
                sym = e.get("symbol")
                if sym not in exp_tickers or e.get("epsActual") is None:
                    continue
                post_numbers(sym, e.get("date") or today.isoformat(),
                             e.get("epsActual"), e.get("epsEstimate"),
                             e.get("revenueActual"), e.get("revenueEstimate"), "Finnhub")

        time.sleep(POLL_SECONDS)

    # --- persist state ------------------------------------------------------------
    E.save_json(ALERTED_FILE, sorted(alerted)[-1000:])
    E.save_json(E.REPORTED_FILE, sorted(reported)[-2000:])
    print("Session ended.")


if __name__ == "__main__":
    main()
