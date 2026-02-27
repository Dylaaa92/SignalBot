import asyncio
import json
import time
import httpx
import websockets
import subprocess
import os
import uuid

from grid_engine import GridBot, GridParams
from telegram_control import telegram_poll_commands

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
from journal import Journal
from trade_events import append_event, new_trade_id


# ============================
# SYSTEMD CONTROL (NEW)
# ============================

ALL_SYMBOLS = ["BTC", "ETH", "SOL", "JUP", "HYPE"]

def _svc_name(sym: str) -> str:
    return f"signalbot@{sym}.service"

def _run_systemctl(action: str, symbols: list[str]):
    services = [_svc_name(s) for s in symbols]
    p = subprocess.run(
        ["sudo", "systemctl", action, *services],
        capture_output=True, text=True,
    )
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out.strip()

ONE_HOUR = 3600
grid = None  # ensure global exists


# ============================
# TELEGRAM COMMAND HANDLER
# ============================

TELEGRAM_OFFSET_FILE = os.getenv("TELEGRAM_OFFSET_FILE", "telegram_offset.json")

def load_tg_offset() -> int:
    try:
        with open(TELEGRAM_OFFSET_FILE, "r") as f:
            return int(json.load(f).get("last_update_id", 0))
    except Exception:
        return 0

def save_tg_offset(last_update_id: int):
    try:
        tmp = TELEGRAM_OFFSET_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_update_id": int(last_update_id)}, f)
        os.replace(tmp, TELEGRAM_OFFSET_FILE)
    except Exception:
        pass

async def handle_command(text: str):
    global grid
    t = (text or "").strip()
    if not t:
        return
    parts = t.split()
    cmd = parts[0].lower().split("@")[0]

    # ---- HELP ----
    if cmd in ("/start", "/help"):
        await notify(
            "✅ SignalBot control is live.\n\n"
            "Service Control:\n"
            "/status\n"
            "/start_all\n"
            "/stop_all\n"
            "/restart_all\n\n"
            "Single Ticker:\n"
            "/start BTC|ETH|SOL|JUP|HYPE\n"
            "/stop BTC|ETH|SOL|JUP|HYPE\n"
            "/restart BTC|ETH|SOL|JUP|HYPE\n\n"
            "Grid Commands:\n"
            "/grid_start SYMBOL lower upper grids usd_per_order\n"
            "/grid_stop SYMBOL\n"
            "/grid_status SYMBOL\n"
            "/grid_rebuild SYMBOL\n"
        )
        return

    # ---- SERVICE STATUS ----
    if cmd == "/status":
        lines = []
        for s in ALL_SYMBOLS:
            p = subprocess.run(["systemctl", "is-active", _svc_name(s)], capture_output=True, text=True)
            lines.append(f"{s}: {p.stdout.strip()}")
        await notify("\n".join(lines))
        return

    # ---- ALL SERVICE ACTIONS ----
    if cmd in ("/start_all", "/stop_all", "/restart_all"):
        action = cmd.replace("_all", "").replace("/", "")
        rc, out = _run_systemctl(action, ALL_SYMBOLS)
        if rc == 0:
            await notify(f"✅ {action.upper()} ALL OK")
        else:
            await notify(f"⚠️ {action.upper()} ALL failed:\n{out[:1500]}")
        return

    # ---- SINGLE SERVICE ACTION ----
    if cmd in ("/start", "/stop", "/restart"):
        if len(parts) < 2:
            await notify("Usage: /start BTC (or /start_all)")
            return
        sym = parts[1].upper()
        if sym not in ALL_SYMBOLS:
            await notify(f"Unknown symbol: {sym}")
            return
        action = cmd.replace("/", "")
        rc, out = _run_systemctl(action, [sym])
        if rc == 0:
            await notify(f"✅ {action.upper()} {sym} OK")
        else:
            await notify(f"⚠️ {action.upper()} {sym} failed:\n{out[:1500]}")
        return

    # ---- GRID COMMANDS (UNCHANGED LOGIC) ----
    if grid is None:
        return

    if len(parts) < 2:
        return

    target_symbol = parts[1].upper()
    if target_symbol != SYMBOL:
        return

    if cmd == "/grid_start":
        if len(parts) < 6:
            await notify("Usage: /grid_start SYMBOL lower upper grids usd_per_order")
            return
        lower = float(parts[2])
        upper = float(parts[3])
        grids = int(parts[4])
        usd = float(parts[5])
        await grid.start(GridParams(lower, upper, grids, usd))

    elif cmd == "/grid_stop":
        await grid.stop()

    elif cmd == "/grid_status":
        await notify(await grid.status())

    elif cmd == "/grid_rebuild":
        await grid.rebuild()


