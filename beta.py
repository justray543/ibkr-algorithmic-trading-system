"""
beta.py

Rolling beta against a benchmark, and the high-beta short watchlist.

FLAG ONLY. This module never places an order and never imports the order or
client modules, so it cannot. It scores instruments and hands back a list;
acting on that list is a human decision.

Why beta at all: the daily strategy is long-only, so a bearish EMA/RSI setup
on a high-beta name currently produces nothing at all -- the signal is
computed, found to be non-entry, and discarded. Beta is what makes that
discarded half worth looking at: a name that moves 1.8x the index is where a
downtrend is worth knowing about, and it is also the name whose long entries
carry the most index risk.

Pure functions on pandas Series, no IBKR dependency, so this is unit-testable
offline the same way metrics.py is.
"""

import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

WATCHLIST_FILE = "short_watchlist.json"

# Beta above this counts as "high". 1.0 is the benchmark itself; 1.5 means the
# name has historically moved half again as hard as the index in both
# directions. Deliberately not tuned to anything -- it is a starting threshold,
# not a fitted parameter.
HIGH_BETA_THRESHOLD = 1.5

# Below this r-squared the beta is not describing the relationship well enough
# to be worth quoting: the name is moving on its own news, not on the index.
# Flagging on a beta of 2.4 with an r-squared of 0.05 would be noise dressed
# as a number.
MIN_R_SQUARED = 0.20

# Mirror of the long entry band (50 < RSI < 70). A short-side setup wants
# downward momentum that has not already exhausted itself, which is the same
# argument reflected around 50.
RSI_SHORT_MIN = 30
RSI_SHORT_MAX = 50


def _aligned_returns(asset_closes, benchmark_closes):
    """
    Daily simple returns for asset and benchmark on their COMMON dates.

    Aligning on the index rather than by position matters here: the universe
    spans NASDAQ and NYSE listings whose bar counts differ after holidays and
    halts, and pairing row i of one with row i of the other silently compares
    different days. That is the same positional-alignment trap that makes the
    portfolio backtest's correlation figures unreliable.
    """
    a = pd.Series(asset_closes).dropna()
    b = pd.Series(benchmark_closes).dropna()

    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(joined) < 3:
        return None, None

    rets = joined.pct_change().dropna()
    if len(rets) < 2:
        return None, None

    return rets.iloc[:, 0], rets.iloc[:, 1]


def compute_beta(asset_closes, benchmark_closes):
    """
    Ordinary least-squares beta of the asset against the benchmark.

    Returns a dict with beta, r_squared and the sample size, or None when
    there is not enough overlapping history to say anything. Returning None
    rather than a number is deliberate: a beta computed from four bars is
    worse than no beta, because it looks equally authoritative on a dashboard.
    """
    asset_ret, bench_ret = _aligned_returns(asset_closes, benchmark_closes)
    if asset_ret is None:
        return None

    bench_var = float(bench_ret.var(ddof=1))
    if bench_var <= 0 or np.isnan(bench_var):
        return None

    covariance = float(asset_ret.cov(bench_ret))
    beta = covariance / bench_var

    correlation = float(asset_ret.corr(bench_ret))
    if np.isnan(correlation):
        return None

    if np.isnan(beta):
        return None

    return {
        "beta": round(beta, 3),
        "r_squared": round(correlation ** 2, 3),
        "correlation": round(correlation, 3),
        "samples": int(len(asset_ret)),
    }


def is_high_beta(stats, threshold=HIGH_BETA_THRESHOLD, min_r2=MIN_R_SQUARED):
    """High beta, and a beta we actually believe. Both conditions or neither."""
    if not stats:
        return False
    return stats["beta"] >= threshold and stats["r_squared"] >= min_r2


