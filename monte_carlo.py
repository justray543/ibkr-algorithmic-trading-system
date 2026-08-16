"""
monte_carlo.py

How much of a backtest result is edge, and how much is the order the trades
happened to arrive in?

THE QUESTION THIS ANSWERS
-------------------------
A backtest gives one path. That path had one sequence of trades, and sequence
matters enormously for the things that actually stop you trading: a strategy
whose three biggest losers land consecutively looks nothing like the same
strategy with them spread out, even though every trade is identical.

Resampling the trades gives a distribution instead of a point. The useful
outputs are not the average -- that just reproduces the expectancy you already
measured -- but the tails:

  - the 5th percentile of final equity: a bad-but-plausible run
  - the distribution of maximum drawdown: what you would have had to sit through
  - P(final < starting): how often this edge loses money anyway
  - risk of ruin: how often it goes below a threshold you could not survive

TWO MODES, ANSWERING DIFFERENT QUESTIONS
----------------------------------------
  bootstrap : resample trades WITH replacement. Treats the observed trades as a
              sample from a wider population and asks what other draws from that
              population would look like.
  shuffle   : reorder the SAME trades without replacement. Holds the edge
              exactly fixed and isolates pure sequence risk. Final equity barely
              moves; drawdown moves a lot. That gap is the point.

WHAT THIS CANNOT DO -- READ THIS BEFORE TRUSTING A NUMBER
---------------------------------------------------------
1. It cannot invent a trade worse than the worst one observed. Resampling draws
   only from history, so if the sample never contained a catastrophic loss, the
   simulated tail will not contain one either. This UNDERSTATES tail risk, and
   understates it most when the sample is short.

2. It assumes trades are independent. They are not. Trend strategies lose
   across correlated instruments simultaneously -- your own correlation panel
   shows NQ, NKD and SOXX around 0.91 -- so real losing streaks cluster harder
   than an independent resample can produce.

3. With few trades it is resampling noise. Eighty-two trades bootstrapped ten
   thousand times is still eighty-two trades of information; the tight-looking
   percentile bands are a property of the resampling, not of the market.

So treat the output as a floor on how bad things get, never a ceiling.
"""

import numpy as np


DEFAULT_RUNS = 10000


def _equity_path(returns, starting=100000.0):
    """Compound a sequence of per-trade returns into an equity curve."""
    return starting * np.cumprod(1.0 + returns)


def _max_drawdown(path):
    peak = np.maximum.accumulate(path)
    return float((path / peak - 1.0).min())


def to_account_returns(trade_returns, stop_pct, risk_pct):
    """
    Convert raw per-trade returns into what they do to the ACCOUNT.

    This conversion is not cosmetic -- without it the simulation is meaningless.
    backtest_intraday_strategy runs every symbol as its own independent account
    starting at the full initial capital. Pooling those trades and compounding
    them one after another pretends that positions which overlapped in time
    happened sequentially, each staking the entire account. On this strategy
    that produced a median final equity of ~14 billion from 100k.

    The fix is to express each trade in R -- multiples of the risk taken:

        R              = trade_return / stop_pct
        account_return = R * risk_pct

    A +53.8% trade on an 8% stop is +6.7R; risking 1% of the account per trade,
    that moves the account +6.7%, not +53.8%. This is also exactly how
    position_sizing.calculate_risk_based_size actually sizes positions, so the
    simulation now matches what the live system would do.
    """
    r = np.asarray([x for x in trade_returns if np.isfinite(x)], dtype=float)
    if stop_pct <= 0:
        raise ValueError("stop_pct must be positive to express trades in R")
    return (r / stop_pct) * risk_pct


