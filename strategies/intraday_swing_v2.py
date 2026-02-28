import time
import httpx

from indicators import ema
from pivots import last_confirmed_swing_high, last_confirmed_swing_low


# =========================================================
# Indicators
# =========================================================

def rsi(closes, length=14):
    if len(closes) < length + 1:
        return None

    gains = 0.0
    losses = 0.0

    for i in range(-length, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff

    if losses == 0:
        return 100.0

    rs = gains / losses
    return 100 - (100 / (1 + rs))


def atr(candles, length=14):
    if len(candles) < length + 1:
        return None

    trs = []

    for i in range(-length, 0):
        c = candles[i]
        p = candles[i - 1]

        tr = max(
            c["h"] - c["l"],
            abs(c["h"] - p["c"]),
            abs(c["l"] - p["c"]),
        )

        trs.append(tr)

    return sum(trs) / len(trs)


# =========================================================
# Hyperliquid Candle Snapshot
# =========================================================

async def candle_snapshot(symbol: str, tf_sec: int, limit: int = 250):
    url = "https://api.hyperliquid.xyz/info"

    now = int(time.time() * 1000)
    start = now - (limit * tf_sec * 1000)

    if tf_sec == 900:
        interval = "15m"
    elif tf_sec == 3600:
        interval = "1h"
    elif tf_sec == 14400:
        interval = "4h"
    else:
        interval = f"{tf_sec // 60}m"

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": interval,
            "startTime": start,
            "endTime": now,
        },
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()

    out = []

    for c in data:
        t = int(c["t"] / 1000)
        out.append({
            "t": t - (t % tf_sec),
            "o": float(c["o"]),
            "h": float(c["h"]),
            "l": float(c["l"]),
            "c": float(c["c"]),
        })

    return out


# =========================================================
# Decision Engine
# =========================================================

def decide(params, c15, c1h, c4h):
    if len(c15) < 80 or len(c1h) < 80 or len(c4h) < 80:
        return {"action": "NO_TRADE", "reason": "not_enough_data"}

    closes15 = [c["c"] for c in c15]
    closes1  = [c["c"] for c in c1h]
    closes4  = [c["c"] for c in c4h]

    ema9_15  = ema(closes15, params["EMA_FAST"])
    ema21_15 = ema(closes15, params["EMA_SLOW"])
    ema9_1h  = ema(closes1, params["EMA_FAST"])
    ema21_1h = ema(closes1, params["EMA_SLOW"])
    ema9_4h  = ema(closes4, params["EMA_FAST"])
    ema21_4h = ema(closes4, params["EMA_SLOW"])

    if None in (ema9_15, ema21_15, ema9_1h, ema21_1h, ema9_4h, ema21_4h):
        return {"action": "NO_TRADE", "reason": "ema_na"}

    # 4H Bias
    biasBull = ema9_4h > ema21_4h and closes4[-1] > ema9_4h
    biasBear = ema9_4h < ema21_4h and closes4[-1] < ema9_4h

    # 1H Structure
    highs1 = [c["h"] for c in c1h]
    lows1  = [c["l"] for c in c1h]

    idx_hi = last_confirmed_swing_high(highs1, params["PIVOT_1H_LEN"])
    idx_lo = last_confirmed_swing_low(lows1, params["PIVOT_1H_LEN"])

    if idx_hi is None or idx_lo is None:
        return {"action": "NO_TRADE", "reason": "no_pivots"}

    lastHigh = highs1[idx_hi]
    lastLow  = lows1[idx_lo]

    prev1 = closes1[-2]
    cur1  = closes1[-1]

    bosUp   = prev1 <= lastHigh and cur1 > lastHigh
    bosDown = prev1 >= lastLow  and cur1 < lastLow

    if params["REQUIRE_1H_EMA21_SIDE"]:
        acceptLong  = cur1 > ema21_1h
        acceptShort = cur1 < ema21_1h
    else:
        acceptLong = acceptShort = True

    longConfirm  = bosUp and acceptLong
    shortConfirm = bosDown and acceptShort

    # 15m Reclaim
    close15 = closes15[-1]
    reclaimLong  = close15 > ema9_15 and ema9_15 > ema21_15
    reclaimShort = close15 < ema9_15 and ema9_15 < ema21_15

    r = rsi(closes15, params["RSI_LEN"])
    if r is None:
        return {"action": "NO_TRADE", "reason": "rsi_na"}

    rsiLongOk  = r >= params["RSI_LONG_MIN"]
    rsiShortOk = r <= params["RSI_SHORT_MAX"]

    canLong  = True if params["ALLOW_COUNTER_TREND"] else biasBull
    canShort = True if params["ALLOW_COUNTER_TREND"] else biasBear

    h1atr = atr(c1h, 14)
    if h1atr is None:
        return {"action": "NO_TRADE", "reason": "atr_na"}

    longStop  = lastLow  - (h1atr * params["ATR_BUF_MULT"])
    shortStop = lastHigh + (h1atr * params["ATR_BUF_MULT"])

    longR  = close15 - longStop
    shortR = shortStop - close15

    if longR <= 0 or shortR <= 0:
        return {"action": "NO_TRADE", "reason": "bad_stop"}

    longTP1  = close15 + (longR * params["RISK_R"])
    shortTP1 = close15 - (shortR * params["RISK_R"])

    if canLong and longConfirm and reclaimLong and rsiLongOk:
        return {
            "action": "ENTER_LONG",
            "entry_px": close15,
            "stop_px": longStop,
            "tp1_px": longTP1,
        }

    if canShort and shortConfirm and reclaimShort and rsiShortOk:
        return {
            "action": "ENTER_SHORT",
            "entry_px": close15,
            "stop_px": shortStop,
            "tp1_px": shortTP1,
        }

    return {"action": "NO_TRADE", "reason": "no_setup"}
