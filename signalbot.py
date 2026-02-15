import asyncio
import json
import time
import httpx
import websockets

from config import (
    SYMBOL, TF_SECONDS,
    EMA_FAST, EMA_SLOW,
    PIVOT_L,
    WS_URL,
    SYMBOL_PROFILES, DEFAULT_PROFILE, ENV,
)

from indicators import ema
from pivots import last_confirmed_swing_low, last_confirmed_swing_high
from logger import log
from notifier import notify

ONE_HOUR = 3600

# Strategy params (match Pine defaults)
RETEST_BUF_ATR = 0.15
ACCEPT_BARS = 2
ATR_LEN = 14
TP1_R_MULT = 1.0
TP2_R_MULT = 2.0


class CandleBuilder:
    def __init__(self, tf_seconds: int):
        self.tf = tf_seconds
        self.current = None
        self.candles = []

    def _bucket(self, ts: float) -> int:
        return int(ts // self.tf) * self.tf

    def update(self, ts: float, price: float):
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


def atr_from_candles(candles, length=14):
    if len(candles) < length + 1:
        return None
    trs = []
    for i in range(-length, 0):
        c = candles[i]
        prev = candles[i - 1]
        tr = max(
            c["h"] - c["l"],
            abs(c["h"] - prev["c"]),
            abs(c["l"] - prev["c"]),
        )
        trs.append(tr)
    return sum(trs) / len(trs)


class StructureState:
    def __init__(self):
        self.bosLevelLong = None
        self.bosLevelShort = None
        self.waitingRetestLong = False
        self.waitingRetestShort = False
        self.retestRefLong = None
        self.retestRefShort = None
        self.accCountLong = 0
        self.accCountShort = 0


class SignalTradeState:
    """
    Tracks an active signal so we can alert TP1/TP2 hits.
    No orders are placed.
    """
    def __init__(self):
        self.active = False
        self.side = None           # "LONG" / "SHORT"
        self.entry = None
        self.stop = None
        self.R = None
        self.tp1 = None
        self.tp2 = None
        self.tp1_sent = False
        self.tp2_sent = False

    def clear(self):
        self.__init__()


async def bootstrap_candles(symbol: str, tf_seconds: int, limit: int = 300):
    interval = "5m"
    url = "https://api.hyperliquid.xyz/info"
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": interval,
            "startTime": int((time.time() - limit * tf_seconds) * 1000),
            "endTime": int(time.time() * 1000),
        }
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        candles = r.json()

    out = []
    for c in candles:
        out.append({
            "t": int(c["t"] // 1000),
            "o": float(c["o"]),
            "h": float(c["h"]),
            "l": float(c["l"]),
            "c": float(c["c"]),
        })
    return out


async def main():
    structure = StructureState()
    sig = SignalTradeState()
    profile = SYMBOL_PROFILES.get(SYMBOL, DEFAULT_PROFILE)
    MIN_STOP_PCT = profile["min_stop_pct"]
    MAX_STOP_PCT = profile["max_stop_pct"]
    STOP_BUFFER_PCT = profile["stop_buffer_pct"]


    candle_5m = CandleBuilder(TF_SECONDS)
    candle_1h = CandleBuilder(ONE_HOUR)

    last_candle_t = None

    # --- bootstrap ---
    history = await bootstrap_candles(SYMBOL, TF_SECONDS, limit=300)
    if history:
        candle_5m.candles = history[:-1]
        candle_5m.current = history[-1]
        last_candle_t = candle_5m.candles[-1]["t"] if candle_5m.candles else None

        for c in candle_5m.candles[-240:]:
            candle_1h.update(c["t"] + TF_SECONDS, c["c"])

    log({"event": "bootstrapped", "candles_5m": len(candle_5m.candles), "candles_1h": len(candle_1h.candles)})

    sub_msg = {"method": "subscribe", "subscription": {"type": "allMids"}}
    log({"event": "startup", "ws": WS_URL, "symbol": SYMBOL, "mode": "signal_only"})
    await notify(f"✅ Signalbot LIVE: {SYMBOL} | ENV={ENV} | 5m exec / 1h bias")


    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(sub_msg))

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)

                    if data.get("channel") != "allMids":
                        continue

                    mids = data.get("data", {}).get("mids", {})
                    if SYMBOL not in mids:
                        continue

                    price = float(mids[SYMBOL])
                    ts = time.time()

                    candle_5m.update(ts, price)

                    closed = candle_5m.last_closed()
                    if not closed:
                        continue

                    if closed["t"] == last_candle_t:
                        continue
                    last_candle_t = closed["t"]

                    # build 1h from closes
                    candle_1h.update(closed["t"] + TF_SECONDS, closed["c"])

                    log({"event": "candle_closed", "t": closed["t"], "c": closed["c"]})

                    # ---- if an active signal exists, watch for TP hits ----
                    if sig.active:
                        if sig.side == "LONG":
                            if (not sig.tp1_sent) and closed["h"] >= sig.tp1:
                                sig.tp1_sent = True
                                await notify(f"{SYMBOL} LONG TP1 ✅ hit {sig.tp1:.2f}")
                            if (not sig.tp2_sent) and closed["h"] >= sig.tp2:
                                sig.tp2_sent = True
                                await notify(f"{SYMBOL} LONG TP2 ✅ hit {sig.tp2:.2f}")
                                sig.clear()
                                continue
                            # optional: stop invalidates signal tracking
                            if closed["l"] <= sig.stop:
                                await notify(f"{SYMBOL} LONG invalidated ❌ stop tagged {sig.stop:.2f}")
                                sig.clear()
                                continue
                        else:  # SHORT
                            if (not sig.tp1_sent) and closed["l"] <= sig.tp1:
                                sig.tp1_sent = True
                                await notify(f"{SYMBOL} SHORT TP1 ✅ hit {sig.tp1:.2f}")
                            if (not sig.tp2_sent) and closed["l"] <= sig.tp2:
                                sig.tp2_sent = True
                                await notify(f"{SYMBOL} SHORT TP2 ✅ hit {sig.tp2:.2f}")
                                sig.clear()
                                continue
                            if closed["h"] >= sig.stop:
                                await notify(f"{SYMBOL} SHORT invalidated ❌ stop tagged {sig.stop:.2f}")
                                sig.clear()
                                continue

                    # ---- need enough candles ----
                    if len(candle_5m.candles) < 120 or len(candle_1h.candles) < 40:
                        continue

                    c5 = candle_5m.candles
                    c1 = candle_1h.candles

                    closes5 = [c["c"] for c in c5][-300:]
                    highs5 = [c["h"] for c in c5][-300:]
                    lows5 = [c["l"] for c in c5][-300:]
                    closes1 = [c["c"] for c in c1][-200:]

                    efast5 = ema(closes5[-(EMA_FAST * 4):], EMA_FAST)
                    eslow5 = ema(closes5[-(EMA_SLOW * 4):], EMA_SLOW)
                    efast1 = ema(closes1[-(EMA_FAST * 4):], EMA_FAST)
                    eslow1 = ema(closes1[-(EMA_SLOW * 4):], EMA_SLOW)
                    if None in (efast5, eslow5, efast1, eslow1):
                        continue

                    biasLong = efast1 > eslow1
                    biasShort = efast1 < eslow1
                    emaTrendLong = efast5 > eslow5
                    emaTrendShort = efast5 < eslow5

                    idx_hi = last_confirmed_swing_high(highs5, PIVOT_L)
                    idx_lo = last_confirmed_swing_low(lows5, PIVOT_L)
                    if idx_hi is None or idx_lo is None:
                        continue

                    lastSwingHigh = highs5[idx_hi]
                    lastSwingLow = lows5[idx_lo]

                    prev_close = c5[-2]["c"]
                    bosUp = (prev_close <= lastSwingHigh) and (closed["c"] > lastSwingHigh)
                    bosDown = (prev_close >= lastSwingLow) and (closed["c"] < lastSwingLow)

                    a = atr_from_candles(c5, length=ATR_LEN)
                    if a is None:
                        continue
                    buf = a * RETEST_BUF_ATR

                    # BOS arms retest
                    if bosUp:
                        structure.bosLevelLong = lastSwingHigh
                        structure.waitingRetestLong = True
                        structure.waitingRetestShort = False
                        structure.bosLevelShort = None
                        structure.retestRefShort = None
                        structure.accCountShort = 0

                    if bosDown:
                        structure.bosLevelShort = lastSwingLow
                        structure.waitingRetestShort = True
                        structure.waitingRetestLong = False
                        structure.bosLevelLong = None
                        structure.retestRefLong = None
                        structure.accCountLong = 0

                    # Retest
                    retestLong = structure.waitingRetestLong and structure.bosLevelLong is not None and (closed["l"] <= (structure.bosLevelLong + buf))
                    retestShort = structure.waitingRetestShort and structure.bosLevelShort is not None and (closed["h"] >= (structure.bosLevelShort - buf))

                    if retestLong:
                        structure.retestRefLong = structure.bosLevelLong
                        structure.accCountLong = 0

                    if retestShort:
                        structure.retestRefShort = structure.bosLevelShort
                        structure.accCountShort = 0

                    # Acceptance closes
                    if structure.waitingRetestLong and structure.retestRefLong is not None:
                        structure.accCountLong = structure.accCountLong + 1 if closed["c"] > structure.retestRefLong else 0

                    if structure.waitingRetestShort and structure.retestRefShort is not None:
                        structure.accCountShort = structure.accCountShort + 1 if closed["c"] < structure.retestRefShort else 0

                    acceptedLong = structure.waitingRetestLong and structure.retestRefLong is not None and structure.accCountLong >= ACCEPT_BARS
                    acceptedShort = structure.waitingRetestShort and structure.retestRefShort is not None and structure.accCountShort >= ACCEPT_BARS

                    longSignal = biasLong and emaTrendLong and acceptedLong
                    shortSignal = biasShort and emaTrendShort and acceptedShort

                    # Only send a new signal if we’re not currently tracking one
                    if (not sig.active) and longSignal:
                        entry = closed["c"]
                        stop = float(lastSwingLow) * (1 - STOP_BUFFER_PCT)
                        R = entry - stop
                        if R > 0:
                            sig.active = True
                            sig.side = "LONG"
                            sig.entry = entry
                            sig.stop = stop
                            sig.R = R
                            sig.tp1 = entry + TP1_R_MULT * R
                            sig.tp2 = entry + TP2_R_MULT * R
                            await notify(
                                f"{SYMBOL} LONG ✅ (BOS+Retest+Accept)\n"
                                f"entry={entry:.2f} stop={stop:.2f}\n"
                                f"tp1={sig.tp1:.2f} tp2={sig.tp2:.2f}"
                                f"\nstop_guardrails={MIN_STOP_PCT*100:.2f}%–{MAX_STOP_PCT*100:.2f}% "
                                f"buffer={STOP_BUFFER_PCT*100:.2f}%"
                            )


                            structure.waitingRetestLong = False
                            structure.retestRefLong = None
                            structure.accCountLong = 0

                    elif (not sig.active) and shortSignal:
                        entry = closed["c"]
                        stop = float(lastSwingHigh) * (1 + STOP_BUFFER_PCT)
                        R = stop - entry
                        if R > 0:
                            sig.active = True
                            sig.side = "SHORT"
                            sig.entry = entry
                            sig.stop = stop
                            sig.R = R
                            sig.tp1 = entry - TP1_R_MULT * R
                            sig.tp2 = entry - TP2_R_MULT * R
                            await notify(
                                f"{SYMBOL} SHORT ✅ (BOS+Retest+Accept)\n"
                                f"entry={entry:.2f} stop={stop:.2f}\n"
                                f"tp1={sig.tp1:.2f} tp2={sig.tp2:.2f}"
                                f"\nstop_guardrails={MIN_STOP_PCT*100:.2f}%–{MAX_STOP_PCT*100:.2f}% "
                                f"buffer={STOP_BUFFER_PCT*100:.2f}%"
                            )


                            structure.waitingRetestShort = False
                            structure.retestRefShort = None
                            structure.accCountShort = 0

        except Exception as e:
            log({"event": "ws_disconnected", "error": str(e)})
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
