import asyncio
import json
import time
import httpx
import websockets

from config import (
    SYMBOL, TF_SECONDS,
    EMA_FAST, EMA_SLOW,
    PIVOT_L, STOP_BUFFER_PCT,
    RISK_USDT_PER_TRADE,
    DAILY_MAX_LOSS_USDT,
    MAX_CONSEC_LOSSES,
    COOLDOWN_SECONDS,
    MIN_STOP_PCT, MAX_STOP_PCT,
    WS_TESTNET, WS_MAINNET,
    TAKER_FEE_PCT, ENTRY_SLIPPAGE_PCT, STOP_SLIPPAGE_PCT,
    TP1_R_MULT, TP1_FRACTION, TP_SLIPPAGE_PCT, BE_BUFFER_PCT
)

from indicators import ema
from pivots import last_confirmed_swing_low, last_confirmed_swing_high
from risk import RiskState, size_from_risk
from paper import PaperPosition, mark_to_market_pnl
from logger import log
from notifier import notify

ENV = "testnet"
WS_URL = WS_TESTNET if ENV == "testnet" else WS_MAINNET

ONE_HOUR = 3600

# Strategy params (match Pine defaults)
RETEST_BUF_ATR = 0.15
ACCEPT_BARS = 2
ATR_LEN = 14
TP2_R_MULT = 2.0


