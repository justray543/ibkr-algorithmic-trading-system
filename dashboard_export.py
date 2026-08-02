"""
dashboard_export.py

Assembles state.json for the dashboard.

Every number is computed here in Python using metrics.py, the same module
portfolio_backtest.py should import. Nothing is recomputed in JavaScript.
The frontend only formats what it is given.

Run at the end of live_trade.py, after the instrument loop, before disconnect.
"""

import json
import os
from datetime import datetime, date

import pandas as pd

import metrics
import trade_ledger as ledger

STATE_JSON = "docs/state.json"      # docs/ is the GitHub Pages source dir
EQUITY_FILE = "equity_history.jsonl"

# Contract multipliers. Wrong values here silently corrupt futures P&L.
MULTIPLIERS = {
    "NQ": 20,
    "DAX": 25,
    "NKD": 5,
    "HSI": 50,
    "SOXX": 1,
}

# Expiries, mirrored from build_instruments(). Drives the roll warnings.
EXPIRIES = {
    "NQ": "202609",
    "DAX": "202609",
    "NKD": "20260910",
    "HSI": "20260828",
    "SOXX": None,
}

VENUES = {
    "NQ": "CME", "DAX": "EUREX", "NKD": "CME",
    "SOXX": "NASDAQ", "HSI": "HKFE",
}


# ----------------------------------------------------------------------
# equity history
# ----------------------------------------------------------------------

def append_equity_point(nav, cash=None, path=EQUITY_FILE, on_date=None):
    """
    One NAV snapshot per run. Appended, never rewritten, so the equity
    curve is built from observations rather than reconstructed after the
    fact. Reconstruction is where the mark-to-market bug crept in last time.

    on_date lets you write a historical point ('YYYY-MM-DD'). Live runs omit
    it and get today. Without this, backfilling an equity curve from an
    existing log is impossible and every point lands on the current date.
    """
    stamp = on_date or datetime.now().strftime("%Y-%m-%d")

    point = {
        "date": stamp,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "nav": round(float(nav), 2),
    }
    if cash is not None:
        point["cash"] = round(float(cash), 2)

    existing = read_equity(path)

    # One point per calendar day, holding the LATEST nav for that day. A
    # daily trading run writes it once; an intraday dashboard refresh
    # overwrites the same day's point with a fresher nav rather than either
    # skipping (which would freeze the curve at the first reading of the
    # day) or appending (which would put 30 points on one date). The file
    # is rewritten only when today's point already exists, so the common
    # append path stays append-only.
    if existing and existing[-1]["date"] == stamp:
        existing[-1] = point
        f = open(path, "w")
        for pt in existing:
            f.write(json.dumps(pt) + "\n")
        f.close()
    else:
        f = open(path, "a")
        f.write(json.dumps(point) + "\n")
        f.close()


def read_equity(path=EQUITY_FILE):
    if not os.path.exists(path):
        return []
    out = []
    f = open(path, "r")
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    f.close()
    return out


def equity_series(path=EQUITY_FILE):
    points = read_equity(path)
    if not points:
        return pd.Series(dtype=float)
    s = pd.Series(
        [p["nav"] for p in points],
        index=pd.to_datetime([p["date"] for p in points]),
    )
    return s[~s.index.duplicated(keep="last")].sort_index()


# ----------------------------------------------------------------------
# benchmark
# ----------------------------------------------------------------------

def build_benchmark(price_history, starting_capital):
    """
    Equal-weight buy-and-hold across the same instruments.

    This is the honest comparison: it isolates what the timing rules
    contribute, separately from what the instrument selection contributes.
    Benchmarking against SPX would conflate the two and flatter the
    strategy whenever the basket happens to outperform US large caps.
    """
    if not price_history:
        return pd.Series(dtype=float)

    frame = pd.DataFrame(price_history).dropna(how="all")
    if frame.empty:
        return pd.Series(dtype=float)

    frame = frame.ffill().dropna(how="all")
    normalised = frame.div(frame.iloc[0])
    basket = normalised.mean(axis=1)
    return basket * starting_capital


# ----------------------------------------------------------------------
# main export
# ----------------------------------------------------------------------

