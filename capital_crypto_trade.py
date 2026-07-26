"""
Capital.com crypto CFD trading daemon.

Mirrors the IBKR 5-min EMA9/21 + RSI setup, adapted for Capital.com's REST API.
Instruments: BTC, ETH, SOL (CFDs). Long AND short. Demo by default.

MODEL:
  - Long-running process (option B). Holds ONE session, keepalive-pinged.
  - Wakes on each 5-minute wall-clock boundary, evaluates every instrument, acts, sleeps.
  - Stateless-per-cycle logic on top of per-epic JSON state files (same pattern as IBKR code).
  - Server-side stops via stopDistance (ATR-based), so the stop fills at the broker
    even if this process dies. No Python-side price-check stop.

DIFFERENCES FROM IBKR SETUP YOU SHOULD KEEP IN MIND:
  - Crypto CFDs trade 24/7 and accrue OVERNIGHT FINANCING every night. A multi-day
    trend hold bleeds financing that your backtests do not model.
  - CFD spread is wider than spot. Frequent 5-min trading pays this spread repeatedly.
  - Epic names are UNVERIFIED. See capital_config.py. The script logs search results
    on startup; confirm before trusting order placement.

AUTH:
  POST /api/v1/session with headers X-CAP-API-KEY + body {identifier, password}
  returns CST and X-SECURITY-TOKEN headers, valid ~10 min idle. Re-auth on 401.
"""

import json
import os
import re
import time
import math
import threading
import requests
import pandas as pd
from datetime import datetime, timezone

import capital_config as cfg
import sentiment

LOG_FILE = "capital_crypto_trade_log.txt"
TRADE_HISTORY_FILE = "trade_history.jsonl"
SUGGESTIONS_FILE = "strategy_suggestions.json"
SESSION_START_FILE = "session_start.txt"

# Session profit target: once realized P&L since SESSION_START_TIME reaches
# cfg.PROFIT_TARGET_USD, the daemon closes every open position and stops opening
# new ones. SESSION_START_TIME persists in SESSION_START_FILE across process
# restarts (config/code changes shouldn't reset progress toward the target) --
# delete that file to start a fresh session.
SESSION_START_TIME = None
TRADING_HALTED = False


# ---------------------------------------------------------------------------
# Logging / notify
# ---------------------------------------------------------------------------
def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[" + timestamp + "] " + message
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def send_telegram(message_text):
    log("NOTIFY: " + message_text)
    # Reuse the same Telegram bot the IBKR side uses. Optional: if telegram_config
    # isn't present the daemon still runs and just logs the notification above.
    try:
        from telegram_config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    except Exception:
        return
    try:
        url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message_text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log("Telegram send failed: " + str(e))


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
class CapitalSession:
    """Holds one authenticated session. Thread-safe-ish for a single keepalive thread."""

    def __init__(self):
        self.base = cfg.BASE_URL
        self.cst = None
        self.security_token = None
        self._lock = threading.Lock()

    def _auth_headers(self):
        return {
            "X-CAP-API-KEY": cfg.CAPITAL_API_KEY,
            "CST": self.cst,
            "X-SECURITY-TOKEN": self.security_token,
            "Content-Type": "application/json",
        }

    def login(self):
        url = self.base + "/api/v1/session"
        headers = {"X-CAP-API-KEY": cfg.CAPITAL_API_KEY, "Content-Type": "application/json"}
        body = {"identifier": cfg.CAPITAL_IDENTIFIER, "password": cfg.CAPITAL_PASSWORD}
        r = requests.post(url, headers=headers, json=body, timeout=15)
        if r.status_code != 200:
            raise RuntimeError("Login failed: " + str(r.status_code) + " " + r.text)
        with self._lock:
            self.cst = r.headers.get("CST")
            self.security_token = r.headers.get("X-SECURITY-TOKEN")
        if not self.cst or not self.security_token:
            raise RuntimeError("Login returned no session tokens.")
        log("Session established.")

    def ping(self):
        try:
            r = requests.get(self.base + "/api/v1/ping", headers=self._auth_headers(), timeout=10)
            if r.status_code == 401:
                log("Keepalive got 401, re-authenticating.")
                self.login()
        except Exception as e:
            log("Keepalive ping error: " + str(e))

    def request(self, method, path, **kwargs):
        """Wrapper that re-auths once on 401."""
        url = self.base + path
        headers = self._auth_headers()
        r = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        if r.status_code == 401:
            log("401 on " + path + ", re-authenticating and retrying once.")
            self.login()
            headers = self._auth_headers()
            r = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        return r


