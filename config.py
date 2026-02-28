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

# Execution timeframe (default for legacy bot)
TF_SECONDS = int(os.getenv("TF_SECONDS", "300"))  # 5m legacy

# New multi-TF strategy timeframes
EXEC_TF_SECONDS   = int(os.getenv("EXEC_TF_SECONDS", "900"))    # 15m
STRUCT_TF_SECONDS = int(os.getenv("STRUCT_TF_SECONDS", "3600")) # 1h
BIAS_TF_SECONDS   = int(os.getenv("BIAS_TF_SECONDS", "14400"))  # 4h

TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()       # paper|live
STRATEGY     = os.getenv("STRATEGY", "BOS_RETEST_ACCEPT_V1")    # or INTRADAY_SWING_V2

# Risk
RISK_USDT_PER_TRADE = float(os.getenv("RISK_USDT_PER_TRADE", "5.0"))
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
    "HYPE": {"min_stop_pct": 0.70/100, "max_stop_pct": 3.50/100, "stop_buffer_pct": 0.12/100},

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

# Intraday Swing v2 params (Pine mirror)
PIVOT_1H_LEN = int(os.getenv("PIVOT_1H_LEN", "3"))
RSI_LEN      = int(os.getenv("RSI_LEN", "14"))
RSI_LONG_MIN = int(os.getenv("RSI_LONG_MIN", "45"))
RSI_SHORT_MAX= int(os.getenv("RSI_SHORT_MAX", "55"))
ATR_BUF_MULT = float(os.getenv("ATR_BUF_MULT", "0.25"))
TP1_QTY_PCT  = float(os.getenv("TP1_QTY_PCT", "50"))
RISK_R       = float(os.getenv("RISK_R", "1.0"))
REQUIRE_1H_EMA21_SIDE = os.getenv("REQUIRE_1H_EMA21_SIDE", "1") == "1"
ALLOW_COUNTER_TREND   = os.getenv("ALLOW_COUNTER_TREND", "0") == "1"



