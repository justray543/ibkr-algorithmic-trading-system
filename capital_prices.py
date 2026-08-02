"""
capital_prices.py

Historical price history from Capital.com, shaped for the backtest.

WHY THIS EXISTS SEPARATELY FROM capital_crypto_trade.py
-------------------------------------------------------
That module is a live trading daemon: importing it starts nothing, but it does
`import capital_config as cfg` at module scope, so it cannot even be imported
without credentials present. This module reads config lazily and is read-only
by construction -- it has no order functions at all, so a backtest can never
place a trade through it.

WHAT THE DATA IS
----------------
Capital.com is a CFD broker. These are THEIR prices for THEIR contracts, mid of
their own bid/ask, carrying their spread markup. That is not the interbank FX
market. For asking "does EMA9/21 + RSI produce signal on EURUSD" it is fine;
for anything sensitive to exact levels it is a different instrument.

WHAT THE DATA OMITS
-------------------
Overnight financing. A CFD position pays or receives a daily swap, and on JPY
crosses in particular the carry can exceed whatever edge a trend rule shows.
The backtest models a flat per-side transaction cost and nothing else, so any
result from multi-day holds here is optimistic by an amount this code does not
know. estimate_financing_drag() below makes that explicit rather than leaving
it as a footnote.
"""

import time
from datetime import datetime, timedelta

import pandas as pd
import requests

# Capital.com caps a single /prices call. Daily bars over a few years fit
# comfortably; finer resolutions need the windowing below.
MAX_BARS_PER_REQUEST = 1000

RESOLUTIONS = ["MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30",
               "HOUR", "HOUR_4", "DAY", "WEEK"]

# Rough daily financing cost per side, as a fraction of notional, for a
# retail FX CFD. Deliberately an order-of-magnitude placeholder, not a quote:
# real swap rates are per-instrument, per-direction, and change daily. Used
# only to show how large the unmodelled cost is relative to a measured edge.
TYPICAL_DAILY_FINANCING = 0.0002   # ~2bp/day ~= 7%/yr


def _config():
    """Load capital_config lazily, with an actionable error if absent."""
    try:
        import capital_config as cfg
    except ImportError:
        raise SystemExit(
            "capital_config.py not found.\n"
            "  1. Register a Capital.com demo account\n"
            "  2. Generate an API key in Settings > API integrations\n"
            "  3. cp capital_config.example.py capital_config.py and fill in\n"
            "     CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_PASSWORD\n"
            "  Keep USE_DEMO = True."
        )
    for name in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_PASSWORD"):
        value = getattr(cfg, name, "")
        if not value or str(value).startswith("PUT_YOUR"):
            raise SystemExit(f"capital_config.py: {name} is still the placeholder value.")
    return cfg