class CandleBuilder:
    def __init__(self, tf_seconds: int):
        self.tf = tf_seconds
        self.current = None  # dict
        self.candles = []    # list of dicts

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
    risk = RiskState()
    structure = StructureState()

    candle_5m = CandleBuilder(TF_SECONDS)
    candle_1h = CandleBuilder(ONE_HOUR)

    position = None
    last_candle_t = None

    # --- bootstrap historical candles (5m) ---
    history = await bootstrap_candles(SYMBOL, TF_SECONDS, limit=300)
    if history:
        candle_5m.candles = history[:-1]
        candle_5m.current = history[-1]
        last_candle_t = candle_5m.candles[-1]["t"] if candle_5m.candles else None

        # build initial 1H history from bootstrapped 5m closes
        for c in candle_5m.candles[-240:]:  # last ~20 hours
            candle_1h.update(c["t"] + TF_SECONDS, c["c"])

    log({"event": "bootstrapped", "candles_5m": len(candle_5m.candles), "candles_1h": len(candle_1h.candles)})

    sub_msg = {"method": "subscribe", "subscription": {"type": "allMids"}}
    log({"event": "startup", "ws": WS_URL, "symbol": SYMBOL, "mode": "paper"})

    while True:
        try:
            log({"event": "ws_connecting"})
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                log({"event": "ws_connected"})
                await ws.send(json.dumps(sub_msg))
                log({"event": "ws_subscribed"})

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)

                    if data.get("channel") != "allMids":
                        continue

                    payload = data.get("data", {})
                    mids = payload.get("mids", {})
                    if SYMBOL not in mids:
                        continue

                    price = float(mids[SYMBOL])
                    ts = time.time()

                    # Update 5m builder
                    candle_5m.update(ts, price)

                    closed = candle_5m.last_closed()
                    if not closed:
                        continue

                    # New 5m close?
                    if closed["t"] == last_candle_t:
                        continue
                    last_candle_t = closed["t"]

                    log({
                        "event": "candle_closed",
                        "candle_time": closed["t"],
                        "close": closed["c"],
                        "candles_built": len(candle_5m.candles)
                    })

                    # Build 1H from 5m closes
                    candle_1h.update(closed["t"] + TF_SECONDS, closed["c"])

                    # ---- circuit breakers ----
                    if risk.daily_pnl <= -DAILY_MAX_LOSS_USDT:
                        log({"event": "disabled_daily_max_loss", "daily_pnl": risk.daily_pnl})
                        continue
                    if risk.in_cooldown():
                        log({"event": "cooldown", "until": risk.cooldown_until})
                        continue

                    # ---- POSITION MANAGEMENT ----
                    if position is not None:
                        # TP1 check
                        if not position.tp1_taken:
                            tp1_hit = (closed["h"] >= position.tp1_price) if position.side == "LONG" else (closed["l"] <= position.tp1_price)
                            if tp1_hit:
                                if position.side == "LONG":
                                    tp_fill = position.tp1_price * (1 - TP_SLIPPAGE_PCT)
                                    gross_tp = (tp_fill - position.entry) * position.tp1_size
                                    new_stop = position.entry * (1 + BE_BUFFER_PCT)
                                else:
                                    tp_fill = position.tp1_price * (1 + TP_SLIPPAGE_PCT)
                                    gross_tp = (position.entry - tp_fill) * position.tp1_size
                                    new_stop = position.entry * (1 - BE_BUFFER_PCT)

                                tp_fees = (tp_fill * position.tp1_size) * TAKER_FEE_PCT
                                tp_pnl = gross_tp - tp_fees

                                position.size -= position.tp1_size
                                position.tp1_taken = True

                                if (position.side == "LONG" and new_stop > position.stop) or \
                                   (position.side == "SHORT" and new_stop < position.stop):
                                    position.stop = new_stop

                                risk.daily_pnl += tp_pnl

                                log({"event": "tp1_taken", "side": position.side, "tp_fill": tp_fill, "pnl": tp_pnl, "new_stop": position.stop})
                                await notify(f"{SYMBOL} {position.side} TP1 ✅ fill={tp_fill:.2f} pnl={tp_pnl:.2f} new_stop={position.stop:.2f}")

                        # TP2 check
                        if position.tp2_price is not None:
                            tp2_hit = (closed["h"] >= position.tp2_price) if position.side == "LONG" else (closed["l"] <= position.tp2_price)
                            if tp2_hit:
                                if position.side == "LONG":
                                    tp_fill = position.tp2_price * (1 - TP_SLIPPAGE_PCT)
                                    gross_tp = (tp_fill - position.entry) * position.size
                                else:
                                    tp_fill = position.tp2_price * (1 + TP_SLIPPAGE_PCT)
                                    gross_tp = (position.entry - tp_fill) * position.size

                                tp_fees = (tp_fill * position.size) * TAKER_FEE_PCT
                                tp_pnl = gross_tp - tp_fees

                                risk.daily_pnl += tp_pnl
                                risk.register_trade_result(tp_pnl, COOLDOWN_SECONDS, MAX_CONSEC_LOSSES)

                                log({"event": "tp2_taken", "side": position.side, "tp_fill": tp_fill, "pnl": tp_pnl})
                                await notify(f"{SYMBOL} {position.side} TP2 ✅ fill={tp_fill:.2f} pnl={tp_pnl:.2f}")

                                position = None
                                continue

                        # STOP check
                        stop_hit = (closed["l"] <= position.stop) if position.side == "LONG" else (closed["h"] >= position.stop)
                        if stop_hit:
                            if position.side == "LONG":
                                exit_price = position.stop * (1 - STOP_SLIPPAGE_PCT)
                                gross_pnl = (exit_price - position.entry) * position.size
                            else:
                                exit_price = position.stop * (1 + STOP_SLIPPAGE_PCT)
                                gross_pnl = (position.entry - exit_price) * position.size

                            fees = (position.entry * position.size + exit_price * position.size) * TAKER_FEE_PCT
                            pnl = gross_pnl - fees

                            risk.daily_pnl += pnl
                            risk.register_trade_result(pnl, COOLDOWN_SECONDS, MAX_CONSEC_LOSSES)

                            log({"event": "stop_hit", "side": position.side, "exit_price": exit_price, "pnl": pnl, "daily_pnl": risk.daily_pnl})
                            await notify(f"{SYMBOL} {position.side} STOP ❌ exit={exit_price:.2f} pnl={pnl:.2f} daily={risk.daily_pnl:.2f}")

                            position = None
                            continue

                        # MTM (optional)
                        mtm = mark_to_market_pnl(position, closed["c"])
                        log({"event": "position_mtm", "side": position.side, "mtm_pnl": mtm})

                        continue  # flat-only entries

                    # ---- ENTRY LOGIC (FLAT ONLY) ----
                    if len(candle_5m.candles) < 120 or len(candle_1h.candles) < 40:
                        log({"event": "waiting_data", "candles_5m": len(candle_5m.candles), "candles_1h": len(candle_1h.candles)})
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
                        log({"event": "waiting_indicators"})
                        continue

                    biasLong = efast1 > eslow1
                    biasShort = efast1 < eslow1
                    emaTrendLong = efast5 > eslow5
                    emaTrendShort = efast5 < eslow5

                    idx_hi = last_confirmed_swing_high(highs5, PIVOT_L)
                    idx_lo = last_confirmed_swing_low(lows5, PIVOT_L)
                    if idx_hi is None or idx_lo is None:
                        log({"event": "waiting_swings"})
                        continue

                    lastSwingHigh = highs5[idx_hi]
                    lastSwingLow = lows5[idx_lo]

                    prev_close = c5[-2]["c"]
                    bosUp = (prev_close <= lastSwingHigh) and (closed["c"] > lastSwingHigh)
                    bosDown = (prev_close >= lastSwingLow) and (closed["c"] < lastSwingLow)

                    a = atr_from_candles(c5, length=ATR_LEN)
                    if a is None:
                        log({"event": "waiting_atr"})
                        continue
                    buf = a * RETEST_BUF_ATR

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

                    retestLong = structure.waitingRetestLong and (structure.bosLevelLong is not None) and (closed["l"] <= (structure.bosLevelLong + buf))
                    retestShort = structure.waitingRetestShort and (structure.bosLevelShort is not None) and (closed["h"] >= (structure.bosLevelShort - buf))

                    if retestLong:
                        structure.retestRefLong = structure.bosLevelLong
                        structure.accCountLong = 0

                    if retestShort:
                        structure.retestRefShort = structure.bosLevelShort
                        structure.accCountShort = 0

                    if structure.waitingRetestLong and structure.retestRefLong is not None:
                        structure.accCountLong = structure.accCountLong + 1 if closed["c"] > structure.retestRefLong else 0

                    if structure.waitingRetestShort and structure.retestRefShort is not None:
                        structure.accCountShort = structure.accCountShort + 1 if closed["c"] < structure.retestRefShort else 0

                    acceptedLong = structure.waitingRetestLong and structure.retestRefLong is not None and structure.accCountLong >= ACCEPT_BARS
                    acceptedShort = structure.waitingRetestShort and structure.retestRefShort is not None and structure.accCountShort >= ACCEPT_BARS

                    longSetup = biasLong and emaTrendLong and acceptedLong
                    shortSetup = biasShort and emaTrendShort and acceptedShort

                    if longSetup:
                        stop = float(lastSwingLow) * (1 - STOP_BUFFER_PCT)
                        entry = closed["c"]
                        stop_dist = entry - stop
                        if stop_dist <= 0:
                            continue

                        stop_pct = stop_dist / entry
                        if not (MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT):
                            log({"event": "skip_stop_distance", "stop_pct": stop_pct, "entry": entry, "stop": stop})
                            continue

                        size = size_from_risk(RISK_USDT_PER_TRADE, entry, stop)
                        if size <= 0:
                            continue

                        filled_entry = entry * (1 + ENTRY_SLIPPAGE_PCT)
                        R = filled_entry - stop
                        tp1_price = filled_entry + TP1_R_MULT * R
                        tp2_price = filled_entry + TP2_R_MULT * R
                        tp1_size = size * TP1_FRACTION

                        position = PaperPosition(
                            side="LONG",
                            entry=filled_entry,
                            stop=stop,
                            size=size,
                            initial_size=size,
                            tp1_price=tp1_price,
                            tp1_size=tp1_size,
                            tp2_price=tp2_price
                        )

                        structure.waitingRetestLong = False
                        structure.retestRefLong = None
                        structure.accCountLong = 0

                        log({"event": "enter_long_paper_bos", "entry": filled_entry, "stop": stop, "tp1": tp1_price, "tp2": tp2_price})
                        await notify(f"{SYMBOL} LONG ✅ entry={filled_entry:.2f} stop={stop:.2f} tp1={tp1_price:.2f} tp2={tp2_price:.2f}")

                    elif shortSetup:
                        stop = float(lastSwingHigh) * (1 + STOP_BUFFER_PCT)
                        entry = closed["c"]
                        stop_dist = stop - entry
                        if stop_dist <= 0:
                            continue

                        stop_pct = stop_dist / entry
                        if not (MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT):
                            log({"event": "skip_stop_distance", "stop_pct": stop_pct, "entry": entry, "stop": stop})
                            continue

                        size = size_from_risk(RISK_USDT_PER_TRADE, stop, entry)
                        if size <= 0:
                            continue

                        filled_entry = entry * (1 - ENTRY_SLIPPAGE_PCT)
                        R = stop - filled_entry
                        tp1_price = filled_entry - TP1_R_MULT * R
                        tp2_price = filled_entry - TP2_R_MULT * R
                        tp1_size = size * TP1_FRACTION

                        position = PaperPosition(
                            side="SHORT",
                            entry=filled_entry,
                            stop=stop,
                            size=size,
                            initial_size=size,
                            tp1_price=tp1_price,
                            tp1_size=tp1_size,
                            tp2_price=tp2_price
                        )

                        structure.waitingRetestShort = False
                        structure.retestRefShort = None
                        structure.accCountShort = 0

                        log({"event": "enter_short_paper_bos", "entry": filled_entry, "stop": stop, "tp1": tp1_price, "tp2": tp2_price})
                        await notify(f"{SYMBOL} SHORT ✅ entry={filled_entry:.2f} stop={stop:.2f} tp1={tp1_price:.2f} tp2={tp2_price:.2f}")

        except Exception as e:
            log({"event": "ws_disconnected", "error": str(e)})
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
