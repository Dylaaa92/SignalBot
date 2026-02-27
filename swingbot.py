import asyncio
import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx
import websockets

from config import ENV, WS_URL
from logger import log
from notifier import notify
from strategies.swing_strategy import generate_swing_signal

SYMBOL = os.getenv("SYMBOL", "BTC")

TF_5M = 300

# How much history to pull on startup
BOOTSTRAP_DAYS = int(os.getenv("SWING_BOOTSTRAP_DAYS", "5"))

# Status throttling: 4 x 15m = 1 hour
STATUS_EVERY_N_15M = int(os.getenv("SWING_STATUS_EVERY_N_15M", "4"))


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


def aggregate_from_5m(c5m: list, group_n: int) -> Dict[str, list]:
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


async def bootstrap_5m_candles(symbol: str, limit_days: int = 5) -> list:
    """
    Prefill N days of 5m candles so SwingBot is signal-ready immediately.
    Hyperliquid candleSnapshot endpoint:
      POST https://api.hyperliquid.xyz/info
      { type: "candleSnapshot", req: { coin, interval, startTime, endTime } }
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

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post("https://api.hyperliquid.xyz/info", json=payload)
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


def _unpack_strategy_result(
    result: Any,
) -> Tuple[Optional[Any], Dict[str, Any], Dict[str, Any]]:
    """
    Supports both:
      - sig
      - (sig, state, debug)
    """
    if isinstance(result, tuple) and len(result) == 3:
        sig, state, dbg = result
        return sig, (state or {}), (dbg or {})
    return result, {}, {}


async def swing_loop():
    print(f"[SWING] starting {SYMBOL} env={ENV}")

    # Clear Telegram message: ONLINE != signal
    try:
        await notify(f"[SWING] ONLINE {SYMBOL} ({ENV}) - this is NOT a trade signal.")
        print("[SWING] online notify sent")
    except Exception as e:
        print(f"[SWING] online notify failed: {e}")

    cb5 = CandleBuilder(TF_5M)

    # Strategy state (works if your swing_strategy returns/uses it)
    state: Dict[str, Any] = {"phase": "IDLE"}
    last_phase = state.get("phase")
    last_signal_key = None

    # Evaluate only when a new 15m candle closes
    last_15m_close_count = 0

    # Status throttling
    last_status_15m = 0

    # -----------------------
    # Bootstrap history
    # -----------------------
    try:
        hist = await bootstrap_5m_candles(SYMBOL, limit_days=BOOTSTRAP_DAYS)
        cb5.candles.extend(hist)
        msg = f"[SWING] Bootstrapped {SYMBOL}: {len(hist)} x 5m candles (~{BOOTSTRAP_DAYS}d)"
        print(msg)
        await notify(msg)
    except Exception as e:
        msg = f"[SWING] Bootstrap FAILED {SYMBOL}: {e}"
        print(msg)
        try:
            await notify(msg)
        except Exception:
            pass

    # -----------------------
    # Live WebSocket
    # -----------------------
    async with websockets.connect(WS_URL) as ws:
        print(f"[SWING] ws connected {WS_URL}")

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

            # Build aggregates from closed 5m candles
            if len(cb5.candles) < 48 * 2:  # ~8 hours minimum safeguard
                continue

            c15 = aggregate_from_5m(cb5.candles, 3)
            if not c15["close"]:
                continue

            # Run only on NEW 15m close
            if len(c15["close"]) == last_15m_close_count:
                continue
            last_15m_close_count = len(c15["close"])

            c1h = aggregate_from_5m(cb5.candles, 12)
            c4h = aggregate_from_5m(cb5.candles, 48)
            if not c1h["close"] or not c4h["close"]:
                continue

            # Call strategy (supports both old and new signatures)
            try:
                result = generate_swing_signal(
                    symbol=SYMBOL,
                    c4h=c4h,
                    c1h=c1h,
                    c15m=c15,
                    state=state,  # if strategy doesn't accept it, we'll catch TypeError below
                )
            except TypeError:
                # Strategy might not accept state param
                result = generate_swing_signal(
                    symbol=SYMBOL,
                    c4h=c4h,
                    c1h=c1h,
                    c15m=c15,
                )

            sig, new_state, dbg = _unpack_strategy_result(result)
            if new_state:
                state = new_state

            phase = state.get("phase", "IDLE")

            # STATUS: hourly OR on phase change
            should_status = False
            if len(c15["close"]) >= last_status_15m + STATUS_EVERY_N_15M:
                should_status = True
                last_status_15m = len(c15["close"])
            if phase != last_phase:
                should_status = True
                last_phase = phase

            if should_status:
                s = (
                    f"[SWING][STATUS] {SYMBOL} phase={phase} "
                    f"bias={dbg.get('bias_4h')} "
                    f"1h_close={dbg.get('last_1h_close')} "
                    f"sh={dbg.get('swing_high')} sl={dbg.get('swing_low')}"
                )
                print(s)
                try:
                    await notify(s)
                except Exception as e:
                    print(f"[SWING] status notify failed: {e}")

            # SIGNAL
            if sig:
                side = getattr(sig, "side", None) or sig.get("side")
                entry = getattr(sig, "entry", None) or sig.get("entry")
                stop = getattr(sig, "stop", None) or sig.get("stop")
                tp1 = getattr(sig, "tp1", None) or sig.get("tp1")
                reason = getattr(sig, "reason", None) or sig.get("reason", "")

                key = f"{SYMBOL}:{side}:{round(float(entry),4)}:{round(float(stop),4)}"
                if key == last_signal_key:
                    continue
                last_signal_key = key

                msg = (
                    f"[SWING SIGNAL] {SYMBOL} {side}\n"
                    f"Reason: {reason}\n"
                    f"Entry: {float(entry):.4f}\n"
                    f"Stop: {float(stop):.4f}\n"
                    f"TP1: {float(tp1):.4f}"
                )
                print(msg)
                await notify(msg)
                log(
                    {
                        "event": "swing_signal",
                        "symbol": SYMBOL,
                        "side": side,
                        "entry": float(entry),
                        "stop": float(stop),
                        "tp1": float(tp1),
                        "phase": phase,
                    }
                )


def main():
    asyncio.run(swing_loop())


if __name__ == "__main__":
    main()
