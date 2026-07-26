# IBKR Algorithmic Trading System

An automated, self-monitoring systematic trading system. Generates signals from a
trend-following strategy, validates them across multiple time horizons through a
backtesting engine, and executes live orders through Interactive Brokers, with a
separate module for crypto CFDs through Capital.com.

Built in Python. Runs unattended on a scheduled basis with email and Telegram
notifications on every trade.

## What it does

- **Signal generation** — EMA9/EMA21 crossover with RSI confirmation across a
  configurable universe of futures, ETFs, and equities.
- **Backtesting** — single-instrument and portfolio-level backtests with
  transaction costs and stop-loss modelling, validated over 1Y / 5Y / 10Y / 20Y
  daily-bar windows.
- **Live execution** — order placement and position management through the
  Interactive Brokers TWS API, with state reconciliation against broker-reported
  positions on every run.
- **Crypto module** — a long-running daemon trading BTC / ETH / SOL CFDs through
  the Capital.com REST API, with server-side ATR-based stops and long/short logic.
- **Monitoring** — HTML-formatted email summaries and Telegram notifications;
  runs are logged to disk for audit.

## Strategy

Trend-following. Long (and, in the crypto module, short) on EMA9/EMA21 crossover
confirmed by RSI:

- **Entry:** EMA9 crosses above EMA21 with RSI between 50 and 70.
- **Exit:** crossover reversal, or RSI below 40.
- **Risk:** 5% stop-loss (equities/futures); ATR-based server-side stop (crypto).
- **No take-profit:** fixed risk/reward targets were shown in backtesting to hurt
  trend-following returns, so positions are held until the trend exit or stop.

Portfolio-level diversification across uncorrelated instruments was confirmed
empirically to reduce maximum drawdown versus single-instrument trading.

## Architecture

```
strategy.py            Signal logic (EMA/RSI)
backtest.py            Single-instrument backtest
portfolio_backtest.py  Portfolio backtest with transaction costs + allocation
client.py / wrapper.py IBKR TWS API client and callback wrapper
contract.py / order.py IBKR contract and order construction
live_trade.py          Live execution + state reconciliation (IBKR)
capital_crypto_trade.py  Capital.com crypto daemon (long/short, ATR stops)
email_config.py        Notification config (gitignored)
telegram_config.py     Notification config (gitignored)
```

## Stack

- **Language:** Python (pandas, NumPy, requests)
- **Brokers/execution:** Interactive Brokers TWS API (`ibapi`); Capital.com REST API
- **Notifications:** Gmail (SMTP), Telegram Bot API
- **Deployment:** scheduled via cron (equities/futures); long-running daemon (crypto)

## Development approach

Iterative and validation-first: environment → strategy → backtest → live execution
→ monitoring. Every strategy is validated quantitatively across multiple time
horizons and paper-traded before any live capital is committed.

## Disclaimer

Personal research project. Not investment advice. Trading involves substantial risk
of loss. All figures are from backtests and paper trading unless otherwise stated.
