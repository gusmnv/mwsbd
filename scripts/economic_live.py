"""
Mr Wall Street — LIVE US economic data watcher (#economic-data)

Runs as a continuous session around the big US release windows. The moment an
official source publishes a number, posts:  actual vs forecast — ABOVE/BELOW.
Also stores actuals in state/econ_actuals.json for the daily/weekly recaps.

Sources (all official, all free):
  BLS  — CPI, Core CPI, NFP, Unemployment rate, Avg hourly earnings, PPI
  BEA  — GDP q/q, Core PCE m/m
  Fed  — FOMC statement (alert only)

Env vars:
  DISCORD_WEBHOOK_ECONOMIC_DATA — webhook URL
  BLS_API_KEY / BEA_API_KEY     — free official keys
  LIVE_MINUTES                  — session length (default 105)
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_ECONOMIC_DATA", "").strip()
BLS_KEY = os.environ.get("BLS_API_KEY", "").strip()
BEA_KEY = os.environ.get("BEA_API_KEY", "").strip()
LIVE_MINUTES = int(os.environ.get("LIVE_MINUTES") or "105")
POLL_SECONDS = 15   # relaxed cadence away from release times
POLL_FAST = 1       # burst cadence in the hot window around each release (HORÁRIO É CHAVE)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
ACTUALS_FILE = STATE_DIR / "econ_actuals.json"
FED_FEED = "https://www.federalreserve.gov/feeds/press_monetary.xml"

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- HTTP helpers
def http_get(url, timeout=20, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "MrWallStreetBot (contact: alternartivebull@gmail.com)"})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    raise last


def post_discord(content: str):
    payload = {"content": content[:2000], "allowed_mentions": {"parse": []}}
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "MrWallStreetBot"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def save_json(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=0))


# ---------------------------------------------------------------- BLS source
def bls_series(series_ids: list[str]) -> dict:
    """-> {series_id: [(period_key, value_float), ...] newest first}"""
    body = json.dumps({"seriesid": series_ids, "registrationkey": BLS_KEY,
                       "startyear": str(datetime.now().year - 1),
                       "endyear": str(datetime.now().year)}).encode()
    req = urllib.request.Request("https://api.bls.gov/publicAPI/v2/timeseries/data/",
                                 data=body, headers={"Content-Type": "application/json",
                                                     "User-Agent": "MrWallStreetBot"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode())
    out = {}
    for s in data.get("Results", {}).get("series", []):
        rows = [(f"{d['year']}-{d['period']}", float(d["value"]))
                for d in s.get("data", []) if d.get("period", "").startswith("M")]
        out[s["seriesID"]] = rows  # newest first per BLS
    return out


def mom_pct(rows):
    """m/m % change from an index series, newest first."""
    if len(rows) < 2:
        return None
    return round((rows[0][1] / rows[1][1] - 1) * 100, 1)


def yoy_pct(rows):
    if len(rows) < 13:
        return None
    return round((rows[0][1] / rows[12][1] - 1) * 100, 1)


def mom_diff_k(rows):
    """m/m change in thousands (payrolls level series)."""
    if len(rows) < 2:
        return None
    return round(rows[0][1] - rows[1][1])


def level_m(rows):
    """Latest level, thousands → millions (JOLTS job openings)."""
    if not rows:
        return None
    return round(rows[0][1] / 1000.0, 2)


# ---------------------------------------------------------------- BEA source
def bea_table(table, freq):
    url = ("https://apps.bea.gov/api/data/?&UserID=" + BEA_KEY +
           "&method=GetData&datasetname=NIPA&TableName=" + table +
           "&Frequency=" + freq + "&Year=X&ResultFormat=JSON")
    return json.loads(http_get(url).decode())


def bea_latest(table, freq, line_desc_re):
    """-> (period, value) newest matching line."""
    try:
        data = bea_table(table, freq)
        rows = data["BEAAPI"]["Results"]["Data"]
    except Exception:
        return None
    matches = [r for r in rows if re.search(line_desc_re, r.get("LineDescription", ""), re.I)]
    if not matches:
        return None
    matches.sort(key=lambda r: r.get("TimePeriod", ""))
    last = matches[-1]
    try:
        return last["TimePeriod"], float(last["DataValue"].replace(",", ""))
    except Exception:
        return None


# ------------------------------------------------- event handlers (US majors)
# Each handler: snapshot() -> comparable marker; value(snapshot_before) -> actual or None
class BLSHandler:
    def __init__(self, series, calc, unit="%"):
        self.series, self.calc, self.unit = series, calc, unit
        self.before = None

    def snapshot(self):
        rows = bls_series([self.series]).get(self.series, [])
        self.before = rows[0][0] if rows else None

    def value(self):
        rows = bls_series([self.series]).get(self.series, [])
        if not rows or not self.before:
            return None
        if rows[0][0] == self.before:      # no new period yet
            return None
        v = self.calc(rows)
        return None if v is None else (v, self.unit)

    def value_latest(self):
        """Latest published value, no snapshot needed (recap-time backfill)."""
        rows = bls_series([self.series]).get(self.series, [])
        if not rows:
            return None
        v = self.calc(rows)
        return None if v is None else (v, self.unit)


class BEAHandler:
    def __init__(self, table, freq, line_re, unit="%"):
        self.args = (table, freq, line_re)
        self.unit = unit
        self.before = None

    def snapshot(self):
        r = bea_latest(*self.args)
        self.before = r[0] if r else None

    def value(self):
        r = bea_latest(*self.args)
        if not r or not self.before or r[0] == self.before:
            return None
        return (r[1], self.unit)

    def value_latest(self):
        r = bea_latest(*self.args)
        return None if not r else (r[1], self.unit)


class FedHandler:
    """Alert-only: FOMC statement published."""
    unit = ""

    def __init__(self):
        self.before = None

    def snapshot(self):
        try:
            xml = http_get(FED_FEED).decode(errors="replace")
            self.before = re.search(r"<item>.*?<link>(.*?)</link>", xml, re.S).group(1)
        except Exception:
            self.before = None

    def value(self):
        try:
            xml = http_get(FED_FEED).decode(errors="replace")
            first = re.search(r"<item>.*?<link>(.*?)</link>", xml, re.S).group(1)
        except Exception:
            return None
        if self.before and first != self.before:
            return ("STATEMENT", "")
        return None


# FF event title (regex) -> handler factory   (USD High-impact majors)
HANDLERS = [
    (r"^Core CPI m/m$",            lambda: BLSHandler("CUSR0000SA0L1E", mom_pct)),
    (r"^CPI m/m$",                 lambda: BLSHandler("CUSR0000SA0", mom_pct)),
    (r"^CPI y/y$",                 lambda: BLSHandler("CUUR0000SA0", yoy_pct)),
    (r"^Non-?Farm Employment",     lambda: BLSHandler("CES0000000001", mom_diff_k, unit="K")),  # anchored: never match "ADP Non-Farm..."
    (r"^Unemployment Rate$",       lambda: BLSHandler("LNS14000000", lambda r: r[0][1])),
    (r"Average Hourly Earnings",   lambda: BLSHandler("CES0500000003", mom_pct)),
    (r"JOLTS Job Openings",        lambda: BLSHandler("JTS000000000000000JOL", level_m, unit="M")),
    (r"^Core PPI m/m$",            lambda: BLSHandler("WPSFD49116", mom_pct)),
    (r"^PPI m/m$",                 lambda: BLSHandler("WPSFD4", mom_pct)),
    (r"GDP (q/q|Price)",           lambda: BEAHandler("T10101", "Q", r"^Gross domestic product$")),
    (r"^Core PCE Price Index m/m$", lambda: BEAHandler("T20804", "M",
                                     r"PCE excluding food and energy|Personal consumption expenditures excluding")),
    (r"Federal Funds Rate|FOMC",   FedHandler),
]


def match_handler(title):
    for pat, factory in HANDLERS:
        if re.search(pat, title, re.I):
            return factory()
    return None


def fmt_actual(v, unit):
    if unit == "K":
        return f"{v:+.0f}K".replace("+-", "-")
    if unit == "M":
        return f"{v:.2f}M"
    if unit == "%":
        return f"{v}%"
    return str(v)


def parse_forecast(s):
    try:
        return float(str(s).replace("%", "").replace("K", "").replace("M", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- main session
def main():
    if not WEBHOOK:
        sys.exit("Missing DISCORD_WEBHOOK_ECONOMIC_DATA")
    now_et = datetime.now(ET)
    today = now_et.date()
    deadline = time.time() + LIVE_MINUTES * 60
    session_end_et = now_et + timedelta(minutes=LIVE_MINUTES)

    events = json.loads(http_get(FEED_URL).decode())
    watch = []
    for ev in events:
        if ev.get("country", "").upper() != "USD":
            continue
        if ev.get("impact", "").title() != "High":
            continue
        try:
            dt = datetime.fromisoformat(ev["date"])
        except Exception:
            continue
        if dt.date() != today or not (now_et - timedelta(minutes=10) <= dt <= session_end_et):
            continue
        h = match_handler(ev.get("title", ""))
        if h:
            watch.append({"ev": ev, "dt": dt, "h": h, "done": False})

    if not watch:
        print("No US High-impact events with handlers in this window. Exiting.")
        return
    print("Watching:", ", ".join(w["ev"]["title"] for w in watch))
    for w in watch:
        try:
            w["h"].snapshot()
        except Exception as e:
            print(f"[warn] snapshot failed for {w['ev']['title']}: {e}")

    actuals = load_json(ACTUALS_FILE, {})

    while time.time() < deadline and any(not w["done"] for w in watch):
        now = datetime.now(ET)
        for w in watch:
            if w["done"] or now < w["dt"] - timedelta(seconds=30):
                continue
            try:
                res = w["h"].value()
            except Exception as e:
                print(f"[warn] poll failed {w['ev']['title']}: {e}")
                continue
            if res is None:
                continue
            v, unit = res
            title = w["ev"]["title"]
            fc_raw = w["ev"].get("forecast")
            if v == "STATEMENT":
                msg = f"🔴 **US — FOMC statement released.** Rate decision & guidance are out."
            else:
                actual_s = fmt_actual(v, unit)
                fc = parse_forecast(fc_raw)
                if fc is not None:
                    verdict = "ABOVE FORECAST" if float(v) > fc else ("BELOW FORECAST" if float(v) < fc else "IN LINE")
                    msg = (f"🔴 **US {title}: {actual_s}** vs forecast {fc_raw} — **{verdict}**")
                else:
                    msg = f"🔴 **US {title}: {actual_s}**"
                key = f"{today.isoformat()}|USD|{title}"
                actuals[key] = actual_s
            try:
                post_discord(msg)
                print("POSTED:", msg)
                w["done"] = True
            except Exception as e:
                print(f"[warn] discord post failed: {e}")
        # Burst mode: poll every second from 45s before a release until it is
        # captured (or 10 min pass); otherwise relax to protect API quotas.
        hot = any(
            (not w["done"]) and (w["dt"] - timedelta(seconds=45) <= now <= w["dt"] + timedelta(minutes=10))
            for w in watch
        )
        time.sleep(POLL_FAST if hot else POLL_SECONDS)

    save_json(ACTUALS_FILE, actuals)
    print("Session ended. Captured:", sum(1 for w in watch if w["done"]), "/", len(watch))


if __name__ == "__main__":
    main()
