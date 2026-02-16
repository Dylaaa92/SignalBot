import asyncio
import time
from dataclasses import dataclass
from typing import Optional, List

from notifier import notify
from logger import log
from hl_trade import HL


@dataclass
class GridParams:
    lower: float
    upper: float
    grids: int
    usd_per_order: float

    post_only: bool = True
    breakout_buffer_pct: float = 0.004   # 0.4%
    poll_seconds: float = 2.0

    # Safety defaults:
    max_orders_per_side: int = 8
    flatten_on_breakout: bool = False  # start False, optional later


class GridBot:
    """
    Simple live grid bot:
      - Places buy limits on grid levels below mid
      - Places sell limits on grid levels above mid
      - If price breaks out of range, cancels orders and disables grid

    It does NOT do complex per-fill replacement yet.
    Instead it rebuilds the ladder if open orders get low.
    This is robust and a good "day 1" live implementation.
    """

    def __init__(self, symbol: str, env: str):
        self.symbol = symbol
        self.env = env
        self.hl = HL(env)

        self.enabled: bool = False
        self.params: Optional[GridParams] = None

        self._last_rebuild_ts: float = 0.0
        self._rebuild_cooldown_s: float = 10.0

    def _levels(self, p: GridParams) -> List[float]:
        if p.grids < 2:
            return []
        step = (p.upper - p.lower) / p.grids
        return [p.lower + i * step for i in range(p.grids + 1)]

    def _round_px(self, px: float) -> float:
        # TODO: replace with tick-size rounding from HL meta.
        # For now, 4 decimals is usually fine for many alts; adjust if HL rejects.
        return float(f"{px:.4f}")

    def _size_from_usd(self, usd: float, px: float) -> float:
        # NOTE: HL has size increments; if rejected, add lot-size rounding later.
        if px <= 0:
            return 0.0
        return usd / px

    async def start(self, p: GridParams):
        if p.lower <= 0 or p.upper <= 0 or p.upper <= p.lower:
            await notify("[GRID] Invalid range. lower and upper must be positive, upper > lower.")
            return
        if p.grids < 2:
            await notify("[GRID] Invalid grids. Must be >= 2.")
            return
        if p.usd_per_order <= 0:
            await notify("[GRID] Invalid usd_per_order. Must be > 0.")
            return

        self.params = p
        self.enabled = True

        await notify(
            f"[GRID START] {self.symbol} range={p.lower}-{p.upper} grids={p.grids} usd_per_order={p.usd_per_order}"
        )
        await self.rebuild(force=True)

    async def stop(self):
        self.enabled = False
        await self.cancel_all()
        await notify(f"[GRID STOP] {self.symbol}")

    async def status(self) -> str:
        if not self.params:
            return f"[GRID STATUS] {self.symbol} OFF (no params)"
        oos = self.hl.open_orders(self.symbol) or []
        p = self.params
        return (
            f"[GRID STATUS] {self.symbol} "
            f"{'ON' if self.enabled else 'OFF'} "
            f"range={p.lower}-{p.upper} grids={p.grids} usd_per_order={p.usd_per_order} "
            f"open_orders={len(oos)}"
        )

    async def cancel_all(self):
        try:
            self.hl.cancel_all(self.symbol)
        except Exception as e:
            log({"event": "grid_cancel_failed", "symbol": self.symbol, "error": str(e)})

    async def rebuild(self, force: bool = False):
        if not self.enabled or not self.params:
            return

        now = time.time()
        if not force and (now - self._last_rebuild_ts) < self._rebuild_cooldown_s:
            return

        p = self.params
        mid = float(self.hl.mid(self.symbol))

        # Breakout safety: do not place orders if already out of range
        buf = p.breakout_buffer_pct
        if mid < p.lower * (1 - buf) or mid > p.upper * (1 + buf):
            await notify(f"[GRID] {self.symbol} mid={mid:.4f} outside range. Not placing orders.")
            self.enabled = False
            await self.cancel_all()
            return

        levels = self._levels(p)
        if not levels:
            await notify("[GRID] Could not build levels.")
            return

        buys = [x for x in levels if x < mid]
        sells = [x for x in levels if x > mid]

        # Limit order count to reduce risk and API spam
        max_side = max(1, int(p.max_orders_per_side))
        buys = buys[-max_side:]
        sells = sells[:max_side]

        await self.cancel_all()

        placed = 0
        # Place buys
        for px in buys:
            pxr = self._round_px(px)
            sz = self._size_from_usd(p.usd_per_order, pxr)
            if sz <= 0:
                continue
            try:
                self.hl.place_limit(self.symbol, is_buy=True, px=pxr, sz=sz, reduce_only=False, post_only=p.post_only)
                placed += 1
            except Exception as e:
                log({"event": "grid_place_failed", "symbol": self.symbol, "side": "buy", "px": pxr, "error": str(e)})

        # Place sells
        for px in sells:
            pxr = self._round_px(px)
            sz = self._size_from_usd(p.usd_per_order, pxr)
            if sz <= 0:
                continue
            try:
                self.hl.place_limit(self.symbol, is_buy=False, px=pxr, sz=sz, reduce_only=False, post_only=p.post_only)
                placed += 1
            except Exception as e:
                log({"event": "grid_place_failed", "symbol": self.symbol, "side": "sell", "px": pxr, "error": str(e)})

        self._last_rebuild_ts = now
        await notify(f"[GRID REBUILD] {self.symbol} mid={mid:.4f} placed={placed} (buys={len(buys)} sells={len(sells)})")

    async def loop(self):
        while True:
            await asyncio.sleep(self.params.poll_seconds if self.params else 2.0)

            if not self.enabled or not self.params:
                continue

            p = self.params
            try:
                mid = float(self.hl.mid(self.symbol))
            except Exception as e:
                log({"event": "grid_mid_error", "symbol": self.symbol, "error": str(e)})
                continue

            # Breakout kill-switch
            buf = p.breakout_buffer_pct
            if mid < p.lower * (1 - buf) or mid > p.upper * (1 + buf):
                await notify(
                    f"[GRID BREAKOUT] {self.symbol} mid={mid:.4f} outside {p.lower}-{p.upper}. Canceling and disabling grid."
                )
                await self.cancel_all()
                self.enabled = False

                # Optional flatten - OFF by default
                if p.flatten_on_breakout:
                    try:
                        self.hl.close_position_market(self.symbol)  # only if your HL wrapper has this
                        await notify(f"[GRID] {self.symbol} flattened on breakout.")
                    except Exception as e:
                        log({"event": "grid_flatten_failed", "symbol": self.symbol, "error": str(e)})

                continue

            # Health check: if open orders are low, rebuild
            try:
                oos = self.hl.open_orders(self.symbol) or []
                if len(oos) < max(2, p.max_orders_per_side):
                    await notify(f"[GRID] {self.symbol} open_orders low ({len(oos)}). Rebuilding ladder.")
                    await self.rebuild(force=False)
            except Exception as e:
                log({"event": "grid_open_orders_error", "symbol": self.symbol, "error": str(e)})
