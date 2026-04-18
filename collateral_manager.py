"""
collateral_manager.py — self-managed margin tracking for agent wallet setups
where free_collateral_usd() always returns 0.0 from the Hyperliquid SDK.

Allocated margin and a pending order queue are persisted to disk as JSON.
All file I/O uses fcntl.flock for exclusive locking to be safe across
multiple bot processes writing concurrently.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from notifier import notify_bg

# =========================================================
# Constants
# =========================================================

MARGIN_STATE_PATH      = "/etc/signalbot/margin_state.json"
ORDER_QUEUE_PATH       = "/etc/signalbot/order_queue.json"
DEFAULT_ACCOUNT_EQUITY = 500.0
QUEUE_EXPIRY_HOURS     = 4
ENTRY_STALENESS_PCT    = 0.015   # 1.5%

_DEFAULT_MARGIN_STATE = {
    "allocated": {},
    "account_equity": DEFAULT_ACCOUNT_EQUITY,
    "last_updated": "",
}


# =========================================================
# Low-level locked file I/O
# =========================================================

def _read_margin_state() -> dict:
    try:
        with open(MARGIN_STATE_PATH, "r") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        state = dict(_DEFAULT_MARGIN_STATE)
        state["allocated"] = {}
        _write_margin_state(state)
        return state
    except Exception:
        return dict(_DEFAULT_MARGIN_STATE)


def _write_margin_state(state: dict) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    tmp = MARGIN_STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(state, f, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp, MARGIN_STATE_PATH)


def _read_queue() -> list:
    try:
        with open(ORDER_QUEUE_PATH, "r") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        _write_queue([])
        return []
    except Exception:
        return []


def _write_queue(orders: list) -> None:
    tmp = ORDER_QUEUE_PATH + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(orders, f, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp, ORDER_QUEUE_PATH)


# =========================================================
# Public API — collateral tracking
# =========================================================

def get_free_collateral() -> float:
    state = _read_margin_state()
    equity = float(state.get("account_equity", DEFAULT_ACCOUNT_EQUITY))
    allocated_total = sum(float(v) for v in state.get("allocated", {}).values())
    return max(0.0, equity - allocated_total)


def required_margin(size: float, entry: float, leverage: int) -> float:
    return (size * entry) / leverage


def can_afford(req_margin: float) -> tuple[bool, float]:
    free = get_free_collateral()
    return free >= req_margin, free


def allocate(trade_id: str, margin: float) -> None:
    state = _read_margin_state()
    state.setdefault("allocated", {})[trade_id] = margin
    _write_margin_state(state)


def release(trade_id: str) -> float:
    state = _read_margin_state()
    allocated = state.setdefault("allocated", {})
    released = float(allocated.pop(trade_id, 0.0))
    _write_margin_state(state)
    return released


def set_account_equity(usd: float) -> None:
    state = _read_margin_state()
    state["account_equity"] = usd
    _write_margin_state(state)


# =========================================================
# Public API — order queue
# =========================================================

def queue_order(order: dict) -> None:
    now = datetime.now(timezone.utc)
    order["queued_at"]  = now.isoformat()
    order["expires_at"] = (now + timedelta(hours=QUEUE_EXPIRY_HOURS)).isoformat()

    orders = _read_queue()
    orders.append(order)
    _write_queue(orders)

    symbol = order.get("symbol", "?")
    side   = order.get("side", "?")
    margin = order.get("required_margin", 0.0)
    score  = order.get("signal_score", "?")
    notify_bg(
        f"📋 Order queued: {symbol} {side} | "
        f"margin=${margin:.2f} | score={score} | "
        f"expires in {QUEUE_EXPIRY_HOURS}h"
    )


def is_order_stale(order: dict, current_mid: float) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)

    # Expiry check
    try:
        expires_at = datetime.fromisoformat(order["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now > expires_at:
            return True, f"expired at {order['expires_at']}"
    except (KeyError, ValueError):
        return True, "missing or invalid expires_at"

    # Staleness check
    try:
        entry = float(order["entry"])
        if entry > 0:
            drift = abs(current_mid - entry) / entry
            if drift > ENTRY_STALENESS_PCT:
                return True, (
                    f"mid moved {drift*100:.2f}% from entry "
                    f"({entry:.4f} → {current_mid:.4f}), "
                    f"threshold {ENTRY_STALENESS_PCT*100:.1f}%"
                )
    except (KeyError, ValueError, ZeroDivisionError):
        return True, "invalid entry price in order"

    return False, ""


def flush_queue(get_mid_fn: Callable[[str], float]) -> list[dict]:
    """
    Scans the queue:
    - Drops stale orders (expired or price drifted) and sends Telegram alerts.
    - Returns orders that are affordable and not stale.
    - Removes returned orders from the queue file.
    Caller is responsible for executing the returned orders.
    """
    orders = _read_queue()
    remaining   = []
    executable  = []
    dropped     = []

    for order in orders:
        symbol = order.get("symbol", "?")
        try:
            mid = get_mid_fn(symbol)
        except Exception:
            mid = float(order.get("entry", 0.0))

        stale, reason = is_order_stale(order, mid)
        if stale:
            dropped.append((order, reason))
            continue

        req = float(order.get("required_margin", 0.0))
        affordable, free = can_afford(req)
        if affordable:
            executable.append(order)
        else:
            remaining.append(order)

    # Persist the queue minus the executable ones
    _write_queue(remaining + [o for o, _ in dropped if False])  # dropped are gone
    _write_queue(remaining)

    # Notify for each drop
    for order, reason in dropped:
        symbol = order.get("symbol", "?")
        side   = order.get("side", "?")
        notify_bg(f"🗑️ Queued order dropped: {symbol} {side} — {reason}")

    return executable


# =========================================================
# __main__ — simulation
# =========================================================

if __name__ == "__main__":
    import uuid
    from datetime import timezone

    print("=== collateral_manager simulation ===\n")

    # 1. Set starting equity
    set_account_equity(500.0)
    print(f"set_account_equity(500.0)")

    # 2. Simulate two open positions
    allocate("BTC_long_abc123", 45.0)
    print(f"allocate('BTC_long_abc123', 45.0)")

    allocate("SOL_long_def456", 12.0)
    print(f"allocate('SOL_long_def456', 12.0)")

    free = get_free_collateral()
    print(f"\nget_free_collateral() = {free:.2f}  (expected 443.00)\n")

    # 3. Queue two pending orders
    hype_order = {
        "id":              str(uuid.uuid4())[:8],
        "symbol":          "HYPE",
        "side":            "LONG",
        "size":            3.0,
        "entry":           4.50,
        "stop":            4.20,
        "tp1":             5.10,
        "leverage":        5,
        "required_margin": 15.0,
        "signal_score":    0.78,
    }
    ada_order = {
        "id":              str(uuid.uuid4())[:8],
        "symbol":          "ADA",
        "side":            "LONG",
        "size":            250.0,
        "entry":           0.44,
        "stop":            0.41,
        "tp1":             0.51,
        "leverage":        5,
        "required_margin": 10.0,
        "signal_score":    0.65,
    }

    queue_order(hype_order)
    print(f"queue_order(HYPE LONG, margin=$15.00)")

    queue_order(ada_order)
    print(f"queue_order(ADA LONG, margin=$10.00)")

    # 4. BTC position closes
    released = release("BTC_long_abc123")
    print(f"\nrelease('BTC_long_abc123') — released ${released:.2f}")

    free = get_free_collateral()
    print(f"get_free_collateral() = {free:.2f}  (expected 488.00)\n")

    # 5. Flush — dummy mid returns entry price exactly (no staleness)
    def dummy_mid(symbol: str) -> float:
        mids = {"HYPE": 4.50, "ADA": 0.44}
        return mids.get(symbol, 0.0)

    executable = flush_queue(dummy_mid)
    print(f"flush_queue() returned {len(executable)} executable order(s):")
    for o in executable:
        print(f"  → {o['symbol']} {o['side']}  margin=${o['required_margin']:.2f}  score={o['signal_score']}")

    remaining_queue = _read_queue()
    print(f"\nQueue depth after flush: {len(remaining_queue)} order(s) remaining")
