# Hybrid Crypto Trading Bot

A regime-aware crypto trading bot for KuCoin, built on **freqtrade**, using
Grok (xAI) as an in-loop confirmation layer and a Grok Bot "strategy council"
as a bounded, off-loop adaptive layer. Fully hands-off: it trades autonomously
and narrates to Telegram. There are no approval prompts anywhere in the trade loop.

Freqtrade owns the chassis — market data, order execution, exchange-side stops,
Telegram, protections, backtesting, and hyperopt. This repo owns the decision
logic: the regime classifier, the weighted composite, the Grok veto, and the
council's bounded adaptive layer.

**Status: paper mode. Not yet profitable — under calibration.** See
[Known state](#known-state) below before running anything with real money.

## How it works

Every 15-minute candle close, per pair:

```
freqtrade candle → features (indicators, regime, composite) → entry conditions
  → Grok confirmation (live only) → freqtrade protections → order + exchange stop
```

The deterministic layer decides *what is worth looking at*; Grok can only ever
veto, never originate; freqtrade's protections and the exchange-side stop run
regardless. LLM latency never sits in a price-reaction path — Grok is called at
candle close, and never during a backtest.

### Three tiers of change

| Tier | What | Who | Where |
|---|---|---|---|
| Baseline | Entry/exit parameters found by hyperopt | Offline search, committed | `user_data/strategies/*.json` |
| Levers | Overrides within bounds, live only | Grok Bot council, automatically | `user_data/strategy_config.json` |
| Logic | Indicators, rules, new venues, bug fixes | Human + coding agent | Python source, via git |
| Constitution | Lever bounds, risk constants, mode | Human only, deliberately | `core/levers.py` |

The council writes **data, never code**, inside bounds it cannot resize. Every
lever moves toward less risk freely and toward more risk only up to a hard cap.
Proposals pass signature validation, bounds clamping, forbidden-key rejection,
and a 30-day backtest gate, then face a 72h/20-trade auto-revert watchdog.

## Layout

| File | Role |
|---|---|
| `user_data/strategies/RegimeComposite.py` | The strategy: entries, exits, ATR trail, Grok veto |
| `user_data/config.json` | Freqtrade config, static pair universe (backtest-safe) |
| `user_data/config_live.json` | Live overlay adding volume/spread/volatility filters |
| `user_data/strategy_config.json` | The council's tunable levers |
| `core/features.py` | Vectorised indicators, regime, composite (freqtrade path) |
| `core/indicators.py` | Scalar TA reference implementation, unit-tested |
| `core/regime.py` | Regime classifier and entry preconditions |
| `core/levers.py` | The lever constitution: bounds, forbidden keys |
| `core/settings.py` | Env settings and KuCoin BTC/XBT symbol assertions |
| `core/strategy_state.py` | Glue: lever access, sentiment cache, Grok veto |
| `core/grok_agent.py` | xAI decision layer with a strict JSON contract |
| `core/sentiment.py` | Grok X-sentiment via the xAI Responses API |
| `core/adaptive.py` | Council intake: validation, backtest gate, auto-revert |
| `council/` | SCOUT / ANALYST / RED TEAM / STRATEGIST prompts |

This public copy is the decision logic, research scripts and tests. Deployment, provisioning, instance management and trade data are deliberately left out.

## Quick start

Requires Python >= 3.11 and the TA-Lib C library.

```bash
uv venv --python 3.12 .venv          # or python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                 # xAI + Telegram; KuCoin keys only for live
.venv/bin/python -m pytest -q
```

Backtest a week, then run paper:

```bash
.venv/bin/freqtrade download-data -c user_data/config.json --timeframes 15m --days 14
.venv/bin/freqtrade backtesting -c user_data/config.json -s RegimeComposite \
    --timerange=20260822-20260829 --cache none
.venv/bin/freqtrade trade -c user_data/config.json -c user_data/config_live.json \
    -s RegimeComposite
```

Tune properly with hyperopt rather than by hand:

```bash
.venv/bin/freqtrade hyperopt -c user_data/config.json -s RegimeComposite \
    --hyperopt-loss SharpeHyperOptLoss -e 200
```

## Known state

Built on freqtrade, tests passing, backtested on 12 KuCoin pairs over four
months of 15m data (2026-05-01 to 2026-08-29). Parameters were searched with
hyperopt on May-July and validated on unseen August data.

**The strategy does not have a demonstrated edge. Do not put money on it.**

| Window | Params | Trades | Win rate | Result |
|---|---|---|---|---|
| Train (May-Jul) | baseline | 518 | 27.6% | −9.77% |
| Train (May-Jul) | hyperopt best of 150 epochs | 409 | 24.9% | −6.33% |
| Test (Aug, unseen) | hyperopt | 172 | 29.7% | **+1.74%** |
| **Full (May-Aug)** | **hyperopt** | **580** | **26.6%** | **−4.54%** |

Read those rows together. Hyperopt searched 150 configurations across the whole
parameter space and the best one it could find *on the data it was fitting to*
still lost 6.33%. The tuned parameters did generalise — August, which the search
never saw, came out +1.74% — but one good month after three bad ones is regime
luck, not edge. Over the full period: **profit factor 0.88, CAGR −13.5%.**

What the search did buy us is real but modest: on the same unseen August window,
tuned parameters returned +1.74% where the baseline returned −0.26%. The tuning
works; the underlying signal is what is missing.

### Where the loss actually comes from

Trade shape is right, edge is not. Winners hold ~10x longer than losers (median
615 min vs 60 min), which is the correct signature for trend-following. But at a
26.6% win rate and 1.80:1 reward/risk, break-even needs 35.8% — a 13-point gap.
Every dollar of loss comes from trades that go wrong within the hour; once a
trade survives long enough to trail, it is profitable.

So the gap is **entry quality**, not exits, sizing, or frequency. Two things
remain untested against it: Grok's confirmation veto (live-only, never modelled
in a backtest, and designed to remove exactly these immediate losers), and
entry timing — currently entries fire at signal-candle close rather than waiting
for a pullback.

