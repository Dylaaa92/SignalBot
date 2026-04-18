import asyncio
import json
import time
import random
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
    STRATEGY,
)

from indicators import ema
from pivots import last_confirmed_swing_low, last_confirmed_swing_high
from logger import log
from notifier import notify
from journal import Journal
from trade_events import append_event, new_trade_id
from dataclasses import dataclass
from datetime import datetime, timezone

async def safe_notify(msg: str):
    try:
        await notify(msg)
    except Exception as e:
        log({"event": "notify_failed", "error": str(e), "msg": msg[:200]})

# ADX trend tracking — last 6 readings per symbol.
_adx_history: dict[str, list[float]] = {}
# Rate-limit tracker for ADX rising-toward-floor alerts — 60 min cooldown.
_adx_rising_last_sent: dict[str, float] = {}

@dataclass
class BotStats:
    day: str = ""
    trades_entered: int = 0
    tp1_hits: int = 0
    stops: int = 0
    runner_exits: int = 0
    invalidations: int = 0
    skips: int = 0
    consec_stops: int = 0          # consecutive stops without an intervening TP1
    daily_realised_pnl: float = 0.0  # running USD P&L for today (losses are negative)
    cooldown_until: float = 0.0    # unix ts; no new entries before this time

    def reset_if_new_day(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if self.day != today:
            self.day = today
            self.trades_entered = 0
            self.tp1_hits = 0
            self.stops = 0
            self.runner_exits = 0
            self.invalidations = 0
            self.skips = 0
            self.consec_stops = 0
            self.daily_realised_pnl = 0.0
            self.cooldown_until = 0.0

STATS = BotStats(day=datetime.now(timezone.utc).date().isoformat())



# ============================
# SYSTEMD CONTROL (NEW)
# ============================

from config import ALL_SYMBOLS, SYMBOL_JITTER

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
RUNNER_ATR_MULT_1H = float(os.getenv("RUNNER_ATR_MULT_1H", "1.5"))  # V2 seatbelt on 1H ATR


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
                                    time_exit = (sig.tp1_t is not None) and ((closed["t"] - sig.tp1_t) >= RUNNER_TIME_STOP_BARS * TF_SECONDS)

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
                                    time_exit = (sig.tp1_t is not None) and ((closed["t"] - sig.tp1_t) >= RUNNER_TIME_STOP_BARS * TF_SECONDS)

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

async def get_agent_regime_summary(symbol_data: list[dict]) -> str | None:
    """
    Call the Anthropic API to generate a short regime summary paragraph
    from per-symbol status data collected at each hourly digest.

    Auth note: in production, set ANTHROPIC_API_KEY in the .env file.
    The header is conditionally included so the function is a no-op cost-wise
    if the key is missing.
    """
    try:
        from datetime import datetime, timezone as _tz
        _now_str = datetime.now(tz=_tz.utc).strftime("%Y-%m-%d %H:%M UTC")

        _rows = []
        for _s in symbol_data:
            _sym  = _s.get("symbol", "?")
            _act  = _s.get("action", "?")
            _rsn  = _s.get("reason") or "-"
            _b4h  = _s.get("bias4h") or "?"
            _bos  = _s.get("bos1h") or "?"
            _rsi  = _s.get("rsi5m")
            _adx  = _s.get("adx")
            _adir = _s.get("adx_direction") or "?"
            _aflr = _s.get("adx_floor_used") or _s.get("adx_floor") or "?"
            _itr  = _s.get("in_trade", False)
            _rsi_s = f"{_rsi:.1f}" if _rsi is not None else "?"
            _adx_s = f"{_adx:.1f}" if _adx is not None else "?"
            _rows.append(
                f"{_sym}: action={_act} reason={_rsn} bias4h={_b4h} bos1h={_bos} "
                f"rsi={_rsi_s} adx={_adx_s} adx_dir={_adir} adx_floor={_aflr} in_trade={_itr}"
            )

        _user_msg = (
            f"Date/time: {_now_str}\n\n"
            "Per-symbol state:\n"
            + "\n".join(_rows)
        )

        _api_key = os.getenv("ANTHROPIC_API_KEY", "")
        # If ANTHROPIC_API_KEY is not set the x-api-key header is omitted.
        # Outside the claude.ai environment you must set this env var.
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            **({"x-api-key": _api_key} if _api_key else {}),
        }

        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 300,
            "system": (
                "You are a trading regime analyst monitoring a multi-symbol crypto and "
                "equity trading bot. You receive per-symbol market state data every hour. "
                "Write a concise 3-4 sentence paragraph (no bullet points, no headers) "
                "summarising the current market regime, which symbols are closest to "
                "generating signals, and whether the overall environment favours waiting "
                "or watching specific setups. Be direct and specific — mention symbol "
                "names and actual numbers. Do not give financial advice or recommend trades."
            ),
            "messages": [{"role": "user", "content": _user_msg}],
        }

        async with httpx.AsyncClient(timeout=20.0) as _client:
            _resp = await _client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            )
            _data = _resp.json()
            return _data["content"][0]["text"]

    except Exception as _e:
        log({"event": "agent_regime_summary_error", "error": str(_e)})
        return None


