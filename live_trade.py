import json
import os
import threading
import time
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

from wrapper import IBWrapper
from client import IBClient
from contract import future, stock
from order import market, BUY, SELL
from email_config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL

import metrics
import trade_ledger as ledger
import dashboard_export as dx
import position_ownership as owner

STRATEGY = "daily_futures"

# IBKR's updatePortfolio reports average_cost for FUTURES as the full
# contract value (price x multiplier), not the per-point price. Adopting a
# position without dividing gives an entry price 5x to 25x too high: NKD
# showed 332953 instead of 66590. Every stop and P&L derived from it is
# then wrong. Divide by the multiplier when adopting a futures position.
CONTRACT_MULTIPLIERS = {
    "NQ": 20,
    "DAX": 25,
    "NKD": 5,
    "HSI": 50,
    "SOXX": 1,
}

LOG_FILE = "multi_trade_log.txt"

RSI_ENTRY_MIN = 50
RSI_ENTRY_MAX = 70
RSI_EXIT_THRESHOLD = 40

# Opening NAV of the paper account. Written once to inception.json on the
# first run and never read from here again. Every "since inception" figure
# on the dashboard anchors to it, so set it correctly before the first run.
STARTING_CAPITAL = 1000000.0
ACCOUNT_CURRENCY = "EUR"

summary_lines = []

# Collected during the instrument loop and handed to the exporter at the end.
signal_snapshot = {}   # label -> {price, ema9, ema21, rsi}
live_states = {}       # label -> {position, entry_price, stop_price}
price_history = {}     # label -> pd.Series of daily closes
error_count = 0


def build_instruments():
    instruments = {}

    nq_contract = future("NQ", "CME", "202609", currency="USD")
    instruments["NQ"] = {"contract": nq_contract, "quantity": 1, "state_file": "nq_position_state.json"}

    dax_contract = future("DAX", "EUREX", "202609", currency="EUR", multiplier="25", trading_class="FDAX")
    instruments["DAX"] = {"contract": dax_contract, "quantity": 1, "state_file": "dax_position_state.json"}

    nkd_contract = future("NKD", "CME", "20260910", currency="USD")
    instruments["NKD"] = {"contract": nkd_contract, "quantity": 1, "state_file": "nkd_position_state.json"}

    soxx_contract = stock("SOXX", "SMART", "USD", primary_exchange="NASDAQ")
    instruments["SOXX"] = {"contract": soxx_contract, "quantity": 10, "state_file": "soxx_position_state.json"}

    # Rolled 2026-08-02 from 20260730, which expired and cash-settled on Jul 30.
    # The broker closed the position; the local state file went on claiming it
    # was open, so the dashboard reported ~+42,600 of unrealised P&L on a
    # contract that no longer existed. Verified against IBKR's listed chain
    # (app.front_contract) rather than assumed.
    hsi_contract = future("HSI", "HKFE", "20260828", currency="HKD")
    instruments["HSI"] = {"contract": hsi_contract, "quantity": 1, "state_file": "hsi_position_state.json"}

    return instruments


class IBApp(IBWrapper, IBClient):
    def __init__(self, ip, port, client_id, account):
        IBWrapper.__init__(self)
        IBClient.__init__(self, wrapper=self)
        self.account = account
        self.connect(ip, port, client_id)
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        time.sleep(3)


def load_state(state_file):
    if os.path.exists(state_file):
        f = open(state_file, "r")
        data = json.load(f)
        f.close()
        if "quantity" not in data:
            data["quantity"] = 0
        if "manual" not in data:
            data["manual"] = False
        return data
    return {"position": 0, "entry_price": 0.0, "stop_price": 0.0,
            "quantity": 0, "manual": False}


def save_state(state_file, position, entry_price, stop_price,
               quantity=0, manual=False):
    state = {
        "position": position,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "quantity": quantity,
        "manual": manual,
    }
    f = open(state_file, "w")
    json.dump(state, f)
    f.close()


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[" + timestamp + "] " + message
    print(line)
    f = open(LOG_FILE, "a")
    f.write(line + "\n")
    f.close()
    summary_lines.append(message)