def bearish_setup(ema9, ema21, rsi):
    """
    The short-side mirror of the long entry rule.

    Long is  EMA9 > EMA21 and 50 < RSI < 70.
    Short is EMA9 < EMA21 and 30 < RSI < 50.

    The lower bound matters as much as the upper one. Below 30 the move is
    already extended and flagging it is chasing, which is exactly the reason
    the long side refuses entries above RSI 70.
    """
    if ema9 is None or ema21 is None or rsi is None:
        return False
    if pd.isna(ema9) or pd.isna(ema21) or pd.isna(rsi):
        return False
    return ema9 < ema21 and RSI_SHORT_MIN < rsi < RSI_SHORT_MAX


def evaluate(label, closes, benchmark_closes, ema9, ema21, rsi, price,
             threshold=HIGH_BETA_THRESHOLD, min_r2=MIN_R_SQUARED):
    """
    Score one instrument. Returns a row for the watchlist, always -- including
    when it does not qualify -- so the dashboard can show the whole universe
    ranked by beta rather than only the names that happened to trip today.
    """
    stats = compute_beta(closes, benchmark_closes)
    high = is_high_beta(stats, threshold, min_r2)
    bearish = bearish_setup(ema9, ema21, rsi)

    row = {
        "label": label,
        "price": round(float(price), 2) if price is not None else None,
        "beta": stats["beta"] if stats else None,
        "r_squared": stats["r_squared"] if stats else None,
        "samples": stats["samples"] if stats else None,
        "ema9": round(float(ema9), 4) if ema9 is not None and not pd.isna(ema9) else None,
        "ema21": round(float(ema21), 4) if ema21 is not None and not pd.isna(ema21) else None,
        "rsi": round(float(rsi), 2) if rsi is not None and not pd.isna(rsi) else None,
        "high_beta": high,
        "bearish_setup": bearish,
        "flagged": high and bearish,
    }

    if stats is None:
        row["note"] = "insufficient overlapping history for a beta"
    elif not high and bearish:
        row["note"] = "bearish setup but beta below threshold"
    elif high and not bearish:
        row["note"] = "high beta, no bearish setup today"

    return row


def rank(rows):
    """Flagged names first, then by beta descending. Nulls sort last."""
    return sorted(
        rows,
        key=lambda r: (not r["flagged"], -(r["beta"] if r["beta"] is not None else -99)),
    )


def write_watchlist(rows, benchmark, path=WATCHLIST_FILE,
                    threshold=HIGH_BETA_THRESHOLD, min_r2=MIN_R_SQUARED):
    """
    Persist the scored universe.

    Written atomically via tmp + os.replace, the same pattern
    position_ownership.py uses, so a crash mid-write cannot leave the
    dashboard reading half a JSON document.
    """
    ranked = rank(rows)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "benchmark": benchmark,
        "beta_threshold": threshold,
        "min_r_squared": min_r2,
        "rsi_short_band": [RSI_SHORT_MIN, RSI_SHORT_MAX],
        "note": "Flagging only. No orders are placed from this file.",
        "flagged_count": sum(1 for r in ranked if r["flagged"]),
        "universe_count": len(ranked),
        "rows": ranked,
    }

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)
    return payload


def read_watchlist(path=WATCHLIST_FILE):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def summary_lines(payload):
    """Plain-text lines for the daily email and Telegram summary."""
    if not payload:
        return ["Short watchlist: not generated this run."]

    flagged = [r for r in payload["rows"] if r["flagged"]]
    out = ["Short watchlist (flag only, no orders placed):"]

    if not flagged:
        out.append("  No high-beta name has a bearish setup today.")
    else:
        for r in flagged:
            out.append(
                "  " + r["label"]
                + "  beta " + str(r["beta"])
                + " (r2 " + str(r["r_squared"]) + ")"
                + "  RSI " + str(r["rsi"])
                + "  price " + str(r["price"])
            )

    top = [r for r in payload["rows"] if r["beta"] is not None][:3]
    if top:
        out.append("  Highest beta vs " + str(payload["benchmark"]) + ": "
                   + ", ".join(r["label"] + " " + str(r["beta"]) for r in top))
    return out
