"""
trade_ledger.py

Append-only record of every entry and exit, with dates.

Why this exists: save_state() in live_trade.py stores only position,
entry_price and stop_price. No timestamps. Without an entry date there is
no inception date, no holding period, no closed-trade history and no
duration analysis. The ledger fixes that without disturbing the existing
state files, which keep doing their reconciliation job.

Format is JSON Lines: one JSON object per line, append-only, never rewritten.
Survives partial writes, trivially greppable, and git-diffs cleanly.
"""

import json
import os
from datetime import datetime

LEDGER_FILE = "trade_ledger.jsonl"
INCEPTION_FILE = "inception.json"


# ----------------------------------------------------------------------
# inception
# ----------------------------------------------------------------------

def ensure_inception(starting_capital, currency="EUR", path=INCEPTION_FILE,
                     on_date=None):
    """
    Records the day the system went live and the capital it started with.
    Written once. Every 'since inception' number on the dashboard anchors
    here, so if this file is lost the equity curve loses its origin.
    """
    if os.path.exists(path):
        f = open(path, "r")
        data = json.load(f)
        f.close()
        return data

    data = {
        "inception_date": on_date or datetime.now().strftime("%Y-%m-%d"),
        "inception_timestamp": datetime.now().isoformat(timespec="seconds"),
        "starting_capital": starting_capital,
        "currency": currency,
        "mode": "paper",
    }
    f = open(path, "w")
    json.dump(data, f, indent=2)
    f.close()
    return data


def load_inception(path=INCEPTION_FILE):
    if not os.path.exists(path):
        return None
    f = open(path, "r")
    data = json.load(f)
    f.close()
    return data


def instrument_inception(label, ledger_path=LEDGER_FILE):
    """First date this specific instrument was ever traded."""
    events = read_ledger(ledger_path)
    for e in events:
        if e.get("label") == label:
            return e.get("date")
    return None


# ----------------------------------------------------------------------
# writing
# ----------------------------------------------------------------------

def _append(event, path=LEDGER_FILE):
    f = open(path, "a")
    f.write(json.dumps(event) + "\n")
    f.close()


def record_entry(label, price, quantity, stop_price, order_id=None,
                 rsi=None, ema9=None, ema21=None, path=LEDGER_FILE,
                 on_date=None, strategy=None):
    """
    on_date ('YYYY-MM-DD') backdates the record. Live runs omit it.

    strategy tags which script opened this position. Two strategies can trade
    the same symbol -- SOXX is in both the futures and the equities universe --
    and without the tag build_closed_trades pairs one strategy's entry with the
    other's exit, inventing round trips that never happened.
    """
    now = datetime.now()
    event = {
        "event": "entry",
        "label": label,
        "strategy": strategy,
        "date": on_date or now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(timespec="seconds"),
        "price": round(float(price), 4),
        "quantity": quantity,
        "stop_price": round(float(stop_price), 4),
        "order_id": order_id,
        "rsi": round(float(rsi), 2) if rsi is not None else None,
        "ema9": round(float(ema9), 4) if ema9 is not None else None,
        "ema21": round(float(ema21), 4) if ema21 is not None else None,
    }
    _append(event, path)
    return event


def record_exit(label, price, quantity, reason, order_id=None,
                rsi=None, ema9=None, ema21=None, path=LEDGER_FILE,
                on_date=None, strategy=None):
    """
    reason should be one of: 'stop', 'crossover', 'rsi_exit', 'manual', 'roll'
    Kept as a controlled vocabulary so the exit-reason breakdown on the
    dashboard stays meaningful rather than turning into free text.
    """
    now = datetime.now()
    event = {
        "event": "exit",
        "label": label,
        "strategy": strategy,
        "date": on_date or now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(timespec="seconds"),
        "price": round(float(price), 4),
        "quantity": quantity,
        "reason": reason,
        "order_id": order_id,
        "rsi": round(float(rsi), 2) if rsi is not None else None,
        "ema9": round(float(ema9), 4) if ema9 is not None else None,
        "ema21": round(float(ema21), 4) if ema21 is not None else None,
    }
    _append(event, path)
    return event


def record_suppressed(label, price, rsi, ema9, ema21, reason, path=LEDGER_FILE,
                      on_date=None, strategy=None):
    """
    A crossover fired but the entry was vetoed.

    This is the signal-to-fill audit. Your log already prints
    'signal fired but RSI is overbought. Skipping entry' but never counts
    it. If RSI is vetoing most crossovers, that is a direct argument for
    ablating RSI in the backtest, which you already flagged as worth testing.
    """
    now = datetime.now()
    event = {
        "event": "suppressed",
        "label": label,
        "strategy": strategy,
        "date": on_date or now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(timespec="seconds"),
        "price": round(float(price), 4),
        "rsi": round(float(rsi), 2),
        "ema9": round(float(ema9), 4),
        "ema21": round(float(ema21), 4),
        "reason": reason,
    }
    _append(event, path)
    return event