def send_summary_email():
    subject = "Trading Bot Daily Summary - " + datetime.now().strftime("%Y-%m-%d")
    body = "\n".join(summary_lines)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
        server.quit()
        print("Summary email sent.")
    except Exception as e:
        print("Failed to send summary email: " + str(e))


def compute_signal(prices, rsi_window=14):
    ema9 = prices.ewm(span=9, adjust=False).mean()
    ema21 = prices.ewm(span=21, adjust=False).mean()

    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=rsi_window).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=rsi_window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    return ema9.iloc[-1], ema21.iloc[-1], rsi.iloc[-1]


def normalise_avg_cost(label, raw_avg_cost, contract):
    """
    Convert IBKR's average_cost into a per-point entry price.

    Stocks report average_cost per share, so they pass through unchanged.
    Futures report it as the whole contract value, so divide by the
    multiplier. Returns 0.0 on nonsense input rather than guessing.
    """
    if raw_avg_cost is None or raw_avg_cost <= 0:
        return 0.0

    if contract.secType != "FUT":
        return float(raw_avg_cost)

    multiplier = CONTRACT_MULTIPLIERS.get(label, 1)
    if multiplier <= 0:
        return float(raw_avg_cost)

    return float(raw_avg_cost) / multiplier


def reconcile_state(label, state_file, real_positions, contract=None):
    state = load_state(state_file)
    local_position = state["position"]

    real_qty = 0
    real_avg_cost = 0.0
    if label in real_positions:
        real_qty = real_positions[label].get("position", 0)
        real_avg_cost = real_positions[label].get("average_cost", 0.0)

    broker_has_position = real_qty != 0

    if local_position == 1 and not broker_has_position:
        message = "STATE MISMATCH - local file says position open, broker shows none. Resetting to flat."
        log(label + ": " + message)
        ledger.record_health("warning", label, message)
        save_state(state_file, 0, 0.0, 0.0, 0, False)
        owner.release(label, STRATEGY)
        return load_state(state_file)

    if local_position == 0 and broker_has_position:
        entry_px = real_avg_cost
        if contract is not None:
            entry_px = normalise_avg_cost(label, real_avg_cost, contract)
            if contract.secType == "FUT" and entry_px != real_avg_cost:
                log(label + ": normalised broker avg_cost " +
                    str(round(real_avg_cost, 2)) + " to per-point " +
                    str(round(entry_px, 2)) + " (multiplier " +
                    str(CONTRACT_MULTIPLIERS.get(label, 1)) + ")")

        message = ("position (" + str(real_qty) + ") not tracked locally, "
                   "treating as MANUAL. Bot will not auto-sell it.")
        log(label + ": " + message)
        ledger.record_health("warning", label, message)
        save_state(state_file, 1, entry_px, 0.0, int(abs(real_qty)), True)
        return load_state(state_file)

    return state


