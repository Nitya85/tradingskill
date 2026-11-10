"""trader-backtest (v2.1): the setups, the fill model, the statistics, the
walk-forward, the data checks, and how entry, exit, the time machine, budget
and risk use them. Offline, on the real TSLA fixture and on small made-up
sessions where the right answer can be worked out by hand."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

st = load("trader-backtest", "setups")
md = load("trader-backtest", "marketdata")
bt = load("trader-backtest", "backtest")
en = load("trader-entry", "entry")
ex = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
size = load("trader-budget", "size")
check = load("trader-risk", "check")

TM_DIR = os.path.join(FIX, "trader-timemachine")
NOW = md.utc_of(1900000000)                       # after every bar in the fixture
REEL_NOW = datetime.datetime(2026, 10, 19, 14, 15)  # 10:15 New York, in UTC
NO_COSTS = {k: 0.0 for k in st.DEFAULT_COSTS}


def tsla():
    text = md.offline_text(TM_DIR, "TSLA")
    bars, counts = md.parse(text, "TSLA", NOW)
    full, short = md.sessions_of(bars)
    return text, full, short, counts


def bar(o, h, l, c, v=1000):
    return {"o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v)}


def flat_session(price=100.0, n=78, v=1000):
    return [bar(price, price + 0.05, price - 0.05, price, v) for _ in range(n)]


def run_cli(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


class Windows(unittest.TestCase):
    def test_entry_window_matches_the_config_times(self):
        self.assertEqual(st.entry_window("09:45", "15:30"), (2, 71))   # bars closing 9:45 and 15:30
        self.assertEqual(st.slot_end_min(2), 9 * 60 + 45)
        self.assertEqual(st.slot_end_min(71), 15 * 60 + 30)

    def test_flat_slot(self):
        self.assertEqual(st.flat_slot("15:55"), 76)
        self.assertEqual(st.slot_end_min(76), 15 * 60 + 55)


class Orb(unittest.TestCase):
    def test_reproduces_the_v2_history_exactly(self):
        # trigger close, no costs, through the close at 4:00: the reel's 33 / 13 / 15 / 5
        _, full, _, _ = tsla()
        data = {"TSLA": [x for x in full if x[0] < "2026-10-19"]}
        p = st.params_for("orb", {"or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        tr = bt.trades_for("orb", p, data, NO_COSTS, (3, 77), 77, None, fill="close")
        outs = [t["outcome"] for t in tr]
        self.assertEqual((len(tr), outs.count("target"), outs.count("stop"), outs.count("flat")), (33, 13, 15, 5))

    def test_triggers_on_a_close_not_a_wick(self):
        D = flat_session()
        D[0] = bar(100, 101, 99, 100)
        D[3] = bar(100, 102, 100, 100.9)            # wick above 101, close under
        D[4] = bar(100.9, 101.5, 100.8, 101.2)      # first close above
        sig = st.signal("orb", D, None, {"or_minutes": 15})
        self.assertEqual(sig["index"], 4)
        self.assertEqual(sig["stop"], 100.0)        # the midpoint of 101 and 99
        self.assertEqual(sig["target"], 102.4)      # 1R

    def test_or_low_stop_and_2r(self):
        D = flat_session()
        D[0] = bar(100, 101, 99, 100)
        D[4] = bar(100.9, 101.5, 100.8, 101.2)
        sig = st.signal("orb", D, None, {"stop_rule": "or_low", "target_r": 2.0})
        self.assertEqual((sig["stop"], sig["target"]), (99.0, 105.6))

    def test_respects_the_entry_window(self):
        D = flat_session()
        D[0] = bar(100, 101, 99, 100)
        D[75] = bar(100, 101.5, 100, 101.2)         # closes 15:50, after the 15:30 cutoff
        self.assertIsNone(st.signal("orb", D, None, None, st.entry_window()))


class Vwap(unittest.TestCase):
    def session(self):
        D = flat_session(100)
        for i in range(5, 9):
            D[i] = bar(99.5, 99.6, 99.0, 99.2)       # four closes under VWAP, low 99.0
        D[9] = bar(99.3, 100.4, 99.3, 100.3)         # back above
        return D

    def test_reclaim_after_enough_bars_under(self):
        sig = st.signal("vwap", self.session(), None, {"min_below": 3, "target_r": 1.5})
        self.assertEqual(sig["index"], 9)
        self.assertEqual(sig["stop"], 99.0)          # the swing low of the run under VWAP
        self.assertEqual(sig["target"], 102.25)      # 100.30 + 1.5 x 1.30

    def test_not_enough_bars_under(self):
        self.assertIsNone(st.signal("vwap", self.session(), None, {"min_below": 6}))

    def test_too_wide_a_stop_is_skipped(self):
        D = self.session()
        D[6] = bar(99.5, 99.6, 90.0, 99.2)           # a 10 % swing low
        self.assertIsNone(st.signal("vwap", D, None, {"min_below": 3, "max_risk_pct": 2.0}))

    def test_vwap_series(self):
        w = st.vwap_series([bar(10, 12, 8, 10, 100), bar(10, 11, 9, 13, 300)])
        self.assertAlmostEqual(w[0], 10.0)
        self.assertAlmostEqual(w[1], (100 * 10 + 300 * 11) / 400.0)


class Gap(unittest.TestCase):
    def session(self):
        D = flat_session(103)
        D[0] = bar(103, 104, 102.5, 103.5)
        D[3] = bar(103.6, 104.5, 103.5, 104.3)
        return D

    def test_gap_and_go(self):
        sig = st.signal("gap", self.session(), 100.0, {"min_gap_pct": 2.0, "target_r": 1.0})
        self.assertEqual(sig["index"], 3)
        self.assertEqual(sig["stop"], 102.5)         # the first bar's low

    def test_too_small_a_gap(self):
        self.assertIsNone(st.signal("gap", self.session(), 102.0, {"min_gap_pct": 2.0}))

    def test_a_filled_gap_is_no_go(self):
        D = self.session()
        D[1] = bar(103, 103, 99.5, 102.6)            # traded under yesterday's close
        self.assertIsNone(st.signal("gap", D, 100.0))

    def test_needs_a_previous_close(self):
        self.assertIsNone(st.signal("gap", self.session(), None))

    def test_unknown_setup_and_bad_stop_rule(self):
        with self.assertRaises(st.SetupError):
            st.signal("flag", flat_session())
        with self.assertRaises(st.SetupError):
            st.params_for("vwap", {"stop_rule": "or_mid"})


class NoLookAhead(unittest.TestCase):
    def test_no_setup_reads_a_bar_after_its_trigger(self):
        # scramble every bar after the trigger: a rule that only reads the past gives the same signal
        _, full, _, _ = tsla()
        n = 0
        for setup in st.SETUPS:
            for d, D, pc in full:
                sig = st.signal(setup, D, pc, None, st.entry_window())
                if not sig:
                    continue
                n += 1
                E = [dict(b) for b in D]
                for b in E[sig["index"] + 1:]:
                    b.update(o=b["o"] * 1.3, h=b["h"] * 1.5, l=b["l"] * 0.5, c=b["c"] * 0.7, v=b["v"] * 9)
                self.assertEqual(st.signal(setup, E, pc, None, st.entry_window()), sig, "%s %s" % (setup, d))
        self.assertEqual(n, 83)

    def test_the_walk_forward_picks_on_in_sample_days_only(self):
        _, full, _, _ = tsla()
        data = {"TSLA": full}
        r = bt.walk_forward("vwap", data, st.costs_for(None), st.entry_window(), 76)
        later = {"TSLA": [x for x in full if x[0] >= r["split_date"]]}
        scrambled = {"TSLA": [x for x in full if x[0] < r["split_date"]] +
                     [(d, [dict(b, c=b["c"] * 0.9, l=b["l"] * 0.9) for b in D], pc) for d, D, pc in later["TSLA"]]}
        r2 = bt.walk_forward("vwap", scrambled, st.costs_for(None), st.entry_window(), 76)
        self.assertEqual(r2["params"], r["params"])     # what the later days do cannot change the choice


class Fills(unittest.TestCase):
    SIG = {"index": 4, "trigger": 101.2, "stop": 100.0, "target": 102.4, "risk": 1.2, "target_r": 1.0}

    def test_enters_at_the_next_open_and_hits_the_target_only_through_it(self):
        D = flat_session(101.3)
        D[5] = bar(101.3, 102.4, 101.2, 102.3)       # touches 102.40, no trade through: not filled
        D[6] = bar(102.3, 102.6, 102.2, 102.5)       # through: filled at the target
        t = st.simulate_trade(D, self.SIG, NO_COSTS)
        self.assertEqual((t["outcome"], t["exit_slot"], t["raw_entry"], t["raw_exit"]), ("target", 6, 101.3, 102.4))
        self.assertAlmostEqual(t["R"], (102.4 - 101.3) / 1.2, places=4)

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        D = flat_session(101.3)
        D[7] = bar(99.0, 99.5, 98.8, 99.2)           # opens under the 100 stop
        t = st.simulate_trade(D, self.SIG, NO_COSTS)
        self.assertEqual((t["outcome"], t["raw_exit"]), ("stop", 99.0))
        self.assertLess(t["R"], -1.5)                # worse than -1R, like a real account

    def test_a_bar_that_reaches_both_is_the_stop(self):
        D = flat_session(101.3)
        D[6] = bar(101.3, 103.0, 99.9, 101.0)
        self.assertEqual(st.simulate_trade(D, self.SIG, NO_COSTS)["outcome"], "stop")

    def test_skipped_when_the_next_open_is_past_the_stop_or_target(self):
        D = flat_session(101.3)
        D[5] = bar(99.9, 100, 99.8, 99.9)
        self.assertIsNone(st.simulate_trade(D, self.SIG, NO_COSTS))
        D[5] = bar(102.5, 102.6, 102.4, 102.5)
        self.assertIsNone(st.simulate_trade(D, self.SIG, NO_COSTS))

    def test_flat_by_sells_at_that_bars_close(self):
        D = flat_session(101.3)
        D[76] = bar(101.3, 101.5, 101.2, 101.4)
        t = st.simulate_trade(D, self.SIG, NO_COSTS, flat=76)
        self.assertEqual((t["outcome"], t["exit_slot"], t["raw_exit"]), ("flat", 76, 101.4))

    def test_costs_come_off_every_market_fill(self):
        D = flat_session(101.3)
        D[7] = bar(101.3, 101.4, 99.9, 100.1)
        c = st.costs_for(None)
        t = st.simulate_trade(D, self.SIG, c)
        self.assertEqual(t["outcome"], "stop")
        paid = 101.3 + max(101.3 * 4 / 20000.0, 0.005) + 101.3 * 3 / 10000.0
        got = 100.0 - max(100.0 * 4 / 20000.0, 0.005) - 100.0 * 3 / 10000.0
        got -= got * 0.3 / 10000.0
        self.assertAlmostEqual(t["R"], (got - paid) / 1.2, places=4)
        self.assertGreater(t["cost_R"], 0)

    def test_cost_per_share(self):
        c = st.costs_for({"spread_bps": 0, "min_spread_cents": 0, "slippage_bps": 10, "sell_fee_bps": 0,
                          "commission_per_share": 0.005})
        self.assertAlmostEqual(st.cost_per_share(100.0, 90.0, c), 0.1 + 0.09 + 0.01, places=6)
        with self.assertRaises(st.SetupError):
            st.costs_for({"slippage_bps": -1})


class Statistics(unittest.TestCase):
    def test_wilson(self):
        lo, hi = bt.wilson(13, 33)
        self.assertAlmostEqual(lo, 0.2468, places=3)
        self.assertAlmostEqual(hi, 0.5632, places=3)
        self.assertEqual(bt.wilson(0, 0), (None, None))

    def test_cluster_bootstrap_resamples_days_and_is_repeatable(self):
        tr = [{"date": "d%d" % (i // 3), "R": (1.0 if i % 2 else -1.0)} for i in range(60)]
        a, b = bt.cluster_ci(tr), bt.cluster_ci(tr)
        self.assertEqual(a, b)
        self.assertLess(a[0], 0)
        self.assertGreater(a[1], 0)

    def test_max_drawdown(self):
        self.assertEqual(bt.max_drawdown([1, -1, -1, 2, -3, 1]), 3)

    def test_verdicts(self):
        def s(n, avg, lo, hi):
            return {"trades": n, "avg_R": avg, "avg_R_95": [lo, hi]}
        self.assertEqual(bt.verdict(s(29, 0.5, 0.2, 0.8))[0], "NOT ENOUGH EVIDENCE")
        self.assertEqual(bt.verdict(s(40, 0.2, 0.05, 0.35))[0], "EDGE")
        self.assertEqual(bt.verdict(s(40, -0.1, -0.4, 0.2))[0], "NO EDGE")
        self.assertEqual(bt.verdict(s(40, 0.1, -0.1, 0.3))[0], "NOT ENOUGH EVIDENCE")


class WalkForward(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, full, _, _ = tsla()
        cls.data = {"TSLA": full}

    def test_out_of_sample_never_sees_the_in_sample_days(self):
        r = bt.walk_forward("orb", self.data, st.costs_for(None), st.entry_window(), 76)
        self.assertEqual(r["mode"], "walk-forward")
        self.assertEqual(r["combos_tried"], len(st.GRID["orb"]))
        self.assertTrue(all(t["date"] >= r["split_date"] for t in r["trades"]))
        self.assertIn(r["verdict"], ("EDGE", "NO EDGE", "NOT ENOUGH EVIDENCE"))

    def test_fixed_params_skip_the_grid(self):
        r = bt.walk_forward("orb", self.data, st.costs_for(None), st.entry_window(), 76,
                            params={"or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        self.assertEqual((r["mode"], r["combos_tried"], r["in_sample"]), ("fixed", 1, None))

    def test_costs_make_it_worse_never_better(self):
        p = {"or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0}
        a = bt.walk_forward("orb", self.data, NO_COSTS, st.entry_window(), 76, params=p)["out_of_sample"]
        b = bt.walk_forward("orb", self.data, st.costs_for(None), st.entry_window(), 76, params=p)["out_of_sample"]
        self.assertLess(b["avg_R"], a["avg_R"])

    def test_too_few_days(self):
        with self.assertRaises(bt.BacktestError):
            bt.walk_forward("orb", {"X": self.data["TSLA"][:5]}, NO_COSTS, st.entry_window(), 76)


class DataChecks(unittest.TestCase):
    def sessions(self, n=25, price=100.0, v=100000):
        out, prev = [], None
        for i in range(n):
            D = flat_session(price, v=v)
            out.append(("2026-09-%02d" % (i + 1), D, prev))
            prev = D[-1]["c"]
        return out

    def test_the_real_fixture_keeps_a_wide_but_real_opening_bar(self):
        _, full, short, counts = tsla()
        kept, rep = md.qa("TSLA", full, counts, NOW)
        self.assertIsNone(rep["exclude"])
        self.assertEqual(len(kept), len(full))       # 2026-09-25's 4 % opening bar is real, not an outlier
        self.assertEqual(short, ["2026-10-19"])

    def test_a_bad_bar_drops_its_session(self):
        ss = self.sessions()
        ss[3][1][10] = bar(100, 99, 98, 100)          # high under the open
        kept, rep = md.qa("X", ss)
        self.assertEqual(len(kept), 24)
        self.assertIn(ss[3][0], rep["dropped_sessions"])

    def test_an_outlier_bar_drops_its_session(self):
        ss = self.sessions()
        ss[5][1][40] = bar(100, 120, 100, 100)
        kept, rep = md.qa("X", ss)
        self.assertIn(ss[5][0], rep["dropped_sessions"])

    def test_a_split_leaves_the_ticker_out(self):
        ss = self.sessions()
        for i in range(10, 25):
            ss[i] = (ss[i][0], flat_session(50.0, v=100000), ss[i][2])
        ss[10] = (ss[10][0], ss[10][1], 100.0)
        _, rep = md.qa("X", ss)
        self.assertIn("split", rep["exclude"])

    def test_cheap_and_thin_stocks_are_left_out(self):
        self.assertIn("price", md.qa("X", self.sessions(price=3.0, v=10 ** 7))[1]["exclude"])
        self.assertIn("dollar volume", md.qa("X", self.sessions(v=10))[1]["exclude"])

    def test_stale_and_duplicate_warnings(self):
        _, rep = md.qa("X", self.sessions(), {"duplicate": 2}, datetime.datetime(2026, 11, 10))
        text = " ".join(rep["warnings"])
        self.assertIn("2 duplicate", text)
        self.assertIn("stale", text)

    def test_stooq_crosscheck(self):
        ss = self.sessions(3)
        closes = md.parse_stooq_daily("Date,Open,High,Low,Close,Volume\n2026-09-01,1,1,1,100.0,1\n"
                                      "2026-09-02,1,1,1,101.5,1\n")
        cc = md.crosscheck("X", ss, closes)
        self.assertEqual(cc["checked"], 2)
        self.assertEqual([m[0] for m in cc["mismatches"]], ["2026-09-02"])
        self.assertEqual(md.parse_stooq_daily("No data"), {})

    def test_parse_refuses_another_symbol(self):
        text, _, _, _ = tsla()
        with self.assertRaises(md.DataError):
            md.parse(text, "AAPL", NOW)


class EntryAgrees(unittest.TestCase):
    """The live entry timer and the backtest must fire on the same bar."""

    def test_every_setup_every_day(self):
        text, full, _, _ = tsla()
        chart = en.parse_chart(text, "TSLA")
        fired = {}
        for setup in st.SETUPS:
            for d, D, pc in full:
                now = en.eastern_to_utc(datetime.datetime.fromisoformat(d + " 15:45"))
                res = en.evaluate(chart, now, setup=setup)
                sig = st.signal(setup, D, pc, None, st.entry_window())
                self.assertEqual((res.get("trigger") or {}).get("bar_index"), sig["index"] if sig else None,
                                 "%s %s" % (setup, d))
                if sig:
                    self.assertEqual(res.get("setup_stop", sig["stop"]), sig["stop"])
                    fired[setup] = fired.get(setup, 0) + 1
        self.assertEqual(fired, {"orb": 33, "vwap": 47, "gap": 3})

    def test_vwap_and_gap_need_5_minute_bars(self):
        text, _, _, _ = tsla()
        with self.assertRaises(en.EntryError):
            en.evaluate(en.parse_chart(text, "TSLA"), NOW, setup="vwap", interval="1m")


class ExitUsesTheSetupStop(unittest.TestCase):
    def test_vwap_entry_to_plan(self):
        tmp = tempfile.mkdtemp()
        try:
            entry = os.path.join(tmp, "entry.json")
            code, _, _ = run_cli(en.main, ["check", "TSLA", "--setup", "vwap", "--offline",
                                           os.path.join(TM_DIR, "TSLA-60d5m.json.gz"), "--now", "2026-10-16 15:45",
                                           "--write", entry, "--edge", os.path.join(tmp, "none.json")])
            self.assertEqual(code, 0)
            with open(entry) as fh:
                doc = json.load(fh)
            self.assertEqual((doc["setup"], doc["bar_index"], doc["setup_stop"]), ("vwap", 6, 363.36))
            plan_path = os.path.join(tmp, "plan.json")
            code, out, err = run_cli(ex.main, ["plan", "--from", entry, "--now", "2026-10-16 15:45",
                                               "--write", plan_path, "--config", os.path.join(tmp, "none.json")])
            self.assertEqual(code, 0, err)
            with open(plan_path) as fh:
                plan = json.load(fh)
            self.assertEqual((plan["stop"], plan["stop_rule"], plan["target_r"]), (363.36, "setup", 1.5))
            self.assertEqual(plan["setup_params"]["min_below"], 3)
        finally:
            shutil.rmtree(tmp)


class TimeMachineBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(FIX, "trader-budget", "plan-tsla.json")) as fh:
            cls.plan = tm.normalize_plan(json.load(fh))
        cls.text = md.offline_text(TM_DIR, "TSLA")
        cls.doc = tm.time_machine(cls.text, cls.plan, REEL_NOW, replays=2000)

    def test_block_is_the_default_and_repeatable(self):
        m = self.doc["mc"]
        self.assertEqual((m["method"], m["block"], m["pool_sessions"], m["all_sessions"]), ("block", 6, 30, 59))
        again = tm.time_machine(self.text, self.plan, REEL_NOW, replays=2000)
        self.assertEqual(again["mc"]["p_target"], m["p_target"])
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=3)

    def test_costs_come_off(self):
        m = self.doc["mc"]
        self.assertLess(m["avg_R_net"], m["avg_R"])

    def test_the_verdict_comes_from_the_history_after_costs(self):
        an = self.doc["analog"]
        self.assertEqual((an["stats"]["trades"], an["own_trades"], an["peer_trades"]), (33, 33, 0))
        self.assertTrue(self.doc["verdict"].startswith("History says no (NO EDGE)"))
        self.assertTrue(any(n.startswith("Replays:") for n in self.doc["verdict_notes"]))

    def test_peers_add_history(self):
        doc = tm.time_machine(self.text, self.plan, REEL_NOW, replays=500,
                              peers={"TSLB": tsla()[1][:-1]})
        self.assertEqual(doc["analog"]["own_trades"], 33)
        self.assertEqual(doc["analog"]["peer_trades"], 33)

    def test_similar_zero_uses_every_session(self):
        doc = tm.time_machine(self.text, self.plan, REEL_NOW, replays=300, similar=0)
        self.assertEqual(doc["mc"]["pool_sessions"], 59)

    def test_cli_report(self):
        code, out, _ = run_cli(tm.main, ["run", "--plan", os.path.join(FIX, "trader-budget", "plan-tsla.json"),
                                         "--offline", os.path.join(TM_DIR, "TSLA-60d5m.json.gz"),
                                         "--now", "2026-10-19 10:15", "--replays", "500",
                                         "--config", "/nonexistent.json"])
        self.assertEqual(code, 0)
        self.assertIn("6-bar runs", out)
        self.assertIn("History, realistic fills after costs: 33 past triggers", out)


class BudgetWithCosts(unittest.TestCase):
    def test_costs_shrink_the_size_when_they_push_over_the_max_loss(self):
        with open(os.path.join(FIX, "trader-budget", "plan-tsla.json")) as fh:
            plan = json.load(fh)
        bare = size.size_trade(plan, {"budget": 5000})
        self.assertEqual((bare["qty"], bare["est_cost_per_share"]), (8, None))
        cfg = {"budget": 5000, "max_loss_per_trade_usd": 48, "costs": {}}
        s = size.size_trade(plan, cfg)
        self.assertEqual(s["qty"], 7)                          # 8 x (5.86 + 0.37) = 49.86 is over $48
        self.assertLessEqual(s["max_loss_with_costs"], 48)
        self.assertIn("est. costs", s["limits"][0]["how"])
        self.assertIn("est_cost_per_share", size.bracket_order(s)["plan"])


class RiskEdgeGate(unittest.TestCase):
    def order(self, setup="orb"):
        with open(os.path.join(FIX, "trader-risk", "orders-tsla.json")) as fh:
            o = json.load(fh)["orders"][0]
        o["plan"]["setup"] = setup
        return o

    def edge(self, verdict, days_ago=1):
        t = REEL_NOW - datetime.timedelta(days=days_ago)
        return {"setups": {"orb": {"verdict": verdict, "why": "test", "tested_at": t.strftime("%Y-%m-%dT%H:%M:%SZ")}}}

    def run_check(self, edge, **cfg):
        c = {"budget": 5000}
        c.update(cfg)
        return check.check([self.order()], c, now=REEL_NOW, edge=edge)["results"][0]

    def test_paper_warns_live_blocks(self):
        r = self.run_check({})
        self.assertEqual(r["verdict"], "PASS")
        self.assertTrue(any("edge:" in w for w in r["warnings"]))
        r = self.run_check({}, paper=False)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("edge:") for x in r["reasons"]))

    def test_a_fresh_edge_passes_live(self):
        r = self.run_check(self.edge("EDGE"), paper=False)
        self.assertFalse(any(x.startswith("edge:") for x in r["reasons"] + r["warnings"]))

    def test_a_stale_edge_or_no_edge_blocks_live(self):
        self.assertEqual(self.run_check(self.edge("EDGE", 10), paper=False)["verdict"], "BLOCK")
        self.assertEqual(self.run_check(self.edge("NO EDGE"), paper=False)["verdict"], "BLOCK")

    def test_always_and_never(self):
        self.assertEqual(self.run_check({}, require_edge="always")["verdict"], "BLOCK")
        r = self.run_check({}, paper=False, require_edge="never")
        self.assertFalse(any("edge" in x for x in r["reasons"]))

    def test_no_edge_document_means_no_check(self):
        r = check.check([self.order()], {"budget": 5000, "paper": False}, now=REEL_NOW)["results"][0]
        self.assertFalse(any("edge" in x for x in r["reasons"] + r["warnings"]))

    def test_costs_count_in_the_max_loss(self):
        r = self.run_check(None, costs={}, max_loss_per_trade_usd=48)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any("est. costs" in x for x in r["reasons"]))
        self.assertEqual(self.run_check(None, max_loss_per_trade_usd=48)["verdict"], "PASS")


class Cli(unittest.TestCase):
    def test_run_write_show(self):
        tmp = tempfile.mkdtemp()
        try:
            out_path = os.path.join(tmp, "edge.json")
            code, out, err = run_cli(bt.main, ["run", "--offline", TM_DIR, "--tickers", "TSLA", "--setup", "orb",
                                               "--now", "2026-10-19 10:15", "--write", "--out", out_path,
                                               "--config", os.path.join(tmp, "none.json")])
            self.assertEqual(code, 1, err)                   # no EDGE
            self.assertIn("VERDICT", out)
            self.assertIn("costs ate", out)
            with open(out_path) as fh:
                doc = json.load(fh)
            self.assertEqual(sorted(doc["setups"]), ["orb"])
            self.assertEqual(doc["setups"]["orb"]["tested_at"], "2026-10-19T14:15:00Z")
            code, out, _ = run_cli(bt.main, ["show", "--out", out_path])
            self.assertEqual(code, 0)
            self.assertIn("orb", out)
        finally:
            shutil.rmtree(tmp)

    def test_json_and_a_missing_ticker(self):
        code, out, _ = run_cli(bt.main, ["run", "--offline", TM_DIR, "--tickers", "TSLA,NOPE", "--setup", "gap",
                                         "--now", "2026-10-19 10:15", "--json", "--config", "/nonexistent.json"])
        doc = json.loads(out)
        self.assertEqual(doc["tickers"], ["TSLA"])
        self.assertTrue(any(r["symbol"] == "NOPE" and r["exclude"] for r in doc["data_checks"]))

    def test_params_need_one_setup(self):
        code, _, err = run_cli(bt.main, ["run", "--offline", TM_DIR, "--tickers", "TSLA", "--params", "{}"])
        self.assertEqual(code, 1)
        self.assertIn("--params needs one --setup", err)


if __name__ == "__main__":
    unittest.main()
