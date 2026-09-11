"""
mwsbd — LIVE US economic data watcher (#economic-data)

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
from datetime import date, datetime, timedelta
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
    req = urllib.request.Request(url, headers={"User-Agent": "mwsbd/1.0 (contact: alternartivebull@gmail.com)"})
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
        headers={"Content-Type": "application/json", "User-Agent": "mwsbd/1.0"},
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
                                                     "User-Agent": "mwsbd/1.0"})
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
        if not rows:
            return None
        if not self.before:
            # snapshot failed at session start (BLS hiccup) - late baseline:
            # take what we see now and detect the NEXT change, never stall forever
            self.before = rows[0][0]
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


# ════════════ Layer 0: OFFICIAL BLS NEWS-RELEASE PAGES (fastest path) ════════
# 2026-09-11 CPI: api.bls.gov collapsed (503) exactly at 12:30 UTC under the
# global hammering; the news-release PAGE is a CDN-served static file that
# answered in 0.03-0.12s from the same runner. The page is published at the
# exact release second - so THE PAGE is the primary source, the API a backup.

BLS_PAGES = {
    "cpi":    "https://www.bls.gov/news.release/cpi.nr0.htm",
    "ppi":    "https://www.bls.gov/news.release/ppi.nr0.htm",
    "empsit": "https://www.bls.gov/news.release/empsit.nr0.htm",
}

# FF event title pattern -> (page key, metric)
PAGE_METRICS = [
    (r"(?i)^core\s+cpi\s+m/m", "cpi", "core_mm"),
    (r"(?i)^core\s+cpi\s+y/y", "cpi", "core_yy"),
    (r"(?i)^cpi\s+m/m",        "cpi", "all_mm"),
    (r"(?i)^cpi\s+y/y",        "cpi", "all_yy"),
    (r"(?i)^ppi\s+m/m",        "ppi", "all_mm"),
    (r"(?i)^ppi\s+y/y",        "ppi", "all_yy"),
    (r"(?i)non-?farm\s+employment", "empsit", "nfp"),
    (r"(?i)^unemployment\s+rate",   "empsit", "unrate"),
    (r"(?i)average\s+hourly\s+earnings", "empsit", "ahe_mm"),
]

_PG_UP = r"rose|increased|moved\s+up|advanced|edged\s+up|climbed|grew"
_PG_DOWN = r"fell|declined|decreased|moved\s+down|edged\s+down|dropped"
_PG_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def _pg_move(text, lead, tail=""):
    """'<lead> rose 0.3 percent<tail>' -> signed float; 'was unchanged' -> 0."""
    m = re.search(rf"(?is){lead}\s+({_PG_UP}|{_PG_DOWN})\s+([\d.]+)\s*percent{tail}", text)
    if m:
        v = float(m.group(2))
        return -v if re.fullmatch(rf"(?i)(?:{_PG_DOWN})", m.group(1)) else v
    if re.search(rf"(?is){lead}\s+was\s+unchanged", text):
        return 0.0
    return None


def pg_extract(page_key, metric, text):
    """Extract one FF metric from the official release text. None if unsure -
    the API/FMP layers then take over (never guess an official number)."""
    if page_key == "cpi":
        if metric == "all_mm":
            return _pg_move(text, r"Consumer Price Index for All Urban Consumers \(CPI-U\)",
                            r"\s+on\s+a\s+seasonally")
        if metric == "all_yy":
            return _pg_move(text, r"the\s+all\s+items\s+index",
                            r"(?=\s+(?:before\s+seasonal|for\s+the\s+12\s+months|over\s+the))")
        if metric == "core_mm":
            return _pg_move(text, r"index\s+for\s+all\s+items\s+less\s+food\s+and\s+energy",
                            r"(?!\s+over\s+the\s+(?:year|last|past))")
        if metric == "core_yy":
            return _pg_move(text, r"all\s+items\s+less\s+food\s+and\s+energy\s+index",
                            r"(?=\s+over\s+the\s+(?:year|last|past))")
    if page_key == "ppi":
        if metric == "all_mm":
            return _pg_move(text, r"Producer\s+Price\s+Index\s+for\s+final\s+demand")
        if metric == "all_yy":
            return _pg_move(text, r"(?:the\s+)?index\s+for\s+final\s+demand",
                            r"\s+for\s+the\s+12\s+months")
    if page_key == "empsit":
        if metric == "nfp":
            m = re.search(rf"(?is)Total\s+nonfarm\s+payroll\s+employment\s+({_PG_UP}|{_PG_DOWN})"
                          r"\s+by\s+([\d,]+)", text)
            if m:
                v = float(m.group(2).replace(",", "")) / 1000.0
                return -v if re.fullmatch(rf"(?i)(?:{_PG_DOWN})", m.group(1)) else v
            m = re.search(r"(?is)Total\s+nonfarm\s+payroll\s+employment\s+"
                          r"(?:changed\s+little|was\s+(?:essentially\s+)?unchanged)"
                          r"[^(]{0,50}\(([+-])([\d,]+)\)", text)
            if m:
                v = float(m.group(2).replace(",", "")) / 1000.0
                return -v if m.group(1) == "-" else v
            return None
        if metric == "unrate":
            m = re.search(r"(?is)unemployment\s+rate\s+(?:was\s+unchanged\s+at|held\s+at|"
                          r"remained\s+at|was|(?:rose|increased|edged\s+up|declined|"
                          r"decreased|edged\s+down|fell)\s+to)\s+([\d.]+)\s*percent", text)
            return float(m.group(1)) if m else None
        if metric == "ahe_mm":
            m = re.search(r"(?is)average\s+hourly\s+earnings[^.]{0,140}?"
                          rf"({_PG_UP}|{_PG_DOWN})\s+by\s+\d+\s+cents?,\s+or\s+([\d.]+)\s*percent",
                          text)
            if m:
                v = float(m.group(2))
                return -v if re.fullmatch(rf"(?i)(?:{_PG_DOWN})", m.group(1)) else v
            return None
    return None


class PageWatch:
    """Polls one BLS news-release page with cheap conditional GETs; marks
    itself fresh only when the page's embargo date == today (ET)."""
    def __init__(self, key, url):
        self.key, self.url = key, url
        self.etag = None
        self.lastmod = None
        self.text = None
        self.fresh = False
        self.next_poll = 0.0

    def _release_date(self, txt):
        m = re.search(r"(?is)embargoed\s+until.{0,200}?"
                      r"(January|February|March|April|May|June|July|August|"
                      r"September|October|November|December)\s+(\d{1,2}),\s+(\d{4})", txt)
        if not m:
            return None
        try:
            return date(int(m.group(3)), _PG_MONTHS[m.group(1).lower()], int(m.group(2)))
        except (KeyError, ValueError):
            return None

    def poll(self, today_et) -> bool:
        """One conditional GET (3s timeout). True when today's release is up."""
        req = urllib.request.Request(self.url, headers={
            "User-Agent": "mwsbd/1.0 (contact: alternartivebull@gmail.com)",
            "Accept-Encoding": "identity"})
        if self.etag:
            req.add_header("If-None-Match", self.etag)
        if self.lastmod:
            req.add_header("If-Modified-Since", self.lastmod)
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                raw = r.read().decode(errors="replace")
                self.etag = r.headers.get("ETag") or self.etag
                self.lastmod = r.headers.get("Last-Modified") or self.lastmod
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return False        # unchanged - the cheap common case
            raise
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))
        if self._release_date(txt) == today_et:
            self.text, self.fresh = txt, True
            return True
        return False


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
        imp = ev.get("impact", "").title()
        cur = ev.get("country", "").upper()
        # "tem de sair tudo o que for data": for USD we watch every DATA
        # release, High or Medium (e.g. Unemployment Claims is Medium on FF).
        # A "data" event is one with a forecast or previous value - speeches
        # have neither and are never postable numbers, so they are skipped.
        is_data = bool(str(ev.get("forecast") or "").strip() or str(ev.get("previous") or "").strip())
        if cur == "USD":
            if imp not in ("High", "Medium") or not is_data:
                continue
        elif imp != "High":
            continue
        try:
            dt = datetime.fromisoformat(ev["date"])
        except Exception:
            continue
        # late-join tolerance: a session that starts late must still pick up
        # releases from the last 75 min - the FMP layer posts them immediately
        # (late but never silent). Handlers for already-released events simply
        # never fire; the USD->FMP fallback covers them.
        if dt.date() != today or not (now_et - timedelta(minutes=75) <= dt <= session_end_et):
            continue
        # USD majors: official-API handler with 1s burst. Everything else
        # (EUR, GBP, JPY, CAD, ...): FMP layer at a relaxed cadence.
        h = match_handler(ev.get("title", "")) if cur == "USD" else None
        w = {"ev": ev, "dt": dt, "h": h, "done": False, "page": None, "metric": None}
        if cur == "USD":
            for pat, pkey, metric in PAGE_METRICS:
                if re.search(pat, ev.get("title", "")):
                    w["page"], w["metric"] = pkey, metric
                    break
        watch.append(w)

    if not watch:
        print("No watchable data events in this window. Exiting.")
        return
    print("Watching:", ", ".join(f"{w['ev'].get('country','?')} {w['ev']['title']}" for w in watch))
    for w in watch:
        if w["h"] is None:
            continue
        try:
            w["h"].snapshot()
        except Exception as e:
            print(f"[warn] snapshot failed for {w['ev']['title']}: {e}")

    actuals = load_json(ACTUALS_FILE, {})
    import fmp as F
    from datetime import timezone as _tzu
    next_fmp = 0.0

    # one PageWatch per unique BLS release page among today's events
    pages = {}
    for w in watch:
        if w["page"]:
            pages.setdefault(w["page"], PageWatch(w["page"], BLS_PAGES[w["page"]]))
    if pages:
        print("Page layer armed:", ", ".join(pages))

    while time.time() < deadline and any(not w["done"] for w in watch):
        now = datetime.now(ET)

        # ---- Layer 0: official news-release pages, 1s conditional GETs ----
        # (separate host from api.bls.gov; a 304 costs ~0.03s)
        for pkey, pw in pages.items():
            if pw.fresh or time.time() < pw.next_poll:
                continue
            mapped = [w for w in watch if not w["done"] and w["page"] == pkey]
            if not mapped:
                continue
            # poll only in the window release-20s .. release+4min
            if not any(w["dt"] - timedelta(seconds=20) <= now <= w["dt"] + timedelta(minutes=4)
                       for w in mapped):
                continue
            pw.next_poll = time.time() + 1
            try:
                pw.poll(now.date())
            except Exception as e:
                print(f"[warn] page poll failed {pkey}: {e}")
                pw.next_poll = time.time() + 2
                continue
            if not pw.fresh:
                continue
            print(f"PAGE LIVE: {pkey} release is up - parsing")
            for w in mapped:
              try:   # one bad event must NEVER kill the session
                v = pg_extract(pkey, w["metric"], pw.text)
                if v is None:
                    print(f"[warn] page parse missed {w['ev']['title']} - left to API/FMP")
                    continue
                title = w["ev"]["title"]
                fc_raw = w["ev"].get("forecast")
                actual_s = F.fmt_like(v, fc_raw) if fc_raw else f"{v}%"
                fc = parse_forecast(fc_raw)
                if fc is not None:
                    verdict = ("ABOVE FORECAST" if v > fc else
                               ("BELOW FORECAST" if v < fc else "IN LINE"))
                    msg = f"\U0001f6a8 **US {title}: {actual_s}** vs forecast {fc_raw} — **{verdict}**"
                else:
                    msg = f"\U0001f6a8 **US {title}: {actual_s}**"
                try:
                    post_discord(msg)
                    print("POSTED(PAGE):", msg)
                    w["done"] = True
                    actuals[f"{w['dt'].astimezone(_tzu.utc).date().isoformat()}|USD|{title}"] = actual_s
                except Exception as e:
                    print(f"[warn] discord post failed: {e}")
              except Exception as e:
                print(f"[warn] page event {w['ev'].get('title','?')} failed: {e}")

        # ---- FMP layer: global events without an official-API handler ----
        if time.time() >= next_fmp:
            # FMP covers: (a) non-US events, (b) US events whose official-API
            # handler hasn't delivered 150s after the release (BLS hiccup) -
            # the number must reach the channel no matter which source wins.
            # Handler-backed events fall back to FMP after 45s (was 150s: CPI
            # 2026-09-11 posted +2m28s..+3m32s because BLS 503'd and the
            # fallback sat idle) - or IMMEDIATELY once the handler has failed
            # twice after the release (it is clearly down, no point waiting).
            pend = [w for w in watch if not w["done"] and (
                        (w["h"] is None and now >= w["dt"] - timedelta(minutes=2)) or
                        (w["h"] is not None and now >= w["dt"] and (
                            now >= w["dt"] + timedelta(seconds=45) or w.get("errs", 0) >= 2)))]
            if pend and F.API_KEY:
                rows = F.fetch(today.isoformat(), today.isoformat())
                for w in pend:
                  try:   # one bad event must NEVER kill the session
                    ev = w["ev"]
                    cur = ev.get("country", "").upper()
                    title = str(ev.get("title", ""))
                    try:
                        when = w["dt"].astimezone(_tzu.utc)
                    except Exception:
                        when = None
                    a, _est = F.find_actual(rows, title, cur, when_utc=when)
                    if a is None:
                        continue
                    fc_raw = ev.get("forecast")
                    actual_s = F.fmt_like(a, fc_raw)
                    fc, av = parse_forecast(fc_raw), parse_forecast(actual_s)
                    if fc is not None and av is not None:
                        verdict = "ABOVE FORECAST" if av > fc else ("BELOW FORECAST" if av < fc else "IN LINE")
                        msg = f"🚨 **{cur} {title}: {actual_s}** vs forecast {fc_raw} — **{verdict}**"
                    else:
                        msg = f"🚨 **{cur} {title}: {actual_s}**"
                    try:
                        post_discord(msg)
                        print("POSTED(FMP):", msg)
                        w["done"] = True
                        actuals[f"{w['dt'].astimezone(_tzu.utc).date().isoformat()}|{cur}|{title}"] = actual_s
                    except Exception as e:
                        print(f"[warn] discord post failed: {e}")
                  except Exception as e:
                    print(f"[warn] FMP event {w['ev'].get('title','?')} failed: {e}")
            # adaptive cadence: 10s while an FMP-covered release is inside its
            # hot window (release-2min .. release+10min) so it lands within
            # seconds of FMP publishing; 60s otherwise. Starter plan allows it.
            hot_fmp = any((not w["done"]) and
                          w["dt"] - timedelta(minutes=2) <= now <= w["dt"] + timedelta(minutes=10)
                          for w in watch)
            next_fmp = time.time() + (10 if hot_fmp else 60)

        polled_hot = False
        for w in watch:
            if w["done"] or w["h"] is None or now < w["dt"] - timedelta(seconds=30):
                continue
            # quota guard: past release+10min FMP owns the event - stop burning
            # the BLS/BEA daily key on an API that clearly isn't updating.
            if now > w["dt"] + timedelta(minutes=10):
                continue
            # BLS rate-limit guard (Friday NFP: 1s hammering from T-45s made
            # BLS 503 the key for the rest of the day). Rules now:
            #   * capture window = release-5s .. release+90s -> 1s cadence,
            #     but at most ONE hot API call per loop cycle (round-robin);
            #   * outside the window -> one call per event every 20s.
            hot_w = (w["dt"] - timedelta(seconds=5)) <= now <= (w["dt"] + timedelta(seconds=90))
            if hot_w and polled_hot:
                continue
            if time.time() < w.get("next_poll", 0.0):
                continue
            w["next_poll"] = time.time() + (1 if hot_w else 20)
            if hot_w:
                polled_hot = True
            try:
                res = w["h"].value()
            except Exception as e:
                if now >= w["dt"]:
                    w["errs"] = w.get("errs", 0) + 1   # lets FMP take over at once
                print(f"[warn] poll failed {w['ev']['title']}: {e}")
                continue
            if res is None:
                continue
            v, unit = res
            title = w["ev"]["title"]
            fc_raw = w["ev"].get("forecast")
            if v == "STATEMENT":
                msg = f"🚨 **US — FOMC statement released.** Rate decision & guidance are out."
            else:
                actual_s = fmt_actual(v, unit)
                fc = parse_forecast(fc_raw)
                if fc is not None:
                    verdict = "ABOVE FORECAST" if float(v) > fc else ("BELOW FORECAST" if float(v) < fc else "IN LINE")
                    msg = (f"🚨 **US {title}: {actual_s}** vs forecast {fc_raw} — **{verdict}**")
                else:
                    msg = f"🚨 **US {title}: {actual_s}**"
                # HORARIO E CHAVE: key by the event's UTC date so the 23:59
                # recap (which now groups/looks up by UTC day) finds it.
                actuals[f"{w['dt'].astimezone(_tzu.utc).date().isoformat()}|USD|{title}"] = actual_s
            try:
                post_discord(msg)
                print("POSTED:", msg)
                w["done"] = True
            except Exception as e:
                print(f"[warn] discord post failed: {e}")
        # Burst mode: 1s loop cadence only while some event is inside its
        # capture window (release-5s .. release+90s); relaxed otherwise.
        # Per-event next_poll above keeps the API call count quota-safe.
        hot = any(
            (not w["done"]) and (w["dt"] - timedelta(seconds=5) <= now <= w["dt"] + timedelta(seconds=90))
            for w in watch
        )
        time.sleep(POLL_FAST if hot else POLL_SECONDS)

    save_json(ACTUALS_FILE, actuals)
    print("Session ended. Captured:", sum(1 for w in watch if w["done"]), "/", len(watch))


if __name__ == "__main__":
    main()