def keepalive_loop(session, stop_event):
    while not stop_event.is_set():
        stop_event.wait(cfg.KEEPALIVE_SECONDS)
        if stop_event.is_set():
            break
        session.ping()


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
def search_markets(session, term):
    r = session.request("GET", "/api/v1/markets", params={"searchTerm": term})
    if r.status_code != 200:
        log("Market search '" + term + "' failed: " + str(r.status_code))
        return []
    out = []
    for m in r.json().get("markets", []):
        out.append((m.get("epic"), m.get("instrumentName"), m.get("marketStatus")))
    return out


def get_prices(session, epic):
    """Returns DataFrame with columns high, low, close (mid) indexed by UTC time."""
    r = session.request(
        "GET", "/api/v1/prices/" + epic,
        params={"resolution": cfg.RESOLUTION, "max": cfg.HISTORY_BARS},
    )
    if r.status_code != 200:
        log(epic + ": price fetch failed " + str(r.status_code) + " " + r.text)
        return pd.DataFrame()

    rows = r.json().get("prices", [])
    recs = []
    for p in rows:
        # Each field has bid/ask; use mid. Guard against nulls on illiquid bars.
        def mid(field):
            b = p[field].get("bid")
            a = p[field].get("ask")
            if b is None and a is None:
                return None
            if b is None:
                return a
            if a is None:
                return b
            return (b + a) / 2.0
        recs.append({
            "time": p.get("snapshotTimeUTC") or p.get("snapshotTime"),
            "high": mid("highPrice"),
            "low": mid("lowPrice"),
            "close": mid("closePrice"),
        })
    df = pd.DataFrame(recs).dropna()
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    return df


# ---------------------------------------------------------------------------
# Signal  (compute_signal reused unchanged from IBKR code; ATR added)
# ---------------------------------------------------------------------------
def compute_signal(prices, rsi_window=14):
    ema9 = prices.ewm(span=9, adjust=False).mean()
    ema21 = prices.ewm(span=21, adjust=False).mean()
    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=rsi_window).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=rsi_window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return ema9.iloc[-1], ema21.iloc[-1], rsi.iloc[-1]


def compute_atr(df, window=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=window).mean().iloc[-1]


# ---------------------------------------------------------------------------
# State  (per-epic files, same pattern as IBKR)
#   position:  1 long, -1 short, 0 flat
# ---------------------------------------------------------------------------
def load_state(state_file):
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            state = json.load(f)
            state.setdefault("init_stop_distance", 0.0)
            state.setdefault("stop_locked", False)
            return state
    return {"position": 0, "entry_price": 0.0, "deal_id": "", "init_stop_distance": 0.0, "stop_locked": False}


def save_state(state_file, position, entry_price, deal_id, init_stop_distance=0.0, stop_locked=False):
    with open(state_file, "w") as f:
        json.dump({
            "position": position,
            "entry_price": entry_price,
            "deal_id": deal_id,
            "init_stop_distance": init_stop_distance,
            "stop_locked": stop_locked,
        }, f)


