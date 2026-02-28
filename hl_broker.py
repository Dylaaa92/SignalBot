from __future__ import annotations
from typing import Dict, Any
import os

from broker import Broker, Position

# You must adapt these imports to whatever functions/classes exist in hl_trade.py
# Example placeholders:
# from hl_trade import place_market_order, get_position

class HyperliquidBroker(Broker):
    def __init__(self, env: str):
        self.env = env

    async def get_position(self, symbol: str) -> Position:
        # TODO: wire to hl_trade position query
        # return Position(symbol=symbol, side=..., qty=..., entry_px=...)
        return Position(symbol=symbol)

    async def place_entry(self, symbol: str, side: str, qty: float, px: float, meta: Dict[str, Any]) -> Dict[str, Any]:
        # TODO: market order or aggressive limit
        # res = await place_market_order(symbol, side, qty)
        return {"ok": False, "err": "hl_broker_not_wired"}

    async def place_tp1(self, symbol: str, tp1_px: float, qty_pct: float, meta: Dict[str, Any]) -> Dict[str, Any]:
        # TODO: place reduce-only limit at TP1
        return {"ok": False, "err": "hl_broker_not_wired"}

    async def close_position(self, symbol: str, px: float, reason: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        # TODO: reduce-only market close
        return {"ok": False, "err": "hl_broker_not_wired"}
