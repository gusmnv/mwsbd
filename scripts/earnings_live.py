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


# ---- 8-K press-release parser (Layer -1: to-the-second numbers) --------------
import html as _html

def _strip_html(raw: str) -> str:
    """HTML -> text that keeps table structure: cells become tabs, rows lines."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"(?i)</t[dh]>", "\t", t)
    t = re.sub(r"(?i)</tr>", "\n", t)
    t = re.sub(r"(?i)<br[^>]*>", "\n", t)
    t = re.sub(r"(?i)</(p|div|h[1-6]|li)>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = _html.unescape(t)
    t = t.replace("\u00a0", " ")
    return re.sub(r"\n{3,}", "\n\n", t)


def _num(tok) -> float:
    return float(str(tok).replace(",", "").replace("$", "").strip())


_REV_SENT = re.compile(
    r"(?i)(?:net sales|net revenues?|total (?:net )?revenues?|revenues?)\s+"
    r"(?:were|was|of|(?:increased|decreased|grew|rose|declined|improved|fell)[^.\n]{0,60}?to)\s+"
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million)")

_EPS_ADJ_SENT = re.compile(
    r"(?i)adjusted[^.\n]{0,80}?(?:earnings|net income|income)\s+per\s+(?:diluted\s+)?share[^.\n]{0,40}?"
    r"\$\s?([\d,]+\.\d+)")
_EPS_GAAP_SENT = re.compile(
    r"(?i)(?:diluted\s+)(?:earnings|net income|income)\s+per\s+share\s+(?:of|was|were)\s+"
    r"\$\s?([\d,]+\.\d+)")
_FIRST_NUM = re.compile(r"\$?\s?([\d,]+\.\d+)")


def _table_eps(text: str, header_re: str) -> float | None:
    """Find a per-share block whose header matches, take the Diluted row's FIRST
    number (first column = current quarter in every US press release)."""
    m = re.search(header_re, text)
    if not m:
        return None
    window = text[m.end():m.end() + 400]
    row = re.search(r"(?i)^\s*Diluted[^\n]*", window, re.M)
    if not row:
        return None
    n = _FIRST_NUM.search(row.group(0))
    return _num(n.group(1)) if n else None


def parse_press_release(raw_html: str):
    """Return (eps, revenue_usd, basis) or (None, None, reason).

    EPS priority mirrors how consensus works: ADJUSTED diluted when the company
    reports non-GAAP (that's what estimates are set against), else GAAP diluted.
    """
    text = _strip_html(raw_html)
    # EPS - adjusted table > adjusted sentence > GAAP table > GAAP sentence
    eps = _table_eps(text, r"(?i)adjusted\s+(?:net\s+income|earnings)[^\n]{0,40}per\s+share")
    basis = "adj"
    if eps is None:
        m = _EPS_ADJ_SENT.search(text)
        if m:
            eps = _num(m.group(1))
    if eps is None:
        basis = "gaap"
        eps = _table_eps(text, r"(?im)^\s*Net\s+income[^\n]{0,20}per\s+share")
    if eps is None:
        m = _EPS_GAAP_SENT.search(text)
        if m:
            eps = _num(m.group(1))
    # Revenue - prose sentence (present in virtually every release)
    rev = None
    m = _REV_SENT.search(text)
    if m:
        rev = _num(m.group(1)) * (1e9 if m.group(2).lower() == "billion" else 1e6)
    if eps is None or rev is None:
        return None, None, f"parse incomplete (eps={eps}, rev={rev})"
    return eps, rev, basis


def edgar_filing_docs(cik: int, accession: str):
    """List of document URLs for a filing."""
    acc = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"
    idx = json.loads(fetch(f"{base}/index.json", SEC_UA).decode())
    return [f"{base}/{it['name']}" for it in idx.get("directory", {}).get("item", [])]


def fetch_press_release(cik: int, accession: str) -> str | None:
    """The EX-99 exhibit (the press release) of an 8-K, as raw HTML."""
    try:
        docs = edgar_filing_docs(cik, accession)
    except Exception as e:
        print(f"[warn] filing index failed: {e}")
        return None
    ex99 = [d for d in docs if re.search(r"(?i)(ex[-_]?99|99[-_.]?1|press|earningsrelease)", d)
            and d.lower().endswith((".htm", ".html"))]
    for d in ex99 or [d for d in docs if d.lower().endswith((".htm", ".html"))][:3]:
        try:
            raw = fetch(d, SEC_UA).decode(errors="replace")
            if re.search(r"(?i)per\s+share", raw):
                return raw
        except Exception as e:
            print(f"[warn] exhibit fetch failed: {e}")
    return None


def edgar_recent_filings() -> dict:
    """cik -> (accession, items_string) from the real-time 8-K feed."""
    try:
        xml = fetch(EDGAR_FEED, SEC_UA).decode(errors="replace")
    except Exception as e:
        print(f"[warn] EDGAR feed failed: {e}")
        return {}
    out = {}
    for entry in xml.split("<entry>")[1:]:
        cik = re.search(r"\((\d{10})\)", entry)
        acc = re.search(r"accession-n(?:umber|o)>([\d-]+)<", entry) or \
              re.search(r"AccNo:\s*</b>\s*([\d-]+)", entry) or \
              re.search(r"([\d]{10}-[\d]{2}-[\d]{6})", entry)
        items = re.search(r"(?i)Items?:\s*</b>?\s*([\d\., ]+)", entry)
        if cik and acc:
            out[int(cik.group(1))] = (acc.group(1), items.group(1) if items else "")
    return out
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
            for cik in set(edgar_recent_filings()) & set(ticker_by_cik):
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

    posted_vals = {}   # sym -> (eps, rev) we posted from the 8-K parse

    def sane(eps_a, eps_e, rev_a, rev_e) -> bool:
        """Parsed numbers must be in the same universe as the estimates."""
        try:
            if eps_e is not None and abs(float(eps_a) - float(eps_e)) > max(1.0, 3.0 * abs(float(eps_e))):
                return False
            if rev_e is not None and not (0.5 <= float(rev_a) / float(rev_e) <= 2.0):
                return False
        except (TypeError, ValueError, ZeroDivisionError):
            return False
        return True

    # --- live loop ------------------------------------------------------------
    hot = {}         # sym -> {"acc": accession, "tries": n} awaiting numbers
    cycle = 0
    while time.time() < deadline:
        # Layer 0: EDGAR filing detector (every cycle, 2s) - marks symbols hot
        if ticker_by_cik:
            for cik, (acc, items) in edgar_recent_filings().items():
                if cik not in ticker_by_cik:
                    continue
                sym = ticker_by_cik[cik]
                key = f"{sym}:{today.isoformat()}"
                if key in alerted:
                    continue
                if items and "2.02" not in items:
                    print(f"IGNORED {sym} 8-K (items {items.strip()}) - not an earnings filing")
                    alerted.add(key)
                    continue
                alerted.add(key)
                hot[sym] = {"acc": acc, "cik": cik, "tries": 0}
                print(f"FILED {sym} ({acc}) - parsing press release NOW")

        # Layer -1: parse the press release itself - numbers within seconds
        for sym in list(hot):
            if f"{sym}:{today.isoformat()}" in reported:
                hot.pop(sym); continue
            h = hot[sym]
            if h["tries"] >= 10 or not h.get("acc"):
                continue          # give up parsing; FMP/Finnhub layers take over
            h["tries"] += 1
            raw = fetch_press_release(h["cik"], h["acc"])
            if not raw:
                continue
            eps, rev, basis = parse_press_release(raw)
            if eps is None:
                print(f"[parse] {sym}: {basis} - waiting for FMP")
                h["tries"] = 99  # exhibit is up but unparseable: don't refetch
                continue
            prev = est_by_sym.get(sym, {})
            if not sane(eps, prev.get("epsEstimate"), rev, prev.get("revenueEstimate")):
                print(f"[parse] {sym}: eps={eps} rev={rev} failed sanity vs estimates - deferring to FMP")
                h["tries"] = 99
                continue
            posted_vals[sym] = (eps, rev)
            post_numbers(sym, today.isoformat(), eps, None, rev, None, f"8-K ({basis})")

        hot = {s2: h for s2, h in hot.items() if f"{s2}:{today.isoformat()}" not in reported}

        cycle += 1

        # Layer 1: FMP actuals - every 2nd cycle (~4s), EVERY cycle while hot
        if hot or cycle % 2 == 1:
            for r in fmp_actuals(today - timedelta(days=1), today):
                sym = r.get("symbol")
                if sym not in exp_tickers or r.get("epsActual") is None:
                    continue
                # cross-check anything we posted straight from the 8-K
                if sym in posted_vals:
                    p_eps, p_rev = posted_vals.pop(sym)
                    f_eps = r.get("epsActual")
                    try:
                        if f_eps is not None and abs(float(f_eps) - float(p_eps)) > 0.011:
                            E.post_to_discord(
                                f"\u270f\ufe0f **${sym} correction** - consensus-basis EPS is "
                                f"**{E.fmt_eps(f_eps)}** (we posted {E.fmt_eps(p_eps)} from the 8-K)")
                            print(f"CORRECTED {sym}: {p_eps} -> {f_eps}")
                    except (TypeError, ValueError):
                        pass
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
