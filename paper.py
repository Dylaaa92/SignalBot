from dataclasses import dataclass
from typing import Optional

@dataclass
class PaperPosition:
    side: str                 # "LONG" or "SHORT"
    entry: float
    stop: float
    size: float
    initial_size: float
    tp1_price: float
    tp1_size: float
    tp2_price: Optional[float] = None
    tp1_taken: bool = False
    open: bool = True


def mark_to_market_pnl(pos: PaperPosition, price: float) -> float:
    if not pos.open:
        return 0.0
    return (price - pos.entry) * pos.size if pos.side == "LONG" else (pos.entry - price) * pos.size