### Frequency is not the problem

580 trades over 120 days is ~4.8/day across 12 pairs, or 0.4 per pair per day —
inside the original 3-8/day design target. Note that the required edge *falls*
as frequency rises (at 5 trades/day you need +0.46%/trade to clear infra costs;
at 18/day, +0.13%), so cutting frequency is not the fix. Entries are now
governed by a daily risk budget rather than a trade count.

### Bugs found and fixed along the way

- Oscillators were scored with mean-reversion orientation in *every* regime, so
  momentum and trend signals cancelled and the composite could never reach its
  threshold (2 signals in 45 days). They now carry both orientations and the
  regime selects.
- The ATR trail engaged from the entry candle, turning trend trades into
  45-minute scalps. `trail_after_r` now delays it until the trade is up N×risk.
- `max_daily_entries` was a dead lever, never wired to anything. Replaced with
  the daily risk budget.

## Safety properties

- API keys are trade-only, loaded from env, never committed.
- Every position gets an exchange-side stop placed atomically with entry; if the
  stop fails, the position is closed immediately.
- Freqtrade owns the wallet and sizes from real balances, not an internal ledger.
- Protections (cooldown, max-drawdown, stoploss guard, low-profit pairs) run
  independently of the strategy and cannot be disabled by a lever.
- Exits are never throttled by entry-side limits.
- Entries are governed by a **daily risk budget** (default 2% of equity at
  risk across open positions), not a trade count — a wider stop consumes more
  budget and permits fewer trades, automatically.
- Grok Bot never places trades, holds credentials, touches the server, or writes code.
- The bot runs indefinitely with no council input; every council artifact fails safe.

## Do not skip

Paper validation gates live capital: 200+ trades, >60% win rate, <20% max
drawdown, Sharpe >1.0. Shorting (aggressive mode) validates on its own separate
track.
