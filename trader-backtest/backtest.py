#!/usr/bin/env python3
"""Backtest for trader-backtest: does a setup actually make money, after
costs, on data it was not tuned on? Standard library only.

    python3 backtest.py run                         # orb, vwap and gap on the default universe
    python3 backtest.py run --setup orb --tickers TSLA,NVDA,AMD
    python3 backtest.py run --setup vwap --tickers-file trader/watchlist.json --write
    python3 backtest.py run --setup orb --params '{"or_minutes": 15, "stop_rule": "or_mid", "target_r": 1}'
    python3 backtest.py run --offline tests/fixtures/trader-timemachine --tickers TSLA
    python3 backtest.py show                        # the verdicts in trader/state/edge.json

How it judges a setup, in order:
  1. Data. 60 days of 5-minute bars for every ticker (Yahoo's limit for
     5-minute bars), full sessions only, checked by marketdata.qa: bad
     bars, outliers, splits, thin and cheap stocks are left out and listed.
  2. Trades. The setup's rule (setups.py, the same file trader-entry fires
     on) on every session of every ticker, entries only inside your entry
     window, then the fill model: in at the NEXT bar's open, stops filled
     through gaps, targets only when traded through, out by flat_by, spread,
     slippage and fees on every fill.
  3. Walk-forward. The sessions are split by DATE: the first --train share
     (default 60 %) is in-sample, the rest out-of-sample. Each parameter set
     in the setup's small grid is run in-sample, the best average R wins,
     and only that one is run out-of-sample. Only the out-of-sample result
     counts. With --params the grid is skipped and every session counts.
  4. Uncertainty. Trades on the same day move together (the whole market
     moves), so the 95 % range of the average R comes from resampling whole
     DAYS (a cluster bootstrap, 2,000 draws, fixed seed), not single trades.
     The win rate gets a Wilson 95 % interval.
  5. Verdict, out-of-sample:
       NOT ENOUGH EVIDENCE   fewer than 30 trades, or the 95 % range of the
                             average R straddles 0 with a positive average
       EDGE                  the whole 95 % range is above 0 R
       NO EDGE               the average R is 0 or below
     trader-risk reads it from trader/state/edge.json and blocks live entries
     on a setup without an EDGE verdict from the last 7 days.

What it cannot see: 60 days is one market regime. Earnings, halts and news
days are in the data like any other day. Large caps that are large TODAY
flatter the result (survivorship). An EDGE verdict is evidence, not a
promise; NO EDGE on 60 days is strong evidence the other way.

trader/config.json (optional): "costs" {spread_bps, min_spread_cents,
slippage_bps, commission_per_share, sell_fee_bps}, "no_entries_before",
"no_entries_after", "flat_by", "min_price", "min_dollar_volume".
"""

import argparse
import datetime
import json
import math
import os
import random
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import marketdata as md  # noqa: E402
import setups as st      # noqa: E402

MIN_TRADES = 30
MIN_TRAIN_TRADES = 20
BOOT = 2000
SEED = 19
TRAIN = 0.6
EDGE_MAX_AGE_DAYS = 7


class BacktestError(Exception):
    pass


# ---------------------------------------------------------------- statistics

def wilson(k, n, z=1.96):
    if n <= 0:
        return None, None
    p = k / float(n)
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def cluster_ci(trades, key="date", boot=BOOT, seed=SEED):
    """95 % range of the mean R, resampling whole days."""
    groups = {}
    for t in trades:
        groups.setdefault(t[key], []).append(t["R"])
    days = list(groups.values())
    if len(days) < 2:
        return None, None
    rng = random.Random(seed)
    means = []
    for _ in range(boot):
        tot = cnt = 0
        for _ in range(len(days)):
            g = days[rng.randrange(len(days))]
            tot += sum(g)
            cnt += len(g)
        means.append(tot / cnt)
    means.sort()
    return means[int(0.025 * boot)], means[min(boot - 1, int(0.975 * boot))]


