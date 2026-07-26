"""
Capital.com configuration.

DO NOT COMMIT THIS FILE. Add to .gitignore alongside email_config.py / telegram_config.py.
Copy this file to capital_config.py and fill in your own credentials.

Three secrets are required for Capital.com API auth:
  1. CAPITAL_API_KEY      - generated in Settings > API integrations in the web UI
  2. CAPITAL_IDENTIFIER   - your account login email
  3. CAPITAL_PASSWORD     - the API-specific password you set when creating the API key
                            (this is NOT your normal login password; it is set per-key)

The demo and live endpoints are different base URLs. Keep USE_DEMO = True until validated.
"""

CAPITAL_API_KEY = "PUT_YOUR_API_KEY_HERE"
CAPITAL_IDENTIFIER = "PUT_YOUR_LOGIN_EMAIL_HERE"
CAPITAL_PASSWORD = "PUT_YOUR_API_PASSWORD_HERE"

USE_DEMO = True

DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"
LIVE_BASE_URL = "https://api-capital.backend-capital.com"

BASE_URL = DEMO_BASE_URL if USE_DEMO else LIVE_BASE_URL

# ---------------------------------------------------------------------------
# EPIC MAP  -  UNVERIFIED. Confirm each epic on first run before trading.
#
# Capital.com identifies instruments by "epic", not by symbol/exchange.
# Epic names vary by jurisdiction and account. The values below are the
# COMMON forms but are NOT guaranteed for your account.
#
# On first run the script calls search_markets() for each and logs what it
# finds. Correct this dict before enabling order placement.
#
# "size" is the CFD position size in units, NOT a share count. 1 unit of
# BTCUSD is a very different notional than 1 unit of SOLUSD, so these are
# not comparable across instruments. Start tiny on demo and size up only
# after you have seen the per-epic notional in the position response.
# ---------------------------------------------------------------------------
#
# STATE FILES ARE PREFIXED "capital_" -- this differs from the standalone
# capital.com-api-trading-system repo, and deliberately so. Here the daemon
# shares a directory with the IBKR scripts, which write their own
# <symbol>_position_state.json files. Unprefixed, a Capital.com NVDA CFD and
# the IBKR NVDA equity position would share nvda_position_state.json: on the
# first cycle reconcile_state would read the IBKR position, find Capital.com
# flat, and overwrite that file to flat -- after which the IBKR script would
# believe it held nothing and could re-buy a position it already has. The
# prefix keeps the two systems' state disjoint no matter which tickers overlap.
# ---------------------------------------------------------------------------
INSTRUMENTS = {
    "BTC": {"epic": "BTCUSD", "size": 0.005, "state_file": "capital_btc_5min_position_state.json"},
    "ETH": {"epic": "ETHUSD", "size": 0.10, "state_file": "capital_eth_5min_position_state.json"},
    "SOL": {"epic": "SOLUSD", "size": 1.00, "state_file": "capital_sol_5min_position_state.json"},
    "XRP": {"epic": "XRPUSD", "size": 100.0, "state_file": "capital_xrp_5min_position_state.json"},
    "LTC": {"epic": "LTCUSD", "size": 2.0, "state_file": "capital_ltc_5min_position_state.json"},
    "BCH": {"epic": "BCHUSD", "size": 0.5, "state_file": "capital_bch_5min_position_state.json"},
    # Add/remove instruments freely -- any Capital.com epic works here, sized to
    # taste. BNB is omitted in this example after a live run showed a very high
    # order-rejection rate for it; your account's behavior may differ.
    "NASDAQ100": {"epic": "US100", "size": 0.005, "state_file": "capital_nasdaq100_position_state.json"},
    "SP500": {"epic": "US500", "size": 0.02, "state_file": "capital_sp500_position_state.json"},
    "NIKKEI225": {"epic": "J225", "size": 0.002, "state_file": "capital_nikkei225_position_state.json"},
    "OIL": {"epic": "OIL_CRUDE", "size": 1.0, "state_file": "capital_oil_position_state.json"},
    "GOLD": {"epic": "GOLD", "size": 0.025, "state_file": "capital_gold_position_state.json"},
    "SILVER": {"epic": "SILVER", "size": 1.7, "state_file": "capital_silver_position_state.json"},
    # Forex majors -- size is in base-currency units, so notional = size * price
    # directly for EUR/GBP pairs, and size itself for USD-base pairs like USDJPY.
    "EURUSD": {"epic": "EURUSD", "size": 100.0, "state_file": "capital_eurusd_position_state.json"},
    "GBPUSD": {"epic": "GBPUSD", "size": 100.0, "state_file": "capital_gbpusd_position_state.json"},
    "USDJPY": {"epic": "USDJPY", "size": 100.0, "state_file": "capital_usdjpy_position_state.json"},
    # Blue-chip stocks -- won't trade until NYSE/NASDAQ hours (closed all weekend).
    # NOTE: the IBKR daily strategy already trades AAPL and NVDA in this same
    # directory. Enabling them here runs two independent systems on one ticker,
    # doubling real exposure even though each side's state looks like one position.
    "AAPL": {"epic": "AAPL", "size": 0.3, "state_file": "capital_aapl_position_state.json"},
    "TSLA": {"epic": "TSLA", "size": 0.3, "state_file": "capital_tsla_position_state.json"},
    "NVDA": {"epic": "NVDA", "size": 0.5, "state_file": "capital_nvda_position_state.json"},
}

