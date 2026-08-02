import json
import os
import threading
import time
import smtplib
import requests
from email.mime.text import MIMEText
from datetime import datetime

from wrapper import IBWrapper
from client import IBClient
from contract import stock
from order import market, BUY, SELL
from position_sizing import calculate_risk_based_size, describe_size
from email_config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL
from telegram_config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

import beta

LOG_FILE = "daily_trade_log.txt"

RSI_ENTRY_MIN = 50
RSI_ENTRY_MAX = 70
RSI_EXIT_THRESHOLD = 40

STOP_LOSS_PCT = 0.05

# Risk control. RISK_PER_TRADE_PCT is what a stop-out costs the account, not
# what the position is worth -- see position_sizing.py for why those are not
# the same number. MAX_NOTIONAL_PCT caps gross exposure independently, so a
# tight stop cannot talk the risk model into an enormous position.
RISK_PER_TRADE_PCT = 0.01
MAX_NOTIONAL_PCT = 0.20
MAX_QTY_STOCK = 100

# Benchmark for beta. SPY is not in the traded universe; it is fetched once per
# run purely as the regression reference.
BENCHMARK_SYMBOL = "SPY"

summary_lines = []
action_summary = []

# Collected during the instrument loop, handed to beta.write_watchlist at the
# end. benchmark_closes is populated once before the loop.
beta_rows = []
benchmark_closes = None


def build_instruments():
    instruments = {}

    stock_list = [
        ("SOXX", "NASDAQ", "USD"),
        ("AAPL", "NASDAQ", "USD"),
        ("MSFT", "NASDAQ", "USD"),
        ("GOOGL", "NASDAQ", "USD"),
        ("AMZN", "NASDAQ", "USD"),
        ("NVDA", "NASDAQ", "USD"),
        ("META", "NASDAQ", "USD"),
        ("TSLA", "NASDAQ", "USD"),
        ("TSM", "NYSE", "USD"),
        ("MU", "NASDAQ", "USD"),
        ("SNDK", "NASDAQ", "USD"),
        ("SKHY", "NASDAQ", "USD"),
        ("ARM", "NASDAQ", "USD"),
        ("AMD", "NASDAQ", "USD"),
        ("INTC", "NASDAQ", "USD"),
    ]
    for symbol, exch, curr in stock_list:
        contract = stock(symbol, "SMART", curr, primary_exchange=exch)
        state_file = symbol.lower() + "_daily_position_state.json"
        instruments[symbol] = {"contract": contract, "multiplier": 1, "state_file": state_file}

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
    return {"position": 0, "entry_price": 0.0, "stop_price": 0.0, "quantity": 0, "manual": False}


def save_state(state_file, position, entry_price, stop_price, quantity, manual=False):
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


def has_notable_action():
    for label, action in action_summary:
        if action.startswith("BUY") or action.startswith("SELL") or action.startswith("ERROR"):
            return True
    return False


def send_summary_email():
    subject = "DAILY Trading Bot Summary - " + datetime.now().strftime("%Y-%m-%d")

    rows_html = ""
    for label, action in action_summary:
        color = "#333333"
        if action.startswith("BUY"):
            color = "#1a7f37"
        elif action.startswith("SELL"):
            color = "#cf222e"
        rows_html += (
            "<tr>"
            "<td style='padding:8px 14px; font-weight:600; border-bottom:1px solid #eee;'>" + label + "</td>"
            "<td style='padding:8px 14px; color:" + color + "; border-bottom:1px solid #eee;'>" + action + "</td>"
            "</tr>"
        )

    html_body = (
        "<html><body style=\"font-family: 'Helvetica Neue', Arial, sans-serif; font-size:14px; color:#222; max-width:600px;\">"
        "<h2 style=\"font-family: Georgia, 'Times New Roman', serif; color:#111;\">Daily Stocks/ETF Summary</h2>"
        "<p style='color:#666; font-size:12px;'>" + datetime.now().strftime("%Y-%m-%d %H:%M") + "</p>"
        "<table style='border-collapse:collapse; width:100%;'>"
        + rows_html +
        "</table>"
        "</body></html>"
    )

    msg = MIMEText(html_body, "html")
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


