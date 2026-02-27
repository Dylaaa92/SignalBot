import asyncio
import json
import os
import time
import websockets

from notifier import notify
from logger import log
from config import WS_URL, ENV
from strategies.swing_strategy import generate_swing_signal

SYMBOL = os.getenv("SYMBOL", "BTC")

TF_5M = 300
TF_15M = 900
TF_1H = 3600
TF_4H = 14400

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


async def swing_loop():
    print("[SWING] starting", SYMBOL)
    await notify(f"[SWING] SwingBot live for {SYMBOL} ({ENV})")
    print("[SWING] notify sent")

    cb5 = CandleBuilder(TF_5M)

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

            if len(cb5.candles) < 48 * 5:
                continue

            c15 = aggregate_from_5m(cb5.candles, 3)
            c1h = aggregate_from_5m(cb5.candles, 12)
            c4h = aggregate_from_5m(cb5.candles, 48)

            sig = generate_swing_signal(
                symbol=SYMBOL,
                c4h=c4h,
                c1h=c1h,
                c15m=c15,
                pivot_len=PIVOT_LEN,
                atr_len=ATR_LEN,
                atr_buf_mult=ATR_BUF_MULT,
                tp_r=TP_R,
                require_breakout_candle=REQUIRE_BREAKOUT_CANDLE,
            )

            if sig:
                await notify(
                    f"[SWING] {sig.symbol} {sig.side}\n"
                    f"Entry: {sig.entry}\n"
                    f"Stop: {sig.stop}\n"
                    f"TP1: {sig.tp1}"
                )
                log(
                    {
                        "event": "swing_signal",
                        "symbol": sig.symbol,
                        "side": sig.side,
                        "entry": sig.entry,
                        "stop": sig.stop,
                        "tp1": sig.tp1,
                    }
                )


def main():
    asyncio.run(swing_loop())


if __name__ == "__main__":
    main()
