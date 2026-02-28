import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional, Literal, Dict, Any

from logger import log
from hl_trade import HL


Side = Literal["LONG", "SHORT"]


@dataclass
class PaperPosition:
    side: Side
    size: float
    entry_px: float
    entry_time: int
    trade_id: str
    tp1_done: bool = False
    realized_pnl: float = 0.0


class Executor:
    """
    Execution layer supporting:
      - paper mode (simulated fills + PnL)
      - live mode (Hyperliquid orders)

    Enable live mode with:
        export TRADING_MODE=live
        export LIVE_GUARD=I_UNDERSTAND
    """

    def __init__(self, env: str, symbol: str):
        self.env = env
        self.symbol = symbol
        self.mode = os.getenv("TRADING_MODE", "paper").lower()

        # Live safety guard
        if self.mode == "live" and os.getenv("LIVE_GUARD", "") != "I_UNDERSTAND":
            raise RuntimeError(
                "LIVE_GUARD not set. "
                "Export LIVE_GUARD=I_UNDERSTAND to enable live trading."
            )

        self.slippage_bps = float(os.getenv("SLIP_BPS", "5"))  # for live pseudo-market

        self.paper_position: Optional[PaperPosition] = None
        self.hl: Optional[HL] = None

        if self.mode == "live":
            self.hl = HL(env)

    # =========================================================
    # Utility
    # =========================================================

    def _apply_slippage(self, price: float, side: Side, is_entry: bool) -> float:
        """Small slippage model (used for live pseudo-market orders)."""
        slip = price * (self.slippage_bps / 10000.0)

        if is_entry:
            return price + slip if side == "LONG" else price - slip
        else:
            return price - slip if side == "LONG" else price + slip

    def has_position(self) -> bool:
        if self.mode == "paper":
            return self.paper_position is not None
        return True  # live check handled via exchange state if needed

    # =========================================================
    # Paper Trading
    # =========================================================

    async def paper_open(self, side: Side, size: float, entry_px: float, trade_id: str):
        if self.paper_position is not None:
            return {"ok": False, "error": "paper_position_exists"}

        self.paper_position = PaperPosition(
            side=side,
            size=size,
            entry_px=entry_px,
            entry_time=int(time.time()),
            trade_id=trade_id,
        )

        log({
            "event": "paper_open",
            "symbol": self.symbol,
            "side": side,
            "size": size,
            "entry": entry_px,
            "trade_id": trade_id,
        })

        return {"ok": True}

    async def paper_tp1(self, tp_price: float, qty_pct: float):
        p = self.paper_position
        if p is None or p.tp1_done:
            return {"ok": False}

        qty_to_close = abs(p.size) * (qty_pct / 100.0)

        if p.side == "LONG":
            pnl = (tp_price - p.entry_px) * qty_to_close
        else:
            pnl = (p.entry_px - tp_price) * qty_to_close

        p.realized_pnl += pnl
        p.tp1_done = True

        remaining = abs(p.size) - qty_to_close
        p.size = remaining if p.side == "LONG" else -remaining

        log({
            "event": "paper_tp1",
            "symbol": self.symbol,
            "tp_price": tp_price,
            "pnl": pnl,
            "remaining_size": p.size,
        })

        if abs(p.size) < 1e-10:
            self.paper_position = None

        return {"ok": True, "pnl": pnl}

    async def paper_close(self, exit_price: float, reason: str):
        p = self.paper_position
        if p is None:
            return {"ok": False}

        qty = abs(p.size)

        if p.side == "LONG":
            pnl = (exit_price - p.entry_px) * qty
        else:
            pnl = (p.entry_px - exit_price) * qty

        total_pnl = p.realized_pnl + pnl

        log({
            "event": "paper_close",
            "symbol": self.symbol,
            "exit_price": exit_price,
            "reason": reason,
            "trade_pnl": pnl,
            "total_pnl": total_pnl,
        })

        self.paper_position = None

        return {"ok": True, "total_pnl": total_pnl}

    # =========================================================
    # Live Trading (market-like via limit + slippage)
    # =========================================================

    async def live_open_marketlike(self, side: Side, size: float):
        if self.hl is None:
            return {"ok": False}

        is_buy = side == "LONG"

        def _call():
            mid = self.hl.mid(self.symbol)
            px = self._apply_slippage(mid, side, is_entry=True)
            return self.hl.place_limit(
                coin=self.symbol,
                is_buy=is_buy,
                px=px,
                sz=size,
                reduce_only=False,
                post_only=False,
            )

        resp = await asyncio.to_thread(_call)

        log({
            "event": "live_entry_sent",
            "symbol": self.symbol,
            "side": side,
            "size": size,
            "resp": resp,
        })

        return {"ok": True, "resp": resp}

    async def live_close_marketlike(self, side: Side, size: float):
        if self.hl is None:
            return {"ok": False}

        is_buy = side == "SHORT"  # opposite to close

        def _call():
            mid = self.hl.mid(self.symbol)
            px = self._apply_slippage(mid, side, is_entry=False)
            return self.hl.place_limit(
                coin=self.symbol,
                is_buy=is_buy,
                px=px,
                sz=size,
                reduce_only=True,
                post_only=False,
            )

        resp = await asyncio.to_thread(_call)

        log({
            "event": "live_exit_sent",
            "symbol": self.symbol,
            "side": side,
            "size": size,
            "resp": resp,
        })

        return {"ok": True, "resp": resp}