def log_trade(label, direction, entry_price, exit_price, size):
    """Append a closed round-trip to the trade history the optimizer reads from.
    Prices are the same candle-close mid values used throughout (not exact fills),
    so pnl is an approximation -- consistent, not exact to the cent."""
    sign = 1 if direction == "LONG" else -1
    pnl_points = (exit_price - entry_price) * sign
    record = {
        "time": datetime.now().isoformat(),
        "label": label,
        "direction": direction,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "size": size,
        "pnl_points": pnl_points,
        "pnl": pnl_points * size,
    }
    with open(TRADE_HISTORY_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def session_realized_pnl(since_iso):
    """Sum of closed-trade pnl from trade_history.jsonl since a given ISO timestamp."""
    if not os.path.exists(TRADE_HISTORY_FILE):
        return 0.0
    total = 0.0
    with open(TRADE_HISTORY_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec["time"] >= since_iso:
                    total += rec["pnl"]
            except Exception:
                continue
    return total


def close_all_open_positions(session):
    """Close every open position and reset local state to flat -- used when the
    session profit target is hit, to lock in the gain rather than risk giving it back."""
    epic_to_label = {inst["epic"]: (label, inst) for label, inst in cfg.INSTRUMENTS.items()}
    for epic, pos in get_open_positions(session).items():
        label, inst = epic_to_label.get(epic, (epic, None))
        deal_id, direction, size = pos.get("dealId"), pos.get("direction"), pos.get("size")
        if not (deal_id and direction and size):
            continue
        if close_position(session, epic, deal_id, direction, size):
            if inst:
                save_state(inst["state_file"], 0, 0.0, "")
            log(str(label) + ": closed to lock in session profit target.")


def get_open_positions(session):
    """Returns {epic: {'direction': 'BUY'/'SELL', 'size': float, 'dealId': str, 'level': float}}."""
    r = session.request("GET", "/api/v1/positions")
    if r.status_code != 200:
        log("Position fetch failed: " + str(r.status_code))
        return {}
    out = {}
    for item in r.json().get("positions", []):
        pos = item.get("position", {})
        market = item.get("market", {})
        epic = market.get("epic")
        if epic:
            out[epic] = {
                "direction": pos.get("direction"),
                "size": pos.get("size"),
                "dealId": pos.get("dealId"),
                "level": pos.get("level"),
            }
    return out


def reconcile_state(label, epic, state_file, broker_positions):
    """Same intent as the IBKR reconcile: if local and broker disagree, trust broker."""
    state = load_state(state_file)
    local = state["position"]
    broker = broker_positions.get(epic)

    if local != 0 and broker is None:
        log(label + ": STATE MISMATCH - local says in position, broker flat. Resetting to flat.")
        save_state(state_file, 0, 0.0, "")
        return load_state(state_file)

    if local == 0 and broker is not None:
        direction = 1 if broker["direction"] == "BUY" else -1
        log(label + ": STATE MISMATCH - broker holds a position not tracked locally. Adopting.")
        # stop_locked=True: we don't know this position's original stop distance, so
        # leave its broker-side stop alone rather than guess.
        save_state(state_file, direction, broker.get("level", 0.0) or 0.0, broker.get("dealId", ""),
                   stop_locked=True)
        return load_state(state_file)

    # Both in position but directions disagree -> trust broker.
    if local != 0 and broker is not None:
        broker_dir = 1 if broker["direction"] == "BUY" else -1
        if broker_dir != local:
            log(label + ": STATE MISMATCH - direction disagreement. Adopting broker.")
            save_state(state_file, broker_dir, broker.get("level", 0.0) or 0.0, broker.get("dealId", ""),
                       stop_locked=True)
            return load_state(state_file)

    return state


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def _parse_stop_bound(error_text):
    """Capital.com embeds the actual required stop LEVEL in the errorCode string,
    e.g. 'error.invalid.stoploss.maxvalue: 74.8549'. Extract (bound_type, value)."""
    m = re.search(r"stoploss\.(maxvalue|minvalue):\s*([\d.]+)", error_text)
    if not m:
        return None, None
    return m.group(1), float(m.group(2))


def open_position(session, epic, direction, size, stop_distance, profit_distance, price):
    """direction: 'BUY' or 'SELL'. stop_distance/profit_distance in price points (server-side).
    price is the reference price, used to convert a rejected stop LEVEL bound back into a distance."""
    def submit(sd, pd_):
        body = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "guaranteedStop": False,
            "stopDistance": round(sd, 6),
            "profitDistance": round(pd_, 6),
        }
        return session.request("POST", "/api/v1/positions", json=body)

    # Distance was too tight for Capital.com's enforced minimum. The error tells us the
    # actual boundary level, so back out the required distance and retry, widening the
    # buffer each time -- the mid-price we compute from is an underestimate for SHORTs
    # (filled at bid) and overestimate for LONGs (filled at offer), so one pass often isn't enough.
    sd, pd_ = stop_distance, profit_distance
    r = submit(sd, pd_)
    attempt = 1
    while r.status_code not in (200, 201) and attempt <= 3:
        bound_type, bound_value = _parse_stop_bound(r.text)
        if bound_value is None:
            break
        required = (price - bound_value) if bound_type == "maxvalue" else (bound_value - price)
        buffer_mult = 1.1 + 0.2 * attempt
        buffer_add = price * 0.002 * attempt
        sd = max(sd, required) * buffer_mult + buffer_add
        pd_ = sd * cfg.TP_RR_MULT
        log(epic + ": stop too tight (" + r.text + "), retry #" + str(attempt) + " stopDist=" + str(round(sd, 6)))
        r = submit(sd, pd_)
        attempt += 1

    if r.status_code not in (200, 201):
        log(epic + ": OPEN failed " + str(r.status_code) + " " + r.text)
        return None
    # Capital.com returns a dealReference; confirm via /confirms for the dealId + status.
    deal_ref = r.json().get("dealReference")
    return confirm_deal(session, epic, deal_ref)


def amend_stop_level(session, epic, deal_id, new_stop_level):
    """Move an open position's server-side stop to lock in profit. Capital.com's
    trailingStopsPreference is NOT_AVAILABLE on these instruments, so this is a manual
    equivalent: only ever moves the stop in the favorable direction, never loosens it."""
    r = session.request("PUT", "/api/v1/positions/" + deal_id, json={"stopLevel": round(new_stop_level, 6)})
    if r.status_code not in (200, 201):
        log(epic + ": stop amend failed " + str(r.status_code) + " " + r.text)
        return False
    log(epic + ": stop moved to " + str(round(new_stop_level, 6)) + " to lock in profit")
    return True


def close_position(session, epic, deal_id, direction, size):
    """Close by submitting the opposite direction on the dealId."""
    opp = "SELL" if direction == "BUY" else "BUY"
    # Capital.com closes positions via DELETE /positions/{dealId}
    r = session.request("DELETE", "/api/v1/positions/" + deal_id)
    if r.status_code not in (200, 201):
        log(epic + ": CLOSE failed " + str(r.status_code) + " " + r.text)
        return False
    deal_ref = r.json().get("dealReference")
    confirm_deal(session, epic, deal_ref)
    return True


def confirm_deal(session, epic, deal_ref):
    """Poll /confirms/{dealReference} to see whether the deal actually opened/closed."""
    if not deal_ref:
        return None
    time.sleep(1.0)
    r = session.request("GET", "/api/v1/confirms/" + deal_ref)
    if r.status_code != 200:
        log(epic + ": confirm lookup failed " + str(r.status_code))
        return None
    data = r.json()
    status = data.get("dealStatus")
    if status != "ACCEPTED":
        reason = data.get("reason", "unknown")
        log(epic + ": deal REJECTED (" + str(reason) + ")")
        return None
    deal_id = data.get("affectedDeals", [{}])[0].get("dealId") or data.get("dealId")
    log(epic + ": deal ACCEPTED dealId=" + str(deal_id))
    return deal_id


# ---------------------------------------------------------------------------
# Per-instrument logic
# ---------------------------------------------------------------------------
def process_instrument(session, label, inst, broker_positions):
    epic = inst["epic"]
    size = inst["size"]
    state_file = inst["state_file"]

    df = get_prices(session, epic)
    if df.empty or len(df) < 30:
        log(label + ": insufficient data (" + str(len(df)) + " rows). Skipping.")
        return None

    state = reconcile_state(label, epic, state_file, broker_positions)

    ema9, ema21, rsi = compute_signal(df["close"])
    atr = compute_atr(df, cfg.ATR_WINDOW)
    price = df["close"].iloc[-1]

    if atr is None or (isinstance(atr, float) and math.isnan(atr)):
        log(label + ": ATR not ready. Skipping.")
        return None

    # Per-instrument overrides let the optimizer suggest instrument-specific tuning
    # without touching the global defaults other instruments still rely on.
    atr_mult = inst.get("atr_mult", cfg.ATR_MULT)
    tp_rr_mult = inst.get("tp_rr_mult", cfg.TP_RR_MULT)
    rsi_entry_min = inst.get("rsi_entry_min", cfg.RSI_ENTRY_MIN)
    rsi_entry_max = inst.get("rsi_entry_max", cfg.RSI_ENTRY_MAX)
    rsi_exit_threshold = inst.get("rsi_exit_threshold", cfg.RSI_EXIT_THRESHOLD)

    stop_distance = max(atr_mult * atr, price * 0.001)  # floor so low-priced instruments never submit ~0
    profit_distance = tp_rr_mult * stop_distance

    log(label + ": price=" + str(round(price, 2))
        + " EMA9=" + str(round(ema9, 2)) + " EMA21=" + str(round(ema21, 2))
        + " RSI=" + str(round(rsi, 2)) + " ATR=" + str(round(atr, 2)))

    position = state["position"]
    deal_id = state.get("deal_id", "")
    entry_price = state.get("entry_price", 0.0)

    long_entry = ema9 > ema21 and rsi_entry_min < rsi < rsi_entry_max
    short_entry = ema9 < ema21 and (100 - rsi_entry_min) > rsi > (100 - rsi_entry_max)
    long_exit = ema9 < ema21 or rsi < rsi_exit_threshold
    short_exit = ema9 > ema21 or rsi > (100 - rsi_exit_threshold)

    # News sentiment is a FILTER only -- it can veto an entry the technicals already
    # signaled, never trigger one on its own. A dead/empty feed scores neutral (0),
    # so a network hiccup fails open rather than silently blocking every trade.
    if long_entry or short_entry:
        sentiment_score, matched = sentiment.get_sentiment(label)
        if long_entry and sentiment_score <= -cfg.SENTIMENT_BLOCK_THRESHOLD:
            log(label + ": LONG signal blocked by negative news sentiment (score="
                + str(sentiment_score) + ", " + str(len(matched)) + " matched headlines).")
            long_entry = False
        if short_entry and sentiment_score >= cfg.SENTIMENT_BLOCK_THRESHOLD:
            log(label + ": SHORT signal blocked by positive news sentiment (score="
                + str(sentiment_score) + ", " + str(len(matched)) + " matched headlines).")
            short_entry = False

    # FLAT -> maybe enter
    if position == 0:
        if TRADING_HALTED:
            log(label + ": flat, trading halted (session profit target reached).")
        elif long_entry:
            log(label + ": LONG ENTRY. size=" + str(size) + " stopDist=" + str(round(stop_distance, 2))
                + " profitDist=" + str(round(profit_distance, 2)))
            did = open_position(session, epic, "BUY", size, stop_distance, profit_distance, price)
            if did:
                save_state(state_file, 1, price, did, stop_distance, False)
                return "🟢 LONG " + label + " @ " + str(round(price, 2))
        elif short_entry:
            log(label + ": SHORT ENTRY. size=" + str(size) + " stopDist=" + str(round(stop_distance, 2))
                + " profitDist=" + str(round(profit_distance, 2)))
            did = open_position(session, epic, "SELL", size, stop_distance, profit_distance, price)
            if did:
                save_state(state_file, -1, price, did, stop_distance, False)
                return "🔴 SHORT " + label + " @ " + str(round(price, 2))
        else:
            log(label + ": flat, no entry.")
        return None

    # LONG -> maybe exit, else consider locking in profit
    if position == 1:
        if long_exit:
            log(label + ": LONG EXIT.")
            if close_position(session, epic, deal_id, "BUY", size):
                save_state(state_file, 0, 0.0, "")
                log_trade(label, "LONG", entry_price, price, size)
                _reevaluate_now()
                return "⚪ CLOSE LONG " + label + " @ " + str(round(price, 2))
        else:
            init_stop = state.get("init_stop_distance", 0.0)
            favorable_move = price - entry_price
            if (not state.get("stop_locked") and init_stop > 0
                    and favorable_move >= init_stop * cfg.PROFIT_LOCK_TRIGGER_MULT):
                new_stop_level = entry_price + init_stop * cfg.PROFIT_LOCK_MULT
                if amend_stop_level(session, epic, deal_id, new_stop_level):
                    save_state(state_file, 1, entry_price, deal_id, init_stop, True)
            log(label + ": holding long.")
        return None

    # SHORT -> maybe exit, else consider locking in profit
    if position == -1:
        if short_exit:
            log(label + ": SHORT EXIT.")
            if close_position(session, epic, deal_id, "SELL", size):
                save_state(state_file, 0, 0.0, "")
                log_trade(label, "SHORT", entry_price, price, size)
                _reevaluate_now()
                return "⚪ CLOSE SHORT " + label + " @ " + str(round(price, 2))
        else:
            init_stop = state.get("init_stop_distance", 0.0)
            favorable_move = entry_price - price
            if (not state.get("stop_locked") and init_stop > 0
                    and favorable_move >= init_stop * cfg.PROFIT_LOCK_TRIGGER_MULT):
                new_stop_level = entry_price - init_stop * cfg.PROFIT_LOCK_MULT
                if amend_stop_level(session, epic, deal_id, new_stop_level):
                    save_state(state_file, -1, entry_price, deal_id, init_stop, True)
            log(label + ": holding short.")
        return None

    return None


# ---------------------------------------------------------------------------
# Startup verification
# ---------------------------------------------------------------------------
def verify_epics(session):
    log("Verifying epics (UNVERIFIED until confirmed against your account)...")
    for label, inst in cfg.INSTRUMENTS.items():
        results = search_markets(session, label if label != "BTC" else "bitcoin")
        log(label + " configured epic='" + inst["epic"] + "'. Search candidates:")
        for epic, name, status in results[:5]:
            flag = "  <-- MATCHES CONFIG" if epic == inst["epic"] else ""
            log("    " + str(epic) + " | " + str(name) + " | " + str(status) + flag)


# ---------------------------------------------------------------------------
# Strategy optimizer  (suggest-only: writes recommendations, never edits config)
# ---------------------------------------------------------------------------
def _read_recent_trades(window_seconds):
    if not os.path.exists(TRADE_HISTORY_FILE):
        return []
    cutoff = time.time() - window_seconds
    trades = []
    with open(TRADE_HISTORY_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                t = datetime.fromisoformat(rec["time"]).timestamp()
                if t >= cutoff:
                    trades.append(rec)
            except Exception:
                continue
    return trades


def _suggest_for_instrument(inst, trades):
    """Rule-based heuristic, not a statistical optimizer -- with only a few hours of
    trades per instrument, real parameter fitting would just be fitting to noise.
    This surfaces the raw stats plus one explainable, conservative suggestion."""
    n = len(trades)
    stats = {"n_trades": n}
    if n < cfg.MIN_TRADES_FOR_SUGGESTION:
        stats["note"] = ("insufficient data (n=" + str(n) + ", need >= "
                          + str(cfg.MIN_TRADES_FOR_SUGGESTION) + "), no suggestion")
        return stats

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(wins) / n
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    pf_display = round(profit_factor, 2) if profit_factor != float("inf") else "inf"

    stats.update({
        "win_rate": round(win_rate, 2),
        "avg_win": round(gross_win / len(wins), 4) if wins else 0.0,
        "avg_loss": round(gross_loss / len(losses), 4) if losses else 0.0,
        "profit_factor": pf_display,
        "total_pnl": round(total_pnl, 4),
    })

    atr_mult = inst.get("atr_mult", cfg.ATR_MULT)
    tp_rr_mult = inst.get("tp_rr_mult", cfg.TP_RR_MULT)
    rsi_entry_min = inst.get("rsi_entry_min", cfg.RSI_ENTRY_MIN)
    rsi_entry_max = inst.get("rsi_entry_max", cfg.RSI_ENTRY_MAX)

    if profit_factor < 0.7:
        new_atr_mult = round(atr_mult + 0.5, 2)
        new_min = min(rsi_entry_min + 5, 60)
        new_max = max(rsi_entry_max - 5, new_min + 10)
        stats["suggestion"] = (
            "Weak profit factor (" + str(pf_display) + ") on " + str(n) + " trades. "
            "Suggest widening the stop and tightening entry selectivity: "
            "atr_mult " + str(atr_mult) + " -> " + str(new_atr_mult) + ", "
            "rsi_entry_min " + str(rsi_entry_min) + " -> " + str(new_min) + ", "
            "rsi_entry_max " + str(rsi_entry_max) + " -> " + str(new_max)
        )
    elif profit_factor < 1.3:
        stats["suggestion"] = ("Roughly breakeven (profit factor " + str(pf_display)
                                + "). No change suggested -- let more trades accumulate.")
    else:
        new_tp = round(tp_rr_mult + 0.25, 2)
        stats["suggestion"] = (
            "Profitable (profit factor " + str(pf_display) + ") on " + str(n) + " trades. "
            "Suggest letting winners run further: tp_rr_mult " + str(tp_rr_mult) + " -> " + str(new_tp)
        )

    return stats


def analyze_and_suggest(trigger="backstop timer"):
    trades = _read_recent_trades(cfg.OPTIMIZE_WINDOW_SECONDS)
    by_label = {}
    for t in trades:
        by_label.setdefault(t["label"], []).append(t)

    report = {
        "time": datetime.now().isoformat(),
        "trigger": trigger,
        "window_hours": cfg.OPTIMIZE_WINDOW_SECONDS / 3600,
        "instruments": {label: _suggest_for_instrument(inst, by_label.get(label, []))
                         for label, inst in cfg.INSTRUMENTS.items()},
    }

    with open(SUGGESTIONS_FILE, "w") as f:
        json.dump(report, f, indent=2)

    log("=== Strategy suggestions (" + trigger + ") written to " + SUGGESTIONS_FILE
        + " (review manually -- nothing auto-applied) ===")
    for label, stats in report["instruments"].items():
        log("  " + label + ": " + stats.get("suggestion", stats.get("note", "")))


def _reevaluate_now():
    """Re-run the optimizer immediately -- called right after a trade closes, since
    that's new data actually worth reacting to, rather than waiting on a fixed clock."""
    try:
        analyze_and_suggest(trigger="trade closed")
    except Exception as e:
        log("Optimizer error: " + str(e))


def optimizer_loop(stop_event):
    """Backstop only: the real trigger is _reevaluate_now() after each closed trade.
    This just keeps suggestions from going stale during a stretch with no trades."""
    while not stop_event.is_set():
        stop_event.wait(cfg.OPTIMIZE_BACKSTOP_SECONDS)
        if stop_event.is_set():
            break
        try:
            analyze_and_suggest(trigger="backstop timer")
        except Exception as e:
            log("Optimizer error: " + str(e))


# ---------------------------------------------------------------------------
# Main loop  (option B: long-running, wakes on 5-min boundary)
# ---------------------------------------------------------------------------
def seconds_to_next_boundary(bar_seconds):
    now = time.time()
    return bar_seconds - (now % bar_seconds)


def main():
    global SESSION_START_TIME, TRADING_HALTED
    if os.path.exists(SESSION_START_FILE):
        with open(SESSION_START_FILE, "r") as f:
            SESSION_START_TIME = f.read().strip()
    else:
        SESSION_START_TIME = datetime.now().isoformat()
        with open(SESSION_START_FILE, "w") as f:
            f.write(SESSION_START_TIME)
    TRADING_HALTED = session_realized_pnl(SESSION_START_TIME) >= cfg.PROFIT_TARGET_USD
    if TRADING_HALTED:
        log("=== Session profit target already reached before this restart -- starting halted. "
            "Delete " + SESSION_START_FILE + " to begin a fresh session. ===")

    session = CapitalSession()
    session.login()

    verify_epics(session)
    log(">>> Review the epic candidates above. If any config epic is wrong, stop now "
        "(Ctrl-C), fix capital_config.py INSTRUMENTS, and restart before trusting orders.")

    stop_event = threading.Event()
    ka = threading.Thread(target=keepalive_loop, args=(session, stop_event), daemon=True)
    ka.start()
    opt = threading.Thread(target=optimizer_loop, args=(stop_event,), daemon=True)
    opt.start()

    log("=== Capital.com crypto daemon started (demo=" + str(cfg.USE_DEMO) + ") ===")
    send_telegram("🔬 *Capital.com crypto daemon started* (demo=" + str(cfg.USE_DEMO) + ")")

    try:
        while True:
            wait = seconds_to_next_boundary(cfg.BAR_SECONDS)
            time.sleep(wait + 2)  # +2s so the just-closed bar is available from the API

            cycle_msgs = []
            try:
                broker_positions = get_open_positions(session)
                for label, inst in cfg.INSTRUMENTS.items():
                    try:
                        msg = process_instrument(session, label, inst, broker_positions)
                        if msg:
                            cycle_msgs.append(msg)
                    except Exception as e:
                        log(label + ": EXCEPTION - " + str(e))
                        cycle_msgs.append("⚠️ " + label + " error: " + str(e))
            except Exception as e:
                log("CYCLE EXCEPTION - " + str(e))

            if not TRADING_HALTED:
                pnl = session_realized_pnl(SESSION_START_TIME)
                if pnl >= cfg.PROFIT_TARGET_USD:
                    log("=== SESSION PROFIT TARGET HIT: $" + str(round(pnl, 2))
                        + " >= $" + str(cfg.PROFIT_TARGET_USD)
                        + ". Closing all positions, halting new entries until restart. ===")
                    close_all_open_positions(session)
                    TRADING_HALTED = True
                    cycle_msgs.append("🎯 Session profit target hit: $" + str(round(pnl, 2))
                                       + ". All positions closed, trading halted until restart.")

            if cycle_msgs:
                send_telegram("🔬 *Capital.com crypto*\n" + "\n".join(cycle_msgs))

    except KeyboardInterrupt:
        log("Interrupted. Shutting down.")
    finally:
        stop_event.set()
        log("=== daemon stopped ===")


if __name__ == "__main__":
    main()
