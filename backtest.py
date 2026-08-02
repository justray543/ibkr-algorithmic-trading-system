import pandas as pd
import numpy as np

import volume_pressure

def backtest_intraday_strategy(
    price_history: dict,
    initial_capital: float = 100000.0,
    transaction_cost: float = 0.001,
    stop_loss_pct: float = 0.05,
    risk_reward_ratio: float = None,  # e.g. 2.0 means take-profit at 2x the stop distance
    periods_per_year: int = 252,      # 252 daily, 252*6.5 hourly, 252*78 5-min
    require_volume_confirm: bool = False,  # gate entries on rising cumulative delta
    volume_lookback: int = 5,
):
    results = {}

    for symbol, df_raw in price_history.items():
        if df_raw is None or len(df_raw) < 100:
            print(f"Skipping {symbol}: Not enough data")
            continue

        df = df_raw.copy()
        if 'close' not in df.columns:
            df = df.rename(columns={df.columns[0]: 'close'})

        df['close'] = df['close'].ffill()
        df = df.dropna(subset=['close'])

        df['EMA9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['EMA21'] = df['close'].ewm(span=21, adjust=False).mean()

        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(window=14).mean()
        loss = -delta.where(delta < 0, 0).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        # Optional volume-pressure gate. Computed before the dropna so it stays
        # aligned to the price rows it was derived from.
        if require_volume_confirm:
            rising = volume_pressure.pressure_rising(df, lookback=volume_lookback)
            if rising is None:
                # No traded volume -- MIDPOINT bars return -1. Running unfiltered
                # here would silently produce a "with volume filter" result that
                # is identical to the baseline, which is worse than skipping.
                print(f"Skipping {symbol}: volume filter requested but bars carry no volume")
                continue
            df['VOL_OK'] = rising
        else:
            df['VOL_OK'] = True

        df = df.dropna().reset_index(drop=True)

        if len(df) == 0:
            print(f"Skipping {symbol}: empty after dropna")
            continue

        position = 0
        equity = [initial_capital]
        trades = 0
        wins = 0
        losses = 0
        stop_loss_hits = 0
        take_profit_hits = 0
        entry_price = 0.0
        stop_price = 0.0
        target_price = 0.0

        # Per-trade round-trip returns, kept separately from the equity path.
        # The equity curve compounds ONE bar at a time; a trade's total return
        # is a different quantity and mixing the two is what produced the
        # double-counting bug this loop used to have. Win rate, payoff ratio
        # and expectancy are all computed from this list.
        trade_returns = []

        # Equity compounds ONE bar per iteration, in every branch. An exit bar
        # contributes the move from the previous close to the exit price and
        # nothing more -- the bars before it were already compounded while the
        # position was held. Applying the whole-trade return here as well is
        # the double-count that used to inflate every figure this function
        # produced, roughly squaring the return of any multi-bar trend trade.
        for i in range(1, len(df)):
            curr = df.iloc[i]
            prev_close = df.iloc[i - 1]['close']

            def close_trade(exit_price, tag):
                """Book one exit: single-bar move for equity, round-trip for stats."""
                bar_return = (exit_price / prev_close) - 1
                equity.append(equity[-1] * (1 + bar_return) * (1 - transaction_cost))
                trade_return = (exit_price / entry_price) - 1
                trade_returns.append({"ret": trade_return, "reason": tag})
                return trade_return

            if (position == 0 and curr['EMA9'] > curr['EMA21'] and curr['RSI'] > 50
                    and curr['VOL_OK']):
                position = 1
                entry_price = curr['close']
                stop_price = entry_price * (1 - stop_loss_pct)
                if risk_reward_ratio is not None:
                    risk_distance = entry_price - stop_price
                    target_price = entry_price + (risk_distance * risk_reward_ratio)
                else:
                    target_price = None
                trades += 1
                equity.append(equity[-1] * (1 - transaction_cost))

            elif position == 1 and curr['close'] <= stop_price:
                # Filled at the close, not at the stop level. The close is at
                # or below the stop in this branch, so this is the conservative
                # read and it stops the backtest assuming a gap-free fill.
                ret = close_trade(curr['close'], "stop")
                position = 0
                stop_loss_hits += 1
                if ret > 0:
                    wins += 1
                else:
                    losses += 1

            elif position == 1 and target_price is not None and curr['close'] >= target_price:
                ret = close_trade(curr['close'], "target")
                position = 0
                take_profit_hits += 1
                if ret > 0:
                    wins += 1
                else:
                    losses += 1

            elif position == 1 and (curr['EMA9'] < curr['EMA21'] or curr['RSI'] < 40):
                ret = close_trade(curr['close'], "trend")
                position = 0
                if ret > 0:
                    wins += 1
                else:
                    losses += 1

            elif position == 1:
                bar_return = (curr['close'] / prev_close) - 1
                equity.append(equity[-1] * (1 + bar_return))

            else:
                equity.append(equity[-1])

        # Settle any position still open at the end of the sample. The equity
        # path already carries every bar up to the final one, so this only
        # books the exit cost -- no extra return is applied.
        if position == 1:
            equity.append(equity[-1] * (1 - transaction_cost))
            final_ret = (df.iloc[-1]['close'] / entry_price) - 1
            trade_returns.append({"ret": final_ret, "reason": "open_at_end"})
            if final_ret > 0:
                wins += 1
            else:
                losses += 1

        total_return = (equity[-1] / initial_capital) - 1
        buy_hold_return = (df.iloc[-1]['close'] / df.iloc[0]['close']) - 1

        returns = pd.Series(equity).pct_change().dropna()
        # Annualisation must match the bar size the caller passed in. This was
        # hardcoded to sqrt(252*6.5), an hourly-bar constant, which overstated
        # Sharpe by about 2.5x on every daily-bar run.
        sharpe = (returns.mean() / returns.std() * np.sqrt(periods_per_year)) \
            if len(returns) > 1 and returns.std() > 0 else 0.0

        closed = [t["ret"] for t in trade_returns]
        won = [r for r in closed if r > 0]
        lost = [r for r in closed if r <= 0]

        win_rate = (len(won) / len(closed)) if closed else 0.0
        avg_win = (sum(won) / len(won)) if won else 0.0
        avg_loss = (abs(sum(lost)) / len(lost)) if lost else 0.0

        # Payoff ratio is the realised reward-to-risk: how much the average
        # winner makes per unit the average loser costs. Expectancy combines it
        # with win rate into the only number that decides whether a setting is
        # better -- a high payoff at a low win rate and a low payoff at a high
        # win rate can produce the same expectancy, and neither is "the goal".
        payoff = (avg_win / avg_loss) if avg_loss > 0 else None
        expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

        gross_win = sum(won)
        gross_loss = abs(sum(lost))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

        results[symbol] = {
            'total_return_strategy': round(total_return, 6),
            'total_return_buy_hold': round(buy_hold_return, 6),
            'sharpe_ratio': round(sharpe, 6),
            'max_drawdown': round((pd.Series(equity) / pd.Series(equity).cummax() - 1).min(), 5),
            'num_trades': trades,
            'closed_trades': len(closed),
            'win_rate_pct': round(win_rate * 100, 2),
            'avg_win_pct': round(avg_win * 100, 4),
            'avg_loss_pct': round(avg_loss * 100, 4),
            'payoff_ratio': round(payoff, 3) if payoff is not None else None,
            'expectancy_pct': round(expectancy * 100, 4),
            'profit_factor': round(profit_factor, 3) if profit_factor is not None else None,
            'stop_loss_hits': stop_loss_hits,
            'take_profit_hits': take_profit_hits,
            'final_equity_strategy': round(equity[-1], 2),
            'final_equity_buy_hold': round(initial_capital * (1 + buy_hold_return), 2)
        }

    return pd.DataFrame(results).T