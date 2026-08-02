"""
rr_sweep.py

Finds the reward-to-risk setting that actually pays, by sweeping take-profit
targets and stop widths through the backtest and reporting the trade-off.

THE THING TO UNDERSTAND FIRST
-----------------------------
"High RR" and "high win rate" are not two goals to balance. They are two ends
of one lever. Move the take-profit further out and you win less often by
construction, because a more distant target is reached less often. Pull it in
and you win more often and earn less each time. You cannot have both, and a
setting that looks good on either number alone is telling you nothing.

The number that decides is EXPECTANCY -- the average return per trade:

    expectancy = win_rate x avg_win - (1 - win_rate) x avg_loss

A 30% win rate at a 4:1 payoff and a 70% win rate at a 0.6:1 payoff can carry
identical expectancy. Neither is "better"; they differ in how the same edge is
distributed, which matters for drawdown and for whether you can sit through it,
not for how much it makes. So this tool reports the whole curve rather than a
single winner: expectancy picks the peak, and the surrounding shape tells you
how sharp that peak is. A peak that collapses if the target moves by 0.25 is
a fitted artefact, not a setting.

The current strategy runs with NO take-profit (risk_reward_ratio=None), on the
README's claim that fixed targets hurt trend-following. That claim is testable
and it is included in the sweep as the `None` row, so it either survives or it
does not.

USAGE
-----
    python3 rr_sweep.py --selftest              # no data needed, validates the engine
    python3 rr_sweep.py --csv prices.csv        # CSV with a date index and a close column
    python3 rr_sweep.py --tws                   # pull 1Y daily bars for the equity universe

CAVEAT
------
The backtest enters and exits at the same bar's close, models no slippage, and
charges a flat per-side transaction cost. On a paper account with 5-minute
churn those omissions dominate; on daily swing trades they are smaller but not
zero. Treat the ranking as more reliable than the absolute numbers.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from backtest import backtest_intraday_strategy

# None means "no take-profit, exit on the trend rule only" -- the current live
# behaviour, included so it competes against the targeted variants.
DEFAULT_RR_VALUES = [None, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0]
DEFAULT_STOP_VALUES = [0.03, 0.05, 0.08]

# Below this many closed trades a row is reported but never chosen as the sweet
# spot. Expectancy on nine trades is a rumour.
MIN_TRADES_FOR_CONFIDENCE = 30


def sweep(price_history, rr_values=None, stop_values=None,
          initial_capital=100000.0, transaction_cost=0.001,
          periods_per_year=252):
    """
    Run the backtest across the RR x stop grid.

    price_history: dict of symbol -> DataFrame with a 'close' column, the same
                   shape backtest_intraday_strategy expects.

    Returns a DataFrame with one row per (stop, rr) combination, aggregated
    across symbols. Aggregation is trade-weighted, not a mean of per-symbol
    means: a symbol that traded twice should not carry the same weight as one
    that traded eighty times.
    """
    rr_values = rr_values if rr_values is not None else DEFAULT_RR_VALUES
    stop_values = stop_values if stop_values is not None else DEFAULT_STOP_VALUES

    rows = []
    for stop in stop_values:
        for rr in rr_values:
            res = backtest_intraday_strategy(
                price_history,
                initial_capital=initial_capital,
                transaction_cost=transaction_cost,
                stop_loss_pct=stop,
                risk_reward_ratio=rr,
                periods_per_year=periods_per_year,
            )
            if res.empty:
                continue

            total_trades = int(res["closed_trades"].sum())
            if total_trades == 0:
                continue

            # Trade-weighted reconstruction of the pooled statistics.
            w = res["closed_trades"].astype(float)
            wins = (res["win_rate_pct"] / 100.0 * w).sum()
            win_rate = wins / total_trades

            gross_win = (res["avg_win_pct"] / 100.0 * (res["win_rate_pct"] / 100.0 * w)).sum()
            losers = w - (res["win_rate_pct"] / 100.0 * w)
            gross_loss = (res["avg_loss_pct"] / 100.0 * losers).sum()

            avg_win = gross_win / wins if wins > 0 else 0.0
            avg_loss = gross_loss / losers.sum() if losers.sum() > 0 else 0.0
            expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

            rows.append({
                "stop_pct": stop,
                "rr": rr if rr is not None else np.nan,
                "rr_label": "none" if rr is None else str(rr),
                "trades": total_trades,
                "win_rate_pct": round(win_rate * 100, 2),
                "avg_win_pct": round(avg_win * 100, 3),
                "avg_loss_pct": round(avg_loss * 100, 3),
                "payoff_ratio": round(avg_win / avg_loss, 3) if avg_loss > 0 else None,
                "expectancy_pct": round(expectancy * 100, 4),
                "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
                "mean_return_pct": round(res["total_return_strategy"].mean() * 100, 2),
                "mean_sharpe": round(res["sharpe_ratio"].mean(), 3),
                "worst_drawdown_pct": round(res["max_drawdown"].min() * 100, 2),
                "tp_hits": int(res["take_profit_hits"].sum()),
                "stop_hits": int(res["stop_loss_hits"].sum()),
            })

    return pd.DataFrame(rows)


def find_sweet_spot(results, min_trades=MIN_TRADES_FOR_CONFIDENCE):
    """
    The highest-expectancy setting that has enough trades to be believed,
    plus how sharp that peak is.

    `robustness` is the expectancy of the chosen row divided by the mean
    expectancy of its immediate neighbours in RR at the same stop width. Near
    1.0 means a broad plateau you can actually trust. Much above 1.0 means the
    peak is a spike, and a spike in a parameter sweep is usually the sample
    talking, not the market.
    """
    if results.empty:
        return None

    eligible = results[results["trades"] >= min_trades]
    pool = eligible if not eligible.empty else results

    best = pool.loc[pool["expectancy_pct"].idxmax()]

    same_stop = results[results["stop_pct"] == best["stop_pct"]].sort_values(
        "rr", na_position="first").reset_index(drop=True)
    pos = same_stop.index[same_stop["rr_label"] == best["rr_label"]]

    robustness = None
    positive_neighbours = None
    neighbour_count = 0
    if len(pos):
        i = int(pos[0])
        neighbours = [float(same_stop.loc[j, "expectancy_pct"])
                      for j in (i - 1, i + 1) if 0 <= j < len(same_stop)]
        neighbour_count = len(neighbours)
        positive_neighbours = sum(1 for x in neighbours if x > 0)
        neighbour_mean = float(np.mean(neighbours)) if neighbours else 0.0
        # A ratio is only meaningful when the neighbours are themselves
        # profitable. If they are not, dividing by a negative mean produces a
        # negative "robustness" that would read as reassuring while describing
        # a peak whose adjacent settings both lose money -- the definition of
        # a spike. Leave it None in that case and let positive_neighbours say so.
        if neighbour_mean > 0:
            robustness = round(float(best["expectancy_pct"]) / neighbour_mean, 2)

    return {
        "positive_neighbours": positive_neighbours,
        "neighbour_count": neighbour_count,
        "stop_pct": float(best["stop_pct"]),
        "rr": best["rr_label"],
        "trades": int(best["trades"]),
        "win_rate_pct": float(best["win_rate_pct"]),
        "payoff_ratio": best["payoff_ratio"],
        "expectancy_pct": float(best["expectancy_pct"]),
        "profit_factor": best["profit_factor"],
        "worst_drawdown_pct": float(best["worst_drawdown_pct"]),
        "robustness": robustness,
        "enough_trades": int(best["trades"]) >= min_trades,
    }


def report(results, sweet):
    """Print the trade-off table and the conclusion."""
    if results.empty:
        print("No results -- the sweep produced no trades on this data.")
        return

    print("\nRR / win-rate trade-off  (all figures per closed trade)")
    print("=" * 96)
    cols = ["stop_pct", "rr_label", "trades", "win_rate_pct", "payoff_ratio",
            "expectancy_pct", "profit_factor", "worst_drawdown_pct"]
    print(results[cols].to_string(index=False))

    if not sweet:
        return

    print("\n" + "=" * 96)
    print("Sweet spot: stop {:.0%}, take-profit {}".format(
        sweet["stop_pct"], sweet["rr"] if sweet["rr"] != "none" else "none (trend exit only)"))
    print("  win rate {:.1f}%  payoff {}  ->  expectancy {:+.4f}% per trade".format(
        sweet["win_rate_pct"], sweet["payoff_ratio"], sweet["expectancy_pct"]))
    print("  over {} closed trades, worst drawdown {:.2f}%".format(
        sweet["trades"], sweet["worst_drawdown_pct"]))

    if not sweet["enough_trades"]:
        print("  WARNING: fewer than {} trades. Directional at best.".format(
            MIN_TRADES_FOR_CONFIDENCE))

    pos_n, n_count = sweet["positive_neighbours"], sweet["neighbour_count"]
    if n_count and pos_n == 0:
        print("  WARNING: every adjacent RR setting loses money. This is an "
              "isolated spike, not an edge -- do not trade it.")
    elif sweet["robustness"] is None and n_count:
        print("  WARNING: adjacent settings are not profitable on average. "
              "Treat this peak as unstable.")
    elif sweet["robustness"] is not None:
        if sweet["robustness"] > 1.35:
            print("  WARNING: robustness {} -- this is a spike, not a plateau. "
                  "Its neighbours are much worse, which usually means the "
                  "sample is talking.".format(sweet["robustness"]))
        else:
            print("  Robustness {} ({}/{} neighbours also profitable) -- sits "
                  "on a plateau.".format(sweet["robustness"], pos_n, n_count))


# ---------------------------------------------------------------------------
# self-test: synthetic data, no TWS and no CSV needed
# ---------------------------------------------------------------------------
def _synthetic_series(n=1200, seed=7):
    """
    A trending series with noise. Not a market -- just enough structure for the
    EMA/RSI rules to fire so the engine can be exercised end to end.
    """
    rng = np.random.default_rng(seed)
    drift = 0.0004
    shocks = rng.normal(drift, 0.011, n)
    closes = 100 * np.exp(np.cumsum(shocks))
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({"close": closes}, index=idx)


def selftest():
    """
    Validates the double-compounding fix.

    A single buy-and-hold trade must return the underlying move, not its
    square. With transaction costs set to zero and a stop far enough away never
    to trigger, a strategy that holds one position from entry to the end of the
    sample must land within rounding of the underlying's return over that span.
    Before the fix this test failed by orders of magnitude on any trending
    series, which is exactly how the bug hid: everything merely looked good.
    """
    print("Self-test: engine sanity on synthetic data")
    print("-" * 60)

    # Strictly monotonic ramp: entry fires early, no exit ever triggers, so the
    # strategy holds one position to the end and its return must equal the ramp.
    n = 300
    idx = pd.bdate_range("2021-01-01", periods=n)
    closes = np.linspace(100.0, 200.0, n)
    ramp = {"RAMP": pd.DataFrame({"close": closes}, index=idx)}

    res = backtest_intraday_strategy(
        ramp, initial_capital=100000.0, transaction_cost=0.0,
        stop_loss_pct=0.90, risk_reward_ratio=None, periods_per_year=252)

    strat = float(res.loc["RAMP", "total_return_strategy"])
    hold = float(res.loc["RAMP", "total_return_buy_hold"])
    print("  buy-and-hold over the ramp : {:+.4f}".format(hold))
    print("  strategy (one held trade)  : {:+.4f}".format(strat))

    # The strategy enters a few bars in (EMA/RSI warm-up), so it captures
    # slightly less than the full ramp. It must never capture MORE, and it must
    # be the same order of magnitude -- the old bug produced roughly hold^2.
    ok_upper = strat <= hold + 1e-6
    ok_scale = strat > hold * 0.5
    squared = (1 + hold) ** 2 - 1
    print("  what the old double-count would have produced: {:+.4f}".format(squared))

    if ok_upper and ok_scale:
        print("  PASS: no double-compounding, return is bounded by buy-and-hold.")
    else:
        print("  FAIL: strat={} hold={} upper_ok={} scale_ok={}".format(
            strat, hold, ok_upper, ok_scale))
        return 1

    print("\nSweep on a noisy synthetic series (engine exercise, not a result):")
    data = {"SYN": _synthetic_series()}
    results = sweep(data, stop_values=[0.05], rr_values=[None, 1.0, 2.0, 3.0])
    report(results, find_sweet_spot(results))
    return 0


def load_csv(path):
    df = pd.read_csv(path)
    lower = {c.lower(): c for c in df.columns}
    if "close" not in lower:
        raise SystemExit("CSV needs a 'close' column. Found: " + ", ".join(df.columns))
    close_col = lower["close"]
    for cand in ("date", "time", "timestamp"):
        if cand in lower:
            df[lower[cand]] = pd.to_datetime(df[lower[cand]])
            df = df.set_index(lower[cand])
            break
    return {path.split("/")[-1].split(".")[0]: df.rename(columns={close_col: "close"})}


def load_from_tws():
    """Pull 1Y of daily bars for the daily equity universe. Requires TWS up."""
    import threading
    import time
    from wrapper import IBWrapper
    from client import IBClient
    from live_trade_daily import build_instruments

    class App(IBWrapper, IBClient):
        def __init__(self):
            IBWrapper.__init__(self)
            IBClient.__init__(self, wrapper=self)
            self.connect("127.0.0.1", 7497, 191)
            threading.Thread(target=self.run, daemon=True).start()
            time.sleep(3)

    app = App()
    app.reqMarketDataType(3)
    out = {}
    for i, (label, cfg) in enumerate(build_instruments().items()):
        df = app.get_historical_data(91000 + i, cfg["contract"], "1 Y", "1 day", "MIDPOINT")
        if not df.empty and len(df) > 60:
            out[label] = df
            print("  {}: {} bars".format(label, len(df)))
        else:
            print("  {}: skipped ({} bars)".format(label, len(df)))
        time.sleep(2)
    app.disconnect()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--selftest", action="store_true",
                     help="validate the engine on synthetic data, no market data needed")
    src.add_argument("--csv", help="CSV with a date index and a close column")
    src.add_argument("--tws", action="store_true",
                     help="pull 1Y daily bars for the equity universe from TWS")
    ap.add_argument("--cost", type=float, default=0.001, help="per-side transaction cost")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    data = load_csv(args.csv) if args.csv else load_from_tws()
    if not data:
        print("No usable price data.")
        return 1

    results = sweep(data, transaction_cost=args.cost)
    report(results, find_sweet_spot(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