def max_drawdown(rs):
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def stats(trades):
    n = len(trades)
    if not n:
        return {"trades": 0, "days": 0}
    rs = [t["R"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    gw = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    lo, hi = wilson(wins, n)
    clo, chi = cluster_ci(trades)
    by = {}
    for t in trades:
        by[t["outcome"]] = by.get(t["outcome"], 0) + 1
    ordered = [t["R"] for t in sorted(trades, key=lambda t: (t["date"], t["symbol"]))]
    return {
        "trades": n, "days": len({t["date"] for t in trades}),
        "tickers": len({t["symbol"] for t in trades}),
        "win_rate": round(wins / float(n), 4), "win_rate_95": [round(lo, 4), round(hi, 4)],
        "avg_R": round(statistics.mean(rs), 4),
        "avg_R_95": [round(clo, 4), round(chi, 4)] if clo is not None else None,
        "avg_R_before_costs": round(statistics.mean(t["R_gross"] for t in trades), 4),
        "avg_cost_R": round(statistics.mean(t["cost_R"] for t in trades), 4),
        "total_R": round(sum(rs), 2),
        "profit_factor": round(gw / gl, 3) if gl > 0 else None,
        "max_drawdown_R": round(max_drawdown(ordered), 2),
        "worst_R": round(min(rs), 3), "best_R": round(max(rs), 3),
        "outcomes": by,
    }


def verdict(s):
    """EDGE / NO EDGE / NOT ENOUGH EVIDENCE, with the reason in plain words."""
    n = s.get("trades", 0)
    lo, hi = s.get("avg_R_95") or (None, None)
    if n < MIN_TRADES:
        why = "%d trade%s out-of-sample, under the %d it takes to say anything" % (n, "" if n == 1 else "s", MIN_TRADES)
        if n and lo is not None:
            why += "; so far %+.3f R a trade after costs (95 %% range %+.3f to %+.3f)" % (s["avg_R"], lo, hi)
        return "NOT ENOUGH EVIDENCE", why
    if lo is not None and lo > 0:
        return "EDGE", ("average %+.3f R a trade after costs, 95 %% range %+.3f to %+.3f: above zero"
                        % (s["avg_R"], lo, hi))
    if s["avg_R"] <= 0:
        return "NO EDGE", ("average %+.3f R a trade after costs over %d trades (95 %% range %+.3f to %+.3f): "
                           "it lost money" % (s["avg_R"], n, lo, hi))
    return "NOT ENOUGH EVIDENCE", ("average %+.3f R a trade after costs, but the 95 %% range %+.3f to %+.3f "
                                   "includes losing money" % (s["avg_R"], lo, hi))


# ---------------------------------------------------------------- running a rule

def trades_for(setup, params, data, costs, window, flat, dates=None, fill="next_open"):
    """Every trade of one parameter set over {sym: [(date, bars, prev_close)]}."""
    out = []
    for sym, sessions in data.items():
        for d, D, pc in sessions:
            if dates is not None and d not in dates:
                continue
            sig = st.signal(setup, D, pc, params, window)
            if not sig:
                continue
            t = st.simulate_trade(D, sig, costs, flat, fill)
            if not t:
                continue
            t.update(symbol=sym, date=d, setup=setup, trigger=sig["trigger"], stop=sig["stop"],
                     target=sig["target"], risk=sig["risk"],
                     time=_clock(st.slot_end_min(sig["index"])))
            out.append(t)
    return out


def _clock(m):
    return "%02d:%02d" % divmod(m, 60)


def walk_forward(setup, data, costs, window, flat, train=TRAIN, params=None, fill="next_open"):
    """In-sample selection over the grid, out-of-sample judgement."""
    dates = sorted({d for s in data.values() for d, _, _ in s})
    if len(dates) < 10:
        raise BacktestError("only %d trading days in the data: not enough to test anything" % len(dates))
    if params is not None:
        p = st.params_for(setup, params)
        tr = trades_for(setup, p, data, costs, window, flat, None, fill)
        s = stats(tr)
        v, why = verdict(s)
        return {"setup": setup, "mode": "fixed", "params": p, "combos_tried": 1,
                "dates": [dates[0], dates[-1]], "split_date": None,
                "in_sample": None, "out_of_sample": s, "verdict": v, "why": why,
                "note": "no parameter search, so every session counts as out-of-sample",
                "trades": tr}
    cut = max(1, min(len(dates) - 1, int(round(len(dates) * train))))
    ins, oos = set(dates[:cut]), set(dates[cut:])
    tried = []
    for g in st.GRID[setup]:
        p = st.params_for(setup, g)
        tr = trades_for(setup, p, data, costs, window, flat, ins, fill)
        s = stats(tr)
        tried.append((p, s))
    eligible = [(p, s) for p, s in tried if s["trades"] >= MIN_TRAIN_TRADES]
    if not eligible:
        best_p, best_s = max(tried, key=lambda x: x[1]["trades"])
        note = "no parameter set reached %d in-sample trades; picked the one with the most" % MIN_TRAIN_TRADES
    else:
        best_p, best_s = max(eligible, key=lambda x: (x[1]["avg_R"], x[1]["trades"]))
        note = ("picked the best of %d parameter sets in-sample; the in-sample number is flattered by that "
                "choice, which is why only the out-of-sample one counts" % len(tried))
    tr = trades_for(setup, best_p, data, costs, window, flat, oos, fill)
    s = stats(tr)
    v, why = verdict(s)
    return {"setup": setup, "mode": "walk-forward", "params": best_p, "combos_tried": len(tried),
            "dates": [dates[0], dates[-1]], "split_date": dates[cut],
            "in_sample": best_s, "out_of_sample": s, "verdict": v, "why": why, "note": note,
            "grid": [{"params": {k: p[k] for k in sorted(g)}, "trades": s2["trades"], "avg_R": s2.get("avg_R")}
                     for (p, s2), g in zip(tried, st.GRID[setup])],
            "trades": tr}


# ---------------------------------------------------------------- loading the universe

def tickers_from(a):
    if a.tickers:
        ts = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    elif a.tickers_file:
        with open(a.tickers_file, encoding="utf-8") as fh:
            raw = fh.read()
        try:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                doc = doc.get("tickers") or doc.get("symbols") or doc.get("watchlist") or list(doc)
            ts = [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in doc]
        except ValueError:
            ts = [x.strip().upper() for x in raw.replace(",", "\n").splitlines() if x.strip()]
    else:
        ts = list(md.DEFAULT_UNIVERSE)
    seen, out = set(), []
    for t in ts:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_universe(tickers, cfg, offline=None, now_utc=None, cache_dir="trader/cache", stooq=None,
                  log=lambda s: None, fetch=None, sleep=None):
    data, reports = {}, []
    now_utc = now_utc or md.utc_now()
    for sym in tickers:
        try:
            kw = {"fetch": fetch} if fetch else {}
            if sleep:
                kw["sleep"] = sleep
            text, src = md.load_chart(sym, offline, cache_dir, **kw)
            bars, counts = md.parse(text, sym, now_utc)
        except md.DataError as e:
            reports.append({"symbol": sym, "exclude": str(e), "dropped_sessions": {}, "warnings": []})
            log("  %-6s left out: %s" % (sym, e))
            continue
        full, short = md.sessions_of(bars)
        kept, rep = md.qa(sym, full, counts, now_utc, float(cfg.get("min_price", 5.0)),
                          float(cfg.get("min_dollar_volume", 20e6)))
        rep["short_sessions"] = short
        rep["source"] = src
        if stooq is not None and kept:
            closes = stooq(sym)
            if closes:
                cc = md.crosscheck(sym, kept, closes)
                rep["stooq"] = cc
                if cc["mismatches"]:
                    rep["warnings"].append("%d of %d closes differ from Stooq by more than 0.5 %%"
                                           % (len(cc["mismatches"]), cc["checked"]))
        reports.append(rep)
        if rep["exclude"]:
            log("  %-6s left out: %s" % (sym, rep["exclude"]))
            continue
        data[sym] = kept
        log("  %-6s %d sessions%s" % (sym, len(kept), (" (" + "; ".join(rep["warnings"]) + ")") if rep["warnings"] else ""))
    return data, reports


# ---------------------------------------------------------------- output

def edge_doc(results, costs, universe, reports, cfg, now_utc, old=None):
    doc = dict(old or {})
    doc.update({"created_by": "trader-backtest", "updated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "costs": costs, "fill": "next bar's open"})
    setups = dict(doc.get("setups") or {})
    for r in results:
        setups[r["setup"]] = {
            "verdict": r["verdict"], "why": r["why"], "params": r["params"], "mode": r["mode"],
            "combos_tried": r["combos_tried"], "dates": r["dates"], "split_date": r["split_date"],
            "out_of_sample": r["out_of_sample"], "in_sample": r["in_sample"],
            "tickers": sorted(universe), "tested_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    doc["setups"] = setups
    doc["data_checks"] = [x for x in reports if x.get("exclude") or x.get("warnings") or x.get("dropped_sessions")]
    doc["note"] = ("Evidence from 60 days of 5-minute bars, out-of-sample, after estimated costs. Not a promise: "
                   "one market regime, today's large caps (survivorship), and the future can do what these days did not.")
    return doc


def _n(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _r(x):
    return "n/a" if x is None else "%+.3f R" % x


def _pc(x):
    return "n/a" if x is None else "%.1f %%" % (100 * x)


def report(r):
    s = r["out_of_sample"]
    L = []
    L.append("%s (%s)" % (st.SETUP_WORDS[r["setup"]], r["setup"]))
    L.append("  VERDICT  %s: %s" % (r["verdict"], r["why"]))
    L.append("  rule     %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(r["params"].items()) if v is not None))
    if r["mode"] == "walk-forward":
        i = r["in_sample"] or {}
        L.append("  split    in-sample %s to the day before %s, out-of-sample from %s to %s"
                 % (r["dates"][0], r["split_date"], r["split_date"], r["dates"][1]))
        L.append("  in       %s, %s a trade (picked as the best of %d: flattered)"
                 % (_n(i.get("trades", 0), "trade"), _r(i.get("avg_R")), r["combos_tried"]))
    else:
        L.append("  data     %s to %s, all of it (fixed parameters, nothing tuned)" % tuple(r["dates"]))
    if s.get("trades"):
        L.append("  out      %s on %s in %s" % (_n(s["trades"], "trade"), _n(s["days"], "day"), _n(s["tickers"], "stock")))
        L.append("           win rate %s (95 %%: %s to %s)" % (_pc(s["win_rate"]), _pc(s["win_rate_95"][0]),
                                                          _pc(s["win_rate_95"][1])))
        rng = s["avg_R_95"] or [None, None]
        L.append("           average %s after costs (95 %%: %s to %s), %s before costs"
                 % (_r(s["avg_R"]), _r(rng[0]), _r(rng[1]), _r(s["avg_R_before_costs"])))
        L.append("           costs ate %.3f R a trade; profit factor %s; worst drawdown %.1f R; worst trade %+.2f R"
                 % (s["avg_cost_R"], s["profit_factor"] if s["profit_factor"] is not None else "n/a",
                    s["max_drawdown_R"], s["worst_R"]))
        L.append("           outcomes: %s" % ", ".join("%s %d" % (k, v) for k, v in sorted(s["outcomes"].items())))
    else:
        L.append("  out      no trades out-of-sample")
    return "\n".join(L)


def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(path + ".tmp", path)


def parse_now(text):
    if not text:
        return md.utc_now()
    dt = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        for off in (-4, -5):
            u = dt - datetime.timedelta(hours=off)
            if md.eastern_offset(u) == off:
                return u
        return dt + datetime.timedelta(hours=5)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def cmd_run(a):
    cfg = read_json(a.config, {}) or {}
    costs = st.costs_for(cfg.get("costs"))
    if a.no_costs:
        costs = {k: 0.0 for k in costs}
    window = st.entry_window(cfg.get("no_entries_before", "09:45"), cfg.get("no_entries_after", "15:30"))
    flat = st.flat_slot(cfg.get("flat_by", "15:55"))
    setups = [a.setup] if a.setup != "all" else list(st.SETUPS)
    params = None
    if a.params:
        if a.setup == "all":
            raise BacktestError("--params needs one --setup")
        try:
            params = json.loads(a.params)
        except ValueError:
            raise BacktestError("--params wants JSON, e.g. '{\"target_r\": 1.5}'")
    now = parse_now(a.now)
    tickers = tickers_from(a)
    log = (lambda s: None) if a.json else print
    log("Loading %d ticker(s)%s" % (len(tickers), " from %s" % a.offline if a.offline else ""))
    stooq = None
    if a.crosscheck:
        def stooq(sym):
            try:
                if a.stooq_offline:
                    p = os.path.join(a.stooq_offline, "%s-daily.csv" % sym)
                    text = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
                else:
                    text = md._get(md.stooq_daily_url(sym))
                return md.parse_stooq_daily(text)
            except Exception:
                return {}
    data, reports = load_universe(tickers, cfg, a.offline, now, a.cache, stooq, log)
    if not data:
        raise BacktestError("no usable data for any ticker: nothing to test")
    results = []
    for s in setups:
        r = walk_forward(s, data, costs, window, flat, a.train, params, a.fill)
        results.append(r)
    out = {"results": [{k: v for k, v in r.items() if k != "trades"} for r in results],
           "costs": costs, "tickers": sorted(data), "data_checks": reports}
    if a.trades:
        out["trades"] = {r["setup"]: r["trades"] for r in results}
    if a.write:
        write_json(a.out, edge_doc(results, costs, data, reports, cfg, now, read_json(a.out)))
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print("")
        print("Costs: spread %g bps (at least %g cent), slippage %g bps a market fill, fees %g bps on sales, "
              "commission $%g a share. Entry at the next bar's open."
              % (costs["spread_bps"], costs["min_spread_cents"], costs["slippage_bps"], costs["sell_fee_bps"],
                 costs["commission_per_share"]))
        for r in results:
            print("")
            print(report(r))
        print("")
        if a.write:
            print("Saved to %s: trader-risk reads it." % a.out)
        print("Evidence, not a promise: 60 days is one market regime.")
    return 0 if any(r["verdict"] == "EDGE" for r in results) else 1


def cmd_show(a):
    doc = read_json(a.out)
    if not doc:
        print("No %s yet: run backtest.py run --write" % a.out)
        return 1
    for name, r in sorted((doc.get("setups") or {}).items()):
        s = r.get("out_of_sample") or {}
        print("%-5s %-20s %s (tested %s, %d trades)" % (name, r["verdict"], r.get("why", ""), r.get("tested_at", "?"),
                                                       s.get("trades", 0)))
    return 0


def is_fresh(entry, now_utc, max_age_days=EDGE_MAX_AGE_DAYS):
    try:
        t = datetime.datetime.strptime(entry["tested_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        return False
    return (now_utc - t).days <= max_age_days


def main(argv=None):
    ap = argparse.ArgumentParser(description="Does a setup make money after costs, out-of-sample? (trader-backtest)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--setup", default="all", choices=["all"] + list(st.SETUPS))
    r.add_argument("--tickers", help="comma-separated, e.g. TSLA,NVDA,AMD")
    r.add_argument("--tickers-file", help="a JSON list or watchlist, or one ticker a line")
    r.add_argument("--params", help="fixed parameters as JSON: skips the grid, every session counts")
    r.add_argument("--train", type=float, default=TRAIN, help="in-sample share of the days (default 0.6)")
    r.add_argument("--fill", default="next_open", choices=["next_open", "close"],
                   help="entry fill: the next bar's open (default, honest) or the trigger close (optimistic)")
    r.add_argument("--no-costs", action="store_true", help="zero costs, to see how much they matter")
    r.add_argument("--offline", help="a folder of saved Yahoo charts (<SYM>-60d5m.json[.gz])")
    r.add_argument("--cache", default="trader/cache")
    r.add_argument("--crosscheck", action="store_true", help="compare daily closes with Stooq")
    r.add_argument("--stooq-offline", help="a folder of <SYM>-daily.csv Stooq files")
    r.add_argument("--config", default="trader/config.json")
    r.add_argument("--out", default="trader/state/edge.json")
    r.add_argument("--write", action="store_true", help="save the verdicts for trader-risk")
    r.add_argument("--trades", action="store_true", help="include every trade in --json")
    r.add_argument("--now", help="pretend it is this New York time")
    r.add_argument("--json", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("--out", default="trader/state/edge.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        if a.cmd == "run":
            if not 0.2 <= a.train <= 0.9:
                raise BacktestError("--train must be between 0.2 and 0.9")
            return cmd_run(a)
        return cmd_show(a)
    except (BacktestError, st.SetupError, md.DataError) as e:
        print("backtest.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
