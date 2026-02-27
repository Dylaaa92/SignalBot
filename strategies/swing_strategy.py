from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Any


@dataclass
class SwingSignal:
    symbol: str
    side: str  # "LONG" or "SHORT"
    entry: float
    stop: float
    tp1: float
    reason: str


def ema(values, length: int):
    if not values or len(values) < length:
        return None
    k = 2 / (length + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def atr(high, low, close, length: int = 14):
    if len(close) < length + 1:
        return None
    trs = []
    for i in range(1, len(close)):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        trs.append(tr)
    if len(trs) < length:
        return None
    return sum(trs[-length:]) / length


def pivot_high(high, left=5, right=5):
    """Return index of last confirmed pivot high."""
    n = len(high)
    if n < left + right + 1:
        return None
    for i in range(n - right - 1, left, -1):
        h = high[i]
        if all(h > high[i - j] for j in range(1, left + 1)) and all(
            h >= high[i + j] for j in range(1, right + 1)
        ):
            return i
    return None


def pivot_low(low, left=5, right=5):
    """Return index of last confirmed pivot low."""
    n = len(low)
    if n < left + right + 1:
        return None
    for i in range(n - right - 1, left, -1):
        l = low[i]
        if all(l < low[i - j] for j in range(1, left + 1)) and all(
            l <= low[i + j] for j in range(1, right + 1)
        ):
            return i
    return None


def bias_4h(c4h) -> str:
    c = c4h["close"]
    if len(c) < 30:
        return "NEUTRAL"
    e9 = ema(c[-60:], 9)
    e21 = ema(c[-60:], 21)
    if e9 is None or e21 is None:
        return "NEUTRAL"
    if e9 > e21 and c[-1] > e21:
        return "LONG"
    if e9 < e21 and c[-1] < e21:
        return "SHORT"
    return "NEUTRAL"


def generate_swing_signal(
    symbol: str,
    c4h: Dict[str, list],
    c1h: Dict[str, list],
    c15m: Dict[str, list],
    state: Dict[str, Any],
    pivot_len: int = 5,
    atr_len: int = 14,
    atr_buf_mult: float = 0.25,
    tp_r: float = 1.0,
    accept_bars: int = 2,
) -> Tuple[Optional[SwingSignal], Dict[str, Any], Dict[str, Any]]:
    """
    Returns: (signal_or_None, updated_state, debug)
    state keys used:
      phase: IDLE|ARMED|RETEST|IN_TRADE
      side: LONG|SHORT
      bos_level: float
      entry: float
      stop: float
      tp1: float
      tp1_hit: bool
    """

    debug = {"symbol": symbol}
    updated = dict(state or {})
    phase = updated.get("phase", "IDLE")

    # ---- Bias filter (4H) ----
    bias = bias_4h(c4h)
    debug["bias_4h"] = bias
    if bias == "NEUTRAL":
        updated["phase"] = "IDLE"
        return None, updated, debug

    # ---- ATR (1H) ----
    a1h = atr(c1h["high"], c1h["low"], c1h["close"], atr_len)
    debug["atr_1h"] = a1h
    if a1h is None:
        return None, updated, debug
    buf = a1h * atr_buf_mult
    debug["buf"] = buf

    # ---- Pivots (1H) ----
    ph_i = pivot_high(c1h["high"], pivot_len, pivot_len)
    pl_i = pivot_low(c1h["low"], pivot_len, pivot_len)
    if ph_i is None or pl_i is None:
        return None, updated, debug

    swing_high = c1h["high"][ph_i]
    swing_low = c1h["low"][pl_i]
    debug["swing_high"] = swing_high
    debug["swing_low"] = swing_low

    last_1h_close = c1h["close"][-1]
    debug["last_1h_close"] = last_1h_close

    # ---- Manage active trade (runner logic later in swingbot) ----
    if phase == "IN_TRADE":
        return None, updated, debug

    # ---- Phase: IDLE -> ARM on BOS ----
    if phase == "IDLE":
        if bias == "LONG" and last_1h_close > swing_high:
            updated.update(
                {
                    "phase": "ARMED",
                    "side": "LONG",
                    "bos_level": swing_high,
                    "armed_at_1h_close": last_1h_close,
                }
            )
            debug["event"] = "BOS_LONG_ARMED"
            return None, updated, debug

        if bias == "SHORT" and last_1h_close < swing_low:
            updated.update(
                {
                    "phase": "ARMED",
                    "side": "SHORT",
                    "bos_level": swing_low,
                    "armed_at_1h_close": last_1h_close,
                }
            )
            debug["event"] = "BOS_SHORT_ARMED"
            return None, updated, debug

        return None, updated, debug

    # ---- Phase: ARMED -> wait for retest into zone ----
    if phase == "ARMED":
        side = updated.get("side")
        bos_level = float(updated.get("bos_level"))

        last_15_close = c15m["close"][-1]
        debug["last_15_close"] = last_15_close

        if side == "LONG":
            # retest zone around BOS level
            in_zone = (bos_level - buf) <= last_15_close <= (bos_level + buf)
            debug["retest_in_zone"] = in_zone
            if in_zone:
                updated["phase"] = "RETEST"
                updated["retest_level"] = bos_level
                updated["accept_count"] = 0
            return None, updated, debug

        if side == "SHORT":
            in_zone = (bos_level - buf) <= last_15_close <= (bos_level + buf)
            debug["retest_in_zone"] = in_zone
            if in_zone:
                updated["phase"] = "RETEST"
                updated["retest_level"] = bos_level
                updated["accept_count"] = 0
            return None, updated, debug

        # fallback
        updated["phase"] = "IDLE"
        return None, updated, debug

    # ---- Phase: RETEST -> acceptance on 15m closes ----
    if phase == "RETEST":
        side = updated.get("side")
        lvl = float(updated.get("retest_level"))
        accept = int(updated.get("accept_count", 0))

        closes = c15m["close"]
        if len(closes) < accept_bars:
            return None, updated, debug

        last_close = closes[-1]
        debug["last_15_close"] = last_close

        if side == "LONG":
            # acceptance = consecutive closes above lvl + buffer
            if last_close > (lvl + buf):
                accept += 1
            else:
                accept = 0
            updated["accept_count"] = accept
            debug["accept_count"] = accept

            if accept >= accept_bars:
                entry = last_close
                stop = (lvl - buf)  # below retest zone
                risk = entry - stop
                if risk <= 0:
                    updated["phase"] = "IDLE"
                    return None, updated, debug
                tp1 = entry + (risk * tp_r)

                updated.update(
                    {
                        "phase": "IN_TRADE",
                        "entry": entry,
                        "stop": stop,
                        "tp1": tp1,
                        "tp1_hit": False,
                    }
                )
                sig = SwingSignal(
                    symbol=symbol,
                    side="LONG",
                    entry=entry,
                    stop=stop,
                    tp1=tp1,
                    reason="4H bias LONG + 1H BOS + retest + 15m acceptance",
                )
                return sig, updated, debug

            return None, updated, debug

        if side == "SHORT":
            if last_close < (lvl - buf):
                accept += 1
            else:
                accept = 0
            updated["accept_count"] = accept
            debug["accept_count"] = accept

            if accept >= accept_bars:
                entry = last_close
                stop = (lvl + buf)
                risk = stop - entry
                if risk <= 0:
                    updated["phase"] = "IDLE"
                    return None, updated, debug
                tp1 = entry - (risk * tp_r)

                updated.update(
                    {
                        "phase": "IN_TRADE",
                        "entry": entry,
                        "stop": stop,
                        "tp1": tp1,
                        "tp1_hit": False,
                    }
                )
                sig = SwingSignal(
                    symbol=symbol,
                    side="SHORT",
                    entry=entry,
                    stop=stop,
                    tp1=tp1,
                    reason="4H bias SHORT + 1H BOS + retest + 15m acceptance",
                )
                return sig, updated, debug

            return None, updated, debug

        updated["phase"] = "IDLE"
        return None, updated, debug

    # fallback
    updated["phase"] = "IDLE"
    return None, updated, debug
