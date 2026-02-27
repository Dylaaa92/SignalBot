import asyncio
import json
import os
import time
import websockets
import httpx

from notifier import notify
from logger import log
from config import WS_URL, ENV
from strategies.swing_strategy import generate_swing_signal

SYMBOL = os.getenv("SYMBOL", "BTC")

TF_5M = 300

PIVOT_LEN = 5
ATR_LEN = 14
ATR_BUF_MULT = 0.25
TP_R = 1.0
REQUIRE_BREAKOUT_CANDLE = True


class CandleBuilder:
    def __init__(self, tf_seconds):
        self.tf = tf_seconds
        self.current = None
        self.candles = []

    def _bucket(self, ts):
        return int(ts // self.tf) * self.tf

    def update(self, ts, price):
        b = self._bucket(ts)
        if self.current is None or self.current["t"] != b:
            if self.current is not None:
                self.candles.append(self.current)
            self.current = {"t": b, "o": price, "h": price, "l": price, "c": price}
        else:
            self.current["h"] = max(self.current["h"], price)
            self.current["l"] = min(self.current["l"], price)
            self.current["c"] = price

    def last_closed(self):
        return self.candles[-1] if self.candles else None


def aggregate_from_5m(c5m, group_n):
    out = {"open": [], "high": [], "low": [], "close": []}
    total = len(c5m)
    usable = (total // group_n) * group_n

    if usable < group_n:
        return out

    for i in range(0, usable, group_n):
        chunk = c5m[i : i + group_n]
        out["open"].append(chunk[0]["o"])
        out["close"].append(chunk[-1]["c"])
        out["high"].append(max(x["h"] for x in chunk))
        out["low"].append(min(x["l"] for x in chunk))

    return out


async def bootstrap_5m_candles(symbol: str, limit_days: int = 5):
    """
    Bootstrap recent 5m candles so SwingBot is immediately signal-ready.
    """
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (limit_days * 24 * 60 * 60 * 1000)

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": "5m",
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }

    url = "https://api.hyperliquid.xyz/info"

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()

    candles = []

    if isinstance(data, list):
        for x in data:
            try:
                t = int(x["t"]) // 1000
                candles.append(
                    {
                        "t": t,
                        "o": float(x["o"]),
                        "h": float(x["h"]),
                        "l": float(x["l"]),
                        "c": float(x["c"]),
                    }
                )
            except Exception:
                continue

    candles.sort(key=lambda d: d["t"])
    return candles


async def swing_loop():
    print("[SWING] starting", SYMBOL)

    await notify(
        f"[SWING] SwingBot ONLINE for {SYMBOL} ({ENV}). This is NOT a trade signal."
    )

    cb5 = CandleBuilder(TF_5M)
    last_15m_close_count = 0
    last_signal_key = None

    state = {"phase": "IDLE"}
    last_status_15m = 0
    STATUS_EVERY_N_15M = 4  # 4 = once per hour

    # -----------------------
    # Bootstrap history
    # -----------------------
    try:
        hist = await bootstrap_5m_candles(SYMBOL, limit_days=5)
        cb5.candles.extend(hist)
        await notify(
            f"[SWING] Bootstrapped {SYMBOL}: {len(hist)} x 5m candles loaded."
        )
    except Exception as e:
        await notify(f"[SWING] Bootstrap failed for {SYMBOL}: {e}")

    # -----------------------
    # Live WebSocket
    # -----------------------
    async with websockets.connect(WS_URL) as ws:
        print("[SWING] ws connected")

        sub = {"method": "subscribe", "subscription": {"type": "allMids"}}
        await ws.send(json.dumps(sub))
        print("[SWING] ws subscribed")

        while True:
            msg = await ws.recv()

            try:
                data = json.loads(msg)
            except Exception:
                continue

            if data.get("channel") != "allMids":
                continue

            mids = data.get("data", {}).get("mids")
            if not isinstance(mids, dict):
                continue

            if SYMBOL not in mids:
                continue

            try:
                price = float(mids[SYMBOL])
            except Exception:
                continue

            cb5.update(time.time(), price)

            # Require minimum history (~8 hours of 5m candles)
            if len(cb5.candles) < 48 * 2:
                continue

            c15 = aggregate_from_5m(cb5.candles, 3)

            # Evaluate only on NEW 15m close
            if len(c15["close"]) == last_15m_close_count:
                continue
            last_15m_close_count = len(c15["close"])

            c1h = aggregate_from_5m(cb5.candles, 12)
            c4h = aggregate_from_5m(cb5.candles, 48)

            await notify(
                f"[SWING][STATUS] {SYMBOL} | 15m={len(c15['close'])} 1h={len(c1h['close'])} 4h={len(c4h['close'])}"
            )

            sig, state, dbg = generate_swing_signal(
                symbol=SYMBOL,
                c4h=c4h,
                c1h=c1h,
                c15m=c15,
                state=state,
                pivot_len=PIVOT_LEN,
                atr_len=ATR_LEN,
                atr_buf_mult=ATR_BUF_MULT,
                tp_r=TP_R,
            )
            
            # Throttled status (hourly)
            if len(c15["close"]) >= last_status_15m + STATUS_EVERY_N_15M:
                last_status_15m = len(c15["close"])
                await notify(
                    f"[SWING][STATUS] {SYMBOL} phase={state.get('phase')} bias={dbg.get('bias_4h')} "
                    f"1h_close={dbg.get('last_1h_close')} sh={dbg.get('swing_high')} sl={dbg.get('swing_low')}"
                )
            
            if sig:
                key = f"{sig.symbol}:{sig.side}:{round(sig.entry,4)}:{round(sig.stop,4)}"
                if key != last_signal_key:
                    last_signal_key = key
                    await notify(
                        f"[SWING SIGNAL] {sig.symbol} {sig.side}\n"
                        f"Reason: {sig.reason}\n"
                        f"Entry: {sig.entry:.4f}\n"
                        f"Stop: {sig.stop:.4f}\n"
                        f"TP1: {sig.tp1:.4f}"
                    )
                    log(
                        {
                            "event": "swing_signal",
                            "symbol": sig.symbol,
                            "side": sig.side,
                            "entry": sig.entry,
                            "stop": sig.stop,
                            "tp1": sig.tp1,
                            "phase": state.get("phase"),
                        }
                    )


def main():
    asyncio.run(swing_loop())


if __name__ == "__main__":
    main()
