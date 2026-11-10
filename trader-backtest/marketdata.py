#!/usr/bin/env python3
"""Market data for trader-backtest: 60 days of 5-minute bars a ticker from
Yahoo (cached), split into sessions, and checked before anything trusts them.
Standard library only.

Data checks (qa), each with what happens:
  bad bar         high under the open/close, low over them, a price at or
                  under 0: that session is left out
  duplicate       the same 5-minute timestamp twice: the later copy is kept,
                  counted as a warning
  outlier bar     a bar's range more than 15 times the ticker's median range
                  for that time of day: that session is left out (a bad print
                  or a halt)
  zero volume     more than 5 % of a ticker's bars traded nothing: warning
  overnight jump  an open more than 40 % away from the previous close (a
                  split, a reverse split or bad data): the ticker is left out
  stale           the newest bar is more than 4 days older than now: warning
  too cheap       median price under min_price (default $5): left out, the
                  cost model does not hold for pennies
  too thin        median daily dollar volume under min_dollar_volume (default
                  $20M): left out, same reason
  Stooq           optional (--crosscheck): each session's last close against
                  Stooq's daily close; a gap over 0.5 % is a warning
"""

import datetime
import gzip
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.1; trader-backtest)"
PAUSE = 0.5
BACKOFF = 5.0
INTERVAL = 300
SESSION_BARS = 78
OPEN_MIN = 9 * 60 + 30
EPOCH = datetime.datetime(1970, 1, 1)

OUTLIER_X = 15.0
ZERO_VOL_SHARE = 0.05
JUMP = 0.40
STALE_DAYS = 4
STOOQ_TOL = 0.005

# A default universe for testing a setup across many stocks: liquid US large
# caps and ETFs. They are the stocks that are big TODAY (survivorship bias):
# results on them flatter a setup a little compared with the whole market.
DEFAULT_UNIVERSE = (
    "SPY QQQ IWM AAPL MSFT NVDA AMZN META GOOGL TSLA AMD AVGO NFLX CRM ORCL ADBE INTC MU QCOM "
    "JPM BAC WFC GS MS C V MA PYPL XOM CVX COP UNH LLY JNJ PFE MRK ABBV WMT COST HD "
    "NKE DIS BA CAT GE F GM UBER SHOP PLTR COIN SBUX MCD KO PEP T VZ"
).split()


class DataError(Exception):
    pass


# ---------------------------------------------------------------- New York time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


# ---------------------------------------------------------------- fetching

def chart_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
        host, urllib.parse.quote(sym.upper().replace(".", "-")))


def stooq_daily_url(sym):
    return "https://stooq.com/q/d/l/?s=%s.us&i=d" % sym.lower().replace(".", "-")


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_yahoo(sym, sleep=time.sleep):
    last = None
    for n, host in enumerate(("query2", "query1")):
        if n:
            sleep(PAUSE)
        for attempt in (0, 1):
            try:
                return _get(chart_url(sym, host))
            except urllib.error.HTTPError as e:
                last = e.code
                if e.code == 429 and attempt == 0:
                    sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                raise DataError("Yahoo answered HTTP %d for %s" % (e.code, sym))
            except (urllib.error.URLError, OSError) as e:
                raise DataError("could not reach Yahoo for %s: %s" % (sym, getattr(e, "reason", e)))
    raise DataError("Yahoo answered HTTP %s for %s on both hosts" % (last, sym))


def offline_text(folder, sym):
    for name in ("%s-60d5m.json.gz" % sym, "%s-60d5m.json" % sym, "%s.json.gz" % sym, "%s.json" % sym):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt", encoding="utf-8") as fh:
                return fh.read()
    raise DataError("no %s-60d5m.json(.gz) in %s" % (sym, folder))