# ============================
# Strategy params (FINAL LOGIC)
# ============================

ATR_LEN = 14
RETEST_BUF_ATR = 0.30      # retest buffer = ATR * 0.30
ACCEPT_BARS = 2            # acceptance closes after retest
TP1_R_MULT = 1.0
TP1_PARTIAL_PCT = 0.30     # paper bookkeeping only (signal-only bot)

# Runner protection (after TP1)
BE_BUF_ATR = 0.10          # BE buffer = ATR * 0.10
STRUCT_PAD_ATR = 0.10      # structure pad = ATR * 0.10
ATR_SEATBELT_MULT = 1.2    # ATR seatbelt trail distance = ATR * 1.2
RUNNER_TIME_STOP_BARS = 12 # after TP1, exit runner after 12 bars (~60 mins)


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


def crossed_down(prev_fast, prev_slow, fast, slow) -> bool:
    return (prev_fast is not None and prev_slow is not None) and (prev_fast >= prev_slow and fast < slow)


def crossed_up(prev_fast, prev_slow, fast, slow) -> bool:
    return (prev_fast is not None and prev_slow is not None) and (prev_fast <= prev_slow and fast > slow)


def safe_append(event: dict):
    """ Never let a filesystem write crash the bot.
    Logs an error to bot logger instead.
    """
    try:
        append_event(SYMBOL, event)
    except Exception as e:
        log({"event": "trade_event_write_failed", "error": str(e), "type": event.get("type")})


class StructureState:
    """ BOS -> Retest -> Accept state (per symbol instance).
    Stores BOS level and the swing anchor at time of BOS to build stop later.
    """
    def __init__(self):
        self.direction = None
        self.bosLevel = None
        self.waitingRetest = False
        self.retestRef = None
        self.accCount = 0
        self.bosSwingLow = None
        self.bosSwingHigh = None

    def reset(self):
        self.direction = None
        self.bosLevel = None
        self.waitingRetest = False
        self.retestRef = None
        self.accCount = 0
        self.bosSwingLow = None
        self.bosSwingHigh = None


class SignalTradeState:
    def __init__(self):
        self.active = False
        self.phase = None  # PRE_TP1, RUNNER
        self.side = None
        self.trade_id = None
        self.entry_t = None
        self.entry = None
        self.stop_init = None
        self.R = None
        self.tp1 = None
        self.tp1_sent = False
        self.tp1_t = None
        self.highest_high_since_tp1 = None
        self.lowest_low_since_tp1 = None
        self.struct_stop = None
        self.atr_stop = None
        self.prev_efast5 = None
        self.prev_eslow5 = None

    def clear(self):
        self.__init__()


