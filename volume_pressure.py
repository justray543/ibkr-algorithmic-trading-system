"""
volume_pressure.py

A daily-bar approximation of cumulative volume delta.

WHAT THIS IS NOT
----------------
This is not CVD. Real CVD classifies every individual trade by whether it hit
the bid or lifted the offer, which requires tick data with the prevailing quote
attached. A daily bar contains no aggressor information at all, at any IBKR
whatToShow setting, so that number cannot be recovered here and nothing in this
module pretends otherwise.

WHAT IT IS
----------
The standard bar-delta approximation: use where the close sits inside the bar's
range as a stand-in for which side was leaning on the market.

    position = (close - low) / (high - low)      in [0, 1]
    delta    = volume * (2 * position - 1)       in [-volume, +volume]

Close on the high  -> the whole bar's volume counts as buying pressure.
Close on the low   -> the whole bar's volume counts as selling pressure.
Close mid-range    -> nets to roughly zero.

Cumulating that gives a series shaped like CVD, and it does capture the one
genuinely useful thing CVD shows: pressure diverging from price. If price makes
a higher high while cumulative delta does not, buyers spent volume without
gaining ground -- day-scale absorption.

WHAT IT CANNOT SEE
------------------
Intraday absorption, which is the case CVD is actually famous for. A bar that
opens low, is bought aggressively all session against a large passive seller,
and closes mid-range reads as "neutral" here, while true tick CVD would show a
large positive delta diverging from flat price. One number per day cannot
represent a within-day process. Treat agreement with real CVD as coincidental.

Volume is only present when bars are requested with whatToShow="TRADES".
MIDPOINT bars return volume = -1, and every function here returns None on that
input rather than computing a confident number from a sentinel.
"""

import numpy as np
import pandas as pd

# Bars whose volume is <= 0 are missing data, not zero-volume sessions. IBKR
# uses -1 on MIDPOINT bars.
MIN_VALID_VOLUME = 0


def has_volume(df):
    """True when this frame carries usable traded volume."""
    if df is None or "volume" not in df.columns or df.empty:
        return False
    v = pd.to_numeric(df["volume"], errors="coerce")
    return bool((v > MIN_VALID_VOLUME).any())


def bar_delta(df):
    """
    Per-bar signed volume. Returns a Series aligned to df, or None when the
    frame has no usable volume.

    Zero-range bars (high == low, e.g. a limit-locked or untraded session)
    have no position information, so they contribute 0 rather than dividing
    by zero.
    """
    if not has_volume(df):
        return None

    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")

    rng = high - low
    position = pd.Series(np.where(rng > 0, (close - low) / rng.replace(0, np.nan), 0.5),
                         index=df.index)
    position = position.fillna(0.5)

    delta = volume.where(volume > MIN_VALID_VOLUME, 0.0) * (2.0 * position - 1.0)
    return delta.fillna(0.0)


def cvd_proxy(df):
    """Cumulative bar delta. None when the frame has no usable volume."""
    d = bar_delta(df)
    if d is None:
        return None
    return d.cumsum()


def pressure_rising(df, lookback=5):
    """
    Boolean Series: is cumulative delta higher than it was `lookback` bars ago?

    This is the entry-confirmation form. It asks whether buying pressure has
    been accumulating over the recent window, not merely whether today closed
    strong -- a single strong close is already most of what the EMA/RSI rule
    reacts to, so confirming on it would add almost nothing independent.
    """
    c = cvd_proxy(df)
    if c is None:
        return None
    return c > c.shift(lookback)


def divergence(df, lookback=20):
    """
    Price at a `lookback` high while cumulative delta is not: effort without
    result. Returns a boolean Series, or None without volume.

    This is the part of the CVD idea with actual information in it. It is
    exposed as a diagnostic rather than wired into entries, because on daily
    bars the sample of such events is small and unvalidated.
    """
    c = cvd_proxy(df)
    if c is None:
        return None
    close = pd.to_numeric(df["close"], errors="coerce")
    price_high = close >= close.rolling(lookback).max()
    delta_high = c >= c.rolling(lookback).max()
    return price_high & (~delta_high)


def summarise(df, lookback=5):
    """One-shot snapshot for logging and the dashboard."""
    if not has_volume(df):
        return {"available": False,
                "reason": "no traded volume in these bars (MIDPOINT returns -1)"}

    d = bar_delta(df)
    c = cvd_proxy(df)
    rising = pressure_rising(df, lookback)
    div = divergence(df)

    return {
        "available": True,
        "bar_delta": round(float(d.iloc[-1]), 1),
        "cvd_proxy": round(float(c.iloc[-1]), 1),
        "pressure_rising": bool(rising.iloc[-1]) if rising is not None else None,
        "divergence": bool(div.iloc[-1]) if div is not None and not pd.isna(div.iloc[-1]) else False,
        "lookback": lookback,
    }
