from __future__ import annotations
from typing import Dict, Any
import math
import os
import time

from broker import Broker, Position

FEE_BPS = float(os.getenv("PAPER_FEE_BPS", "4"))         # 0.04% default = 4 bps
SLIP_BPS = float(os.getenv("PAPER_SLIPPAGE_BPS", "2"))   # 2 bps default

def _fee(notional: float) -> float:
    return notional * (FEE_BPS / 10000.0)

def _slip(px: float, side: str, is_entry: bool) -> float:
    # entry: LONG pays up, SHORT pays down; exits opposite
    bps = SLIP_BPS / 10000.0
    if is_entry:
        return px * (1 + bps) if side == "LONG" else px * (1 - bps)
    else:
        return px * (1 - bps) if side == "LONG" else px * (1 + bps)

class PaperBroker(Broker):
    def __init__(self):
        self.pos: Dict[str, Position] = {}

    async def get_position(self, symbol: str) -> Position:
        return self.pos.get(symbol, Position(symbol=symbol))

    async def place_entry(self, symbol: str, side: str, qty: float, px: float, meta: Dict[str, Any]) -> Dict[str, Any]:
        fill_px = _slip(px, side, is_entry=True)
        notional = abs(qty) * fill_px
        fee = _fee(notional)

        p = Position(symbol=symbol, side=side, qty=qty, entry_px=fill_px, entry_ts=int(time.time()), fees_paid=fee)
        p.stop_px = float(meta.get("stop_px", 0.0) or 0.0)
        p.tp1_px = float(meta.get("tp1_px", 0.0) or 0.0)
        self.pos[symbol] = p

        return {"ok": True, "type": "PAPER_ENTRY", "fill_px": fill_px, "fee": fee}

    async def place_tp1(self, symbol: str, tp1_px: float, qty_pct: float, meta: Dict[str, Any]) -> Dict[str, Any]:
        p = self.pos.get(symbol)
        if not p or not p.side or p.qty == 0:
            return {"ok": False, "err": "no_position"}

        if p.tp1_done:
            return {"ok": True, "type": "PAPER_TP1_ALREADY_DONE"}

        # execute TP1 immediately when caller confirms price touched
        exit_px = _slip(tp1_px, p.side, is_entry=False)
        qty_close = abs(p.qty) * (qty_pct / 100.0)
        notional = qty_close * exit_px
        fee = _fee(notional)

        # pnl = (exit - entry) * qty for long; reversed for short
        if p.side == "LONG":
            pnl = (exit_px - p.entry_px) * qty_close
        else:
            pnl = (p.entry_px - exit_px) * qty_close

        p.realized_pnl += pnl
        p.fees_paid += fee
        p.tp1_done = True

        # reduce position
        p.qty = math.copysign(abs(p.qty) - qty_close, 1 if p.side == "LONG" else -1)
        if abs(p.qty) < 1e-12:
            self.pos[symbol] = Position(symbol=symbol)  # flat
        else:
            self.pos[symbol] = p

        return {"ok": True, "type": "PAPER_TP1", "exit_px": exit_px, "fee": fee, "pnl": pnl}

    async def close_position(self, symbol: str, px: float, reason: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        p = self.pos.get(symbol)
        if not p or not p.side or p.qty == 0:
            return {"ok": False, "err": "no_position"}

        exit_px = _slip(px, p.side, is_entry=False)
        qty_close = abs(p.qty)
        notional = qty_close * exit_px
        fee = _fee(notional)

        if p.side == "LONG":
            pnl = (exit_px - p.entry_px) * qty_close
        else:
            pnl = (p.entry_px - exit_px) * qty_close

        p.realized_pnl += pnl
        p.fees_paid += fee

        self.pos[symbol] = Position(symbol=symbol)  # flat

        return {"ok": True, "type": "PAPER_CLOSE", "exit_px": exit_px, "fee": fee, "pnl": pnl, "reason": reason}
