---
name: trader-backtest
description: >-
  Backtest. Answers the only question that matters before a setup goes live:
  does it make money after costs, on days it was not tuned on? Runs the
  opening-range breakout (orb), VWAP reclaim (vwap) and gap and go (gap)
  setups across a universe of liquid stocks on 60 days of 5-minute bars,
  with realistic fills (in at the next bar's open, stops filled through
  gaps, targets only when traded through, spread, slippage and fees), a
  walk-forward split (tune on older days, judge on newer ones), day-clustered
  95 % ranges, and a plain verdict: EDGE, NO EDGE or NOT ENOUGH EVIDENCE.
  Saves the verdicts to trader/state/edge.json, which trader-risk reads to
  block live entries on unproven setups, and which trader-entry reads for the
  tested parameters. Also checks the data first (bad bars, outliers, splits,
  thin and cheap stocks, stale data, optional Stooq cross-check). Use when
  the user says "backtest", "does this setup work", "is there an edge",
  "test the strategy", "walk-forward", "out-of-sample", "how much do costs
  matter", "prove it", or before switching from paper to live.
argument-hint: "[orb | vwap | gap | all] [--tickers A,B,C]"
---

# trader-backtest

The rest of the pack times, sizes and checks a trade. This skill asks
whether the trade is worth taking at all. It runs a setup on every session
of every stock in a universe, the way it would really have been traded, and
reports what is left after costs on data the rule was not tuned on.

Most day-trading setups do not survive this. That is the point: finding out
on paper, for free, instead of in a live account. Day trading loses most
people money, and an EDGE verdict on 60 days is evidence, not a promise.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run                          # all three setups, default universe
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --setup orb --write      # save the verdict for trader-risk
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --setup vwap --tickers NVDA,AMD,TSLA,META
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --setup gap --tickers-file trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --setup orb --params '{"or_minutes": 15, "stop_rule": "or_mid", "target_r": 1}'
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --no-costs               # see how much the costs matter
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --crosscheck             # compare closes with Stooq
python3 "${CLAUDE_SKILL_DIR}/backtest.py" show                         # the saved verdicts
```

Other flags: `--train 0.6` (the in-sample share of the days), `--fill close`
(the old, optimistic entry at the trigger close), `--offline DIR` (saved
charts, `<SYM>-60d5m.json.gz`), `--trades` (every trade in `--json`),
`--now "2026-10-19 10:15"` (New York time).

## What it reads and writes

| file | what |
| --- | --- |
| Yahoo chart API | 60 days of 5-minute bars a ticker, one request each, cached in `trader/cache/` for the day |
| `trader/config.json` | `costs`, `no_entries_before`, `no_entries_after`, `flat_by`, `min_price`, `min_dollar_volume` |
| `trader/state/edge.json` | with `--write`: each setup's verdict, the chosen parameters, the in- and out-of-sample numbers, the costs, the data checks |
| `setups.py` | the setups, the fill model and the cost model. trader-entry, trader-exit, trader-timemachine, trader-budget and trader-risk all load this one file |

## How it judges a setup

1. **The data is checked first** (`marketdata.py`). Full sessions only.
   A session with an impossible bar, or a bar 15 times wider than that time
   of day's usual range, is dropped. A stock whose open jumps 40 % from the
   previous close (a split or bad data), that trades under $5, or under $20M
   a day is left out, because the cost model does not hold there. Stale
   data, duplicate bars and patchy volume are warnings. Every exclusion is
   listed with its reason.
2. **The trades.** The setup's rule runs on every session, inside your entry
   window. The fill model is the honest one. You get in at the next bar's
   open, never the close you just saw. A stop fills at the bar's open when
   the bar gaps through it. A target needs a trade a cent through it. A bar
   that reaches both counts as the stop. You are out by `flat_by`. Spread,
   slippage and fees come off every fill.
3. **Walk-forward.** The days are split by date. Each parameter set in the
   setup's small grid (12 sets) runs on the older 60 %, the best one is
   picked, and only that one runs on the newer 40 %. Only the newer days
   count. The in-sample number is printed too, marked as flattered.
4. **The 95 % range** of the average R comes from resampling whole days,
   because stocks move together on the same day. The win rate gets a Wilson
   interval.
5. **The verdict**, out-of-sample. NOT ENOUGH EVIDENCE: under 30 trades, or
   a positive average whose range still includes losing money. EDGE: the
   whole range above 0 R. NO EDGE: the average is 0 R or worse.

## Steps

1. Ask which setup, or run all three. Use the default universe unless the
   user has a watchlist they trade.
2. Run it with `--write`, so trader-risk can read the verdict.
3. Tell the user, per setup: the verdict and its reason exactly as printed,
   the out-of-sample trades, win rate and average R after costs with their
   95 % ranges, and how much the costs ate. Then the data checks that left
   stocks or days out.
4. NO EDGE: say plainly the setup lost money after costs on days it was not
   tuned on. Do not suggest tweaking until it passes; that is how luck gets
   called an edge.
5. NOT ENOUGH EVIDENCE: say what the numbers lean toward and that more
   stocks (`--tickers`) or more weeks of paper trading are the way to find
   out.
6. EDGE: say it is evidence from one 60-day stretch, and that paper trading
   it first is still the plan. Re-run weekly; trader-risk ignores a verdict
   older than 7 days.

## Example: the reel's stock, TSLA alone

```
opening-range breakout (orb)
  VERDICT  NOT ENOUGH EVIDENCE: 13 trades out-of-sample, under the 30 it takes to say anything;
           so far -0.362 R a trade after costs (95 % range -0.707 to -0.021)
  in       20 trades, +0.108 R a trade (picked as the best of 12: flattered)
  out      win rate 30.8 % (95 %: 12.7 % to 57.6 %)
           average -0.362 R after costs, -0.289 R before costs
```

The rule that looked best on the older days lost money on the newer ones.
With every setting fixed at the reel's (15-minute range, midpoint stop, 1R),
the same 59 sessions give 33 trades and -0.170 R a trade after costs. One
stock is too few trades to judge: run it on the universe.

## Rules

- Only the out-of-sample number decides. Never present the in-sample number
  as the result.
- Every number comes from the run. Repeat the verdict line as printed.
- Never call EDGE a guarantee: 60 days is one market regime, and today's
  large caps flatter the results (survivorship).
- Costs stay on unless the user asks to compare with `--no-costs`.

## What it never does

It never places an order, never sizes a position, never changes the user's
config, and never tunes a rule on the same days it judges it.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