async def main_intraday_swing_v2():
    """
    Intraday Swing v2 runner:
      - 4H bias
      - 1H BOS (pivot break)
      - 15m reclaim entry + RSI filter
      - Paper mode uses Executor paper ledger
      - Live mode guarded by LIVE_GUARD=I_UNDERSTAND
    """
    from executor import Executor
    from strategies.intraday_swing_v2 import candle_snapshot, decide
    from context_engine import build_context
    from veto_engine import evaluate_veto
    from trade_state_store import save_trade_state, load_trade_state, clear_trade_state
    from config import (
        ENV, SYMBOL,
        EMA_FAST, EMA_SLOW,
        EXEC_TF_SECONDS, STRUCT_TF_SECONDS, BIAS_TF_SECONDS,
        PIVOT_1H_LEN, RSI_LEN, RSI_LONG_MIN, RSI_SHORT_MAX,
        ATR_BUF_MULT, TP1_QTY_PCT, RISK_R,
        REQUIRE_1H_EMA21_SIDE, ALLOW_COUNTER_TREND,
        RISK_USDT_PER_TRADE,
        MAX_POSITION_NOTIONAL_USDT,
        DAILY_MAX_LOSS_USDT,
        MAX_CONSEC_LOSSES,
        COOLDOWN_SECONDS,
        TRADING_MODE,
        ADX_MIN_STRENGTH,
        VOL_SPIKE_MULT,
        TP2_R_MULT,
        MAX_CORR_POSITIONS,
        MAX_DAILY_DD_PCT,
        MIN_BAR_RANGE_ATR_MULT,
    )
    from logger import log
    from notifier import notify
    from trade_events import append_event, new_trade_id

    MANAGE_POLL_SECONDS = float(os.getenv("MANAGE_POLL_SECONDS", "1"))
    # How often to probe for a new candle when NOT managing an open trade.
    # 15s is fine — a 5m candle closes every 300s, so we'll catch it within 15s.
    # Keeps total REST calls to ~0.07/s per symbol vs 1/s previously.
    CANDLE_PROBE_SECONDS = float(os.getenv("CANDLE_PROBE_SECONDS", "15"))

    exec_layer = Executor(env=ENV, symbol=SYMBOL)

    params = {
        "SYMBOL": SYMBOL,
        "EMA_FAST": EMA_FAST,
        "EMA_SLOW": EMA_SLOW,
        "PIVOT_1H_LEN": PIVOT_1H_LEN,
        "RSI_LEN": RSI_LEN,
        "RSI_LONG_MIN": RSI_LONG_MIN,
        "RSI_SHORT_MAX": RSI_SHORT_MAX,
        "ATR_BUF_MULT": ATR_BUF_MULT,
        "TP1_QTY_PCT": TP1_QTY_PCT,
        "RISK_R": RISK_R,
        "REQUIRE_1H_EMA21_SIDE": REQUIRE_1H_EMA21_SIDE,
        "ALLOW_COUNTER_TREND": ALLOW_COUNTER_TREND,
        "ADX_MIN_STRENGTH": ADX_MIN_STRENGTH,
        "VOL_SPIKE_MULT": VOL_SPIKE_MULT,
        "TP2_R_MULT": TP2_R_MULT,
        "MAX_CORR_POSITIONS": MAX_CORR_POSITIONS,
        "MAX_DAILY_DD_PCT": MAX_DAILY_DD_PCT,
    }

    log({"event": "startup", "symbol": SYMBOL, "strategy": "INTRADAY_SWING_V2", "mode": TRADING_MODE})
    await notify(f"✅ Intraday Swing v2 running: {SYMBOL} | mode={TRADING_MODE} | 4H/1H/5m")

    # Stagger polling across symbols so they don't hammer the API simultaneously.
    # Offsets come from config.SYMBOL_JITTER (slot × 15s). New symbols get slot
    # 0 if not listed, which is safe — they'll just share BTC's offset.
    _jitter = SYMBOL_JITTER.get(SYMBOL, 0)
    if _jitter:
        log({"event": "startup_jitter", "symbol": SYMBOL, "sleep_s": _jitter})
        await asyncio.sleep(_jitter)

    # Startup metadata audit — runs in both paper and live modes.
    # Validates live Hyperliquid szDecimals against local hardcoded fallback.
    # A mismatch here means sizing/rounding would silently use wrong precision.
    try:
        from hl_trade import _build_live_sz_decimals_map, audit_symbol_sz_decimals
        from hyperliquid.utils import constants as _hl_const
        _hl_rest_url = _hl_const.MAINNET_API_URL if ENV == "mainnet" else _hl_const.TESTNET_API_URL
        _live_sz_map = await asyncio.to_thread(_build_live_sz_decimals_map, _hl_rest_url)
        _audit = audit_symbol_sz_decimals(SYMBOL, _live_sz_map)
        log(_audit)
        if _audit["event"] == "metadata_mismatch":
            await safe_notify(
                f"⚠️ {SYMBOL} metadata mismatch — sz_decimals: "
                f"live={_audit.get('live_sz_decimals')} local={_audit.get('local_sz_decimals')} "
                f"reason={_audit.get('reason')}"
            )
    except Exception as _ae:
        log({"event": "metadata_audit_error", "symbol": SYMBOL, "error": str(_ae)})

    # ── ADX trend tracker ────────────────────────────────────────────────────
    async def track_adx_trend(symbol: str, d: dict):
        try:
            dbg = d.get("debug") or {}
            adx_val = dbg.get("adx_strength") if isinstance(dbg, dict) else None
            if adx_val is None:
                return

            hist = _adx_history.setdefault(symbol, [])
            hist.append(float(adx_val))
            _adx_history[symbol] = hist[-6:]

            if len(_adx_history[symbol]) < 4:
                return

            readings = _adx_history[symbol]
            early_avg = (readings[0] + readings[1]) / 2.0
            late_avg  = (readings[-2] + readings[-1]) / 2.0
            diff = late_avg - early_avg
            if diff > 1.5:
                adx_direction = "RISING"
            elif diff < -1.5:
                adx_direction = "FALLING"
            else:
                adx_direction = "FLAT"

            # Update status file with direction and history
            _status_path = f"/etc/signalbot/{symbol.replace(':', '_')}_status.json"
            try:
                try:
                    with open(_status_path) as _sf:
                        _payload = json.load(_sf)
                except Exception:
                    _payload = {}
                _payload["adx_direction"] = adx_direction
                _payload["adx_history"] = list(_adx_history[symbol])
                _tmp = _status_path + ".tmp"
                with open(_tmp, "w") as _tf:
                    json.dump(_payload, _tf)
                os.replace(_tmp, _status_path)
            except Exception:
                pass  # never block the trading loop

            if adx_direction == "RISING":
                adx_floor = dbg.get("adx_floor_used", 25)
                gap = float(adx_floor) - float(adx_val)
                if 0 < gap <= 4.0:
                    now = time.time()
                    if now - _adx_rising_last_sent.get(symbol, 0.0) > 3600.0:
                        _adx_rising_last_sent[symbol] = now
                        await notify(
                            f"\U0001f4c8 {symbol} | ADX rising toward floor — "
                            f"ADX={adx_val:.1f}, floor={adx_floor}, gap={gap:.1f}. "
                            f"Watching for breakout setup."
                        )
        except Exception as e:
            log({"event": "adx_trend_error", "symbol": symbol, "error": str(e)})
    # ── End ADX trend tracker ─────────────────────────────────────────────────

    last_15m_t = None
    in_trade = False
    exchange_position_open = False
    trade_side = None
    entry_px = None
    stop_px = None
    tp1_px = None
    tp1_done = False
    trade_id = None
    size = 0.0
    pending_order_id = None   # OID returned by exchange after entry order; None when flat/filled
    pending_order_ts = 0.0    # unix ts when that order was sent
    ENTRY_FILL_TIMEOUT = int(os.getenv("ENTRY_FILL_TIMEOUT_SECONDS", "60"))

    # Runner trailing state (active when tp1_done=True and size > 0)
    tp1_t = 0.0              # unix ts when TP1 hit; used to filter post-TP1 pivots
    runner_highest_high = 0.0
    runner_lowest_low = 0.0
    runner_atr_stop = None   # ratcheting ATR seatbelt level
    runner_struct_stop = None  # ratcheting 1H structural level
    last_runner_trail_t = 0  # last 5m candle t on which we updated trailing

    # Cache for manage_open_trade_fast — re-fetch at most every 5s to avoid rate-limiting
    _c5_live_cache: list = []
    _c5_live_cache_ts: float = 0.0
    _C5_LIVE_CACHE_TTL = 5.0  # seconds

    async def sync_live_trade_state():
        nonlocal in_trade, exchange_position_open, trade_side, size, tp1_done
        nonlocal entry_px, stop_px, tp1_px, trade_id
        nonlocal pending_order_id, pending_order_ts
        nonlocal tp1_t, runner_highest_high, runner_lowest_low
        nonlocal runner_atr_stop, runner_struct_stop, last_runner_trail_t

        snap = await exec_layer.live_position_snapshot()

        if not snap.get("ok"):
            log({
                "event": "sync_live_trade_state_failed",
                "symbol": SYMBOL,
                "error": snap.get("error"),
            })
            return

        exchange_in_position = bool(snap.get("in_position"))
        exchange_side = snap.get("side")
        exchange_size = float(snap.get("size", 0.0))

        exchange_position_open = exchange_in_position

        # Exchange says flat -> clear local state
        if not exchange_in_position:
            # If an entry order was just sent and is within its fill window,
            # don't clear state — the order may still fill in the next few seconds.
            if pending_order_id is not None:
                elapsed = time.time() - pending_order_ts
                if elapsed < ENTRY_FILL_TIMEOUT:
                    return  # still waiting; preserve local trade params
                # Order has timed out without a fill — cancel it and clean up.
                log({
                    "event":   "pending_order_timeout",
                    "symbol":  SYMBOL,
                    "oid":     pending_order_id,
                    "elapsed": int(elapsed),
                })
                try:
                    await asyncio.to_thread(exec_layer.hl.cancel_all, SYMBOL)
                except Exception as _ce:
                    log({"event": "pending_order_cancel_error", "symbol": SYMBOL, "error": str(_ce)})
                await notify(
                    f"⚠️ {SYMBOL} entry order not filled after {int(elapsed)}s — cancelled\n"
                    f"oid={pending_order_id} | id={trade_id}"
                )
                pending_order_id = None
                pending_order_ts = 0.0

            if in_trade or trade_side is not None or size > 0:
                log({
                    "event": "position_sync_flattened_local_state",
                    "symbol": SYMBOL,
                    "prev_side": trade_side,
                    "prev_size": size,
                })

            in_trade = False
            exchange_position_open = False
            trade_side = None
            size = 0.0
            tp1_done = False

            # 🔴 CRITICAL FIX — clear ALL local trade state
            entry_px = None
            stop_px = None
            tp1_px = None
            trade_id = None

            tp1_t = 0.0
            runner_highest_high = 0.0
            runner_lowest_low = 0.0
            runner_atr_stop = None
            runner_struct_stop = None
            last_runner_trail_t = 0

            clear_trade_state(SYMBOL)
            return

        # Exchange says a position exists
        if trade_side != exchange_side or abs(size - exchange_size) > 1e-8:
            log({
                "event": "position_sync_exchange_position_seen",
                "symbol": SYMBOL,
                "exchange_side": exchange_side,
                "exchange_size": exchange_size,
                "local_in_trade_before": in_trade,
                "local_side_before": trade_side,
            })

        trade_side = exchange_side
        size = exchange_size

        # Entry order has filled — no longer need the pending tracker.
        if pending_order_id is not None:
            log({"event": "pending_order_filled", "symbol": SYMBOL, "oid": pending_order_id})
            pending_order_id = None
            pending_order_ts = 0.0

        # If local state was lost (restart/crash), try to restore from disk.
        # Only attempt when all required fields are absent and we're in live mode.
        if TRADING_MODE == "live" and entry_px is None:
            saved = load_trade_state(SYMBOL)
            if saved and saved.get("side") == exchange_side:
                entry_px = float(saved["entry_px"])
                stop_px  = float(saved["stop_px"])
                tp1_px   = float(saved["tp1_px"])
                trade_id = str(saved["trade_id"])
                tp1_done = bool(saved.get("tp1_done", False))
                log({
                    "event":      "trade_state_restored",
                    "symbol":     SYMBOL,
                    "side":       trade_side,
                    "entry_px":   entry_px,
                    "stop_px":    stop_px,
                    "tp1_px":     tp1_px,
                    "trade_id":   trade_id,
                    "tp1_done":   tp1_done,
                    "saved_at":   saved.get("saved_at"),
                })
                await notify(
                    f"♻️ {SYMBOL} trade state restored after restart\n"
                    f"side={trade_side} entry={entry_px:.4f} stop={stop_px:.4f} "
                    f"tp1={tp1_px:.4f} tp1_done={tp1_done} id={trade_id}"
                )
            elif saved:
                # File exists but side mismatch — stale from a previous trade direction.
                log({
                    "event":         "trade_state_restore_skipped",
                    "symbol":        SYMBOL,
                    "reason":        "side_mismatch",
                    "file_side":     saved.get("side"),
                    "exchange_side": exchange_side,
                })
                clear_trade_state(SYMBOL)

        # IMPORTANT:
        # Do NOT mark local in_trade=True unless we also have local entry/stop/tp1 state.
        # Otherwise the bot will try to manage a trade it cannot reconstruct safely.
        if (
            entry_px is not None
            and stop_px is not None
            and tp1_px is not None
            and trade_id is not None
        ):
            in_trade = True
        else:
            in_trade = False

    async def manage_open_trade_fast():
        nonlocal in_trade, exchange_position_open, trade_side
        nonlocal entry_px, stop_px, tp1_px, tp1_done, trade_id, size
        nonlocal tp1_t, runner_highest_high, runner_lowest_low
        nonlocal runner_atr_stop, runner_struct_stop, last_runner_trail_t

        # If exchange says no position, nothing to manage
        if not exchange_position_open:
            return False

        # If we don't have enough local info to safely manage, don't guess
        if trade_side is None or entry_px is None or stop_px is None or tp1_px is None:
            return False

        # Use the latest 5m snapshot as a cheap proxy for most recent price action.
        # Cache for _C5_LIVE_CACHE_TTL seconds to avoid rate-limiting Hyperliquid
        # when this function is called every second during an open trade.
        nonlocal _c5_live_cache, _c5_live_cache_ts
        now_ts = time.time()
        if now_ts - _c5_live_cache_ts >= _C5_LIVE_CACHE_TTL:
            fetched = await asyncio.wait_for(
                candle_snapshot(SYMBOL, EXEC_TF_SECONDS, limit=2),
                timeout=60
            )
            if fetched:
                _c5_live_cache = fetched
                _c5_live_cache_ts = now_ts
        c5_live = _c5_live_cache
        if not c5_live:
            return False

        latest = c5_live[-1]
        lo = latest["l"]
        hi = latest["h"]

        # STOP logic
        if trade_side == "LONG" and lo <= stop_px:
            if TRADING_MODE == "paper":
                await exec_layer.paper_close(exit_price=stop_px, reason="STOP")
                fill_px = stop_px
            else:
                resp = await exec_layer.live_close_marketlike(side="LONG", size=size)
                fill_px = stop_px  # trigger level; actual fill may differ

                # Verify the position is actually flat before clearing state.
                _verify = await exec_layer.live_position_snapshot()
                if _verify.get("ok") and _verify.get("in_position"):
                    _rem = float(_verify.get("size", 0.0))
                    log({"event": "stop_close_residual", "symbol": SYMBOL,
                         "side": "LONG", "remaining": _rem, "trade_id": trade_id})
                    await notify(
                        f"⚠️ {SYMBOL} LONG stop close incomplete — {_rem:.4f} remain\n"
                        f"id={trade_id} | will retry next poll"
                    )
                    return True  # keep in_trade intact; manage_open_trade_fast retries

            append_event(SYMBOL, {
                "type": "STOP",
                "trade_id": trade_id,
                "side": trade_side,
                "exit_price": fill_px,
                "stop_px": stop_px,
                "entry_px": entry_px,
                "tp1_done": tp1_done,
                "t": latest["t"],
            })
            STATS.stops += 1
            await notify(
                f"{SYMBOL} LONG ❌ STOP triggered {stop_px:.2f} | id={trade_id}"
            )

            _stop_pnl = (fill_px - entry_px) * size
            STATS.daily_realised_pnl += _stop_pnl
            STATS.consec_stops += 1
            if STATS.consec_stops >= MAX_CONSEC_LOSSES and time.time() >= STATS.cooldown_until:
                STATS.cooldown_until = time.time() + COOLDOWN_SECONDS
                log({
                    "event":          "cooldown_armed",
                    "symbol":         SYMBOL,
                    "consec_stops":   STATS.consec_stops,
                    "cooldown_until": STATS.cooldown_until,
                })
                await notify(
                    f"🛑 {SYMBOL} cooldown armed — {STATS.consec_stops} consecutive stop(s)\n"
                    f"No new entries for {COOLDOWN_SECONDS // 60} min | "
                    f"daily_pnl={STATS.daily_realised_pnl:.2f} USD"
                )

            in_trade = False
            exchange_position_open = False
            trade_side = None
            entry_px = None
            stop_px = None
            tp1_px = None
            tp1_done = False
            trade_id = None
            size = 0.0
            tp1_t = 0.0
            runner_highest_high = 0.0
            runner_lowest_low = 0.0
            runner_atr_stop = None
            runner_struct_stop = None
            last_runner_trail_t = 0
            return True

        if trade_side == "SHORT" and hi >= stop_px:
            if TRADING_MODE == "paper":
                await exec_layer.paper_close(exit_price=stop_px, reason="STOP")
                fill_px = stop_px
            else:
                resp = await exec_layer.live_close_marketlike(side="SHORT", size=size)
                fill_px = stop_px  # trigger level; actual fill may differ

                # Verify the position is actually flat before clearing state.
                _verify = await exec_layer.live_position_snapshot()
                if _verify.get("ok") and _verify.get("in_position"):
                    _rem = float(_verify.get("size", 0.0))
                    log({"event": "stop_close_residual", "symbol": SYMBOL,
                         "side": "SHORT", "remaining": _rem, "trade_id": trade_id})
                    await notify(
                        f"⚠️ {SYMBOL} SHORT stop close incomplete — {_rem:.4f} remain\n"
                        f"id={trade_id} | will retry next poll"
                    )
                    return True  # keep in_trade intact; manage_open_trade_fast retries

            append_event(SYMBOL, {
                "type": "STOP",
                "trade_id": trade_id,
                "side": trade_side,
                "exit_price": fill_px,
                "stop_px": stop_px,
                "entry_px": entry_px,
                "tp1_done": tp1_done,
                "t": latest["t"],
            })
            STATS.stops += 1
            await notify(
                f"{SYMBOL} SHORT ❌ STOP triggered {stop_px:.2f} | id={trade_id}"
            )

            _stop_pnl = (entry_px - fill_px) * size
            STATS.daily_realised_pnl += _stop_pnl
            STATS.consec_stops += 1
            if STATS.consec_stops >= MAX_CONSEC_LOSSES and time.time() >= STATS.cooldown_until:
                STATS.cooldown_until = time.time() + COOLDOWN_SECONDS
                log({
                    "event":          "cooldown_armed",
                    "symbol":         SYMBOL,
                    "consec_stops":   STATS.consec_stops,
                    "cooldown_until": STATS.cooldown_until,
                })
                await notify(
                    f"🛑 {SYMBOL} cooldown armed — {STATS.consec_stops} consecutive stop(s)\n"
                    f"No new entries for {COOLDOWN_SECONDS // 60} min | "
                    f"daily_pnl={STATS.daily_realised_pnl:.2f} USD"
                )

            in_trade = False
            exchange_position_open = False
            trade_side = None
            entry_px = None
            stop_px = None
            tp1_px = None
            tp1_done = False
            trade_id = None
            size = 0.0
            tp1_t = 0.0
            runner_highest_high = 0.0
            runner_lowest_low = 0.0
            runner_atr_stop = None
            runner_struct_stop = None
            last_runner_trail_t = 0
            return True

        # TP1 logic
        if (not tp1_done) and trade_side == "LONG" and hi >= tp1_px:
            tp1_done = True
            qty_to_close = size * (TP1_QTY_PCT / 100.0)

            if TRADING_MODE == "paper":
                await exec_layer.paper_tp1(tp_price=tp1_px, qty_pct=TP1_QTY_PCT)
                size = max(0.0, size - qty_to_close)
            else:
                _pre_tp1_size = size
                await exec_layer.live_close_marketlike(side="LONG", size=qty_to_close)
                _verify = await exec_layer.live_position_snapshot()
                if _verify.get("ok") and _verify.get("in_position"):
                    size = float(_verify.get("size", 0.0))
                    if size >= _pre_tp1_size * 0.9:  # barely changed — close likely didn't fill
                        log({"event": "tp1_close_residual", "symbol": SYMBOL, "side": "LONG",
                             "pre_size": _pre_tp1_size, "post_size": size, "trade_id": trade_id})
                        await notify(
                            f"⚠️ {SYMBOL} LONG TP1 close may not have filled\n"
                            f"pre={_pre_tp1_size:.4f} post={size:.4f} | id={trade_id}"
                        )
                elif _verify.get("ok") and not _verify.get("in_position"):
                    size = 0.0  # fully closed
                else:
                    size = max(0.0, size - qty_to_close)  # fallback if snapshot unavailable

            stop_px = entry_px
            tp1_t = float(latest["t"])
            runner_highest_high = hi
            runner_lowest_low = lo
            runner_atr_stop = None
            runner_struct_stop = None
            last_runner_trail_t = 0

            append_event(SYMBOL, {
                "type": "TP1",
                "trade_id": trade_id,
                "side": trade_side,
                "tp1": tp1_px,
                "qty_closed": qty_to_close,
                "qty_remaining": size,
                "stop_moved_to": stop_px,
                "t": latest["t"],
            })
            STATS.tp1_hits += 1
            STATS.daily_realised_pnl += (tp1_px - entry_px) * qty_to_close
            STATS.consec_stops = 0
            await notify(
                f"{SYMBOL} LONG ✅ TP1 triggered {tp1_px:.2f} | closed={qty_to_close:.4f} rem={size:.4f}\n"
                f"Stop moved to BE: {stop_px:.2f} | id={trade_id}"
            )
            save_trade_state(
                SYMBOL,
                side=trade_side,
                entry_px=entry_px,
                stop_px=stop_px,
                tp1_px=tp1_px,
                trade_id=trade_id,
                tp1_done=True,
            )
            return True

        if (not tp1_done) and trade_side == "SHORT" and lo <= tp1_px:
            tp1_done = True
            qty_to_close = size * (TP1_QTY_PCT / 100.0)

            if TRADING_MODE == "paper":
                await exec_layer.paper_tp1(tp_price=tp1_px, qty_pct=TP1_QTY_PCT)
                size = max(0.0, size - qty_to_close)
            else:
                _pre_tp1_size = size
                await exec_layer.live_close_marketlike(side="SHORT", size=qty_to_close)
                _verify = await exec_layer.live_position_snapshot()
                if _verify.get("ok") and _verify.get("in_position"):
                    size = float(_verify.get("size", 0.0))
                    if size >= _pre_tp1_size * 0.9:  # barely changed — close likely didn't fill
                        log({"event": "tp1_close_residual", "symbol": SYMBOL, "side": "SHORT",
                             "pre_size": _pre_tp1_size, "post_size": size, "trade_id": trade_id})
                        await notify(
                            f"⚠️ {SYMBOL} SHORT TP1 close may not have filled\n"
                            f"pre={_pre_tp1_size:.4f} post={size:.4f} | id={trade_id}"
                        )
                elif _verify.get("ok") and not _verify.get("in_position"):
                    size = 0.0  # fully closed
                else:
                    size = max(0.0, size - qty_to_close)  # fallback if snapshot unavailable

            stop_px = entry_px
            tp1_t = float(latest["t"])
            runner_highest_high = hi
            runner_lowest_low = lo
            runner_atr_stop = None
            runner_struct_stop = None
            last_runner_trail_t = 0

            append_event(SYMBOL, {
                "type": "TP1",
                "trade_id": trade_id,
                "side": trade_side,
                "tp1": tp1_px,
                "qty_closed": qty_to_close,
                "qty_remaining": size,
                "stop_moved_to": stop_px,
                "t": latest["t"],
            })
            STATS.tp1_hits += 1
            STATS.daily_realised_pnl += (entry_px - tp1_px) * qty_to_close
            STATS.consec_stops = 0
            await notify(
                f"{SYMBOL} SHORT ✅ TP1 triggered {tp1_px:.2f} | closed={qty_to_close:.4f} rem={size:.4f}\n"
                f"Stop moved to BE: {stop_px:.2f} | id={trade_id}"
            )
            save_trade_state(
                SYMBOL,
                side=trade_side,
                entry_px=entry_px,
                stop_px=stop_px,
                tp1_px=tp1_px,
                trade_id=trade_id,
                tp1_done=True,
            )
            return True

        # ============================
        # Runner trailing stop update (post-TP1, once per new 5m candle)
        # ============================
        if tp1_done and size > 0 and tp1_t > 0 and latest["t"] != last_runner_trail_t:
            last_runner_trail_t = latest["t"]

            # Expand the high/low range since TP1
            if trade_side == "LONG":
                runner_highest_high = max(runner_highest_high, hi)
            else:
                runner_lowest_low = min(runner_lowest_low, lo)

            # Time stop
            time_elapsed = latest["t"] - tp1_t
            if time_elapsed >= RUNNER_TIME_STOP_BARS * EXEC_TF_SECONDS:
                log({"event": "runner_time_stop", "symbol": SYMBOL, "trade_id": trade_id,
                     "side": trade_side, "elapsed_bars": int(time_elapsed / EXEC_TF_SECONDS)})
                if TRADING_MODE == "paper":
                    await exec_layer.paper_close(exit_price=latest["c"], reason="RUNNER_TIME_STOP")
                else:
                    await exec_layer.live_close_marketlike(side=trade_side, size=size)
                append_event(SYMBOL, {
                    "type": "RUNNER_EXIT",
                    "trade_id": trade_id,
                    "side": trade_side,
                    "reason": "TIME_STOP",
                    "exit_price": latest["c"],
                    "tp1_t": tp1_t,
                    "elapsed_bars": int(time_elapsed / EXEC_TF_SECONDS),
                    "t": latest["t"],
                })
                STATS.runner_exits += 1
                await notify(
                    f"{SYMBOL} {'LONG' if trade_side == 'LONG' else 'SHORT'} 🏁 runner time stop "
                    f"({int(time_elapsed / EXEC_TF_SECONDS)} bars after TP1) | id={trade_id}"
                )
                in_trade = False
                exchange_position_open = False
                trade_side = None
                entry_px = None
                stop_px = None
                tp1_px = None
                tp1_done = False
                trade_id = None
                size = 0.0
                tp1_t = 0.0
                runner_highest_high = 0.0
                runner_lowest_low = 0.0
                runner_atr_stop = None
                runner_struct_stop = None
                last_runner_trail_t = 0
                clear_trade_state(SYMBOL)
                return True

            # Fetch 1H candles for ATR and structural pivots
            try:
                c1h = await asyncio.wait_for(
                    candle_snapshot(SYMBOL, STRUCT_TF_SECONDS, limit=50),
                    timeout=60,
                )
            except Exception as _e:
                log({"event": "runner_trail_1h_fetch_failed", "symbol": SYMBOL, "error": str(_e)})
                c1h = None

            if c1h and len(c1h) >= 15:
                atr_1h = atr_from_candles(c1h, 14)
                highs_1h = [c["h"] for c in c1h]
                lows_1h  = [c["l"] for c in c1h]

                if atr_1h:
                    struct_pad_1h = STRUCT_PAD_ATR * atr_1h
                    if trade_side == "LONG":
                        # ATR seatbelt: trail below running highest high
                        new_atr = runner_highest_high - RUNNER_ATR_MULT_1H * atr_1h
                        runner_atr_stop = max(runner_atr_stop, new_atr) if runner_atr_stop is not None else new_atr
                        # Structural: most protective 1H swing low formed since TP1
                        pidx = last_confirmed_swing_low(lows_1h, PIVOT_1H_LEN)
                        if pidx is not None and c1h[pidx]["t"] >= tp1_t:
                            new_struct = lows_1h[pidx] - struct_pad_1h
                            runner_struct_stop = max(runner_struct_stop, new_struct) if runner_struct_stop is not None else new_struct
                    else:  # SHORT
                        # ATR seatbelt: trail above running lowest low
                        new_atr = runner_lowest_low + RUNNER_ATR_MULT_1H * atr_1h
                        runner_atr_stop = min(runner_atr_stop, new_atr) if runner_atr_stop is not None else new_atr
                        # Structural: most protective 1H swing high formed since TP1
                        pidx = last_confirmed_swing_high(highs_1h, PIVOT_1H_LEN)
                        if pidx is not None and c1h[pidx]["t"] >= tp1_t:
                            new_struct = highs_1h[pidx] + struct_pad_1h
                            runner_struct_stop = min(runner_struct_stop, new_struct) if runner_struct_stop is not None else new_struct

                # Best-of stop: most protective of BE / ATR seatbelt / structural
                be_stop = entry_px
                if trade_side == "LONG":
                    new_stop = be_stop
                    if runner_struct_stop is not None:
                        new_stop = max(new_stop, runner_struct_stop)
                    if runner_atr_stop is not None:
                        new_stop = max(new_stop, runner_atr_stop)
                    if new_stop > stop_px:
                        log({"event": "runner_stop_raised", "symbol": SYMBOL,
                             "trade_id": trade_id, "old_stop": stop_px, "new_stop": new_stop,
                             "atr_stop": runner_atr_stop, "struct_stop": runner_struct_stop})
                        stop_px = new_stop
                        save_trade_state(SYMBOL, side=trade_side, entry_px=entry_px,
                                         stop_px=stop_px, tp1_px=tp1_px,
                                         trade_id=trade_id, tp1_done=True)
                else:  # SHORT
                    new_stop = be_stop
                    if runner_struct_stop is not None:
                        new_stop = min(new_stop, runner_struct_stop)
                    if runner_atr_stop is not None:
                        new_stop = min(new_stop, runner_atr_stop)
                    if new_stop < stop_px:
                        log({"event": "runner_stop_lowered", "symbol": SYMBOL,
                             "trade_id": trade_id, "old_stop": stop_px, "new_stop": new_stop,
                             "atr_stop": runner_atr_stop, "struct_stop": runner_struct_stop})
                        stop_px = new_stop
                        save_trade_state(SYMBOL, side=trade_side, entry_px=entry_px,
                                         stop_px=stop_px, tp1_px=tp1_px,
                                         trade_id=trade_id, tp1_done=True)

        return False

    last_hourly_ping = 0
    last_status = {
        "action": None,
        "debug": None,
        "closed15_t": None,
        "entry_px": None,
        "stop_px": None,
        "tp1_px": None,
        "in_trade": False,
        "exchange_position_open": False,
        "trade_side": None,
        "context": None,
        "veto": None,
    }

    # PID lock: prevent duplicate live instances of the same symbol
    if TRADING_MODE == "live":
        _lock_path = f"/etc/signalbot/{SYMBOL.replace(':', '_')}.pid"
        _my_pid = os.getpid()
        try:
            with open(_lock_path) as _lf:
                _existing_pid = int(_lf.read().strip())
            # Check if that PID is actually still running
            os.kill(_existing_pid, 0)  # signal 0 = existence check
            # If we get here, another instance is running
            log({
                "event": "duplicate_instance_detected",
                "symbol": SYMBOL,
                "existing_pid": _existing_pid,
                "my_pid": _my_pid,
            })
            await notify(
                f"🚨 {SYMBOL} duplicate instance detected — "
                f"PID {_existing_pid} already running. "
                f"This instance (PID {_my_pid}) will exit."
            )
            return  # exit cleanly — systemd will not restart due to clean exit
        except FileNotFoundError:
            pass  # no lock file — we're the first instance, continue
        except ProcessLookupError:
            pass  # PID in file is dead — stale lock, continue
        except Exception as _le:
            log({"event": "pid_lock_check_error", "symbol": SYMBOL,
                 "error": str(_le)})

        # Write our PID to the lock file
        try:
            with open(_lock_path, "w") as _lf:
                _lf.write(str(_my_pid))
        except Exception as _le:
            log({"event": "pid_lock_write_error", "symbol": SYMBOL,
                 "error": str(_le)})

        # The lock file will be overwritten by the next legitimate restart

    # Initial sync with exchange on startup
    await sync_live_trade_state()
    last_status["in_trade"] = in_trade
    last_status["exchange_position_open"] = exchange_position_open
    last_status["trade_side"] = trade_side

    while True:
        try:
            # Fast open-trade management loop
            await sync_live_trade_state()
            last_status["in_trade"] = in_trade
            last_status["exchange_position_open"] = exchange_position_open
            last_status["trade_side"] = trade_side

            managed = await manage_open_trade_fast()
            if managed:
                await asyncio.sleep(MANAGE_POLL_SECONDS)
                continue

            # Step 1: cheap 2-candle probe to detect whether a new 5m candle has closed.
            # Only fetches 2 candles (vs 260) so the rate cost is negligible.
            log({"event": "candle_probe", "symbol": SYMBOL})
            c15_probe = await asyncio.wait_for(
                candle_snapshot(SYMBOL, EXEC_TF_SECONDS, limit=2),
                timeout=60
            )
            if not c15_probe:
                await asyncio.sleep(10)
                continue

            if last_15m_t is not None and c15_probe[-1]["t"] == last_15m_t:
                await asyncio.sleep(CANDLE_PROBE_SECONDS)
                continue

            log({"event": "new_candle_detected", "symbol": SYMBOL, "t": c15_probe[-1]["t"]})

            # Step 2: new candle confirmed — now fetch full history for strategy
            _fetch_jitter = random.uniform(0.5, 4.0)
            await asyncio.sleep(_fetch_jitter)
            log({"event": "full_fetch_start", "symbol": SYMBOL})
            c15 = await asyncio.wait_for(
                candle_snapshot(SYMBOL, EXEC_TF_SECONDS, limit=260),
                timeout=60
            )
            if not c15:
                await asyncio.sleep(10)
                continue

            closed15 = c15[-1]
            last_15m_t = closed15["t"]
            print(f"INTRADAY_V2: NEW 5m candle t={closed15['t']}", flush=True)
            STATS.reset_if_new_day()

            # 2) only now fetch higher TFs — small gaps to avoid rate-limiting
            await asyncio.sleep(0.5)
            c1h = await asyncio.wait_for(
                candle_snapshot(SYMBOL, STRUCT_TF_SECONDS, limit=260),
                timeout=60
            )
            await asyncio.sleep(0.5)
            c4h = await asyncio.wait_for(
                candle_snapshot(SYMBOL, BIAS_TF_SECONDS, limit=260),
                timeout=60
            )

            log({"event": "snapshots_ok", "symbol": SYMBOL, "c15": len(c15), "c1h": len(c1h), "c4h": len(c4h)})

            # Sync local trade state with exchange every cycle
            await sync_live_trade_state()
            last_status["in_trade"] = in_trade
            last_status["exchange_position_open"] = exchange_position_open
            last_status["trade_side"] = trade_side

            # 3) decision
            d = decide(params, c15, c1h, c4h)
            print("INTRADAY_V2:", d.get("action"), d.get("debug"), flush=True)
            asyncio.create_task(track_adx_trend(SYMBOL, d))
            last_status["action"] = d.get("action")
            last_status["debug"] = d.get("debug")
            last_status["closed15_t"] = closed15["t"]
            last_status["entry_px"] = d.get("entry_px")
            last_status["stop_px"] = d.get("stop_px")
            last_status["tp1_px"] = d.get("tp1_px")
            last_status["in_trade"] = in_trade
            last_status["trade_side"] = trade_side

            # 4) build lightweight context
            symbol_price = closed15["c"]

            symbol_atr = None
            try:
                highs_1h = [x["h"] for x in c1h]
                lows_1h = [x["l"] for x in c1h]
                closes_1h = [x["c"] for x in c1h]

                trs = []
                for i in range(1, len(closes_1h)):
                    tr = max(
                        highs_1h[i] - lows_1h[i],
                        abs(highs_1h[i] - closes_1h[i - 1]),
                        abs(lows_1h[i] - closes_1h[i - 1]),
                    )
                    trs.append(tr)

                if len(trs) >= 14:
                    symbol_atr = sum(trs[-14:]) / 14.0
            except Exception:
                pass

            btc_ema_fast = None
            btc_ema_slow = None
            btc_prev_close = None
            btc_last_close = None

            try:
                c4h_closes = [x["c"] for x in c4h]
                if len(c4h_closes) >= max(EMA_FAST, EMA_SLOW) + 2:
                    btc_ema_fast = ema(c4h_closes[-(EMA_FAST * 4):], EMA_FAST)
                    btc_ema_slow = ema(c4h_closes[-(EMA_SLOW * 4):], EMA_SLOW)
                    btc_prev_close = c4h_closes[-2]
                    btc_last_close = c4h_closes[-1]
            except Exception:
                pass

            dbg = d.get("debug") or {}
            swing_high = dbg.get("swing_high") if isinstance(dbg, dict) else None
            swing_low = dbg.get("swing_low") if isinstance(dbg, dict) else None

            context = build_context(
                symbol=SYMBOL,
                btc_price=btc_last_close,
                btc_ema_fast=btc_ema_fast,
                btc_ema_slow=btc_ema_slow,
                btc_prev_close=btc_prev_close,
                btc_last_close=btc_last_close,
                symbol_price=symbol_price,
                symbol_atr=symbol_atr,
                swing_high=swing_high,
                swing_low=swing_low,
                news_risk="LOW",
                manual_event_risk=False,
                liquidation_risk="LOW",
            )

            last_status["context"] = context

            # 5) adapt decide() output into veto signal
            veto_signal = None
            if d.get("action") in ("ENTER_LONG", "ENTER_SHORT"):
                class TempSignal:
                    def __init__(self, side, entry, stop, tp1):
                        self.side = side
                        self.entry = entry
                        self.stop = stop
                        self.tp1 = tp1

                veto_signal = TempSignal(
                    side="LONG" if d["action"] == "ENTER_LONG" else "SHORT",
                    entry=d.get("entry_px"),
                    stop=d.get("stop_px"),
                    tp1=d.get("tp1_px"),
                )

            veto = evaluate_veto(veto_signal, context)
            last_status["veto"] = veto

            # Write compact per-symbol status to shared file so the digest
            # collector can read it regardless of which bot fires first.
            _status_path = f"/etc/signalbot/{SYMBOL.replace(':', '_')}_status.json"
            try:
                _dbg_now = last_status.get("debug") or {}
                _status_payload = {
                    "symbol":      SYMBOL,
                    "action":      last_status.get("action"),
                    "reason":      _dbg_now.get("reason") if isinstance(_dbg_now, dict) else None,
                    "bias4h":      _dbg_now.get("bias4h") if isinstance(_dbg_now, dict) else None,
                    "bos1h":       _dbg_now.get("bos1h") if isinstance(_dbg_now, dict) else None,
                    "rsi5m":       _dbg_now.get("rsi5m") if isinstance(_dbg_now, dict) else None,
                    "adx":         _dbg_now.get("adx_strength") if isinstance(_dbg_now, dict) else None,
                    "in_trade":    last_status.get("in_trade"),
                    "trade_side":  last_status.get("trade_side"),
                    "entry_px":    last_status.get("entry_px"),
                    "stop_px":     last_status.get("stop_px"),
                    "updated_at":  int(time.time()),
                }
                _tmp = _status_path + ".tmp"
                with open(_tmp, "w") as _f:
                    json.dump(_status_payload, _f)
                os.replace(_tmp, _status_path)
            except Exception:
                pass  # never block the trading loop

            now = int(time.time())
            if now - last_hourly_ping >= 3600:
                last_hourly_ping = now

                # ── Consolidated hourly digest ─────────────────────────────
                # First bot to cross the hourly boundary claims the digest send.
                # Others see a fresh lock and skip. Uses /etc/signalbot/ as the
                # shared filesystem (same dir used by trade_state_store).
                _DIGEST_LOCK = "/etc/signalbot/_digest_lock.json"
                _digest_claimed = False
                try:
                    try:
                        with open(_DIGEST_LOCK) as _lf:
                            _lock = json.load(_lf)
                        if now - _lock.get("sent_at", 0) >= 3540:  # 59 min grace
                            _digest_claimed = True
                    except FileNotFoundError:
                        _digest_claimed = True

                    if _digest_claimed:
                        # Claim atomically before sending so a concurrent bot sees it
                        _ltmp = _DIGEST_LOCK + ".tmp"
                        with open(_ltmp, "w") as _lf:
                            json.dump({"sent_at": now, "sender": SYMBOL}, _lf)
                        os.replace(_ltmp, _DIGEST_LOCK)

                        # Collect all status files
                        from datetime import datetime, timezone as _tz
                        _time_str = datetime.fromtimestamp(now, tz=_tz.utc).strftime("%H:%M UTC")
                        _lines = [f"🕐 Hourly Digest — {_time_str}"]

                        from config import ALL_SYMBOLS as _ALL_SYMS
                        _STALE_SECS = 600  # flag symbol as stale if not updated in 10 min
                        for _sym in _ALL_SYMS:
                            _spath = f"/etc/signalbot/{_sym.replace(':', '_')}_status.json"
                            try:
                                with open(_spath) as _sf:
                                    _s = json.load(_sf)
                                _age = now - _s.get("updated_at", 0)
                                if _age > _STALE_SECS:
                                    _lines.append(f"  {_sym:<12} ⚠️ stale ({_age//60}m)")
                                    continue
                                _bias  = (_s.get("bias4h") or "?")[0]        # B/E/N/?
                                _bos   = _s.get("bos1h") or "–"
                                _bos_s = "↑" if _bos=="UP" else ("↓" if _bos=="DOWN" else "–")
                                _rsi   = _s.get("rsi5m")
                                _adx   = _s.get("adx")
                                _act   = _s.get("action") or "?"
                                _rsn   = _s.get("reason") or ""
                                _rsi_s = f"RSI{_rsi:.0f}" if _rsi is not None else "RSI?"
                                _adx_s = f"ADX{_adx:.0f}" if _adx is not None else ""
                                if _s.get("in_trade"):
                                    _side = _s.get("trade_side") or ""
                                    _epx  = _s.get("entry_px")
                                    _spx  = _s.get("stop_px")
                                    _trade_s = f"  🔴{_side}"
                                    if _epx and _spx:
                                        try:
                                            _trade_s += f" e={float(_epx):.2f} s={float(_spx):.2f}"
                                        except Exception:
                                            pass
                                    _summary = f"{_trade_s}"
                                elif _act in ("ENTER_LONG", "ENTER_SHORT"):
                                    _summary = f"  🟡{_act.replace('ENTER_','')}"
                                else:
                                    _summary = f"  {_rsn or _act}"
                                _label = _sym.replace("xyz:", "")
                                _lines.append(
                                    f"  {_label:<10} {_bias}{_bos_s} {_rsi_s} {_adx_s}{_summary}"
                                )
                            except FileNotFoundError:
                                _lines.append(f"  {_sym:<12} – (no data yet)")
                            except Exception:
                                _lines.append(f"  {_sym:<12} – (read error)")

                        # Collect symbol data for agent regime summary
                        _agent_symbol_data = []
                        for _sym in _ALL_SYMS:
                            _spath = f"/etc/signalbot/{_sym.replace(':', '_')}_status.json"
                            try:
                                with open(_spath) as _sf:
                                    _agent_symbol_data.append(json.load(_sf))
                            except Exception:
                                pass

                        if _agent_symbol_data:
                            _summary = await get_agent_regime_summary(_agent_symbol_data)
                            if _summary:
                                _lines.append("")
                                _lines.append(f"🤖 Regime: {_summary}")

                        await notify("\n".join(_lines))
                        log({"event": "hourly_digest_sent", "symbol": SYMBOL, "sender": SYMBOL})

                except Exception as _de:
                    log({"event": "hourly_digest_error", "symbol": SYMBOL, "error": str(_de)})
                # ── End consolidated hourly digest ─────────────────────────

            # --- Entry logic
            if d.get("action") in ("ENTER_LONG", "ENTER_SHORT"):
                if exchange_position_open:
                    log({
                        "event": "entry_skipped_exchange_position_open",
                        "symbol": SYMBOL,
                        "action": d.get("action"),
                        "trade_side": trade_side,
                        "size": size,
                    })
                    await asyncio.sleep(1)
                    continue

                if pending_order_id is not None:
                    log({
                        "event":   "entry_skipped_pending_order",
                        "symbol":  SYMBOL,
                        "action":  d.get("action"),
                        "oid":     pending_order_id,
                        "elapsed": int(time.time() - pending_order_ts),
                    })
                    await asyncio.sleep(1)
                    continue

                # ── Liquidity filter ──────────────────────────────────
                _dbg = d.get("debug") or {}
                _vol_bar = _dbg.get("vol_closed_bar", 0.0)
                _vol_avg = _dbg.get("vol_avg_20", 0.0)
                if _vol_avg > 0 and _vol_bar < _vol_avg:
                    STATS.skips += 1
                    log({
                        "event":      "entry_skipped_below_avg_volume",
                        "symbol":     SYMBOL,
                        "action":     d.get("action"),
                        "vol_bar":    _vol_bar,
                        "vol_avg_20": _vol_avg,
                        "reason":     "below_average_volume",
                    })
                    await asyncio.sleep(1)
                    continue
                # ── End liquidity filter ───────────────────────────────

                # ── Bar-range liquidity filter ─────────────────────────
                # Blocks near-flat "ghost" candles on thin markets (xyz: etc).
                # symbol_atr is None when 1H data was insufficient — allow through.
                if symbol_atr and MIN_BAR_RANGE_ATR_MULT > 0:
                    _bar_range = closed15["h"] - closed15["l"]
                    _min_range = symbol_atr * MIN_BAR_RANGE_ATR_MULT
                    if _bar_range < _min_range:
                        STATS.skips += 1
                        log({
                            "event":     "entry_skipped_bar_range_too_narrow",
                            "symbol":    SYMBOL,
                            "action":    d.get("action"),
                            "bar_range": round(_bar_range, 6),
                            "min_range": round(_min_range, 6),
                            "atr_1h":    round(symbol_atr, 6),
                            "reason":    "bar_range_too_narrow",
                        })
                        await asyncio.sleep(1)
                        continue
                # ── End bar-range liquidity filter ────────────────────

                veto = last_status.get("veto") or {}

                if veto.get("blocked"):
                    STATS.skips += 1
                    log({
                        "event": "trade_veto_blocked",
                        "symbol": SYMBOL,
                        "action": d.get("action"),
                        "reason": veto.get("reason"),
                        "context": last_status.get("context"),
                    })
                    await notify(
                        f"⛔ {SYMBOL} trade blocked by veto\n"
                        f"action={d.get('action')} reason={veto.get('reason')}"
                    )
                    await asyncio.sleep(10)
                    continue

                # ── Risk limit checks ──────────────────────────────────
                # 1) Daily loss ceiling
                if STATS.daily_realised_pnl <= -DAILY_MAX_LOSS_USDT:
                    STATS.skips += 1
                    log({
                        "event":               "entry_blocked_daily_loss",
                        "symbol":              SYMBOL,
                        "daily_realised_pnl":  STATS.daily_realised_pnl,
                        "limit":               DAILY_MAX_LOSS_USDT,
                    })
                    await notify(
                        f"🚫 {SYMBOL} entry blocked — daily loss limit hit\n"
                        f"realised={STATS.daily_realised_pnl:.2f} USD  "
                        f"limit={DAILY_MAX_LOSS_USDT:.2f} USD"
                    )
                    await asyncio.sleep(10)
                    continue

                # 2) Consecutive stop limit (also arms cooldown if not yet set)
                if STATS.consec_stops >= MAX_CONSEC_LOSSES:
                    if time.time() >= STATS.cooldown_until:
                        # Arm cooldown now (covers the case where it wasn't armed at stop time)
                        STATS.cooldown_until = time.time() + COOLDOWN_SECONDS
                        log({
                            "event":          "cooldown_armed_at_entry",
                            "symbol":         SYMBOL,
                            "consec_stops":   STATS.consec_stops,
                            "cooldown_until": STATS.cooldown_until,
                        })
                    STATS.skips += 1
                    await notify(
                        f"🚫 {SYMBOL} entry blocked — {STATS.consec_stops} consecutive loss(es)\n"
                        f"Cooldown until {STATS.cooldown_until:.0f} "
                        f"({COOLDOWN_SECONDS // 60} min)"
                    )
                    await asyncio.sleep(10)
                    continue

                # 3) Active cooldown timer
                if time.time() < STATS.cooldown_until:
                    remaining = int(STATS.cooldown_until - time.time())
                    STATS.skips += 1
                    log({
                        "event":     "entry_blocked_cooldown",
                        "symbol":    SYMBOL,
                        "remaining": remaining,
                    })
                    await notify(
                        f"🚫 {SYMBOL} entry blocked — cooldown active\n"
                        f"{remaining // 60}m {remaining % 60}s remaining"
                    )
                    await asyncio.sleep(10)
                    continue
                # ── End risk limit checks ──────────────────────────────

                trade_side = "LONG" if d["action"] == "ENTER_LONG" else "SHORT"
                entry_px = float(d["entry_px"])
                stop_px = float(d["stop_px"])
                tp1_px = float(d["tp1_px"])
                tp1_done = False
                trade_id = new_trade_id()

                log({
                    "event":     "entry_signal_seen",
                    "symbol":    SYMBOL,
                    "action":    d.get("action"),
                    "entry_px":  d.get("entry_px"),
                    "stop_px":   d.get("stop_px"),
                    "tp1_px":    d.get("tp1_px"),
                    "mode":      TRADING_MODE,
                })

                # Position sizing: simple fixed USD risk / stop distance
                risk_per_unit = abs(entry_px - stop_px)
                if risk_per_unit <= 0:
                    STATS.skips += 1
                    log({
                        "event":        "entry_blocked_after_signal",
                        "symbol":       SYMBOL,
                        "reason":       "risk_per_unit_zero",
                        "entry_px":     entry_px,
                        "stop_px":      stop_px,
                        "risk_per_unit": risk_per_unit,
                    })
                    await asyncio.sleep(10)
                    continue

                base_size = max(0.0, float(RISK_USDT_PER_TRADE) / risk_per_unit)

                if base_size <= 0:
                    STATS.skips += 1
                    log({
                        "event":     "entry_blocked_after_signal",
                        "symbol":    SYMBOL,
                        "reason":    "base_size_zero",
                        "entry_px":  entry_px,
                        "stop_px":   stop_px,
                        "risk_usd":  RISK_USDT_PER_TRADE,
                    })
                    await asyncio.sleep(10)
                    continue

                # Notional cap: prevent an unusually tight stop from producing
                # a dangerously large position.
                intended_notional = base_size * entry_px
                if intended_notional > MAX_POSITION_NOTIONAL_USDT:
                    capped_size = MAX_POSITION_NOTIONAL_USDT / entry_px
                    log({
                        "event":          "position_size_capped",
                        "symbol":         SYMBOL,
                        "side":           trade_side,
                        "original_size":  base_size,
                        "capped_size":    capped_size,
                        "notional_usd":   intended_notional,
                        "cap_usd":        MAX_POSITION_NOTIONAL_USDT,
                        "entry_px":       entry_px,
                    })
                    base_size = capped_size

                if veto.get("reduce_size"):
                    size = base_size * 0.5
                else:
                    size = base_size

                normalized_size = size
                resp = {"ok": True}

                if TRADING_MODE == "paper":
                    await exec_layer.paper_open(
                        side=trade_side,
                        size=size if trade_side == "LONG" else -size,
                        entry_px=entry_px,
                        trade_id=trade_id,
                    )
                else:
                    normalized_size = exec_layer.hl.normalize_entry_size_for_coin(SYMBOL, size)
                    _min_sz = exec_layer.hl._min_size_for_coin(SYMBOL)

                    log({
                        "event":            "order_attempt",
                        "symbol":           SYMBOL,
                        "side":             trade_side,
                        "entry_px":         entry_px,
                        "stop_px":          stop_px,
                        "tp1_px":           tp1_px,
                        "raw_size":         round(size, 8),
                        "normalized_size":  normalized_size,
                        "min_size":         _min_sz,
                        "risk_per_unit":    round(risk_per_unit, 8),
                        "risk_usd":         RISK_USDT_PER_TRADE,
                    })

                    # Hard block invalid size BEFORE hitting exchange
                    if normalized_size <= 0:
                        resp = {"ok": False, "error": "invalid_size_after_rounding"}
                        log({
                            "event":           "entry_blocked_after_signal",
                            "symbol":          SYMBOL,
                            "reason":          "normalized_size_zero",
                            "raw_size":        round(size, 8),
                            "normalized_size": normalized_size,
                            "min_size":        _min_sz,
                            "entry_px":        entry_px,
                        })
                    else:
                        resp = await exec_layer.live_open_marketlike(
                            side=trade_side,
                            size=normalized_size,
                        )

                    if not resp or not resp.get("ok", False):
                        log({
                            "event":           "live_open_rejected",
                            "symbol":          SYMBOL,
                            "side":            trade_side,
                            "resp":            str(resp)[:300],
                            "raw_size":        round(size, 8),
                            "normalized_size": normalized_size,
                        })
                        await safe_notify(f"❌ {SYMBOL} {trade_side} order blocked/failed: {resp}")
                        await asyncio.sleep(10)
                        continue

                    # Use actual sent size for downstream tracking
                    size = normalized_size
                    # Track the pending order so sync can cancel it if it never fills.
                    pending_order_id = resp.get("order_id")
                    pending_order_ts = time.time()

                append_event(SYMBOL, {
                    "type": "ENTER",
                    "trade_id": trade_id,
                    "side": trade_side,
                    "entry": entry_px,
                    "stop": stop_px,
                    "tp1": tp1_px,
                    "size": size,
                    "t": closed15["t"],
                })
                STATS.trades_entered += 1
                ctx = last_status.get("context") or {}
                veto = last_status.get("veto") or {}

                await notify(
                    f"{SYMBOL} {trade_side} 🟡 ENTRY ORDER SENT\n"
                    f"entry={entry_px:.2f} stop={stop_px:.2f} tp1={tp1_px:.2f}\n"
                    f"risk_usd={RISK_USDT_PER_TRADE} size≈{size:.4f}\n"
                    f"veto={veto.get('decision')} reason={veto.get('reason')}\n"
                    f"btc_regime={ctx.get('btc_regime')} vol={ctx.get('volatility')} tradability={ctx.get('tradability')}\n"
                    f"id={trade_id}"
                )

                # Persist trade context before clearing in-memory flags.
                # If the process restarts before fill is confirmed, sync_live_trade_state()
                # will find the open position on the exchange and restore from this file.
                save_trade_state(
                    SYMBOL,
                    side=trade_side,
                    entry_px=entry_px,
                    stop_px=stop_px,
                    tp1_px=tp1_px,
                    trade_id=trade_id,
                    tp1_done=False,
                )

                # Do not assume filled immediately just because order was accepted.
                # Exchange sync will flip this on when a real position appears.
                in_trade = False
                exchange_position_open = False

        except asyncio.TimeoutError:
            log({"event": "snapshot_timeout", "symbol": SYMBOL})
            await notify(f"⚠️ {SYMBOL} INTRADAY_V2 snapshot timeout (HTTP took too long). Retrying…")
            await asyncio.sleep(5)

        except Exception as e:
            log({"event": "intraday_v2_error", "error": str(e)})
            await notify(f"⚠️ {SYMBOL} INTRADAY_V2 loop error: {type(e).__name__}: {str(e)[:300]}")
            await asyncio.sleep(5)


async def run_all():
    tasks = []

    # main trading loop
    if STRATEGY == "INTRADAY_SWING_V2":
        tasks.append(main_intraday_swing_v2())
    else:
        tasks.append(main())

    # heartbeat always on (or gate it with env if you want)
    # tasks.append(heartbeat_loop())

    # telegram control polling (optional)
    if os.getenv("TELEGRAM_CONTROL", "0") == "1":
        tasks.append(telegram_poll_commands(handle_command))

    await asyncio.gather(*tasks)
if __name__ == "__main__":
    asyncio.run(run_all())