def export(nav, price_history, live_states, signal_snapshot,
           starting_capital=None, out_path=STATE_JSON):
    """
    nav              float, current account net liquidation value
    price_history    dict label -> pd.Series of daily closes
    live_states      dict label -> {position, entry_price, stop_price}
    signal_snapshot  dict label -> {price, ema9, ema21, rsi}
    """

    inception = ledger.load_inception()
    if inception is None:
        inception = ledger.ensure_inception(starting_capital or nav)
    start_cap = inception["starting_capital"]

    append_equity_point(nav)
    equity = equity_series()

    trades, open_map = ledger.build_closed_trades(MULTIPLIERS)
    stats = metrics.trade_stats(trades)

    # ---- headline performance ----
    #
    # Return is a point-in-time ratio of NAV to starting capital, so it needs
    # no history at all. Previously it was computed only when the equity
    # series had two or more points, which meant a freshly started system
    # reported +0.00% while the account was visibly up or down. Sharpe,
    # Sortino, volatility, CAGR and drawdown genuinely require a return
    # series, so those stay None until there is one and the dashboard shows
    # a dash rather than a misleading zero.
    current_nav = float(equity.iloc[-1]) if len(equity) else float(nav)

    perf = {
        "nav": round(current_nav, 2),
        "return_pct": round((current_nav / start_cap - 1) * 100, 2) if start_cap else 0.0,
        "pnl_abs": round(current_nav - start_cap, 2),
        "history_points": int(len(equity)),
    }

    # Annualised figures on a handful of days are arithmetically valid and
    # completely meaningless: three rising days produces a Sharpe above 100
    # and a CAGR in the thousands of percent. Publishing that on a public
    # dashboard would be worse than publishing nothing, so hold them back
    # until the sample is large enough to be worth reading.
    MIN_POINTS_RISK = 20     # ~1 month of weekdays for Sharpe/Sortino/vol
    MIN_POINTS_CAGR = 60     # ~3 months before annualising a return

    if len(equity) >= 2:
        perf["max_drawdown_pct"] = round(metrics.max_drawdown(equity) * 100, 2)
    else:
        perf["max_drawdown_pct"] = None

    if len(equity) >= MIN_POINTS_RISK:
        perf["sharpe"] = round(metrics.sharpe(equity), 2)
        perf["sortino"] = round(metrics.sortino(equity), 2)
        perf["volatility_pct"] = round(metrics.volatility(equity) * 100, 2)
    else:
        perf["sharpe"] = None
        perf["sortino"] = None
        perf["volatility_pct"] = None

    if len(equity) >= MIN_POINTS_CAGR:
        perf["cagr_pct"] = round(metrics.cagr(equity) * 100, 2)
    else:
        perf["cagr_pct"] = None

    perf["min_points_risk"] = MIN_POINTS_RISK


    # ---- equity + underwater curves ----
    if len(equity) >= 2:
        bench = build_benchmark(price_history, start_cap)
        bench = bench.reindex(equity.index).ffill()
        dd = metrics.drawdown_series(equity)
        equity_block = {
            "dates": [d.strftime("%Y-%m-%d") for d in equity.index],
            "strategy": [round(float(v), 2) for v in equity.values],
            "benchmark": [round(float(v), 2) if pd.notna(v) else None for v in bench.values],
            "drawdown": [round(float(v) * 100, 3) for v in dd.values],
        }
        dd_detail = metrics.drawdown_detail(equity)
        roll_sharpe = metrics.rolling_sharpe(equity, window=60)
        rolling_block = {
            "sharpe": {
                "dates": [d.strftime("%Y-%m-%d") for d in roll_sharpe.index],
                "values": [round(float(v), 3) for v in roll_sharpe.values],
                "window": 60,
            },
            "win_rate": metrics.rolling_win_rate(trades, window=20),
        }
    else:
        equity_block = {"dates": [], "strategy": [], "benchmark": [], "drawdown": []}
        dd_detail = None
        rolling_block = {"sharpe": {"dates": [], "values": [], "window": 60}, "win_rate": []}

    # ---- trade markers for the equity chart ----
    markers = []
    for t in trades:
        markers.append({"date": t["entry_date"], "type": "entry",
                        "label": t["label"], "price": t["entry_price"]})
        markers.append({"date": t["exit_date"], "type": "exit",
                        "label": t["label"], "price": t["exit_price"],
                        "pnl": t["pnl"], "reason": t["reason"]})
    for label, entry in open_map.items():
        markers.append({"date": entry["date"], "type": "entry",
                        "label": label, "price": entry["price"], "open": True})
    markers.sort(key=lambda m: m["date"])

    # ---- open positions ----
    positions = []
    for label, st in live_states.items():
        if st.get("position", 0) != 1:
            continue
        snap = signal_snapshot.get(label, {})
        current = snap.get("price", st.get("entry_price", 0))
        entry_px = st.get("entry_price", 0)
        stop_px = st.get("stop_price", 0)
        mult = MULTIPLIERS.get(label, 1)

        led = open_map.get(label, {})
        qty = led.get("quantity", 1)
        entry_date = led.get("date")

        if entry_date:
            held = (date.today() - datetime.strptime(entry_date, "%Y-%m-%d").date()).days
        else:
            held = None

        # how far price has travelled from entry toward the stop, 0-100
        if entry_px and stop_px and entry_px > stop_px:
            span = entry_px - stop_px
            travelled = max(0.0, min(1.0, (entry_px - current) / span))
            stop_pct = round(travelled * 100, 1)
        else:
            stop_pct = 0.0

        dte = metrics.days_to_expiry(EXPIRIES.get(label))

        positions.append({
            "label": label,
            "venue": VENUES.get(label, ""),
            "quantity": qty,
            "entry_price": round(entry_px, 2),
            "current_price": round(float(current), 2),
            "stop_price": round(stop_px, 2),
            "unrealised_pnl": round((current - entry_px) * qty * mult, 2),
            "unrealised_pct": round(((current / entry_px) - 1) * 100, 2) if entry_px else 0.0,
            "entry_date": entry_date,
            "holding_days": held,
            "stop_proximity_pct": stop_pct,
            "days_to_expiry": dte,
            "expiry_status": metrics.expiry_status(dte),
        })

    # ---- signal state ----
    signals = []
    for label, snap in signal_snapshot.items():
        st = live_states.get(label, {})
        price = snap.get("price", 0)
        ema9 = snap.get("ema9", 0)
        ema21 = snap.get("ema21", 0)
        rsi = snap.get("rsi", 0)

        spread_pct = ((ema9 - ema21) / price * 100) if price else 0.0
        bullish = ema9 > ema21

        if st.get("position", 0) == 1:
            state = "long"
        elif bullish and rsi >= 70:
            state = "blocked"
        elif bullish and 50 < rsi < 70:
            state = "armed"
        else:
            state = "flat"

        dte = metrics.days_to_expiry(EXPIRIES.get(label))

        signals.append({
            "label": label,
            "price": round(float(price), 2),
            "ema9": round(float(ema9), 2),
            "ema21": round(float(ema21), 2),
            "rsi": round(float(rsi), 2),
            "spread_pct": round(spread_pct, 3),
            "state": state,
            "days_to_expiry": dte,
            "expiry_status": metrics.expiry_status(dte),
            "inception_date": ledger.instrument_inception(label),
        })

    # ---- attribution ----
    by_label = {}
    for t in trades:
        by_label.setdefault(t["label"], {"pnl": 0.0, "trades": 0, "wins": 0})
        by_label[t["label"]]["pnl"] += t["pnl"]
        by_label[t["label"]]["trades"] += 1
        if t["pnl"] > 0:
            by_label[t["label"]]["wins"] += 1

    attribution = []
    for label, v in by_label.items():
        attribution.append({
            "label": label,
            "pnl": round(v["pnl"], 2),
            "trades": v["trades"],
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0.0,
        })
    attribution.sort(key=lambda a: a["pnl"], reverse=True)

    # ---- correlation ----
    if price_history and len(price_history) >= 2:
        frame = pd.DataFrame(price_history).ffill().dropna(how="all")
        corr = metrics.correlation_matrix(frame, window=120)
        corr["effective_bets"] = metrics.effective_bets(frame, window=120)
        corr["avg_pairwise"] = metrics.avg_pairwise_correlation(frame, window=120)
        corr["instrument_count"] = frame.shape[1]
        corr["window_days"] = 120
    else:
        corr = {"labels": [], "matrix": [], "effective_bets": 0,
                "avg_pairwise": 0, "instrument_count": 0, "window_days": 120}

    # ---- assemble ----
    state = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": inception.get("mode", "paper"),
        "currency": inception.get("currency", "EUR"),
        "inception": {
            "date": inception["inception_date"],
            "starting_capital": start_cap,
            "days_live": (date.today() - datetime.strptime(
                inception["inception_date"], "%Y-%m-%d").date()).days,
        },
        "performance": perf,
        "equity": equity_block,
        "drawdown_detail": dd_detail,
        "rolling": rolling_block,
        "markers": markers,
        "positions": positions,
        "signals": signals,
        "attribution": attribution,
        "correlation": corr,
        "trade_stats": stats,
        "duration_histogram": metrics.duration_histogram(trades),
        "suppression": ledger.suppression_stats(),
        "closed_trades": list(reversed(trades))[:50],
        "health": ledger.recent_health(limit=25),
        "last_run": ledger.last_run(),
    }

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    f = open(out_path, "w")
    json.dump(state, f, indent=2, default=str)
    f.close()

    print("Dashboard state written to " + out_path)
    return state
