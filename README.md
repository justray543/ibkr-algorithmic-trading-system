# IBKR Algorithmic Trading System

A systematic daily swing-trading system. Generates trend signals, sizes every
position against a fixed risk budget, executes through Interactive Brokers, and
publishes its own performance to a dashboard — including the parts that look bad.

Built in Python. Runs unattended from cron with email and Telegram notifications.

## What it does

- **Signal generation** — EMA9/EMA21 crossover with RSI confirmation on daily
  bars, across a universe of US equities and index futures.
- **Risk-based position sizing** — every entry is sized so a stop-out costs a
  fixed fraction of account value, with an independent cap on gross exposure.
  Positions that don't fit the risk budget are refused rather than shrunk to one
  unit.
- **High-beta short watchlist** — scores the universe by beta against SPY and
  flags high-beta names showing a bearish setup. **Flagging only; no short
  orders are placed.**
- **Backtesting** — single-instrument and portfolio backtests with transaction
  costs and stop modelling, plus a reward-to-risk sweep that reports the
  win-rate/payoff trade-off rather than a single headline number.
- **Live execution** — order placement and state reconciliation against
  broker-reported positions on every run.
- **Crypto module** — a separate long-running daemon trading CFDs through the
  Capital.com REST API, long and short, with genuine server-side ATR stops.
- **Monitoring** — HTML email and Telegram summaries; every run appends to an
  append-only JSONL ledger that drives the dashboard.

## Strategy

Trend-following on the **daily** chart. Long-only on the IBKR side.

- **Entry:** EMA9 crosses above EMA21 with RSI(14) between 50 and 70.
- **Exit:** crossover reversal, or RSI below 40.
- **Stop:** 5% below entry.
- **No take-profit by default.** Fixed targets appeared to hurt trend-following
  in backtesting; `rr_sweep.py` exists to keep re-testing that claim rather than
  taking it on faith.

### On reward-to-risk versus win rate

These are one lever, not two goals. A more distant take-profit is reached less
often by construction; a nearer one is hit more often and earns less. The number
that decides is expectancy — `win_rate × avg_win − (1 − win_rate) × avg_loss` —
and a 30% win rate at 4:1 can carry exactly the same expectancy as 70% at 0.6:1.

`rr_sweep.py` sweeps the target/stop grid and reports the whole surface, because
the shape around the peak matters as much as the peak: a maximum that collapses
when the target moves one notch is a property of the sample, not of the market.
The tool refuses to endorse a setting whose neighbours all lose money.

## Risk model

Sizing answers "how much can I lose", not "how much am I holding". Those differ
by the width of the stop, and with a multiplier attached they differ a lot:

| Instrument | Notional sizing @ 1% | Actual risk at a 5% stop |
|---|---|---|
| SPY @ 742 (mult 1) | 13 shares | 482 — 0.05% of NAV |
| DAX @ 25,181 (mult 25) | 0 → floored to 1 | 629,525 — **63% of NAV** |

The same "1%" setting meant 0.05% on one instrument and 63% on another, because
a `min_qty=1` floor quietly turned "you cannot afford this" into "buy one
anyway". `calculate_risk_based_size` returns **0** in that case, and callers
treat 0 as a skip.

## Architecture

```
beta.py                  Beta vs benchmark + high-beta short watchlist (flag only)
position_sizing.py       Notional and risk-based sizing models
metrics.py               Performance analytics — the single source for Sharpe etc.
backtest.py              Single-instrument backtest
portfolio_backtest.py    Portfolio backtest with costs + allocation
rr_sweep.py              Reward-to-risk / win-rate trade-off sweep
trade_ledger.py          Append-only JSONL record of entries, exits, health
position_ownership.py    Advisory lock so two strategies can't fight over one position
dashboard_export.py      Builds docs/state.json for the dashboard
client.py / wrapper.py   IBKR TWS API client and callback wrapper
contract.py / order.py   IBKR contract and order construction
live_trade.py            Daily futures execution + reconciliation + dashboard export
live_trade_daily.py      Daily equities execution, risk sizing, beta watchlist
capital_crypto_trade.py  Capital.com CFD daemon (long/short, server-side ATR stops)
docs/                    GitHub Pages dashboard (index.html + state.json)
*_config.py              Credentials (gitignored)
```

`strategy.py` contains an older SMA20/200 + MACD experiment. It is not imported
by any live path and is kept only for reference.

Retired 2026-08-02: `live_trade_hourly.py`, `live_trade_nq_30min.py` and
`live_trade_nq_5min.py`. All three traded the same NQ contract the daily
strategy holds, and IBKR reports one aggregate position, so they competed with
each other over a single position. The files remain; only their schedules were
removed.

## Stack

- **Language:** Python (pandas, NumPy, requests)
- **Brokers:** Interactive Brokers TWS API (`ibapi`); Capital.com REST API
- **Notifications:** Gmail (SMTP), Telegram Bot API
- **Deployment:** cron (equities); long-running daemon (crypto); GitHub Pages
  for the dashboard

## Running it

```bash
python3 rr_sweep.py --selftest      # validates the backtest engine, no data needed
python3 rr_sweep.py --tws           # sweep the RR grid on 1Y daily bars
python3 live_trade_daily.py         # one daily pass (requires TWS on port 7497)
```

TWS or IB Gateway must be running with the API enabled on port 7497. Without it
the scripts connect, receive nothing, log "insufficient data" and exit cleanly —
so check `daily_cron_output.log` for real prices, not just for absence of errors.

## Known limitations

Stated because a dashboard that only reports good news is not a monitoring tool.

- **The 5% stop is not resting at the broker.** It is evaluated when the script
  runs, against the last daily close. A move through the stop between runs is
  not caught at the stop level. The Capital.com module does use real
  server-side stops.
- **Fills are assumed, not confirmed.** The IBKR path sends a market order,
  waits, and records the entry at the signal-bar close rather than the actual
  fill price.
- **Backtest figures published before 2026-08-02 were overstated.** The engine
  compounded each trade's return twice — once bar-by-bar while the position was
  held, then again in full at the exit — which roughly squared the return of any
  multi-bar trend trade. Fixed; `rr_sweep.py --selftest` pins the correct
  behaviour. Treat any earlier number as void.
- **Backtests enter and exit at the same bar's close** and model no slippage, so
  rankings are more trustworthy than absolute returns.
- Only `live_trade.py` writes to the trade ledger, so dashboard trade statistics
  describe the futures strategy alone while NAV reflects the whole account.

## Disclaimer

Personal research project. Not investment advice. Trading involves substantial
risk of loss. All figures are from backtests and paper trading unless otherwise
stated.
