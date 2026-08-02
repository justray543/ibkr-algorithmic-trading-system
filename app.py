import threading
import time
import pandas as pd
from wrapper import IBWrapper
from client import IBClient
from contract import future

class IBApp(IBWrapper, IBClient):
    def __init__(self, ip, port, client_id, account):
        IBWrapper.__init__(self)
        IBClient.__init__(self, wrapper=self)
        self.account = account
        self.connect(ip, port, client_id)
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        time.sleep(3)


def fetch_with_retry(app, base_request_id, contract, what_to_show, duration, bar_size, max_retries=5):
    for attempt in range(1, max_retries + 1):
        request_id = base_request_id + (attempt * 10000)
        df = app.get_historical_data(request_id, contract, duration, bar_size, what_to_show)
        if not df.empty:
            return df
        print("Attempt " + str(attempt) + " failed, retrying after pause...")
        time.sleep(15)
    return df


def compute_signal(prices, rsi_window=14):
    ema9 = prices.ewm(span=9, adjust=False).mean()
    ema21 = prices.ewm(span=21, adjust=False).mean()

    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=rsi_window).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=rsi_window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    return ema9, ema21, rsi


def backtest_no_stop(df, initial_capital=100000, transaction_cost=0.001,
                      rsi_entry_min=50, rsi_entry_max=70, rsi_exit=40):
    df = df.copy()
    df["ema9"], df["ema21"], df["rsi"] = compute_signal(df["close"])
    df = df.dropna()

    cash = initial_capital
    position = 0
    entry_price = 0.0
    trades = []
    equity_curve = []

    for i in range(len(df)):
        row = df.iloc[i]
        price = row["close"]

        entry_signal = row["ema9"] > row["ema21"] and rsi_entry_min < row["rsi"] < rsi_entry_max
        exit_signal = row["ema9"] < row["ema21"] or row["rsi"] < rsi_exit

        if position == 0 and entry_signal:
            entry_price = price * (1 + transaction_cost)
            position = 1
            trades.append({"entry_time": df.index[i], "entry_price": entry_price})

        elif position == 1 and exit_signal:
            exit_price = price * (1 - transaction_cost)
            pnl_pct = (exit_price - entry_price) / entry_price
            cash = cash * (1 + pnl_pct)
            trades[-1]["exit_time"] = df.index[i]
            trades[-1]["exit_price"] = exit_price
            trades[-1]["pnl_pct"] = pnl_pct
            position = 0

        if position == 1:
            unrealized_pct = (price - entry_price) / entry_price
            equity_curve.append(cash * (1 + unrealized_pct))
        else:
            equity_curve.append(cash)

    final_equity = equity_curve[-1] if equity_curve else initial_capital
    total_return = (final_equity - initial_capital) / initial_capital

    equity_series = pd.Series(equity_curve)
    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max
    max_drawdown = drawdown.min()

    closed_trades = [t for t in trades if "exit_price" in t]
    num_trades = len(closed_trades)
    wins = [t for t in closed_trades if t["pnl_pct"] > 0]
    win_rate = (len(wins) / num_trades * 100) if num_trades > 0 else 0

    returns = equity_series.pct_change().dropna()
    sharpe = (returns.mean() / returns.std() * (252 ** 0.5)) if returns.std() > 0 else 0

    buy_hold_return = (df["close"].iloc[-1] - df["close"].iloc[0]) / df["close"].iloc[0]

    print("\nOriginal EMA9/21 + RSI Backtest — NQ, 5-min bars, NO STOP-LOSS:")
    print("Total Return (Strategy):", round(total_return * 100, 2), "%")
    print("Total Return (Buy & Hold):", round(buy_hold_return * 100, 2), "%")
    print("Sharpe Ratio:", round(sharpe, 2))
    print("Max Drawdown:", round(max_drawdown * 100, 2), "%")
    print("Number of Trades:", num_trades)
    print("Win Rate:", round(win_rate, 2), "%")
    print("Final Equity (Strategy): $" + str(round(final_equity, 2)))
    print("Final Equity (Buy & Hold): $" + str(round(initial_capital * (1 + buy_hold_return), 2)))


if __name__ == "__main__":
    app = IBApp("127.0.0.1", 7497, 161, "DUQ153118")
    app.reqMarketDataType(3)

    print("Fetching 6 months of 5-minute bars for NQ...")

    nq_contract = future("NQ", "CME", "202609", currency="USD")

    history = fetch_with_retry(app, 106000, nq_contract, "TRADES", "2 M", "5 mins")

    if not history.empty:
        print("NQ: " + str(len(history)) + " rows, from " + str(history.index.min()) + " to " + str(history.index.max()))
        backtest_no_stop(history)
    else:
        print("NQ: no data returned after retries.")

    app.disconnect()