def send_telegram_summary():
    buys = [item for item in action_summary if item[1].startswith("BUY")]
    sells = [item for item in action_summary if item[1].startswith("SELL")]
    holds = [item for item in action_summary if item[1].startswith("HOLD")]
    errors = [item for item in action_summary if item[1].startswith("ERROR")]

    lines = []
    lines.append("📈 *DAILY Stocks/ETF Summary*")
    lines.append("_" + datetime.now().strftime("%A, %b %d — %H:%M") + "_")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━")

    if buys:
        lines.append("")
        lines.append("🟢 *BUYS* (" + str(len(buys)) + ")")
        for label, action in buys:
            detail = action.replace("BUY (", "").rstrip(")")
            lines.append("   • *" + label + "*  —  " + detail)

    if sells:
        lines.append("")
        lines.append("🔴 *SELLS* (" + str(len(sells)) + ")")
        for label, action in sells:
            detail = action.replace("SELL (", "").rstrip(")")
            lines.append("   • *" + label + "*  —  " + detail)

    if holds:
        lines.append("")
        lines.append("⚪️ *HOLDS* (" + str(len(holds)) + ")")
        hold_labels = ", ".join([label for label, _ in holds])
        lines.append("   " + hold_labels)

    if errors:
        lines.append("")
        lines.append("⚠️ *ERRORS* (" + str(len(errors)) + ")")
        for label, action in errors:
            lines.append("   • *" + label + "*  —  " + action)

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append("_" + str(len(action_summary)) + " instruments checked_")

    message_text = "\n".join(lines)

    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "Markdown",
    }

    try:
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code == 200:
            print("Telegram summary sent.")
        else:
            print("Telegram send failed: " + str(response.status_code) + " " + response.text)
    except Exception as e:
        print("Failed to send Telegram summary: " + str(e))


def compute_signal(prices, rsi_window=14):
    ema9 = prices.ewm(span=9, adjust=False).mean()
    ema21 = prices.ewm(span=21, adjust=False).mean()

    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=rsi_window).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=rsi_window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    return ema9.iloc[-1], ema21.iloc[-1], rsi.iloc[-1]


def compute_signal_short_window(prices, short_span=2, long_span=4, rsi_window=3):
    ema_short = prices.ewm(span=short_span, adjust=False).mean()
    ema_long = prices.ewm(span=long_span, adjust=False).mean()

    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=rsi_window).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=rsi_window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    return ema_short.iloc[-1], ema_long.iloc[-1], rsi.iloc[-1]


def reconcile_state(label, state_file, real_positions):
    state = load_state(state_file)
    local_position = state["position"]

    real_qty = 0
    real_avg_cost = 0.0
    if label in real_positions:
        real_qty = real_positions[label].get("position", 0)
        real_avg_cost = real_positions[label].get("average_cost", 0.0)

    broker_has_position = real_qty != 0

    if local_position == 1 and not broker_has_position:
        log(label + ": STATE MISMATCH - local file says position open, broker shows none. Resetting to flat.")
        save_state(state_file, 0, 0.0, 0.0, 0, False)
        return load_state(state_file)

    if local_position == 0 and broker_has_position:
        log(label + ": position (" + str(real_qty) + ") not tracked locally — treating as MANUAL, bot will not auto-sell this.")
        save_state(state_file, 1, real_avg_cost, 0.0, int(abs(real_qty)), True)
        return load_state(state_file)

    return state


def has_pending_order(label, open_orders):
    if label not in open_orders:
        return False
    return len(open_orders[label]) > 0


