from dataclasses import dataclass
from typing import Optional, Literal, Dict, List, Tuple

Side = Literal["LONG", "SHORT"]

@dataclass
class SwingSignal:
    symbol: str
    side: Side
    entry: float
    stop: float
    tp1: float
    reason: str


def ema(values: List[float], length: int) -> float:
    if not values:
        return 0.0
    k = 2 / (length + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def atr(high: List[float], low: List[float], close: List[float], length: int) -> float:
    if len(close) < 2:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(close)):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        trs.append(tr)
    if not trs:
        return 0.0
    if len(trs) < length:
        return sum(trs) / len(trs)
    return sum(trs[-length:]) / length


def last_pivot_swings(high: List[float], low: List[float], pivot_len: int) -> Tuple[Optional[float], Optional[float]]:
    """
    Strict pivot: a pivot at i is confirmed only after pivot_len bars to the right exist.
    Returns (lastSwingHigh, lastSwingLow).
    """
    n = len(high)
    if n < (pivot_len * 2 + 3):
        return None, None

    last_ph = None
    last_pl = None

    for i in range(pivot_len, n - pivot_len):
        window_h = high[i - pivot_len:i + pivot_len + 1]
        window_l = low[i - pivot_len:i + pivot_len + 1]
        if high[i] == max(window_h):
            last_ph = high[i]
        if low[i] == min(window_l):
            last_pl = low[i]

    return last_ph, last_pl


def generate_swing_signal(
    symbol: str,
    c4h: Dict[str, List[float]],
    c1h: Dict[str, List[float]],
    c15m: Dict[str, List[float]],
    *,
    pivot_len: int = 5,
    atr_len: int = 14,
    atr_buf_mult: float = 0.25,
    tp_r: float = 1.0,
    require_breakout_candle: bool = True,
) -> Optional[SwingSignal]:
    # ---- guards ----
    if len(c4h.get("close", [])) < 30 or len(c1h.get("close", [])) < 60 or len(c15m.get("close", [])) < 60:
        return None

    # ---- 4H bias ----
    ema9_4h  = ema(c4h["close"], 9)
    ema21_4h = ema(c4h["close"], 21)
    close_4h = c4h["close"][-1]

    bias_bull = ema9_4h > ema21_4h and close_4h > ema9_4h
    bias_bear = ema9_4h < ema21_4h and close_4h < ema9_4h

    # ---- 1H structure (strict BOS) ----
    ema9_1h  = ema(c1h["close"], 9)
    ema21_1h = ema(c1h["close"], 21)
    close_1h = c1h["close"][-1]

    last_sh, last_sl = last_pivot_swings(c1h["high"], c1h["low"], pivot_len)

    bos_up = (last_sh is not None) and (close_1h > last_sh) and (close_1h > ema9_1h) and (ema9_1h > ema21_1h)
    bos_dn = (last_sl is not None) and (close_1h < last_sl) and (close_1h < ema9_1h) and (ema9_1h < ema21_1h)

    # ---- 15m execution (strict continuation) ----
    ema9_15  = ema(c15m["close"], 9)
    ema21_15 = ema(c15m["close"], 21)
    close_15 = c15m["close"][-1]

    short_exec = (ema9_15 < ema21_15) and (close_15 < ema9_15)
    long_exec  = (ema9_15 > ema21_15) and (close_15 > ema9_15)

    if require_breakout_candle and len(c15m["close"]) >= 2:
        short_exec = short_exec and (close_15 < c15m["low"][-2])
        long_exec  = long_exec  and (close_15 > c15m["high"][-2])

    # ---- ATR buffer (1H) ----
    atr_1h = atr(c1h["high"], c1h["low"], c1h["close"], atr_len)
    buf = atr_1h * atr_buf_mult

    # ---- Build signal ----
    if bias_bear and bos_dn and short_exec and (last_sh is not None):
        entry = close_15
        stop  = last_sh + buf
        risk  = stop - entry
        if risk <= 0:
            return None
        tp1 = entry - (risk * tp_r)
        return SwingSignal(
            symbol=symbol,
            side="SHORT",
            entry=entry,
            stop=stop,
            tp1=tp1,
            reason="4H bearish; 1H BOS down (strict); 15m continuation. Runner exit: 1H close > EMA9."
        )

    if bias_bull and bos_up and long_exec and (last_sl is not None):
        entry = close_15
        stop  = last_sl - buf
        risk  = entry - stop
        if risk <= 0:
            return None
        tp1 = entry + (risk * tp_r)
        return SwingSignal(
            symbol=symbol,
            side="LONG",
            entry=entry,
            stop=stop,
            tp1=tp1,
            reason="4H bullish; 1H BOS up (strict); 15m continuation. Runner exit: 1H close < EMA9."
        )

    return None