async def bootstrap_candles(symbol: str, tf_sec: int, limit=300):
    """
    Bootstrap from hyperliquid REST-like endpoint (via Info API endpoint through httpx).
    Uses the same candle format as CandleBuilder.
    """
    # NOTE: This is your existing bootstrap; leaving it intact.
    # If you want volume/VWAP/POC later, this is where we'd extend.
    url = "https://api.hyperliquid.xyz/info"
    now = int(time.time() * 1000)
    start = now - (limit * tf_sec * 1000)

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": f"{tf_sec // 60}m" if tf_sec < 3600 else "1h",
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


async def main():
    structure = StructureState()
    sig = SignalTradeState()

    # Keep profiles in place (unused for stop now, but harmless)
    _profile = SYMBOL_PROFILES.get(SYMBOL, DEFAULT_PROFILE)

    candle_5m = CandleBuilder(TF_SECONDS)
    candle_1h = CandleBuilder(ONE_HOUR)

    last_candle_t = None

    # --- bootstrap ---
    history = await bootstrap_candles(SYMBOL, TF_SECONDS, limit=300)
    if history:
        candle_5m.candles = history[:-1]
        candle_5m.current = history[-1]
        last_candle_t = candle_5m.candles[-1]["t"] if candle_5m.candles else None

        # build 1h from historical 5m closes
        for c in candle_5m.candles[-240:]:
            candle_1h.update(c["t"] + TF_SECONDS, c["c"])

    log({"event": "bootstrapped", "candles_5m": len(candle_5m.candles), "candles_1h": len(candle_1h.candles)})

    sub_msg = {"method": "subscribe", "subscription": {"type": "allMids"}}
    log({"event": "startup", "ws": WS_URL, "symbol": SYMBOL, "mode": "signal_only"})
    await notify(f"✅ Signalbot LIVE: {SYMBOL} | ENV={ENV} | 5m exec / 1h bias | BOS→Retest→Accept(1) | TP1+Runner | JSONL events")

    # --- journaling session ---
    session_id = os.getenv("SESSION_ID", str(uuid.uuid4())[:8])
    journal = Journal(session_id=session_id)
    log({"event": "session_started", "session_id": session_id, "symbol": SYMBOL})

    # --- optional grid bot ---
    global grid
    grid = None
    if os.getenv("GRID_ENABLED", "0") == "1":
        grid = GridBot(SYMBOL, ENV)
        asyncio.create_task(grid.loop())

    last_hb = 0

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

                    # --- journal: 5m snapshot (OHLC only; volume/POC/VWAP not available from mids stream) ---
                    journal.write_snapshot({
                        "symbol": SYMBOL,
                        "tf": "5m",
                        "open": closed.get("o"),
                        "high": closed.get("h"),
                        "low": closed.get("l"),
                        "close": closed.get("c"),
                        "volume": "",
                        "ema9": "",
                        "ema21": "",
                        "vwap": "",
                        "poc": "",
                        "rsi": "",
                    })

                    # build 1h from 5m closes
                    candle_1h.update(closed["t"] + TF_SECONDS, closed["c"])

                    # --- heartbeat ---
                    now = int(time.time())
                    if now - last_hb >= 3600:
                        last_hb = now
                        log({"event": "heartbeat", "symbol": SYMBOL})

                    # ---- need enough candles ----
                    if len(candle_5m.candles) < 120 or len(candle_1h.candles) < 40:
                        continue

                    c5_full = candle_5m.candles
                    c1 = candle_1h.candles

                    # Use a consistent 5m window (align indices for pivots + timestamps)
                    c5w = c5_full[-300:]
                    closes5 = [c["c"] for c in c5w]
                    highs5 = [c["h"] for c in c5w]
                    lows5 = [c["l"] for c in c5w]
                    closes1 = [c["c"] for c in c1][-200:]

                    # EMAs
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

                    # ATR + buffers (use same window list)
                    a = atr_from_candles(c5w, length=ATR_LEN)
                    if a is None:
                        continue
                    retest_buf = a * RETEST_BUF_ATR
                    be_buf = a * BE_BUF_ATR
                    struct_pad = a * STRUCT_PAD_ATR
                    atr_seatbelt_dist = a * ATR_SEATBELT_MULT

                    # pivots (confirmed) on window
                    idx_hi = last_confirmed_swing_high(highs5, PIVOT_L)
                    idx_lo = last_confirmed_swing_low(lows5, PIVOT_L)
                    if idx_hi is None or idx_lo is None:
                        continue

                    lastSwingHigh = highs5[idx_hi]
                    lastSwingLow = lows5[idx_lo]

                    prev_close = c5w[-2]["c"]
                    bosUp = (prev_close <= lastSwingHigh) and (closed["c"] > lastSwingHigh)
                    bosDown = (prev_close >= lastSwingLow) and (closed["c"] < lastSwingLow)

                    # ============================
                    # 1) Manage active signal first
                    # ============================
                    if sig.active:
                        # PRE_TP1: initial stop + TP1
                        if sig.phase == "PRE_TP1":
                            if sig.side == "LONG":
                                if closed["l"] <= sig.stop_init:
                                    await notify(f"{SYMBOL} LONG invalidated ❌ stop tagged {sig.stop_init:.2f}")
                                    safe_append({
                                        "type": "STOP",
                                        "trade_id": sig.trade_id,
                                        "side": sig.side,
                                        "phase": "PRE_TP1",
                                        "stop": sig.stop_init,
                                        "exit_price": sig.stop_init,
                                        "t": closed["t"],
                                    })
                                    sig.clear()

                                elif (not sig.tp1_sent) and closed["h"] >= sig.tp1:
                                    sig.tp1_sent = True
                                    sig.tp1_t = closed["t"]

                                    await notify(f"{SYMBOL} LONG TP1 ✅ hit {sig.tp1:.2f} (paper close {int(TP1_PARTIAL_PCT*100)}%)")
                                    safe_append({
                                        "type": "TP1",
                                        "trade_id": sig.trade_id,
                                        "side": sig.side,
                                        "tp1": sig.tp1,
                                        "tp1_partial_pct": TP1_PARTIAL_PCT,
                                        "tp1_t": sig.tp1_t,
                                        "t": closed["t"],
                                    })

                                    sig.phase = "RUNNER"
                                    sig.highest_high_since_tp1 = closed["h"]
                                    sig.lowest_low_since_tp1 = closed["l"]
                                    sig.struct_stop = None
                                    sig.atr_stop = None

                            else:  # SHORT
                                if closed["h"] >= sig.stop_init:
                                    await notify(f"{SYMBOL} SHORT invalidated ❌ stop tagged {sig.stop_init:.2f}")
                                    safe_append({
                                        "type": "STOP",
                                        "trade_id": sig.trade_id,
                                        "side": sig.side,
                                        "phase": "PRE_TP1",
                                        "stop": sig.stop_init,
                                        "exit_price": sig.stop_init,
                                        "t": closed["t"],
                                    })
                                    sig.clear()

                                elif (not sig.tp1_sent) and closed["l"] <= sig.tp1:
                                    sig.tp1_sent = True
                                    sig.tp1_t = closed["t"]

                                    await notify(f"{SYMBOL} SHORT TP1 ✅ hit {sig.tp1:.2f} (paper close {int(TP1_PARTIAL_PCT*100)}%)")
                                    safe_append({
                                        "type": "TP1",
                                        "trade_id": sig.trade_id,
                                        "side": sig.side,
                                        "tp1": sig.tp1,
                                        "tp1_partial_pct": TP1_PARTIAL_PCT,
                                        "tp1_t": sig.tp1_t,
                                        "t": closed["t"],
                                    })

                                    sig.phase = "RUNNER"
                                    sig.highest_high_since_tp1 = closed["h"]
                                    sig.lowest_low_since_tp1 = closed["l"]
                                    sig.struct_stop = None
                                    sig.atr_stop = None

                        # RUNNER: best-of stops + EMA cross exit + time stop
                        elif sig.phase == "RUNNER":
                            sig.highest_high_since_tp1 = max(sig.highest_high_since_tp1, closed["h"])
                            sig.lowest_low_since_tp1 = min(sig.lowest_low_since_tp1, closed["l"])

                            # BE stop
                            be_stop = (sig.entry + be_buf) if sig.side == "LONG" else (sig.entry - be_buf)

                            # Structure trailing AFTER TP1 using timestamps (robust)
                            if sig.tp1_t is not None:
                                if sig.side == "LONG":
                                    pidx = last_confirmed_swing_low(lows5, PIVOT_L)
                                    if pidx is not None and c5w[pidx]["t"] >= sig.tp1_t:
                                        new_struct_stop = lows5[pidx] - struct_pad
                                        sig.struct_stop = max(sig.struct_stop, new_struct_stop) if sig.struct_stop is not None else new_struct_stop
                                else:
                                    pidx = last_confirmed_swing_high(highs5, PIVOT_L)
                                    if pidx is not None and c5w[pidx]["t"] >= sig.tp1_t:
                                        new_struct_stop = highs5[pidx] + struct_pad
                                        sig.struct_stop = min(sig.struct_stop, new_struct_stop) if sig.struct_stop is not None else new_struct_stop

                            # ATR seatbelt trail
                            if sig.side == "LONG":
                                new_atr_stop = sig.highest_high_since_tp1 - atr_seatbelt_dist
                                sig.atr_stop = max(sig.atr_stop, new_atr_stop) if sig.atr_stop is not None else new_atr_stop
                            else:
                                new_atr_stop = sig.lowest_low_since_tp1 + atr_seatbelt_dist
                                sig.atr_stop = min(sig.atr_stop, new_atr_stop) if sig.atr_stop is not None else new_atr_stop

                            # Best protection stop
                            if sig.side == "LONG":
                                runner_stop = be_stop
                                if sig.struct_stop is not None:
                                    runner_stop = max(runner_stop, sig.struct_stop)
                                if sig.atr_stop is not None:
                                    runner_stop = max(runner_stop, sig.atr_stop)

                                # stop tagged?
                                if closed["l"] <= runner_stop:
                                    await notify(f"{SYMBOL} LONG RUNNER 🏁 stop hit {runner_stop:.2f}")
                                    safe_append({
                                        "type": "RUNNER_EXIT",
                                        "trade_id": sig.trade_id,
                                        "side": sig.side,
                                        "reason": "STOP",
                                        "runner_stop": runner_stop,
                                        "exit_price": runner_stop,
                                        "tp1_t": sig.tp1_t,
                                        "t": closed["t"],
                                    })
                                    sig.clear()
                                else:
                                    ema_cross_exit = crossed_down(sig.prev_efast5, sig.prev_eslow5, efast5, eslow5)
                                    time_exit = (sig.tp1_t is not None) and ((closed["t"] - sig.tp1_t) >= 60 * 60)

                                    if ema_cross_exit:
                                        await notify(f"{SYMBOL} LONG RUNNER 🏁 EMA cross exit (9<21)")
                                        safe_append({
                                            "type": "RUNNER_EXIT",
                                            "trade_id": sig.trade_id,
                                            "side": sig.side,
                                            "reason": "EMA_CROSS",
                                            "exit_price": closed["c"],
                                            "tp1_t": sig.tp1_t,
                                            "ema5_fast": efast5,
                                            "ema5_slow": eslow5,
                                            "t": closed["t"],
                                        })
                                        sig.clear()
                                    elif time_exit:
                                        await notify(f"{SYMBOL} LONG RUNNER 🏁 time stop exit (~60 mins)")
                                        safe_append({
                                            "type": "RUNNER_EXIT",
                                            "trade_id": sig.trade_id,
                                            "side": sig.side,
                                            "reason": "TIME_STOP",
                                            "exit_price": closed["c"],
                                            "tp1_t": sig.tp1_t,
                                            "t": closed["t"],
                                        })
                                        sig.clear()

                            else:  # SHORT
                                runner_stop = be_stop
                                if sig.struct_stop is not None:
                                    runner_stop = min(runner_stop, sig.struct_stop)
                                if sig.atr_stop is not None:
                                    runner_stop = min(runner_stop, sig.atr_stop)

                                if closed["h"] >= runner_stop:
                                    await notify(f"{SYMBOL} SHORT RUNNER 🏁 stop hit {runner_stop:.2f}")
                                    safe_append({
                                        "type": "RUNNER_EXIT",
                                        "trade_id": sig.trade_id,
                                        "side": sig.side,
                                        "reason": "STOP",
                                        "runner_stop": runner_stop,
                                        "exit_price": runner_stop,
                                        "tp1_t": sig.tp1_t,
                                        "t": closed["t"],
                                    })
                                    sig.clear()
                                else:
                                    ema_cross_exit = crossed_up(sig.prev_efast5, sig.prev_eslow5, efast5, eslow5)
                                    time_exit = (sig.tp1_t is not None) and ((closed["t"] - sig.tp1_t) >= 60 * 60)

                                    if ema_cross_exit:
                                        await notify(f"{SYMBOL} SHORT RUNNER 🏁 EMA cross exit (9>21)")
                                        safe_append({
                                            "type": "RUNNER_EXIT",
                                            "trade_id": sig.trade_id,
                                            "side": sig.side,
                                            "reason": "EMA_CROSS",
                                            "exit_price": closed["c"],
                                            "tp1_t": sig.tp1_t,
                                            "ema5_fast": efast5,
                                            "ema5_slow": eslow5,
                                            "t": closed["t"],
                                        })
                                        sig.clear()
                                    elif time_exit:
                                        await notify(f"{SYMBOL} SHORT RUNNER 🏁 time stop exit (~60 mins)")
                                        safe_append({
                                            "type": "RUNNER_EXIT",
                                            "trade_id": sig.trade_id,
                                            "side": sig.side,
                                            "reason": "TIME_STOP",
                                            "exit_price": closed["c"],
                                            "tp1_t": sig.tp1_t,
                                            "t": closed["t"],
                                        })
                                        sig.clear()

                    # Update prev EMA values for next-bar cross detection
                    sig.prev_efast5 = efast5
                    sig.prev_eslow5 = eslow5

                    # ============================
                    # 2) Structure state machine (only if not in active trade)
                    # ============================
                    if not sig.active:
                        # If bias/trend flips while waiting, drop setup
                        if structure.waitingRetest:
                            if structure.direction == "LONG" and (not biasLong or not emaTrendLong):
                                structure.reset()
                            elif structure.direction == "SHORT" and (not biasShort or not emaTrendShort):
                                structure.reset()

                        # Arm BOS -> WAIT_RETEST
                        if bosUp and biasLong and emaTrendLong:
                            structure.reset()
                            structure.direction = "LONG"
                            structure.bosLevel = lastSwingHigh
                            structure.waitingRetest = True
                            structure.retestRef = None
                            structure.accCount = 0
                            structure.bosSwingLow = lastSwingLow

                        elif bosDown and biasShort and emaTrendShort:
                            structure.reset()
                            structure.direction = "SHORT"
                            structure.bosLevel = lastSwingLow
                            structure.waitingRetest = True
                            structure.retestRef = None
                            structure.accCount = 0
                            structure.bosSwingHigh = lastSwingHigh

                        # Retest
                        if structure.waitingRetest and structure.bosLevel is not None:
                            if structure.direction == "LONG":
                                if closed["l"] <= (structure.bosLevel + retest_buf):
                                    structure.retestRef = structure.bosLevel
                                    structure.accCount = 0
                            else:
                                if closed["h"] >= (structure.bosLevel - retest_buf):
                                    structure.retestRef = structure.bosLevel
                                    structure.accCount = 0

                        # Acceptance closes
                        if structure.waitingRetest and structure.retestRef is not None:
                            if structure.direction == "LONG":
                                structure.accCount = structure.accCount + 1 if closed["c"] > structure.retestRef else 0
                            else:
                                structure.accCount = structure.accCount + 1 if closed["c"] < structure.retestRef else 0

                        accepted = structure.waitingRetest and structure.retestRef is not None and structure.accCount >= ACCEPT_BARS

                        # --- journal: decision report (rule-based) ---
                        # Determine working direction even before acceptance
                        working_dir = structure.direction
                        if working_dir is None:
                            if bosUp:
                                working_dir = "LONG"
                            elif bosDown:
                                working_dir = "SHORT"

                        gates_required = ["bias_1h", "ema_trend_5m", "bos", "retest", "acceptance"]
                        gates_passed = []
                        gates_failed = []

                        # Gate: BOS (satisfied once we're in BOS->retest state)
                        bos_gate = bool(structure.waitingRetest)
                        (gates_passed if bos_gate else gates_failed).append("bos")

                        # Gate: Retest
                        retest_gate = bool(structure.retestRef is not None)
                        (gates_passed if retest_gate else gates_failed).append("retest")

                        # Gate: Acceptance
                        acc_gate = bool(accepted)
                        (gates_passed if acc_gate else gates_failed).append("acceptance")

                        # Gates: bias + ema trend depend on direction
                        if working_dir == "LONG":
                            (gates_passed if biasLong else gates_failed).append("bias_1h")
                            (gates_passed if emaTrendLong else gates_failed).append("ema_trend_5m")
                        elif working_dir == "SHORT":
                            (gates_passed if biasShort else gates_failed).append("bias_1h")
                            (gates_passed if emaTrendShort else gates_failed).append("ema_trend_5m")
                        else:
                            gates_failed.extend(["bias_1h", "ema_trend_5m"])

                        # Action classification
                        if sig.active:
                            action = "MANAGE"
                        elif accepted and working_dir == "LONG" and biasLong and emaTrendLong:
                            action = "ENTER_LONG"
                        elif accepted and working_dir == "SHORT" and biasShort and emaTrendShort:
                            action = "ENTER_SHORT"
                        elif structure.waitingRetest:
                            action = "WATCH"
                        else:
                            action = "NO_TRADE"

                        # Risk plan (only filled on ENTER_*)
                        entry_px = closed["c"] if action in ("ENTER_LONG", "ENTER_SHORT") else ""
                        stop_px = ""
                        tp1_px = ""
                        rr_to_tp1 = ""
                        inv_px = ""

                        if action == "ENTER_LONG" and structure.bosSwingLow is not None:
                            stop_px = float(structure.bosSwingLow) - (a * 0.10)
                            inv_px = stop_px
                            R_tmp = float(entry_px) - stop_px
                            if R_tmp > 0:
                                tp1_px = float(entry_px) + TP1_R_MULT * R_tmp
                                rr_to_tp1 = 1.0
                        elif action == "ENTER_SHORT" and structure.bosSwingHigh is not None:
                            stop_px = float(structure.bosSwingHigh) + (a * 0.10)
                            inv_px = stop_px
                            R_tmp = stop_px - float(entry_px)
                            if R_tmp > 0:
                                tp1_px = float(entry_px) - TP1_R_MULT * R_tmp
                                rr_to_tp1 = 1.0

                        confidence = int(100 * (len(set(gates_passed)) / max(1, len(set(gates_required)))))

                        report = {
                            "symbol": SYMBOL,
                            "mode": "signal_only",
                            "strategy": "BOS_RETEST_ACCEPT_V1",
                            "data_fresh_ms": int((time.time() - (closed["t"] + TF_SECONDS)) * 1000),
                            "px_last": closed["c"],
                            "spread_bps": "",
                            "vwap_5m": "",
                            "poc_5m": "",
                            "vwap_1h": "",
                            "poc_1h": "",
                            "ema9_1h": efast1,
                            "ema21_1h": eslow1,
                            "bias_1h": "LONG" if biasLong else ("SHORT" if biasShort else "NEUTRAL"),
                            "bias_reason": "ema9>ema21" if biasLong else ("ema9<ema21" if biasShort else "flat"),
                            "rsi_1h": "",
                            "bos_dir": "UP" if bosUp else ("DOWN" if bosDown else "NONE"),
                            "bos_level": structure.bosLevel or "",
                            "retest_level": structure.retestRef or "",
                            "retest_state": "TOUCHED" if structure.retestRef is not None else ("ARMED" if structure.waitingRetest else "NONE"),
                            "acceptance_bars": structure.accCount or 0,
                            "acceptance_required": ACCEPT_BARS,
                            "acceptance_state": "PASS" if accepted else ("FAIL" if structure.retestRef is not None else "NA"),
                            "vol_5m": "",
                            "vol_ma_20": "",
                            "vol_state": "NA",
                            "vol_reason": "",
                            "entry_plan": "LIMIT" if action in ("ENTER_LONG", "ENTER_SHORT") else "NONE",
                            "entry_px": entry_px,
                            "invalidation_px": inv_px,
                            "stop_px": stop_px,
                            "tp1_px": tp1_px,
                            "runner_trail": "ATR+STRUCTURE",
                            "rr_to_tp1": rr_to_tp1,
                            "gates_required": gates_required,
                            "gates_passed": sorted(set(gates_passed)),
                            "gates_failed": sorted(set(gates_failed)),
                            "action": action,
                            "confidence": confidence,
                            "notes": "",
                        }

                        journal.write_decision(report)

                        # If we're signaling an entry, also write a trade-intent row (paper journal)
                        if action in ("ENTER_LONG", "ENTER_SHORT"):
                            journal.write_trade({
                                "symbol": SYMBOL,
                                "side": "LONG" if action == "ENTER_LONG" else "SHORT",
                                "qty": "",
                                "entry_px": entry_px,
                                "stop_px": stop_px,
                                "tp1_px": tp1_px,
                                "exit_px": "",
                                "pnl_usd": "",
                                "pnl_r": "",
                                "reason": "|".join(sorted(set(gates_passed))),
                                "order_id": "",
                                "fill_id": "",
                                "mode": "signal_only",
                            })

                        # ============================
                        # 3) Entry (signal-only) + init TP1 + JSON ENTER event
                        # ============================
                        if accepted:
                            entry = closed["c"]

                            if structure.direction == "LONG" and biasLong and emaTrendLong:
                                if structure.bosSwingLow is None:
                                    structure.reset()
                                else:
                                    stop = float(structure.bosSwingLow) - (a * 0.10)
                                    R = entry - stop
                                    if R <= 0:
                                        structure.reset()
                                    else:
                                        sig.active = True
                                        sig.phase = "PRE_TP1"
                                        sig.side = "LONG"
                                        sig.trade_id = new_trade_id()
                                        sig.entry_t = closed["t"]
                                        sig.entry = entry
                                        sig.stop_init = stop
                                        sig.R = R
                                        sig.tp1 = entry + TP1_R_MULT * R

                                        safe_append({
                                            "type": "ENTER",
                                            "trade_id": sig.trade_id,
                                            "side": sig.side,
                                            "entry": sig.entry,
                                            "stop": sig.stop_init,
                                            "R": sig.R,
                                            "tp1": sig.tp1,
                                            "entry_t": sig.entry_t,
                                            "atr": a,
                                            "retest_buf": retest_buf,
                                            "bos_level": structure.bosLevel,
                                            "bos_swing_low": structure.bosSwingLow,
                                            "ema5_fast": efast5,
                                            "ema5_slow": eslow5,
                                            "ema1_fast": efast1,
                                            "ema1_slow": eslow1,
                                            "t": closed["t"],
                                        })

                                        await notify(
                                            f"{SYMBOL} LONG ✅ (BOS+Retest+Accept)\n"
                                            f"entry={entry:.2f} stop={stop:.2f}\n"
                                            f"TP1(1R)={sig.tp1:.2f}\n"
                                            f"atr={a:.2f} retest_buf={retest_buf:.2f}\n"
                                            f"id={sig.trade_id}"
                                        )
                                        structure.reset()

                            elif structure.direction == "SHORT" and biasShort and emaTrendShort:
                                if structure.bosSwingHigh is None:
                                    structure.reset()
                                else:
                                    stop = float(structure.bosSwingHigh) + (a * 0.10)
                                    R = stop - entry
                                    if R <= 0:
                                        structure.reset()
                                    else:
                                        sig.active = True
                                        sig.phase = "PRE_TP1"
                                        sig.side = "SHORT"
                                        sig.trade_id = new_trade_id()
                                        sig.entry_t = closed["t"]
                                        sig.entry = entry
                                        sig.stop_init = stop
                                        sig.R = R
                                        sig.tp1 = entry - TP1_R_MULT * R

                                        safe_append({
                                            "type": "ENTER",
                                            "trade_id": sig.trade_id,
                                            "side": sig.side,
                                            "entry": sig.entry,
                                            "stop": sig.stop_init,
                                            "R": sig.R,
                                            "tp1": sig.tp1,
                                            "entry_t": sig.entry_t,
                                            "atr": a,
                                            "retest_buf": retest_buf,
                                            "bos_level": structure.bosLevel,
                                            "bos_swing_high": structure.bosSwingHigh,
                                            "ema5_fast": efast5,
                                            "ema5_slow": eslow5,
                                            "ema1_fast": efast1,
                                            "ema1_slow": eslow1,
                                            "t": closed["t"],
                                        })

                                        await notify(
                                            f"{SYMBOL} SHORT ✅ (BOS+Retest+Accept)\n"
                                            f"entry={entry:.2f} stop={stop:.2f}\n"
                                            f"TP1(1R)={sig.tp1:.2f}\n"
                                            f"atr={a:.2f} retest_buf={retest_buf:.2f}\n"
                                            f"id={sig.trade_id}"
                                        )
                                        structure.reset()
                            else:
                                structure.reset()

        except Exception as e:
            log({"event": "ws_disconnected", "error": str(e)})
            await asyncio.sleep(5)


async def run_all():
    tasks = [main()]
    if os.getenv("TELEGRAM_CONTROL", "0") == "1":
        tasks.append(telegram_poll_commands(handle_command))
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(run_all())