# Strategy parameters (mirrors the IBKR setup)
RSI_ENTRY_MIN = 50
RSI_ENTRY_MAX = 70
RSI_EXIT_THRESHOLD = 40

# ATR-based stop. Distance = ATR_MULT * ATR(ATR_WINDOW) on 5-min bars,
# submitted to Capital.com as a server-side stopDistance in price points.
ATR_WINDOW = 14
ATR_MULT = 2.0

# Take-profit distance = TP_RR_MULT * stop distance, submitted as a server-side
# profitDistance alongside the stop. 1.2 => risk:reward of 1:1.2 -- a nearer target
# that's easier to actually reach, favoring frequent small wins over occasional
# bigger ones (most exits happen via RSI/EMA reversal before hitting TP anyway).
TP_RR_MULT = 1.2

# Profit lock: once a trade moves favorably by >= PROFIT_LOCK_TRIGGER_MULT * its
# initial stop distance, move the stop to guarantee PROFIT_LOCK_MULT of that initial
# risk as locked-in profit. One-shot per trade (not a continuous trail) -- once
# locked, a winning trade can no longer round-trip back into a loss. Trigger fires
# sooner (0.5x) and locks in more (0.5x) than before -- banks small gains earlier
# and more often rather than letting them ride toward a bigger, less certain win.
PROFIT_LOCK_TRIGGER_MULT = 0.5
PROFIT_LOCK_MULT = 0.5

# Session profit target: once realized P&L since this run started reaches this
# amount (account currency), the daemon closes every open position and stops
# opening new ones for the rest of this run. Restart the process to resume.
PROFIT_TARGET_USD = 10.0

# News sentiment filter (see sentiment.py): a signal only blocks an entry the
# technicals already fired, never triggers one on its own. Net keyword score
# across matched headlines must reach this magnitude to actually veto a trade --
# keeps a single ambiguous headline from having an outsized effect.
SENTIMENT_BLOCK_THRESHOLD = 2

# Bar resolution and how much history to pull each cycle.
RESOLUTION = "MINUTE"
HISTORY_BARS = 200          # enough to warm up EMA21 + RSI14 + ATR14

# Loop timing
BAR_SECONDS = 60            # 1 minute
KEEPALIVE_SECONDS = 300     # ping session at least every ~10 min; 5 is safe

# Strategy optimizer: analyzes the trailing OPTIMIZE_WINDOW_SECONDS of closed trades
# per instrument and writes suggested parameter tweaks to strategy_suggestions.json.
# Suggest-only -- nothing is auto-applied to this file. Instruments with fewer than
# MIN_TRADES_FOR_SUGGESTION closed trades in the window are skipped (too little data).
#
# Re-evaluation is event-driven -- it reruns immediately after every closed trade,
# since that's the only point new data actually exists. OPTIMIZE_BACKSTOP_SECONDS is
# just a fallback so suggestions don't go stale during a stretch with no trades at all.
OPTIMIZE_WINDOW_SECONDS = 7200      # lookback window for the analysis itself: 2 hours
OPTIMIZE_BACKSTOP_SECONDS = 900     # fallback re-run cadence when no trades close: 15 min
MIN_TRADES_FOR_SUGGESTION = 3
