# swingbot.py
import os
import time

from notifier import notify
from logger import log

from strategies.swing_strategy import generate_swing_signal

SYMBOL = os.getenv("SYMBOL", "BTC")

# ---- you already have something like this in signalbot.py ----
# You can lift these from your existing bot:
# - WS connection
# - 5m candle builder
# - historical bootstrap (if you do it)
# For now, this file focuses on how to evaluate swing logic cleanly.

def aggregate_from_5m(c5m, group_n: int):
    """
    Aggregate 5m candles into higher TF.
    c5m is list of dicts: {"t":..., "open":..., "high":..., "low":..., "close":..., "volume":...}
    Returns arrays dict with open/high/low/close/volume.
    """
    out = {"open": [], "high": [], "low": [], "close": [], "volume": []}
    total = len(c5m)
    usable = (total // group_n) * group_n
    if usable < group_n:
        return out

    for i in range(0, usable, group_n):
        chunk = c5m[i:i+group_n]
        out["open"].append(chunk[0]["open"])
        out["close"].append(chunk[-1]["close"])
        out["high"].append(max(x["high"] for x in chunk))
        out["low"].append(min(x["low"] for x in chunk))
        out["volume"].append(sum(x.get("volume", 0) for x in chunk))
    return out

def main():
    notify(f"🟦 SwingBot live for {SYMBOL} | 4H bias / 1H BOS(strict) / 15m entry")

    last_signal_key = None  # dedupe per 15m close

    # TODO: replace with your actual 5m candle list and WS updates
    c5m = []  # list of 5m candles dicts

    while True:
        # TODO: your WS loop updates c5m in real-time.
        # We only *evaluate* when a 15m candle has just closed.
        # If you already have an "on_candle_close(5m)" hook, use it.

        time.sleep(1)

        if len(c5m) < 48 * 60:  # rough: enough 5m bars to form meaningful 4H / pivots (adjust)
            continue

        # Build higher TF candles from 5m
        c15 = aggregate_from_5m(c5m, 3)
        c1h = aggregate_from_5m(c5m, 12)
        c4h = aggregate_from_5m(c5m, 48)

        # Only evaluate once per new 15m close
        # Use length of c15 as a proxy for "new 15m candle formed"
        current_key = (len(c15["close"]),)
        if current_key == last_signal_key:
            continue
        last_signal_key = current_key

        sig = generate_swing_signal(
            symbol=SYMBOL,
            c4h=c4h,
            c1h=c1h,
            c15m=c15,
            pivot_len=5,
            atr_len=14,
            atr_buf_mult=0.25,
            tp_r=1.0,
            require_breakout_candle=True,
        )

        if sig:
            msg = (
                f"📣 SWING SIGNAL {sig.symbol}\n"
                f"Side: {sig.side}\n"
                f"Entry: {sig.entry:.4f}\n"
                f"Stop: {sig.stop:.4f}\n"
                f"TP1: {sig.tp1:.4f}\n"
                f"{sig.reason}"
            )
            notify(msg)
            log("TRADE", {"event": "swing_signal", "symbol": sig.symbol, "side": sig.side, "entry": sig.entry, "stop": sig.stop, "tp1": sig.tp1})

if __name__ == "__main__":
    main()