def process_instrument(app, label, config, request_id, real_positions):
    contract = config["contract"]
    quantity = config["quantity"]
    state_file = config["state_file"]

    log("--- Checking " + label + " ---")

    state = reconcile_state(label, state_file, real_positions, contract)

    what_to_show = "MIDPOINT"
    if contract.secType == "FUT":
        what_to_show = "TRADES"

    history = app.get_historical_data(request_id, contract, "60 D", "1 day", what_to_show)

    if history.empty or len(history) < 25:
        message = "insufficient data, skipped"
        log(label + ": ERROR insufficient data. Skipping.")
        ledger.record_health("warning", label, message)
        return

    ema9, ema21, rsi = compute_signal(history["close"])
    current_price = history["close"].iloc[-1]

    # Keep the close series for the correlation matrix and the benchmark.
    price_history[label] = history["close"]

    signal_snapshot[label] = {
        "price": float(current_price),
        "ema9": float(ema9),
        "ema21": float(ema21),
        "rsi": float(rsi),
    }

    price_str = str(round(current_price, 2))
    ema9_str = str(round(ema9, 2))
    ema21_str = str(round(ema21, 2))
    rsi_str = str(round(rsi, 2))
    log(label + ": price=" + price_str + " EMA9=" + ema9_str + " EMA21=" + ema21_str + " RSI=" + rsi_str)

    position = state["position"]
    entry_price = state["entry_price"]
    stop_price = state["stop_price"]
    held_quantity = state.get("quantity", quantity)
    is_manual = state.get("manual", False)

    entry_signal = ema9 > ema21 and rsi > RSI_ENTRY_MIN and rsi < RSI_ENTRY_MAX
    exit_signal = ema9 < ema21 or rsi < RSI_EXIT_THRESHOLD
    stop_hit = position == 1 and current_price <= stop_price

    if position == 1 and is_manual:
        entry_str = str(round(entry_price, 2)) if entry_price else "unknown"
        log(label + ": MANUAL position (x" + str(held_quantity) +
            ", entry=" + entry_str + "), bot will not auto-sell.")
        live_states[label] = {"position": 1, "entry_price": float(entry_price),
                              "stop_price": float(stop_price)}
        return

    if position == 0 and entry_signal and not owner.can_trade(label, STRATEGY):
        holder = owner.owner_of(label)
        log(label + ": ENTRY SIGNAL but owned by '" + str(holder) +
            "'. Skipping to avoid two strategies fighting over one contract.")
        live_states[label] = {"position": 0, "entry_price": 0.0, "stop_price": 0.0}
        return

    if position == 0 and entry_signal:
        log(label + ": ENTRY SIGNAL. Placing BUY x" + str(quantity))
        order = market(BUY, quantity)
        order_id = app.send_order(contract, order)
        log(label + ": order sent (id=" + str(order_id) + "). Waiting to confirm fill...")
        time.sleep(5)
        log(label + ": check TWS Trades tab to confirm this order actually filled.")
        new_stop = current_price * 0.95
        save_state(state_file, 1, current_price, new_stop, quantity, False)
        owner.claim(label, STRATEGY, quantity, float(current_price))

        ledger.record_entry(
            label, current_price, quantity, new_stop,
            order_id=order_id, rsi=rsi, ema9=ema9, ema21=ema21
        )
        live_states[label] = {
            "position": 1,
            "entry_price": float(current_price),
            "stop_price": float(new_stop),
        }
        return

    elif position == 0 and ema9 > ema21 and rsi >= RSI_ENTRY_MAX:
        log(label + ": signal fired but RSI is overbought. Skipping entry.")
        ledger.record_suppressed(
            label, current_price, rsi, ema9, ema21, "rsi_overbought"
        )

    elif position == 1 and stop_hit:
        log(label + ": STOP-LOSS HIT. Placing SELL.")
        order = market(SELL, quantity)
        order_id = app.send_order(contract, order)
        log(label + ": close order sent (id=" + str(order_id) + ")")
        time.sleep(5)
        save_state(state_file, 0, 0.0, 0.0, 0, False)
        owner.release(label, STRATEGY)

        ledger.record_exit(
            label, current_price, quantity, "stop",
            order_id=order_id, rsi=rsi, ema9=ema9, ema21=ema21
        )
        live_states[label] = {"position": 0, "entry_price": 0.0, "stop_price": 0.0}
        return

    elif position == 1 and exit_signal:
        log(label + ": EXIT SIGNAL. Placing SELL.")
        order = market(SELL, quantity)
        order_id = app.send_order(contract, order)
        log(label + ": close order sent (id=" + str(order_id) + ")")
        time.sleep(5)
        save_state(state_file, 0, 0.0, 0.0, 0, False)
        owner.release(label, STRATEGY)

        # Distinguish the two exit causes so the dashboard breakdown is real.
        if rsi < RSI_EXIT_THRESHOLD:
            reason = "rsi_exit"
        else:
            reason = "crossover"

        ledger.record_exit(
            label, current_price, quantity, reason,
            order_id=order_id, rsi=rsi, ema9=ema9, ema21=ema21
        )
        live_states[label] = {"position": 0, "entry_price": 0.0, "stop_price": 0.0}
        return

    elif position == 1:
        entry_str = str(round(entry_price, 2))
        current_str = str(round(current_price, 2))
        stop_str = str(round(stop_price, 2))
        log(label + ": holding. entry=" + entry_str + " current=" + current_str + " stop=" + stop_str)

    else:
        log(label + ": no signal, staying flat.")

    # No trade this run, so carry the reconciled state through unchanged.
    live_states[label] = {
        "position": position,
        "entry_price": float(entry_price),
        "stop_price": float(stop_price),
    }