def simulate(trade_returns, runs=DEFAULT_RUNS, mode="bootstrap",
             starting=100000.0, trades_per_run=None, ruin_threshold=0.5,
             seed=None, stop_pct=None, risk_pct=0.01):
    """
    trade_returns : per-trade fractional returns (0.05 = +5%)
    stop_pct      : if given, returns are converted to account impact via
                    R-multiples (see to_account_returns). Pass this whenever
                    the trades come from a multi-instrument backtest, which is
                    almost always -- omitting it compounds concurrent trades as
                    if they were sequential.
    risk_pct      : fraction of the account risked per trade
    mode          : 'bootstrap' (with replacement) or 'shuffle' (reorder only)
    trades_per_run: length of each simulated run; defaults to the sample size
    ruin_threshold: fraction of starting equity below which a run counts as ruin
    """
    if stop_pct is not None:
        r = to_account_returns(trade_returns, stop_pct, risk_pct)
    else:
        r = np.asarray([x for x in trade_returns if np.isfinite(x)], dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return None

    if mode not in ("bootstrap", "shuffle"):
        raise ValueError("mode must be 'bootstrap' or 'shuffle'")

    n = trades_per_run or r.size
    rng = np.random.default_rng(seed)

    finals = np.empty(runs)
    drawdowns = np.empty(runs)

    for i in range(runs):
        if mode == "bootstrap":
            draw = rng.choice(r, size=n, replace=True)
        else:
            # Shuffling cannot change the product of (1+r), so final equity is
            # identical across runs by construction. Only the path differs --
            # which is exactly what isolates sequence risk in the drawdown.
            draw = rng.permutation(r)
        path = _equity_path(draw, starting)
        finals[i] = path[-1]
        drawdowns[i] = _max_drawdown(np.concatenate(([starting], path)))

    pct = lambda a, q: float(np.percentile(a, q))

    return {
        "mode": mode,
        "runs": runs,
        "trades_per_run": int(n),
        "sample_trades": int(r.size),
        "starting": starting,

        "final_p5": pct(finals, 5),
        "final_p25": pct(finals, 25),
        "final_median": pct(finals, 50),
        "final_p75": pct(finals, 75),
        "final_p95": pct(finals, 95),
        "final_mean": float(finals.mean()),

        "dd_median": pct(drawdowns, 50),
        "dd_p95": pct(drawdowns, 5),      # 5th pct of a negative series = worst
        "dd_worst": float(drawdowns.min()),

        "prob_loss": float((finals < starting).mean()),
        "prob_ruin": float((finals < starting * ruin_threshold).mean()),
        "ruin_threshold": ruin_threshold,

        "worst_observed_trade": float(r.min()),
        "best_observed_trade": float(r.max()),
    }


def report(stats, label=""):
    if not stats:
        print("  Not enough trades to simulate.")
        return

    s, start = stats, stats["starting"]
    pc = lambda v: (v / start - 1.0) * 100.0

    head = f"Monte Carlo — {s['mode']}"
    if label:
        head += f" — {label}"
    print("\n" + head)
    print("-" * 74)
    print(f"  {s['runs']:,} runs of {s['trades_per_run']} trades, "
          f"resampled from {s['sample_trades']} observed")
    print()
    print("  Final equity")
    for name, key in [("5th pct ", "final_p5"), ("25th pct", "final_p25"),
                      ("median  ", "final_median"), ("75th pct", "final_p75"),
                      ("95th pct", "final_p95")]:
        print(f"    {name}: {s[key]:>14,.0f}   ({pc(s[key]):+8.1f}%)")
    print()
    print("  Maximum drawdown")
    print(f"    median   : {s['dd_median']*100:>8.1f}%")
    print(f"    95th pct : {s['dd_p95']*100:>8.1f}%   (only 1 run in 20 was worse)")
    print(f"    worst    : {s['dd_worst']*100:>8.1f}%")
    print()
    print(f"  P(ends below starting) : {s['prob_loss']*100:>6.1f}%")
    print(f"  P(ends below {s['ruin_threshold']:.0%})     : {s['prob_ruin']*100:>6.1f}%")
    print()
    print(f"  Worst single trade in the sample: {s['worst_observed_trade']*100:+.1f}%")
    print("  No simulated run can contain anything worse -- resampling draws only")
    print("  from observed history, so this tail is a floor, not a ceiling.")


def compare_modes(trade_returns, runs=DEFAULT_RUNS, starting=100000.0, seed=7,
                  stop_pct=None, risk_pct=0.01):
    """
    Run both modes and show what sequence risk alone contributes.

    Shuffle holds the trade set fixed, so any spread in drawdown between the
    two is sequence risk; the additional spread in bootstrap is sampling risk.
    """
    boot = simulate(trade_returns, runs=runs, mode="bootstrap",
                    starting=starting, seed=seed,
                    stop_pct=stop_pct, risk_pct=risk_pct)
    shuf = simulate(trade_returns, runs=runs, mode="shuffle",
                    starting=starting, seed=seed,
                    stop_pct=stop_pct, risk_pct=risk_pct)
    if stop_pct is not None:
        print(f"\nTrades expressed in R (stop {stop_pct:.0%}), "
              f"risking {risk_pct:.1%} of the account each.")
    report(shuf, "same trades, reordered")
    report(boot, "resampled with replacement")

    if boot and shuf:
        print("\n" + "=" * 74)
        print("  Sequence risk alone (shuffle): median DD "
              f"{shuf['dd_median']*100:.1f}%, worst {shuf['dd_worst']*100:.1f}%")
        print("  Adding sampling risk (bootstrap): median DD "
              f"{boot['dd_median']*100:.1f}%, worst {boot['dd_worst']*100:.1f}%")
        print(f"  P(losing money) rises from {shuf['prob_loss']*100:.1f}% "
              f"to {boot['prob_loss']*100:.1f}%.")
    return boot, shuf


def from_backtest(results, runs=DEFAULT_RUNS, starting=100000.0, seed=7,
                  stop_pct=None, risk_pct=0.01):
    """
    Pool per-trade returns across every symbol in a backtest result frame and
    simulate the combined book.

    Pooling assumes the trades could have arrived in any order, which overstates
    diversification: positions opened on correlated instruments in the same week
    are one bet, not several. See the caveats at the top of this module.
    """
    pooled = []
    for _, row in results.iterrows():
        got = row.get("trade_returns")
        if isinstance(got, (list, tuple, np.ndarray)):
            pooled.extend(list(got))
    if len(pooled) < 2:
        return None, None
    return compare_modes(pooled, runs=runs, starting=starting, seed=seed,
                         stop_pct=stop_pct, risk_pct=risk_pct)


def selftest():
    """
    Validates the engine against cases with known answers.
    """
    print("Self-test")
    print("-" * 74)
    rng = np.random.default_rng(1)

    # 1. Shuffling cannot change final equity -- the product is commutative.
    r = rng.normal(0.01, 0.05, 40)
    s = simulate(r, runs=500, mode="shuffle", seed=3)
    spread = abs(s["final_p95"] - s["final_p5"]) / s["final_median"]
    print(f"  shuffle: final-equity spread across runs = {spread:.2e}")
    assert spread < 1e-9, "shuffle changed final equity; compounding is order-dependent?"
    print("    PASS: reordering leaves final equity identical, as it must.")

    # 2. ...but it does change the drawdown.
    print(f"    drawdown still varies: median {s['dd_median']*100:.1f}%, "
          f"worst {s['dd_worst']*100:.1f}%  <- this is sequence risk")
    assert s["dd_worst"] < s["dd_median"], "shuffle produced no drawdown variation"

    # 3. A strictly positive edge must (almost) never lose over many trades.
    pos = simulate(np.full(60, 0.02), runs=300, seed=5)
    print(f"  all-winners sample: P(loss) = {pos['prob_loss']*100:.1f}% (expect 0.0)")
    assert pos["prob_loss"] == 0.0

    # 4. A negative-expectancy sample must usually lose.
    neg = simulate(rng.normal(-0.01, 0.02, 100), runs=300, seed=5)
    print(f"  negative-edge sample: P(loss) = {neg['prob_loss']*100:.1f}% (expect high)")
    assert neg["prob_loss"] > 0.8

    print("\n  All checks passed.")
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Monte Carlo on the strategy's per-trade returns.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--selftest", action="store_true",
                     help="validate the engine, no market data needed")
    src.add_argument("--tws", action="store_true",
                     help="pull 1Y daily bars from TWS and simulate the book")
    src.add_argument("--capital", nargs="*", metavar="PAIR",
                     help="pull daily bars from Capital.com instead")
    ap.add_argument("--stop", type=float, default=0.08,
                    help="stop width (default 0.08, the sweep's sweet spot)")
    ap.add_argument("--rr", type=float, default=None,
                    help="take-profit as a multiple of the stop (default: none)")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--capital-base", type=float, default=100000.0)
    ap.add_argument("--risk", type=float, default=0.01,
                    help="fraction of account risked per trade (default 0.01)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    import rr_sweep
    from backtest import backtest_intraday_strategy

    if args.capital is not None:
        import capital_prices
        pairs = args.capital or ["EURUSD", "USDJPY", "EURJPY", "GBPJPY", "GBPUSD"]
        data = capital_prices.fetch(pairs, resolution="DAY", bars=800)
    else:
        data = rr_sweep.load_from_tws()

    if not data:
        print("No price data. TWS must be running and logged in on port 7497, "
              "or use --capital with capital_config.py present.")
        return 1

    res = backtest_intraday_strategy(data, stop_loss_pct=args.stop,
                                     risk_reward_ratio=args.rr,
                                     periods_per_year=252)
    if res.empty:
        print("Backtest produced no results.")
        return 1

    total = sum(len(r) for r in res["trade_returns"])
    print(f"\nPooled {total} closed trades across {len(res)} instruments "
          f"(stop {args.stop:.0%}, "
          f"take-profit {'none' if args.rr is None else args.rr})")
    if total < 30:
        print("  WARNING: fewer than 30 trades. The percentile bands below will "
              "look precise and mean very little.")
    from_backtest(res, runs=args.runs, starting=args.capital_base,
                  stop_pct=args.stop, risk_pct=args.risk)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