def record_health(level, label, message, path=LEDGER_FILE):
    """
    level: 'info' | 'warning' | 'error'

    Captures state mismatches, insufficient data, exceptions, client_id
    conflicts. These already appear in multi_trade_log.txt as prose, but
    prose is not queryable. You have hit phantom positions and state
    mismatches before; you want them on the dashboard, not buried at line
    4000 of a text file.
    """
    now = datetime.now()
    event = {
        "event": "health",
        "level": level,
        "label": label,
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(timespec="seconds"),
        "message": message,
    }
    _append(event, path)
    return event


def record_run(status, instruments_checked, errors=0, path=LEDGER_FILE):
    """One record per cron invocation. Drives the freshness indicator."""
    now = datetime.now()
    event = {
        "event": "run",
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(timespec="seconds"),
        "status": status,
        "instruments_checked": instruments_checked,
        "errors": errors,
    }
    _append(event, path)
    return event


# ----------------------------------------------------------------------
# reading
# ----------------------------------------------------------------------

def read_ledger(path=LEDGER_FILE):
    """Tolerates partially written final lines rather than throwing."""
    if not os.path.exists(path):
        return []
    events = []
    f = open(path, "r")
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    f.close()
    return events


def build_closed_trades(multipliers=None, path=LEDGER_FILE):
    """
    Pair entries with exits into round-trip trades.

    multipliers: dict of label -> contract multiplier (DAX is 25, futures
    vary, equities are 1). Without this, futures P&L is understated by the
    multiplier and the attribution chart lies.
    """
    multipliers = multipliers or {}
    events = read_ledger(path)

    open_positions = {}
    trades = []

    # Key on (strategy, label). Events written before the strategy tag existed
    # carry None, so they keep pairing among themselves exactly as before.
    for e in events:
        if e.get("event") == "entry":
            open_positions[(e.get("strategy"), e["label"])] = e

        elif e.get("event") == "exit":
            label = e["label"]
            entry = open_positions.pop((e.get("strategy"), label), None)
            if entry is None:
                continue

            mult = multipliers.get(label, 1)
            qty = entry.get("quantity", 1)

            gross = (e["price"] - entry["price"]) * qty * mult
            pnl_pct = ((e["price"] / entry["price"]) - 1.0) * 100 if entry["price"] else 0.0

            d_in = datetime.strptime(entry["date"], "%Y-%m-%d")
            d_out = datetime.strptime(e["date"], "%Y-%m-%d")

            trades.append({
                "label": label,
                "direction": "Long",
                "entry_date": entry["date"],
                "exit_date": e["date"],
                "entry_price": entry["price"],
                "exit_price": e["price"],
                "quantity": qty,
                "multiplier": mult,
                "pnl": round(gross, 2),
                "pnl_pct": round(pnl_pct, 2),
                "holding_days": (d_out - d_in).days,
                "reason": e.get("reason", "unknown"),
                "entry_rsi": entry.get("rsi"),
                "exit_rsi": e.get("rsi"),
            })

    return trades, open_positions


def suppression_stats(path=LEDGER_FILE):
    """
    How often signals were vetoed, and by what.

    fill_rate is entries divided by (entries + suppressions): the fraction
    of fired crossovers that actually became positions.
    """
    events = read_ledger(path)
    suppressed = [e for e in events if e.get("event") == "suppressed"]
    entries = [e for e in events if e.get("event") == "entry"]

    by_reason = {}
    by_label = {}
    for s in suppressed:
        by_reason[s.get("reason", "unknown")] = by_reason.get(s.get("reason", "unknown"), 0) + 1
        by_label[s["label"]] = by_label.get(s["label"], 0) + 1

    total_fired = len(entries) + len(suppressed)
    fill_rate = (len(entries) / total_fired * 100) if total_fired else 0.0

    return {
        "signals_fired": total_fired,
        "entries_taken": len(entries),
        "suppressed": len(suppressed),
        "fill_rate": round(fill_rate, 1),
        "by_reason": by_reason,
        "by_label": by_label,
    }


def recent_health(limit=25, path=LEDGER_FILE):
    events = read_ledger(path)
    health = [e for e in events if e.get("event") == "health"]
    return list(reversed(health[-limit:]))


def last_run(path=LEDGER_FILE):
    events = read_ledger(path)
    runs = [e for e in events if e.get("event") == "run"]
    return runs[-1] if runs else None