def process_instrument(app, label, config, request_id, real_positions, open_orders):
    contract = config["contract"]
    multiplier = config["multiplier"]
    state_file = config["state_file"]

    log("--- Checking " + label + " ---")

    state = reconcile_state(label, state_file, real_positions)
    is_manual = state.get("manual", False)

    if has_pending_order(label, open_orders):
        log(label + ": pending order already exists (from a previous run). Skipping to avoid duplicate.")
        action_summary.append((label, "HOLD (pending order already open, skipped)"))
        return

    history = app.get_historical_data(request_id, contract, "60 D", "1 day", "MIDPOINT")

    log(label + ": received " + str(len(history)) + " rows of historical data.")

    min_rows_required = 3 if label == "SKHY" else 25

    if history.empty or len(history) < min_rows_required:
        log(label + ": ERROR insufficient data (" + str(len(history)) + " rows, need " + str(min_rows_required) + "). Skipping.")
        action_summary.append((label, "ERROR - no data"))
        return

    if label == "SKHY":
        log(label + ": WARNING - using short-window signal (untested config) due to limited trading history.")
        ema9, ema21, rsi = compute_signal_short_window(history["close"])
    else:
        ema9, ema21, rsi = compute_signal(history["close"])

    current_price = history["close"].iloc[-1]

    price_str = str(round(current_price, 2))
    ema9_str = str(round(ema9, 2))
    ema21_str = str(round(ema21, 2))
    rsi_str = str(round(rsi, 2))
    log(label + ": price=" + price_str + " EMA9=" + ema9_str + " EMA21=" + ema21_str + " RSI=" + rsi_str)

    position = state["position"]
    entry_price = state["entry_price"]
    stop_price = state["stop_price"]
    held_quantity = state.get("quantity", 0)

    # Score this name for the high-beta short watchlist. Flagging only -- no
    # order is ever placed from this, and beta.py cannot place one.
    if benchmark_closes is not None:
        try:
            beta_rows.append(beta.evaluate(
                label, history["close"], benchmark_closes,
                ema9, ema21, rsi, current_price))
        except Exception as e:
            log(label + ": beta scoring failed - " + str(e))

    entry_signal = ema9 > ema21 and rsi > RSI_ENTRY_MIN and rsi < RSI_ENTRY_MAX
    exit_signal = ema9 < ema21 or rsi < RSI_EXIT_THRESHOLD
    stop_hit = position == 1 and current_price <= stop_price

    if position == 0 and entry_signal:
        account_values = app.get_account_values()
        net_liq = account_values.get("NetLiquidation", (0.0, "USD"))[0]

        # Stop first, size second. The stop distance IS the risk denominator,
        # so it has to exist before a quantity can be computed at all.
        new_stop = current_price * (1 - STOP_LOSS_PCT)
        dynamic_qty = calculate_risk_based_size(
            net_liq, current_price, new_stop,
            risk_pct=RISK_PER_TRADE_PCT, multiplier=multiplier,
            max_notional_pct=MAX_NOTIONAL_PCT, max_qty=MAX_QTY_STOCK
        )

        if dynamic_qty <= 0:
            # Zero is a decision, not a rounding error: the account cannot
            # carry this position inside the risk budget. Skipping is correct.
            log(label + ": ENTRY SIGNAL but risk-based size is 0 "
                "(NetLiq=" + str(round(net_liq, 2)) + ", stop distance "
                + str(round(current_price - new_stop, 4)) + "). Skipping entry.")
            action_summary.append((label, "HOLD (entry signal, but 0 size within risk budget)"))
            return

        sizing = describe_size(net_liq, current_price, new_stop, dynamic_qty, multiplier)
        log(label + ": ENTRY SIGNAL. NetLiq=" + str(round(net_liq, 2))
            + " -> " + str(dynamic_qty) + " shares"
            + " | risk " + str(sizing["risk_amount"]) + " (" + str(sizing["risk_pct"]) + "% of NAV)"
            + " | notional " + str(sizing["notional"]) + " (" + str(sizing["notional_pct"]) + "%)")
        order = market(BUY, dynamic_qty)
        order_id = app.send_order(contract, order)
        log(label + ": order sent (id=" + str(order_id) + "). Waiting to confirm fill...")
        time.sleep(5)
        log(label + ": check TWS Trades tab to confirm this order actually filled.")
        save_state(state_file, 1, current_price, new_stop, dynamic_qty, False)
        action_summary.append((label, "BUY (" + str(dynamic_qty) + " shares @ " + price_str
                               + ", risking " + str(sizing["risk_pct"]) + "% of NAV)"))

    elif position == 0 and ema9 > ema21 and rsi >= RSI_ENTRY_MAX:
        log(label + ": signal fired but RSI is overbought. Skipping entry.")
        action_summary.append((label, "HOLD (flat, overbought - skipped entry)"))

    elif position == 1 and is_manual:
        entry_str = str(round(entry_price, 2)) if entry_price else "unknown"
        log(label + ": MANUAL position (x" + str(held_quantity) + ", entry=" + entry_str + ") — bot will not auto-sell.")
        action_summary.append((label, "HOLD (manual position, not managed by bot)"))

    elif position == 1 and stop_hit:
        log(label + ": STOP-LOSS HIT. Placing SELL x" + str(held_quantity))
        order = market(SELL, held_quantity)
        order_id = app.send_order(contract, order)
        log(label + ": close order sent (id=" + str(order_id) + ")")
        time.sleep(5)
        save_state(state_file, 0, 0.0, 0.0, 0, False)
        action_summary.append((label, "SELL (stop-loss hit, price " + price_str + ")"))

    elif position == 1 and exit_signal:
        log(label + ": EXIT SIGNAL. Placing SELL x" + str(held_quantity))
        order = market(SELL, held_quantity)
        order_id = app.send_order(contract, order)
        log(label + ": close order sent (id=" + str(order_id) + ")")
        time.sleep(5)
        save_state(state_file, 0, 0.0, 0.0, 0, False)
        action_summary.append((label, "SELL (trend exit, price " + price_str + ")"))

    elif position == 1:
        entry_str = str(round(entry_price, 2))
        current_str = str(round(current_price, 2))
        stop_str = str(round(stop_price, 2))
        log(label + ": holding x" + str(held_quantity) + ". entry=" + entry_str + " current=" + current_str + " stop=" + stop_str)
        action_summary.append((label, "HOLD (in position, entry " + entry_str + ", now " + current_str + ")"))

    else:
        log(label + ": no signal, staying flat.")
        action_summary.append((label, "HOLD (flat, no signal)"))