def load_chart(sym, offline=None, cache_dir="trader/cache", today=None, fetch=None, sleep=time.sleep):
    """Chart JSON text for sym: from --offline, from today's cache, or from Yahoo
    (then cached as <cache>/<SYM>-<date>-60d5m.json.gz)."""
    sym = sym.upper()
    if offline:
        return offline_text(offline, sym), "offline"
    today = today or to_eastern(utc_now()).date().isoformat()
    path = os.path.join(cache_dir, "%s-%s-60d5m.json.gz" % (sym, today)) if cache_dir else None
    if path and os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read(), "cache"
    text = (fetch or fetch_yahoo)(sym)
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        with gzip.open(path + ".tmp", "wt", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(path + ".tmp", path)
    sleep(PAUSE)
    return text, "yahoo"


# ---------------------------------------------------------------- parsing

def parse(text, sym, now_utc=None):
    """Chart JSON -> (bars, counts). Regular-session 5-minute bars that have
    closed, oldest first; duplicates keep the later copy."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise DataError("no usable chart data for %s" % sym)
    meta = res.get("meta") or {}
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != sym.upper().replace(".", "-"):
        raise DataError("the data is for %s, not %s" % (msym, sym))
    if meta.get("dataGranularity") not in (None, "5m"):
        raise DataError("%s: %s bars, the backtest needs 5-minute bars" % (sym, meta.get("dataGranularity")))
    now_utc = now_utc or utc_now()
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    counts = {"no_price": 0, "off_grid": 0, "outside_hours": 0, "not_complete": 0, "duplicate": 0}
    bars = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            counts["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            counts["off_grid"] += 1
            continue
        start = utc_of(ts)
        local = to_eastern(start)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            counts["outside_hours"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > now_utc:
            counts["not_complete"] += 1
            continue
        if ts in bars:
            counts["duplicate"] += 1
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5,
                    "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v or 0)}
    return [bars[t] for t in sorted(bars)], counts


def sessions_of(bars):
    """[(date, [78 bars], prev_close or None)] for FULL sessions only, oldest
    first, plus the dates of short sessions (half days, gaps) left out. The
    previous close comes from the previous session in the data, full or not."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    full, short, prev = [], [], None
    for d in sorted(days):
        s = days[d]
        last_close = s[max(s)]["c"]
        if len(s) == SESSION_BARS:
            full.append((d, [s[i] for i in range(SESSION_BARS)], prev))
        else:
            short.append(d)
        prev = last_close
    return full, short


# ---------------------------------------------------------------- checks

def _bad_bar(b):
    return (min(b["o"], b["h"], b["l"], b["c"]) <= 0 or b["h"] < max(b["o"], b["c"]) - 1e-9
            or b["l"] > min(b["o"], b["c"]) + 1e-9)


def qa(sym, sessions, counts=None, now_utc=None, min_price=5.0, min_dollar_volume=20e6):
    """Check one ticker's full sessions. Returns (kept_sessions, report) where
    report = {"exclude": reason or None, "dropped_sessions": {date: why},
    "warnings": [...]}."""
    rep = {"symbol": sym, "exclude": None, "dropped_sessions": {}, "warnings": []}
    if counts and counts.get("duplicate"):
        rep["warnings"].append("%d duplicate bar(s): kept the later copy" % counts["duplicate"])
    if not sessions:
        rep["exclude"] = "no full sessions in the data"
        return [], rep
    # Each bar against the median range of the SAME time slot on the other days:
    # the 9:30 bar is always wider than the 1:00 bar, and that is not bad data.
    by_slot = [[] for _ in range(SESSION_BARS)]
    for _, D, _ in sessions:
        for i, b in enumerate(D):
            if b["c"] > 0:
                by_slot[i].append((b["h"] - b["l"]) / b["c"])
    med = [statistics.median(x) if x else 0 for x in by_slot]
    kept = []
    for d, D, pc in sessions:
        if any(_bad_bar(b) for b in D):
            rep["dropped_sessions"][d] = "a bar with an impossible high, low or price"
            continue
        if any(med[i] > 0 and (b["h"] - b["l"]) / b["c"] > OUTLIER_X * med[i] for i, b in enumerate(D)):
            rep["dropped_sessions"][d] = ("an outlier bar (range over %gx that time of day's median): "
                                          "a bad print or a halt" % OUTLIER_X)
            continue
        if pc and abs(D[0]["o"] / pc - 1) > JUMP:
            rep["exclude"] = ("%s opened %.0f %% away from the previous close on %s: a split or bad data"
                              % (sym, 100 * (D[0]["o"] / pc - 1), d))
            return [], rep
        kept.append((d, D, pc))
    nbars = sum(len(D) for _, D, _ in sessions)
    zero = sum(1 for _, D, _ in sessions for b in D if not b["v"])
    if nbars and zero / float(nbars) > ZERO_VOL_SHARE:
        rep["warnings"].append("%.0f %% of the bars show no volume: thin or patchy data" % (100.0 * zero / nbars))
    closes = [D[-1]["c"] for _, D, _ in kept] or [0]
    dollar = [sum(b["c"] * b["v"] for b in D) for _, D, _ in kept] or [0]
    if statistics.median(closes) < min_price:
        rep["exclude"] = "median price $%.2f is under $%.2f" % (statistics.median(closes), min_price)
        return [], rep
    if statistics.median(dollar) < min_dollar_volume:
        rep["exclude"] = ("median dollar volume $%.1fM a day is under $%.0fM: spreads too wide for the cost model"
                          % (statistics.median(dollar) / 1e6, min_dollar_volume / 1e6))
        return [], rep
    if now_utc:
        newest = datetime.date.fromisoformat(sessions[-1][0])
        age = (to_eastern(now_utc).date() - newest).days
        if age > STALE_DAYS:
            rep["warnings"].append("the newest full session is %s, %d days old: stale data" % (sessions[-1][0], age))
    return kept, rep


def parse_stooq_daily(text):
    """Stooq daily CSV (Date,Open,High,Low,Close,Volume) -> {date: close}."""
    out = {}
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    if not lines or not lines[0].lower().startswith("date"):
        return out
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            out[parts[0]] = float(parts[4])
        except ValueError:
            continue
    return out


def crosscheck(sym, sessions, stooq_closes, tol=STOOQ_TOL):
    """Each full session's last 5-minute close against Stooq's daily close.
    Returns {"checked": n, "mismatches": [(date, yahoo, stooq, pct)]}."""
    bad, n = [], 0
    for d, D, _ in sessions:
        s = stooq_closes.get(d)
        if not s:
            continue
        n += 1
        y = D[-1]["c"]
        if abs(y / s - 1) > tol:
            bad.append((d, round(y, 2), round(s, 2), round(100 * (y / s - 1), 2)))
    return {"checked": n, "mismatches": bad}
