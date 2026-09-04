"""
Mr Wall Street — FMP economic calendar helper.

Global actuals (live + recap backfill) from the Financial Modeling Prep
economic calendar: any country, actual / estimate / previous per event.

Required env var:
  FMP_API_KEY — free key from financialmodelingprep.com

Free-tier budget is ~250 requests/day, so callers poll on a relaxed cadence
(the US majors stay on the official BLS/BEA 1-second burst path).
"""
import json
import os
import re
import urllib.request
from datetime import datetime, timezone

API_KEY = os.environ.get("FMP_API_KEY", "").strip()
BASE = "https://financialmodelingprep.com/api/v3/economic_calendar"

# ForexFactory currency -> FMP country codes
CUR2COUNTRIES = {
    "USD": {"US"}, "CAD": {"CA"}, "GBP": {"GB", "UK"}, "JPY": {"JP"},
    "CHF": {"CH"}, "AUD": {"AU"}, "NZD": {"NZ"}, "CNY": {"CN"},
    "EUR": {"EA", "EU", "EMU", "DE", "FR", "IT", "ES", "NL"},
}

STOP = {"m", "mm", "yy", "q", "change", "index", "rate", "flash", "final",
        "prelim", "preliminary", "revised", "the", "of", "and", "s", "adv"}

ALIASES = [
    (r"non-?farm employment change|non-?farm payrolls", "nonfarm payrolls"),
    (r"unemployment claims|initial (jobless )?claims", "initial jobless claims"),
    (r"jolts", "jolts job openings"),
    (r"ism manufacturing", "ism manufacturing pmi"),
    (r"ism services|ism non-?manufacturing", "ism non manufacturing services pmi"),
    (r"average hourly earnings", "average hourly earnings"),
    (r"gdp", "gdp growth"),
    (r"core cpi", "core inflation cpi"),
    (r"\bcpi\b", "cpi inflation consumer price"),
    (r"core pce", "core pce price"),
    (r"retail sales", "retail sales"),
    (r"employment change", "employment change"),
    (r"unemployment rate", "unemployment rate"),
]


def _tok(s):
    s = re.sub(r"[^a-z0-9 /%.]", " ", str(s).lower())
    return {t for t in re.split(r"[ /]+", s) if t and t not in STOP}


def fetch(day_from, day_to, timeout=20):
    """All FMP calendar rows between the two ISO dates (inclusive). [] on any problem."""
    if not API_KEY:
        return []
    url = f"{BASE}?from={day_from}&to={day_to}&apikey={API_KEY}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MrWallStreetBot"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[warn] FMP fetch failed: {e}")
        return []


def find_actual(rows, title, currency, when_utc=None, tol_minutes=90):
    """Best-match FMP row for a ForexFactory event -> (actual, estimate) or (None, None)."""
    countries = CUR2COUNTRIES.get(str(currency).upper(), set())
    want = _tok(title)
    for pat, repl in ALIASES:
        if re.search(pat, title, re.I):
            want |= _tok(repl)
            break
    best, best_score = None, 0.0
    for row in rows:
        if countries and str(row.get("country", "")).upper() not in countries:
            continue
        if when_utc is not None:
            try:
                rdt = datetime.strptime(row["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if abs((rdt - when_utc).total_seconds()) > tol_minutes * 60:
                    continue
            except Exception:
                pass
        have = _tok(row.get("event", ""))
        if not have:
            continue
        inter = len(want & have)
        if not inter:
            continue
        score = inter / max(1, min(len(want), len(have)))
        if score > best_score:
            best, best_score = row, score
    if best is None or best_score < 0.5:
        return None, None
    a = best.get("actual")
    if a in (None, ""):
        return None, None
    return a, best.get("estimate")


def fmt_like(actual, forecast_str):
    """Render an FMP numeric actual in the same style as the FF forecast string
    (same decimals, same %/K/M/B suffix), with a magnitude sanity guard."""
    try:
        a = float(actual)
    except (TypeError, ValueError):
        return str(actual)
    f = str(forecast_str or "")
    m = re.search(r"\d+\.(\d+)", f)
    dec = len(m.group(1)) if m else 0
    suffix = f[-1] if f[-1:] in "%KMB" else ""
    if suffix == "K" and abs(a) >= 20000:
        a /= 1000.0
    elif suffix == "M" and abs(a) >= 20000:
        a /= 1_000_000.0
    elif suffix == "B" and abs(a) >= 20000:
        a /= 1_000_000_000.0
    out = f"{a:.{dec}f}"
    if not dec and not suffix and a != int(a):
        out = f"{a:g}"
    return out + suffix