if __name__ == "__main__":
    app = IBApp("127.0.0.1", 7497, 172, "DUQ153118")
    app.reqMarketDataType(3)

    log("=== DAILY stocks/ETF signal check started ===")

    real_positions = app.get_positions()
    log("Broker-reported positions: " + str(list(real_positions.keys())))

    open_orders = app.get_open_orders()
    log("Pending open orders: " + str(list(open_orders.keys())))

    # Benchmark for beta, fetched once. A failure here disables beta scoring
    # for the run but must never stop trading -- the watchlist is an
    # observation layer, not a precondition for the strategy.
    try:
        bench_contract = stock(BENCHMARK_SYMBOL, "SMART", "USD", primary_exchange="ARCA")
        bench_hist = app.get_historical_data(79999, bench_contract, "60 D", "1 day", "MIDPOINT")
        if not bench_hist.empty and len(bench_hist) >= 25:
            benchmark_closes = bench_hist["close"]
            log("Benchmark " + BENCHMARK_SYMBOL + ": " + str(len(bench_hist)) + " bars for beta.")
        else:
            log("Benchmark " + BENCHMARK_SYMBOL + ": insufficient data, beta scoring disabled this run.")
    except Exception as e:
        log("Benchmark fetch failed (" + str(e) + "), beta scoring disabled this run.")

    instruments = build_instruments()

    i = 0
    for label in instruments:
        config = instruments[label]
        try:
            process_instrument(app, label, config, 80000 + i, real_positions, open_orders)
        except Exception as e:
            log(label + ": EXCEPTION - " + str(e))
            action_summary.append((label, "ERROR - " + str(e)))
        i = i + 1
        time.sleep(3)

    log("=== DAILY signal check complete ===")

    # High-beta short watchlist. Written after the loop so it reflects this
    # run's signals. Flag only -- nothing here places an order.
    if beta_rows:
        try:
            payload = beta.write_watchlist(beta_rows, BENCHMARK_SYMBOL)
            log("Short watchlist: " + str(payload["flagged_count"]) + " of "
                + str(payload["universe_count"]) + " names flagged -> " + beta.WATCHLIST_FILE)
            for line in beta.summary_lines(payload):
                log(line)
        except Exception as e:
            log("Short watchlist write failed: " + str(e))
    else:
        log("Short watchlist: no instruments scored this run.")

    app.disconnect()

    if has_notable_action():
        send_summary_email()
        send_telegram_summary()
    else:
        print("No BUY/SELL/ERROR actions this run — skipping notifications to avoid noise.")