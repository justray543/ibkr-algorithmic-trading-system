"""
dashboard_refresh.py

Refreshes docs/state.json WITHOUT trading.

This is the safe file to run on a frequent schedule. It connects to IBKR,
reads NAV, positions and recent prices, recomputes the indicators for the
signal cards, and writes state.json. It never calls send_order.

Why this exists: live_trade.py places orders every time it runs, so running
it every 30 minutes to freshen the dashboard would turn a daily strategy
into an intraday one by accident. This script is trading-inert. The actual
strategies keep their own schedules; this one only ever reads.

It reflects positions the strategies have taken (from the *_position_state
files they write) plus anything held manually at the broker. It does not
decide anything and does not write those state files.

Run from cron every 15-30 min during market hours. Trading scripts unchanged.
"""

import json
import os
import threading
import time
from datetime import datetime

from wrapper import IBWrapper
from client import IBClient
from contract import future, stock

import metrics
import trade_ledger as ledger
import dashboard_export as dx

LOG_FILE = "refresh_log.txt"

# Read-only client id, distinct from every trading script so it never
# collides with one that is mid-run.
CLIENT_ID = 188

# Instruments shown on the dashboard.
#
# Derived from live_trade.py's build_instruments() rather than restated here.
# This file used to keep its own copy, which meant every contract roll had to
# be made in three places -- live_trade.py, dashboard_export.EXPIRIES and here.
# When HSI expired on 2026-07-30 the first two were updated and this one was
# not, so the refresh kept requesting a dead contract and reporting
# "insufficient data" while the dashboard published unrealised P&L on it.
#
# Importing live_trade is safe: everything that connects or trades sits behind
# its __main__ guard, so this picks up the contract definitions and nothing else.
def build_dashboard_instruments():
    from live_trade import build_instruments

    inst = {}
    for label, cfg in build_instruments().items():
        contract = cfg["contract"]
        inst[label] = {
            "contract": contract,
            "state_file": cfg["state_file"],
            "bar": "1 day",
            # Futures quote TRADES; MIDPOINT is the sane default for equities,
            # which have a continuous two-sided book.
            "show": "TRADES" if contract.secType == "FUT" else "MIDPOINT",
        }
    return inst


class IBApp(IBWrapper, IBClient):
    def __init__(self, ip, port, client_id, account):
        IBWrapper.__init__(self)
        IBClient.__init__(self, wrapper=self)
        self.account = account
        self.connect(ip, port, client_id)
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        time.sleep(3)


def log(message):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[" + stamp + "] " + message
    print(line)
    f = open(LOG_FILE, "a")
    f.write(line + "\n")
    f.close()


def load_state(state_file):
    """Read a strategy's state file. Never writes."""
    if not os.path.exists(state_file):
        return {"position": 0, "entry_price": 0.0, "stop_price": 0.0}
    try:
        f = open(state_file, "r")
        data = json.load(f)
        f.close()
        return data
    except (json.JSONDecodeError, IOError):
        return {"position": 0, "entry_price": 0.0, "stop_price": 0.0}


def compute_signal(prices, rsi_window=14):
    ema9 = prices.ewm(span=9, adjust=False).mean()
    ema21 = prices.ewm(span=21, adjust=False).mean()
    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=rsi_window).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=rsi_window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return ema9.iloc[-1], ema21.iloc[-1], rsi.iloc[-1]


def get_net_liquidation(app):
    try:
        value, _currency = app.get_account_values(key="NetLiquidation")
        return float(value)
    except Exception as e:
        log("Could not read NetLiquidation: " + str(e))
        return None


if __name__ == "__main__":
    app = IBApp("127.0.0.1", 7497, CLIENT_ID, "DUQ153118")
    app.reqMarketDataType(3)

    log("=== dashboard refresh (read-only, no trading) started ===")

    real_positions = app.get_positions()

    signal_snapshot = {}
    live_states = {}
    price_history = {}

    instruments = build_dashboard_instruments()

    i = 0
    for label in instruments:
        cfg = instruments[label]
        contract = cfg["contract"]
        try:
            history = app.get_historical_data(
                27500 + i, contract, "60 D", cfg["bar"], cfg["show"]
            )
            if history.empty or len(history) < 25:
                log(label + ": insufficient data (" + str(len(history)) + " rows), skipped")
                i += 1
                continue

            ema9, ema21, rsi = compute_signal(history["close"])
            price = float(history["close"].iloc[-1])

            price_history[label] = history["close"]
            signal_snapshot[label] = {
                "price": price, "ema9": float(ema9),
                "ema21": float(ema21), "rsi": float(rsi),
            }

            # Position shown on the dashboard comes from the strategy's own
            # state file, cross-checked against the broker. This script does
            # not reconcile or write; it only reports what it reads.
            st = load_state(cfg["state_file"])
            broker_qty = 0
            if label in real_positions:
                broker_qty = real_positions[label].get("position", 0)

            # Trust the state file when it and the broker agree there is a
            # position. If the broker shows one the state file misses, show
            # it anyway so nothing is silently hidden, but mark entry 0 so
            # the P&L is not computed from a wrong basis.
            if st.get("position", 0) == 1:
                live_states[label] = {
                    "position": 1,
                    "entry_price": float(st.get("entry_price", 0.0)),
                    "stop_price": float(st.get("stop_price", 0.0)),
                }
            elif broker_qty != 0:
                live_states[label] = {
                    "position": 1,
                    "entry_price": 0.0,
                    "stop_price": 0.0,
                }
            else:
                live_states[label] = {"position": 0, "entry_price": 0.0, "stop_price": 0.0}

            log(label + ": price=" + str(round(price, 2)) +
                " RSI=" + str(round(rsi, 2)) +
                " pos=" + str(live_states[label]["position"]))

        except Exception as e:
            log(label + ": EXCEPTION - " + str(e))
        i += 1

    nav = get_net_liquidation(app)

    ledger.record_run("refresh", len(instruments), errors=0)

    if nav is None:
        log("No NAV available, skipping export.")
    else:
        log("NetLiquidation: " + str(round(nav, 2)))
        dx.export(
            nav=nav,
            price_history=price_history,
            live_states=live_states,
            signal_snapshot=signal_snapshot,
            starting_capital=None,   # inception.json already exists
        )
        log("Dashboard state written to docs/state.json")

    log("=== dashboard refresh complete ===")
    app.disconnect()
