# logger.py
import json
from datetime import datetime

LOG_LEVEL = "DEBUG"  
# options: "DEBUG", "TRADE"

# events we always want to see in TRADE mode
TRADE_EVENTS = {
    "startup",
    "bootstrapped",
    "enter_long_paper",
    "tp1_taken",
    "stop_hit",
    "disabled_daily_max_loss",
    "enter_short_paper",
    "candle_closed",
}

def log(payload: dict):
    event = payload.get("event")

    if LOG_LEVEL == "TRADE" and event not in TRADE_EVENTS:
        return  # silently ignore

    payload["ts"] = datetime.utcnow().isoformat()
    print(json.dumps(payload))

