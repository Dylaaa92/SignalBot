import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")


# Runtime configuration via environment variables
SYMBOL = os.getenv("SYMBOL", "BTC")
ENV = os.getenv("ENV", "mainnet")  # default to mainnet

# Websocket endpoints
WS_MAINNET = "wss://api.hyperliquid.xyz/ws"
WS_TESTNET = "wss://api.hyperliquid-testnet.xyz/ws"

WS_URL = WS_MAINNET if ENV == "mainnet" else WS_TESTNET


# Execution timeframe
TF_SECONDS = 300  # 5m

# Risk
RISK_USDT_PER_TRADE = 5.0
DAILY_MAX_LOSS_USDT = 20.0
MAX_CONSEC_LOSSES = 1
COOLDOWN_SECONDS = 3 * 60 * 60  # 3 hours

# --- Per-symbol guardrails (percent of entry) ---
SYMBOL_PROFILES = {
    # Majors
    "BTC":    {"min_stop_pct": 0.30/100, "max_stop_pct": 1.50/100, "stop_buffer_pct": 0.05/100},
    "ETH":    {"min_stop_pct": 0.35/100, "max_stop_pct": 1.80/100, "stop_buffer_pct": 0.06/100},
    "SOL":    {"min_stop_pct": 0.45/100, "max_stop_pct": 2.20/100, "stop_buffer_pct": 0.08/100},

    # Higher volatility / more noise
    "JUP":    {"min_stop_pct": 0.60/100, "max_stop_pct": 3.00/100, "stop_buffer_pct": 0.10/100},
    "COIN":   {"min_stop_pct": 0.60/100, "max_stop_pct": 3.00/100, "stop_buffer_pct": 0.10/100},

    # Metals (often steadier, but can spike around macro)
    "GOLD":   {"min_stop_pct": 0.20/100, "max_stop_pct": 1.00/100, "stop_buffer_pct": 0.03/100},
    "SILVER": {"min_stop_pct": 0.30/100, "max_stop_pct": 1.40/100, "stop_buffer_pct": 0.04/100},
}

# Fallback if a symbol isn't listed
DEFAULT_PROFILE = {"min_stop_pct": 0.30/100, "max_stop_pct": 2.00/100, "stop_buffer_pct": 0.05/100}


# Pivot (swing) settings
PIVOT_L = 2
STOP_BUFFER_PCT = 0.05 / 100  # 0.05%

# Trigger
EMA_FAST = 9
EMA_SLOW = 21

# Websocket endpoints
WS_MAINNET = "wss://api.hyperliquid.xyz/ws"
WS_TESTNET = "wss://api.hyperliquid-testnet.xyz/ws"

# Paper trading realism (conservative defaults)
TAKER_FEE_PCT = 0.04 / 100    # 0.04% per side (we can tune later)
ENTRY_SLIPPAGE_PCT = 0.02 / 100
STOP_SLIPPAGE_PCT = 0.05 / 100

TP1_R_MULT = 1.0
TP1_FRACTION = 0.5
TP_SLIPPAGE_PCT = 0.01 / 100  # small slippage on TP fills
BE_BUFFER_PCT = 0.01 / 100    # tiny buffer above entry for BE stop

# Notifier params
RETEST_BUF_ATR = 0.15
ACCEPT_BARS = 2
ATR_LEN = 14