class ReadOnlySession:
    """
    Authenticated Capital.com session with no order methods.

    Deliberately not reusing CapitalSession from the daemon: this class cannot
    place, amend or close a position because those methods do not exist on it.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or _config()
        self.base = self.cfg.BASE_URL
        self.cst = None
        self.token = None

    def login(self):
        r = requests.post(
            self.base + "/api/v1/session",
            headers={"X-CAP-API-KEY": self.cfg.CAPITAL_API_KEY,
                     "Content-Type": "application/json"},
            json={"identifier": self.cfg.CAPITAL_IDENTIFIER,
                  "password": self.cfg.CAPITAL_PASSWORD},
            timeout=20,
        )
        if r.status_code != 200:
            raise SystemExit(f"Capital.com login failed: {r.status_code} {r.text}")
        self.cst = r.headers.get("CST")
        self.token = r.headers.get("X-SECURITY-TOKEN")
        if not self.cst or not self.token:
            raise SystemExit("Capital.com login returned no session tokens.")
        return self

    def get(self, path, params=None):
        return requests.get(
            self.base + path,
            headers={"X-CAP-API-KEY": self.cfg.CAPITAL_API_KEY,
                     "CST": self.cst, "X-SECURITY-TOKEN": self.token},
            params=params or {}, timeout=25,
        )


def find_epic(session, search_term):
    """
    Resolve a search term to a tradable epic.

    Epics differ by jurisdiction and account, so they are looked up rather than
    assumed -- capital_config.example.py's own comment flags its values as
    unverified. Returns (epic, instrumentName) or (None, None).
    """
    r = session.get("/api/v1/markets", params={"searchTerm": search_term})
    if r.status_code != 200:
        return None, None
    markets = r.json().get("markets", [])
    exact = [m for m in markets if m.get("epic", "").upper() == search_term.upper()]
    chosen = (exact or markets or [None])[0]
    if not chosen:
        return None, None
    return chosen.get("epic"), chosen.get("instrumentName")


def _mid(point, field):
    node = point.get(field) or {}
    bid, ask = node.get("bid"), node.get("ask")
    if bid is None and ask is None:
        return None
    if bid is None:
        return ask
    if ask is None:
        return bid
    return (bid + ask) / 2.0


def fetch_one(session, epic, resolution="DAY", bars=800):
    """
    OHLC history for one epic as a DataFrame indexed by UTC time.

    Returns columns high/low/close (mid of bid and ask). No volume column:
    Capital.com does not publish traded volume for CFDs, so the volume-pressure
    filter simply will not engage on this data -- which is correct, rather than
    it computing something from a sentinel.
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {RESOLUTIONS}")

    collected = []
    remaining = bars
    end = datetime.utcnow()

    # Window backwards so histories longer than one request still assemble.
    while remaining > 0:
        chunk = min(remaining, MAX_BARS_PER_REQUEST)
        params = {"resolution": resolution, "max": chunk,
                  "to": end.strftime("%Y-%m-%dT%H:%M:%S")}
        r = session.get("/api/v1/prices/" + epic, params=params)
        if r.status_code != 200:
            if not collected:
                print(f"  {epic}: price fetch failed {r.status_code} {r.text[:120]}")
            break

        prices = r.json().get("prices", [])
        if not prices:
            break

        collected = prices + collected
        remaining -= len(prices)

        first = prices[0].get("snapshotTimeUTC") or prices[0].get("snapshotTime")
        try:
            end = pd.Timestamp(first).to_pydatetime().replace(tzinfo=None) - timedelta(seconds=1)
        except Exception:
            break
        if len(prices) < chunk:
            break
        time.sleep(0.4)   # be polite to the API

    if not collected:
        return pd.DataFrame()

    rows = []
    for p in collected:
        rows.append({
            "time": p.get("snapshotTimeUTC") or p.get("snapshotTime"),
            "high": _mid(p, "highPrice"),
            "low": _mid(p, "lowPrice"),
            "close": _mid(p, "closePrice"),
        })

    df = pd.DataFrame(rows).dropna()
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"])
    df = df.drop_duplicates(subset=["time"]).set_index("time").sort_index()
    return df


def fetch(search_terms, resolution="DAY", bars=800, verbose=True):
    """
    Price history for several instruments, keyed by the term you asked for.

    Shaped exactly as backtest_intraday_strategy expects: dict of
    label -> DataFrame with a 'close' column.
    """
    session = ReadOnlySession().login()
    out = {}
    for term in search_terms:
        epic, name = find_epic(session, term)
        if not epic:
            if verbose:
                print(f"  {term}: no matching epic found")
            continue
        df = fetch_one(session, epic, resolution=resolution, bars=bars)
        if df.empty or len(df) < 100:
            if verbose:
                print(f"  {term} ({epic}): only {len(df)} bars, skipped")
            continue
        out[term] = df
        if verbose:
            print(f"  {term} -> epic {epic} ({name}): {len(df)} bars, "
                  f"{df.index[0].date()} to {df.index[-1].date()}")
        time.sleep(0.3)
    return out


def estimate_financing_drag(avg_holding_days, daily_rate=TYPICAL_DAILY_FINANCING):
    """
    Financing cost of an average trade, as a percentage of notional.

    Compare this against the measured expectancy per trade. If the drag is the
    same order of magnitude as the edge, the backtest has not demonstrated
    anything -- it has measured a gross number against a net cost it omitted.
    """
    return avg_holding_days * daily_rate * 100.0
