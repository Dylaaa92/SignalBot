from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any
import time

@dataclass
class Position:
    symbol: str
    side: Optional[str] = None  # "LONG" / "SHORT"
    qty: float = 0.0
    entry_px: float = 0.0
    entry_ts: int = 0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    tp1_done: bool = False
    tp1_px: float = 0.0
    stop_px: float = 0.0

class Broker:
    async def get_position(self, symbol: str) -> Position:
        raise NotImplementedError

    async def place_entry(self, symbol: str, side: str, qty: float, px: float, meta: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def place_tp1(self, symbol: str, tp1_px: float, qty_pct: float, meta: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def close_position(self, symbol: str, px: float, reason: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError
