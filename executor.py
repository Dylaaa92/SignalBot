# executor.py
import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional, Literal

from logger import log
from hl_trade import HL


Side = Literal["LONG", "SHORT"]
Mode = Literal["paper", "live"]


@dataclass
class Position:
    side: Side
    sz: float
    entry_px: float
    entry_t: int
    trade_id: str


class Executor:
    """
    Paper/live execution wrapper.
    - paper: maintains a local virtual position
    - live : uses HL SDK (hl_trade.HL) synchronously via asyncio.to_thread
    """
    def __init__(self, env: str, symbol: str):
        self.env = env
        self.symbol = symbol
        self.mode: Mode = os.getenv("TRADING_MODE", "paper").lower()  # paper|live
        self.paper_pos: Optional[Position] = None

        self.hl = None
        if self.mode == "live":
            self.hl = HL(env)

    def has_position(self) -> bool:
        return self.paper_pos is not None if self.mode == "paper" else True  # live check done via get_position()

    async def get_position_live(self) -> Optional[dict]:
        """
        Returns raw position info if live, else None.
        Uses user_state from Info API (SDK).
        """
        if self.mode != "live":
            return None

        def _call():
            # user_state structure contains "assetPositions"
            st = self.hl.info.user_state(self.hl.account_address)
            return st

        st = await asyncio.to_thread(_call)

        # Find this symbol’s open position, if any
        for ap in (st.get("assetPositions") or []):
            pos = ap.get("position") or {}
            coin = pos.get("coin")
            szi = float(pos.get("szi", 0))
            if coin == self.symbol and abs(szi) > 0:
                return pos
        return None

    async def open_paper(self, side: Side, sz: float, px: float, trade_id: str):
        if self.paper_pos is not None:
            raise RuntimeError("paper position already open")
        self.paper_pos = Position(side=side, sz=sz, entry_px=px, entry_t=int(time.time()), trade_id=trade_id)
        log({"event": "paper_open", "symbol": self.symbol, "side": side, "sz": sz, "px": px, "trade_id": trade_id})

    async def close_paper(self, px: float, reason: str):
        if self.paper_pos is None:
            return None
        p = self.paper_pos
        self.paper_pos = None
        log({"event": "paper_close", "symbol": self.symbol, "side": p.side, "sz": p.sz, "px": px, "reason": reason, "trade_id": p.trade_id})
        return p

    async def open_live_market(self, side: Side, sz: float):
        """
        MVP live entry: uses a LIMIT close-to-mid (post_only False) to behave like market.
        This avoids relying on trigger/market order schemas.
        """
        if self.mode != "live":
            raise RuntimeError("not in live mode")

        is_buy = (side == "LONG")

        def _call():
            mid = self.hl.mid(self.symbol)
            # small slippage pad: 5 bps default (adjust if needed)
            slip_bps = float(os.getenv("SLIP_BPS", "5"))
            slip = mid * (slip_bps / 10000.0)
            px = (mid + slip) if is_buy else (mid - slip)
            return self.hl.place_limit(self.symbol, is_buy=is_buy, px=px, sz=sz, reduce_only=False, post_only=False)

        resp = await asyncio.to_thread(_call)
        log({"event": "live_open_sent", "symbol": self.symbol, "side": side, "sz": sz, "resp": resp})
        return resp

    async def close_live_market(self, side: Side, sz: float):
        """
        MVP live exit: send opposite-side close-to-mid LIMIT with reduce_only=True.
        """
        if self.mode != "live":
            raise RuntimeError("not in live mode")

        is_buy = (side == "SHORT")  # to close short, buy; to close long, sell
        def _call():
            mid = self.hl.mid(self.symbol)
            slip_bps = float(os.getenv("SLIP_BPS", "5"))
            slip = mid * (slip_bps / 10000.0)
            px = (mid + slip) if is_buy else (mid - slip)
            return self.hl.place_limit(self.symbol, is_buy=is_buy, px=px, sz=sz, reduce_only=True, post_only=False)

        resp = await asyncio.to_thread(_call)
        log({"event": "live_close_sent", "symbol": self.symbol, "side": side, "sz": sz, "resp": resp})
        return resp