def get_net_liquidation(app):
    """
    NAV via reqAccountUpdates. get_account_values returns a (value, currency)
    tuple keyed off updateAccountValue, so unpack it rather than using the
    tuple directly. There is no accountSummary callback in wrapper.py, so
    reqAccountSummary would return nothing.
    """
    try:
        value, currency = app.get_account_values(key="NetLiquidation")
        return float(value)
    except Exception as e:
        log("Could not read NetLiquidation: " + str(e))
        ledger.record_health("error", "SYSTEM", "NetLiquidation read failed: " + str(e))
        return None


if __name__ == "__main__":
    # Bump this on each run if TWS reports "client id is already in use".
    # A crashed run leaves the previous id registered until TWS releases it.
    app = IBApp("127.0.0.1", 7497, 80, "DUQ153118")
    app.reqMarketDataType(3)

    log("=== Multi-instrument daily signal check started ===")

    # Written once. Deleting inception.json orphans the equity curve.
    ledger.ensure_inception(STARTING_CAPITAL, currency=ACCOUNT_CURRENCY)

    real_positions = app.get_positions()
    log("Broker-reported positions: " + str(list(real_positions.keys())))

    cleared = owner.sync_with_broker(real_positions)
    if cleared:
        log("Cleared stale ownership claims: " + str(cleared))

    instruments = build_instruments()

    # Expiry guard. The contract months above are hardcoded, so they go stale
    # in silence: an expired future returns no bars, the run logs "insufficient
    # data, skipped", and everything downstream reports success. HSI sat
    # expired for three days that way while the dashboard published unrealised
    # P&L on it. Check every contract against IBKR's own listed chain and say
    # so loudly.
    for label in instruments:
        contract = instruments[label]["contract"]
        if contract.secType != "FUT":
            continue
        raw = contract.lastTradeDateOrContractMonth
        dte = metrics.days_to_expiry(raw)
        if dte is None:
            continue
        if dte < 0:
            probe = future(contract.symbol, contract.exchange, "",
                           currency=contract.currency)
            probe.lastTradeDateOrContractMonth = ""
            suggested = app.front_contract(probe, 46000 + len(label))
            nxt = suggested.lastTradeDateOrContractMonth if suggested else "unknown"
            message = ("contract " + str(raw) + " EXPIRED " + str(abs(dte)) +
                       "d ago - roll to " + str(nxt) + " in build_instruments()")
            log(label + ": " + message)
            ledger.record_health("error", label, message)
        elif dte <= 5:
            message = "contract " + str(raw) + " expires in " + str(dte) + "d - roll soon"
            log(label + ": " + message)
            ledger.record_health("warning", label, message)

    i = 0
    for label in instruments:
        config = instruments[label]
        try:
            process_instrument(app, label, config, 27000 + i, real_positions)
        except Exception as e:
            log(label + ": EXCEPTION - " + str(e))
            ledger.record_health("error", label, "EXCEPTION - " + str(e))
            error_count = error_count + 1
        i = i + 1

    log("=== Multi-instrument daily signal check complete ===")

    # ---------- dashboard export ----------
    try:
        nav = get_net_liquidation(app)

        # Recorded before the export so state.json includes this run rather
        # than the previous one. The exporter reads the ledger as it stands.
        ledger.record_run("ok", len(instruments), errors=error_count)

        if nav is None:
            log("Skipping dashboard export: no NAV available.")
        else:
            log("NetLiquidation: " + str(round(nav, 2)))
            dx.export(
                nav=nav,
                price_history=price_history,
                live_states=live_states,
                signal_snapshot=signal_snapshot,
                starting_capital=STARTING_CAPITAL,
            )
            log("Dashboard state written to docs/state.json")

    except Exception as e:
        log("Dashboard export failed: " + str(e))
        ledger.record_health("error", "SYSTEM", "Dashboard export failed: " + str(e))
        ledger.record_run("export_failed", len(instruments), errors=error_count + 1)

    app.disconnect()

    send_summary_email()