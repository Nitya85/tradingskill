#!/usr/bin/env python3
"""The setups, the fills and the costs, defined once. Standard library only.

Every other script that needs to know what a setup is (trader-backtest,
trader-entry for vwap and gap, trader-timemachine's history) loads this file,
so a backtest judges exactly the rule the entry timer fires on.

A session is a list of 5-minute bars, slot 0 = the 9:30 bar, each a dict with
o, h, l, c, v. Long side only, one trade per setup per session (the first
trigger), like the rest of the pack.

Setups
  orb   opening-range breakout: the first bar after the first `or_minutes`
        that CLOSES above the range high. Stop: or_mid (default) or or_low.
  vwap  VWAP reclaim: after at least `min_below` bars in a row closed under
        the session VWAP, the first bar that closes back above it. Stop: the
        lowest low of that run under VWAP (swing_low), or pct. Skipped when
        the stop is more than `max_risk_pct` % under the entry.
  gap   gap and go: the session opened at least `min_gap_pct` % over the
        previous close, the gap has not filled (no low under the previous
        close before the trigger), and a bar closes above the high of the
        first `range_bars` bars. Stop: range_low (default) or range_mid.

Entries only between no_entries_before and no_entries_after (bar CLOSE time,
New York), the same window trader-risk enforces.

The fill model (simulate_trade), the honest version of "it touched the stop":
  entry   the OPEN of the bar after the trigger bar (nobody fills at the exact
          close they just saw), plus half the spread and the slippage.
          Skipped, like trader-budget would, when that open is already at or
          under the stop or at or over the target.
  stop    a stop becomes a market sell: filled at the stop, or at the bar's
          open when the bar gaps through it, minus half the spread and the
          slippage.
  target  a limit sell: only when the high trades at least a cent THROUGH the
          target (a touch is not a fill), at the target (or a better open).
  both    a bar that reaches both counts as the stop.
  flat    still open at flat_by: sold at that bar's close, minus half the
          spread and the slippage.
  fees    commission per share on both sides, a regulatory fee on the sale.
R is measured against the risk you sized for (trigger close minus stop), so
a gap through the stop shows up as worse than -1R, the way it does in a real
account.
"""

import math

SESSION_BARS = 78
OPEN_MIN = 9 * 60 + 30
TICK = 0.01

SETUPS = ("orb", "vwap", "gap")
SETUP_WORDS = {"orb": "opening-range breakout", "vwap": "VWAP reclaim", "gap": "gap and go"}

