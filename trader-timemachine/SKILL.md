---
name: trader-timemachine
description: >-
  Time Machine. Replays every possible outcome of a trade before you risk a
  dollar: 10,000 simulated sessions plus every past time the setup fired.
  Takes the plan from trader-exit (entry, stop, target), rebuilds the rest of
  the day 10,000 times from the stock's own 5-minute bars over the last 60
  days, and re-runs the same rule on every past session. Prints the odds of
  target first, stop first, or neither by the close, where the price sits at
  the close, the average R, the history, and one honest verdict, then hands
  off to /trader-budget. Use when the user says "run the time machine",
  "replay this trade", "what are the odds", "simulate this trade", "monte
  carlo", "backtest this setup", "how often does this hit the target", "is
  this a coin flip", or after /trader-exit writes a plan, before
  /trader-budget.
argument-hint: "[path to plan.json, or TICKER]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
days, how often does this plan hit the target first, the stop first, or
neither? It answers with 10,000 replays and with the real record of the same
rule on every past session. It is a simulation built from the stock's own
recent bars, not a prediction. Day trading loses most people money, and a
good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:55      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol TSLA --date 2026-10-19 \
    --entry 364.18 --stop 358.32 --target 370.04 --entry-time "10:15 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`), `--now "2026-10-19 10:15"`
(New York time) to pin the clock. v2.1 flags: `--method block|iid` (default
block), `--block N` (bars a run, default 6), `--similar N` (replay from the N
past days most like today, default 30, 0 = all), `--peers A,B,C` (run the
same rule's history on other stocks for a bigger sample), `--config` (costs).
`--window N` is for the iid method only.

## v2.1: the block method (the default) and the verdict from history

The v2.0 replays drew every bar on its own. That throws away how bars follow
each other (momentum, mean reversion, volatility bursts), so a 1R plan lands
near a coin flip almost by construction. v2.1 fixes three things:

- **Runs of real bars.** Each replay stitches 6-bar runs (half an hour) cut
  from real past sessions at the same time of day, so what happens inside a
  run is what really happened.
- **Similar days.** The runs come from the 30 past days most like today so
  far: the closest in how much the stock had moved by the entry bar.
- **Realistic fills and costs.** A target needs a trade a cent through it,
  a gap through the stop fills at the bar's open, and spread, slippage and
  fees (trader/config.json `costs`) come off. Both R before and after costs
  are printed.

**The verdict now comes from the history, not the replays.** 10,000 replays
of 59 days are still 59 days of evidence. The verdict line is the plan's own
rule on every real past session (plus `--peers`), with trader-backtest's
fill model and costs, judged like trader-backtest judges: "History backs this
rule (EDGE)", "History says no (NO EDGE)" or "Can't tell yet (NOT ENOUGH
EVIDENCE)", with the average R after costs and its 95 % range. The replay
odds follow on the next line as context. With fewer than 30 past triggers it
says so and suggests `--peers`. The vwap and gap setups get a history too.
This needs the trader-backtest skill installed next to this one.

`--method iid` runs v2.0 exactly, the reel's numbers below.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range |
| Yahoo chart API | one request: 60 days of 5-minute bars (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the close |

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date: all 78
   five-minute bars, 9:30 to 4:00 New York time. Half days, gaps and today
   are left out. Every bar becomes four ratios to the bar before it:
   open, high, low and close over the previous close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default 4:00 PM, the
   close, or 1:00 PM on an NYSE early-close day). Green, which means target
   first or above the entry at `--until`.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at `--until` divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions. That is
   what really happened, not a simulation.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them. So a 2R plan with target first
   24.1 % and stop first 50.5 % is close to break-even (it needs 33 % of
   the 74.6 % that hit a side, and got 32 %), not a loser.
6. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (wrong date, not closed yet, a future day), or when the plan
does not make sense (a stop above a buy's entry, an entry off the 5-minute
grid). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. Add `--until 15:55` when the plan has a
   flat-by time, so "neither" means still open at the time you would get out.
   Add `--peers` with a few similar liquid stocks when the history is thin.
3. Tell the user, in this order: the verdict line exactly as printed (it
   comes from the history after costs), the history line with its 95 %
   ranges, then the replay odds, the average R before and after costs, and
   the price range at the close. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Earnings, news and halts can do things those 60 days never did. Check
   `/trader-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example: the reel, v2.1 (the default)

```
History, realistic fills after costs: 33 past triggers (33 on TSLA): win rate 42.4 %
(95 %: 27.2 % to 59.2 %), average -0.170 R (95 %: -0.489 to +0.173).
History says no (NO EDGE): average -0.170 R a trade after costs over 33 trades: it lost money.
Replays: target first 36.5 %, stop first 25.1 %, neither 38.4 %; average +0.119 R, +0.066 R after costs.
```

The replays from similar days look mildly good; the real record of the same
rule, filled the way it would really have filled, lost money. When they
disagree, the history wins: it is what happened.

## Example: the reel, v2.0 (`--method iid`)

TSLA, Monday 2026-10-19. The 15-minute opening range is 354.47 to 362.18.
The 10:10 bar closes above it: entry 364.18 at 10:15 AM (bar 8), stop 358.32
(the range midpoint, rounded down), target 370.04 (1R, risk 5.86 a share).

```
10,000 replays of the rest of the session, 10:15 AM to 4:00 PM (69 five-minute bars), seed 19.
  target first   29.4 %
  stop first     25.5 %
  neither        45.1 %   still open at 4:00 PM
  green          51.7 %   target first, or above the entry at 4:00 PM
  Price at 4:00 PM: median 364.36, 90 % of replays between 355.54 and 373.54.
  Average R: +0.034
History: the same rule on each of those 59 real sessions fired 33 times:
13 target, 15 stop, 5 neither. Average R -0.062.
Close to a coin flip: target first 29.4 % vs stop first 25.5 %.
45.1 % of the replays hit neither by 4:00 PM; the price at 4:00 PM decides those.
```

At 1R the target has to come first in half of the replays that hit one
side. It did in 53.6 %. The replays came out close to a coin flip, and the
real history was slightly worse than even. That is the honest answer for this setup, and it is what
the user should hear before `/trader-budget` sizes 8 shares.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