DEFAULT_PARAMS = {
    "orb": {"or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0, "min_volume_x": None},
    "vwap": {"min_below": 3, "stop_rule": "swing_low", "stop_pct": 0.5, "target_r": 1.5,
             "max_risk_pct": 2.0, "min_volume_x": None},
    "gap": {"min_gap_pct": 2.0, "range_bars": 1, "stop_rule": "range_low", "target_r": 1.5,
            "min_volume_x": None},
}

# The parameter grid the walk-forward picks from. Kept small on purpose: every
# extra combination is one more chance to find luck and call it an edge.
GRID = {
    "orb": [{"or_minutes": m, "stop_rule": s, "target_r": r}
            for m in (15, 30) for s in ("or_mid", "or_low") for r in (1.0, 1.5, 2.0)],
    "vwap": [{"min_below": b, "stop_rule": s, "target_r": r}
             for b in (3, 6) for s in ("swing_low", "pct") for r in (1.0, 1.5, 2.0)],
    "gap": [{"min_gap_pct": g, "stop_rule": s, "target_r": r}
            for g in (2.0, 4.0) for s in ("range_low", "range_mid") for r in (1.0, 1.5, 2.0)],
}

DEFAULT_COSTS = {
    "spread_bps": 4.0,            # the full bid-ask spread, in basis points of the price
    "min_spread_cents": 1.0,      # never less than a cent wide
    "slippage_bps": 3.0,          # extra on every market fill (the entry, a stop, the flat-by sale)
    "commission_per_share": 0.0,  # Alpaca: 0. IBKR Pro fixed: 0.005
    "sell_fee_bps": 0.3,          # SEC fee + FINRA TAF on the sale, roughly; check your broker
}

STOP_RULES = {"orb": ("or_mid", "or_low", "pct"), "vwap": ("swing_low", "pct"),
              "gap": ("range_low", "range_mid", "pct")}


class SetupError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def slot_end_min(k):
    """Minutes after midnight when slot k's bar closes."""
    return OPEN_MIN + 5 * (k + 1)


def entry_window(no_before="09:45", no_after="15:30"):
    """(first, last) slots whose bar CLOSE time is inside the entry window."""
    def mins(t):
        h, m = str(t).split(":")
        return int(h) * 60 + int(m)
    b, a = mins(no_before), mins(no_after)
    first = max(0, int(math.ceil((b - OPEN_MIN) / 5.0)) - 1)
    last = (a - OPEN_MIN) // 5 - 1
    return first, last


def flat_slot(flat_by="15:55"):
    """The slot whose bar closes at flat_by (15:55 -> 76, the 15:50 bar)."""
    h, m = str(flat_by).split(":")
    return (int(h) * 60 + int(m) - OPEN_MIN) // 5 - 1


def cents_down(x):
    """A long's stop rounds down to the cent, away from the entry."""
    return math.floor(round(x * 100, 4)) / 100.0


def cents(x):
    return math.floor(x * 100 + 0.5 + 1e-9) / 100.0


def params_for(setup, params=None):
    if setup not in SETUPS:
        raise SetupError("unknown setup %r: the setups are %s" % (setup, ", ".join(SETUPS)))
    p = dict(DEFAULT_PARAMS[setup])
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    if p["stop_rule"] not in STOP_RULES[setup]:
        raise SetupError("a %s stop is %s, not %r" % (setup, " or ".join(STOP_RULES[setup]), p["stop_rule"]))
    if not p["target_r"] > 0:
        raise SetupError("target_r must be above 0")
    return p


def vwap_series(bars):
    """Session VWAP after each bar, from the typical price (h + l + c) / 3. None
    while no volume has traded."""
    out, pv, vol = [], 0.0, 0.0
    for b in bars:
        v = float(b.get("v") or 0)
        pv += v * (b["h"] + b["l"] + b["c"]) / 3.0
        vol += v
        out.append(pv / vol if vol > 0 else None)
    return out


def _volume_ok(b, avg, need):
    if not need:
        return True
    return bool(avg) and float(b.get("v") or 0) / avg >= need


# ---------------------------------------------------------------- the setups

def signal(setup, bars, prev_close=None, params=None, window=(2, 71)):
    """The first trigger of `setup` in one session, or None.

    bars       the session's closed 5-minute bars from 9:30 on, in order
    prev_close the previous session's close (the gap setup needs it)
    window     (first, last) slots allowed to trigger (entry_window())

    Returns {"index", "trigger", "stop", "target", "risk", "level", "why"}: the
    trigger bar's slot and close, the stop and target from the setup's rules
    and the risk a share you would size for."""
    p = params_for(setup, params)
    first, last = window
    last = min(last, len(bars) - 1)
    if last < 0:
        return None

    if setup == "orb":
        n = int(p["or_minutes"]) // 5
        if n < 1 or len(bars) <= n:
            return None
        oh, ol = max(b["h"] for b in bars[:n]), min(b["l"] for b in bars[:n])
        avg = sum(float(b.get("v") or 0) for b in bars[:n]) / n
        for k in range(max(n, first), last + 1):
            b = bars[k]
            if b["c"] > oh and _volume_ok(b, avg, p.get("min_volume_x")):
                e = cents(b["c"])
                if p["stop_rule"] == "or_mid":
                    s = cents_down((oh + ol) / 2.0)
                elif p["stop_rule"] == "or_low":
                    s = cents_down(ol)
                else:
                    s = cents_down(e * (1 - p.get("stop_pct", 0.5) / 100.0))
                return _finish(k, e, s, p, oh, "closed above the %d-minute range high %.2f" % (n * 5, oh))
        return None

    if setup == "vwap":
        vw = vwap_series(bars)
        need = int(p["min_below"])
        run, run_low = 0, None
        vols = [float(b.get("v") or 0) for b in bars[:max(1, first)]]
        avg = sum(vols) / len(vols) if vols else 0
        for k in range(0, last + 1):
            b, w = bars[k], vw[k]
            if w is None:
                continue
            if b["c"] < w:
                run += 1
                run_low = b["l"] if run == 1 else min(run_low, b["l"])
                continue
            if run >= need and k >= first and b["c"] > w and _volume_ok(b, avg, p.get("min_volume_x")):
                e = cents(b["c"])
                if p["stop_rule"] == "swing_low":
                    s = cents_down(min(run_low, b["l"]))
                else:
                    s = cents_down(e * (1 - p.get("stop_pct", 0.5) / 100.0))
                if e - s > e * p.get("max_risk_pct", 2.0) / 100.0:
                    run, run_low = 0, None
                    continue                  # stop too far away: not this one, maybe a later reclaim
                return _finish(k, e, s, p, w, "closed back above VWAP %.2f after %d bars under it" % (w, run))
            run, run_low = 0, None
        return None

    # gap and go
    if not prev_close or prev_close <= 0 or not bars:
        return None
    gap = bars[0]["o"] / prev_close - 1.0
    if gap * 100.0 < p["min_gap_pct"]:
        return None
    n = max(1, int(p["range_bars"]))
    if len(bars) <= n:
        return None
    rh, rl = max(b["h"] for b in bars[:n]), min(b["l"] for b in bars[:n])
    avg = sum(float(b.get("v") or 0) for b in bars[:n]) / n
    for k in range(0, last + 1):
        b = bars[k]
        if b["l"] < prev_close:
            return None                       # the gap filled: no go
        if k < max(n, first):
            continue
        if b["c"] > rh and _volume_ok(b, avg, p.get("min_volume_x")):
            e = cents(b["c"])
            if p["stop_rule"] == "range_low":
                s = cents_down(rl)
            elif p["stop_rule"] == "range_mid":
                s = cents_down((rh + rl) / 2.0)
            else:
                s = cents_down(e * (1 - p.get("stop_pct", 0.5) / 100.0))
            s = max(s, cents_down(prev_close))  # a stop under the previous close means the gap failed anyway
            return _finish(k, e, s, p, rh, "gapped %.1f %% and closed above the first %d-minute high %.2f"
                           % (gap * 100, n * 5, rh))
    return None


def _finish(k, e, s, p, level, why):
    risk = round(e - s, 4)
    if risk < max(TICK, e * 0.0005):
        return None                           # too tight to survive the spread (trader-exit refuses it too)
    t = cents(e + p["target_r"] * risk)
    return {"index": k, "trigger": e, "stop": s, "target": t, "risk": risk, "level": round(level, 4),
            "target_r": p["target_r"], "why": why}


# ---------------------------------------------------------------- costs and fills

def costs_for(cfg_costs=None):
    c = dict(DEFAULT_COSTS)
    c.update({k: float(v) for k, v in (cfg_costs or {}).items() if k in DEFAULT_COSTS and v is not None})
    for k, v in c.items():
        if v < 0:
            raise SetupError("costs.%s cannot be negative" % k)
    return c


def half_spread(price, costs):
    return max(price * costs["spread_bps"] / 20000.0, costs["min_spread_cents"] / 200.0)


def buy_fill(price, costs):
    """What a market buy really pays."""
    return price + half_spread(price, costs) + price * costs["slippage_bps"] / 10000.0


def sell_fill(price, costs):
    """What a market sell (a triggered stop, a flat-by sale) really gets."""
    return price - half_spread(price, costs) - price * costs["slippage_bps"] / 10000.0


def cost_per_share(entry, stop, costs):
    """Expected extra loss a share at the stop, on top of entry - stop: both
    market fills, the fees. trader-budget and trader-risk size with this."""
    return ((buy_fill(entry, costs) - entry) + (stop - sell_fill(stop, costs))
            + stop * costs["sell_fee_bps"] / 10000.0 + 2 * costs["commission_per_share"])


def simulate_trade(bars, sig, costs, flat=76, fill="next_open"):
    """Walk one signal through the rest of its session with the fill model above.
    Returns None when the trade would not have been taken (the next open is
    past the stop or the target, or the data ends), else a dict with the
    outcome and R before and after costs."""
    k = sig["index"]
    stop, target, risk = sig["stop"], sig["target"], sig["risk"]
    if fill == "next_open":
        if k + 1 >= len(bars) or k + 1 > flat:
            return None
        raw_in = bars[k + 1]["o"]
        start = k + 1
        if raw_in <= stop or raw_in >= target:
            return None
    else:                                     # "close": the old, optimistic model
        raw_in = sig["trigger"]
        start = k + 1
    paid = buy_fill(raw_in, costs)
    end = min(flat, len(bars) - 1)
    if start > end:
        return None
    outcome = raw_out = got = None
    at = end
    for i in range(start, end + 1):
        b = bars[i]
        first_bar = i == start and fill == "next_open"
        if b["l"] <= stop:
            raw_out = stop if first_bar else min(stop, b["o"])
            got, outcome, at = sell_fill(raw_out, costs), "stop", i
            break
        if b["h"] >= target + TICK - 1e-9:
            raw_out = target if first_bar else max(target, b["o"])
            got, outcome, at = raw_out, "target", i
            break
    if outcome is None:
        raw_out = bars[end]["c"]
        got, outcome = sell_fill(raw_out, costs), "flat"
    fees = got * costs["sell_fee_bps"] / 10000.0 + 2 * costs["commission_per_share"]
    gross = (raw_out - raw_in) / risk
    net = (got - fees - paid) / risk
    return {"outcome": outcome, "entry_slot": start, "exit_slot": at, "raw_entry": round(raw_in, 4),
            "paid": round(paid, 4), "raw_exit": round(raw_out, 4), "got": round(got - fees, 4),
            "R_gross": round(gross, 4), "R": round(net, 4), "cost_R": round(gross - net, 4)}
